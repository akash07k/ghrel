"""Integration tests for ``ghrel.downloader``.

Uses ``respx`` to intercept httpx requests at the transport layer. Tests
assert on:

- Successful direct download
- Redirect handling: initial Authorization header sent, but stripped on the
  redirect-followed CDN request (the security-critical bit)
- Progress callback invocation
- Partial-file cleanup on stream failure
- Non-2xx status raised as :class:`DownloadError`
- Async parallel: bounded by ``Semaphore``, all downloads complete
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from ghrel.downloader import (
    AsyncDownloader,
    Downloader,
    DownloadError,
    DownloadProgress,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_router() -> Iterator[respx.MockRouter]:
    """A respx router that intercepts all httpx requests for the duration of
    the test. Tests register routes inside the ``with`` block.
    """
    with respx.mock(assert_all_called=False) as router:
        yield router


# ── Synchronous Downloader ────────────────────────────────────────────────────


class TestDownloaderSync:
    def test_direct_download_succeeds(self, mock_router: respx.MockRouter, tmp_path: Path) -> None:
        url = "https://example.com/asset.zip"
        payload = b"x" * 4096
        mock_router.get(url).mock(
            return_value=httpx.Response(
                200, content=payload, headers={"content-length": str(len(payload))}
            )
        )

        dest = tmp_path / "asset.zip"
        with Downloader() as d:
            result = d.download(url, dest)

        assert result.bytes_written == len(payload)
        assert result.dest_path == dest
        assert dest.read_bytes() == payload

    def test_redirect_strips_authorization(
        self, mock_router: respx.MockRouter, tmp_path: Path
    ) -> None:
        """Critical security check: the Bearer token is sent on the first
        request, but the post-redirect CDN request must NOT carry it."""
        api_url = "https://github.com/owner/repo/releases/download/v1/asset.zip"
        cdn_url = "https://cdn.example.com/signed?key=abc"
        payload = b"\x00" * 1024

        # Initial request — should include the Authorization header.
        api_route = mock_router.get(api_url).mock(
            return_value=httpx.Response(302, headers={"location": cdn_url})
        )
        # CDN request — must NOT include Authorization.
        cdn_route = mock_router.get(cdn_url).mock(
            return_value=httpx.Response(
                200, content=payload, headers={"content-length": str(len(payload))}
            )
        )

        dest = tmp_path / "asset.zip"
        with Downloader(token="ghp_secret") as d:
            d.download(api_url, dest)

        assert api_route.called
        assert cdn_route.called
        api_request = api_route.calls.last.request
        cdn_request = cdn_route.calls.last.request

        assert api_request.headers.get("authorization") == "Bearer ghp_secret"
        assert "authorization" not in {k.lower() for k in cdn_request.headers}, (
            "Bearer token MUST be stripped on redirect to the CDN"
        )

    def test_progress_callback_invoked(self, mock_router: respx.MockRouter, tmp_path: Path) -> None:
        url = "https://example.com/asset.zip"
        payload = b"y" * (3 << 20)  # 3 MiB to ensure multiple chunks
        mock_router.get(url).mock(
            return_value=httpx.Response(
                200, content=payload, headers={"content-length": str(len(payload))}
            )
        )

        captured: list[DownloadProgress] = []

        def on_progress(p: DownloadProgress) -> None:
            captured.append(p)

        dest = tmp_path / "asset.zip"
        with Downloader() as d:
            d.download(url, dest, on_progress=on_progress, progress_interval=0.0)

        assert len(captured) >= 2  # at least start + end
        # First call is bytes_done=0; last is bytes_done=total
        assert captured[0].bytes_done == 0
        assert captured[-1].bytes_done == len(payload)
        assert captured[-1].bytes_total == len(payload)
        assert captured[-1].percent == 100

    def test_partial_file_cleaned_on_stream_failure(
        self, mock_router: respx.MockRouter, tmp_path: Path
    ) -> None:
        url = "https://example.com/asset.zip"
        # respx side-effect that raises mid-stream — simulates a network drop.
        mock_router.get(url).mock(side_effect=httpx.ReadError("connection reset"))

        dest = tmp_path / "asset.zip"
        with Downloader() as d, pytest.raises(DownloadError):
            d.download(url, dest)
        assert not dest.exists(), "Partial file must be removed on failure"

    def test_non_2xx_raises_download_error(
        self, mock_router: respx.MockRouter, tmp_path: Path
    ) -> None:
        url = "https://example.com/asset.zip"
        mock_router.get(url).mock(return_value=httpx.Response(403))

        dest = tmp_path / "asset.zip"
        with Downloader() as d, pytest.raises(DownloadError, match="403"):
            d.download(url, dest)

    def test_initial_request_sends_accept_encoding_identity(
        self, mock_router: respx.MockRouter, tmp_path: Path
    ) -> None:
        """Asking for ``identity`` keeps Content-Length and the bytes we
        count via ``iter_bytes`` describing the same thing — without it,
        the CDN may gzip and the progress meter climbs past 100%.
        """
        url = "https://example.com/asset.zip"
        route = mock_router.get(url).mock(return_value=httpx.Response(200, content=b"x" * 100))

        with Downloader() as d:
            d.download(url, tmp_path / "asset.zip")

        sent = route.calls.last.request
        assert sent.headers.get("accept-encoding", "").lower() == "identity"

    def test_compressed_response_treated_as_size_unknown(
        self, mock_router: respx.MockRouter, tmp_path: Path
    ) -> None:
        """If the server ignores ``Accept-Encoding: identity`` and sends a
        gzipped response anyway, Content-Length describes compressed bytes
        while iter_bytes yields decompressed bytes. The mismatch must
        surface as ``bytes_total=None`` so progress shows "X MB at Y MB/s"
        instead of an absurd "412%" — fixing the user-reported quirk where
        small text assets reported >100%.
        """
        import gzip

        url = "https://example.com/text.json"
        body = b"a" * 10_000  # decompresses to 10 KB
        compressed = gzip.compress(body)
        # Simulate a server that gzipped despite our identity request.
        mock_router.get(url).mock(
            return_value=httpx.Response(
                200,
                content=compressed,
                headers={
                    "content-length": str(len(compressed)),
                    "content-encoding": "gzip",
                },
            )
        )

        captured: list[DownloadProgress] = []

        with Downloader() as d:
            d.download(
                url,
                tmp_path / "text.json",
                on_progress=captured.append,
                progress_interval=0.0,
            )

        # No event should have a defined bytes_total / percent — the
        # compressed/decompressed mismatch makes the percentage meaningless.
        assert captured, "Expected at least one progress event"
        assert all(p.bytes_total is None for p in captured)
        assert all(p.percent is None for p in captured)

    def test_redirect_without_location_header_raises(
        self, mock_router: respx.MockRouter, tmp_path: Path
    ) -> None:
        url = "https://example.com/asset.zip"
        mock_router.get(url).mock(return_value=httpx.Response(302))  # no Location

        dest = tmp_path / "asset.zip"
        with Downloader() as d, pytest.raises(DownloadError, match="without a Location header"):
            d.download(url, dest)


# ── AsyncDownloader ───────────────────────────────────────────────────────────


class TestAsyncDownloader:
    async def test_single_async_download(
        self, mock_router: respx.MockRouter, tmp_path: Path
    ) -> None:
        url = "https://example.com/asset.zip"
        payload = b"z" * 2048
        mock_router.get(url).mock(
            return_value=httpx.Response(
                200, content=payload, headers={"content-length": str(len(payload))}
            )
        )

        d = AsyncDownloader(parallel=1)
        result = await d.download(url, tmp_path / "asset.zip")
        assert result.bytes_written == len(payload)

    async def test_download_many_completes_all(
        self, mock_router: respx.MockRouter, tmp_path: Path
    ) -> None:
        urls = [f"https://example.com/file-{i}.zip" for i in range(5)]
        for url in urls:
            mock_router.get(url).mock(return_value=httpx.Response(200, content=b"data"))

        items = [(u, tmp_path / Path(u).name) for u in urls]
        d = AsyncDownloader(parallel=3)
        results = await d.download_many(items)

        assert len(results) == 5
        # All entries are successful — narrow via isinstance for type safety.
        assert all(not isinstance(r, DownloadError) for r in results)
        assert all(r.bytes_written == 4 for r in results if not isinstance(r, DownloadError))
        for _, dest in items:
            assert dest.exists()

    async def test_download_many_per_item_progress_fires_with_correct_callback(
        self, mock_router: respx.MockRouter, tmp_path: Path
    ) -> None:
        """Regression: each parallel download must get progress events sent
        to the callback returned by ``progress_factory(url)``.

        The previous parallel implementation passed ``on_progress=None`` so
        no progress lines ever printed during a multi-asset download. The
        contract now is: ``progress_factory(url)`` returns a per-item
        callback and that callback receives only its own item's events.
        """
        urls = [f"https://example.com/p-{i}.zip" for i in range(3)]
        for url in urls:
            mock_router.get(url).mock(
                return_value=httpx.Response(
                    200,
                    content=b"x" * (2 << 20),  # 2 MiB so we get >1 chunk
                    headers={"content-length": str(2 << 20)},
                )
            )

        # One bucket per URL — assert each callback only sees its own events.
        seen: dict[str, list[DownloadProgress]] = {url: [] for url in urls}

        def factory(url: str) -> Any:
            bucket = seen[url]

            def cb(progress: DownloadProgress) -> None:
                bucket.append(progress)

            return cb

        items = [(u, tmp_path / Path(u).name) for u in urls]
        d = AsyncDownloader(parallel=2)
        results = await d.download_many(items, progress_factory=factory, progress_interval=0.0)

        # Every download succeeded.
        assert len(results) == 3
        assert all(not isinstance(r, DownloadError) for r in results)

        # Every URL got at least the start (0 bytes) and end (full payload)
        # progress events, and *only* events for that URL — no cross-talk.
        for url in urls:
            events = seen[url]
            assert len(events) >= 2, f"No progress events for {url}: got {events!r}"
            assert events[0].bytes_done == 0
            assert events[-1].bytes_done == 2 << 20

    async def test_download_many_partial_failure_does_not_cancel_others(
        self, mock_router: respx.MockRouter, tmp_path: Path
    ) -> None:
        """Regression: a 403 on one URL must not abort sibling downloads.

        Before the ``return_exceptions=True`` fix, ``asyncio.gather`` would
        cancel all in-flight tasks on the first failure, so subsequent
        downloads were either never written or written partially. The
        contract is now: each entry in the result list is either a
        :class:`DownloadResult` or a :class:`DownloadError`, in input order.
        """
        urls = [f"https://example.com/mix-{i}.zip" for i in range(4)]
        # Index 1 fails (403); the rest succeed.
        for i, url in enumerate(urls):
            if i == 1:
                mock_router.get(url).mock(return_value=httpx.Response(403))
            else:
                mock_router.get(url).mock(return_value=httpx.Response(200, content=b"data"))

        items = [(u, tmp_path / Path(u).name) for u in urls]
        d = AsyncDownloader(parallel=2)
        results = await d.download_many(items)

        assert len(results) == 4
        # Order is preserved.
        assert isinstance(results[0], type(results[0]))  # DownloadResult
        assert isinstance(results[1], DownloadError)
        assert not isinstance(results[2], DownloadError)
        assert not isinstance(results[3], DownloadError)
        # The sibling files were written in full despite the 403 in the middle.
        assert items[0][1].exists()
        assert not items[1][1].exists(), "Failed file must be cleaned up"
        assert items[2][1].exists()
        assert items[3][1].exists()

    async def test_parallel_semaphore_limits_concurrency(
        self, mock_router: respx.MockRouter, tmp_path: Path
    ) -> None:
        """With parallel=2 and 5 downloads, at most 2 should be in-flight at once."""
        in_flight = 0
        max_in_flight = 0
        # asyncio.Lock guards the in_flight counter. We don't await anything
        # while holding it so it doesn't artificially serialize.
        counter_lock = asyncio.Lock()

        async def side_effect(_request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, max_in_flight
            async with counter_lock:
                in_flight += 1
                max_in_flight = max(max_in_flight, in_flight)
            try:
                # Yield to the loop so other tasks can be scheduled.
                await asyncio.sleep(0.05)
                return httpx.Response(200, content=b"x")
            finally:
                async with counter_lock:
                    in_flight -= 1

        urls = [f"https://example.com/p-{i}.zip" for i in range(5)]
        for url in urls:
            mock_router.get(url).mock(side_effect=side_effect)

        items = [(u, tmp_path / Path(u).name) for u in urls]
        d = AsyncDownloader(parallel=2)
        await d.download_many(items)

        assert max_in_flight <= 2, (
            f"Expected at most 2 in-flight downloads, observed {max_in_flight}"
        )

    async def test_async_redirect_strips_auth(
        self, mock_router: respx.MockRouter, tmp_path: Path
    ) -> None:
        api_url = "https://github.com/owner/repo/releases/download/v1/foo.zip"
        cdn_url = "https://cdn.example.com/signed-foo"

        api_route = mock_router.get(api_url).mock(
            return_value=httpx.Response(302, headers={"location": cdn_url})
        )
        cdn_route = mock_router.get(cdn_url).mock(return_value=httpx.Response(200, content=b"data"))

        d = AsyncDownloader(token="ghp_secret", parallel=1)
        await d.download(api_url, tmp_path / "foo.zip")

        assert "authorization" in {k.lower() for k in api_route.calls.last.request.headers}
        assert "authorization" not in {k.lower() for k in cdn_route.calls.last.request.headers}

    async def test_async_progress_callback_supports_coroutine(
        self, mock_router: respx.MockRouter, tmp_path: Path
    ) -> None:
        """The async callback may return a coroutine — it should be awaited."""
        url = "https://example.com/asset.zip"
        mock_router.get(url).mock(return_value=httpx.Response(200, content=b"x" * 1024))

        captured: list[DownloadProgress] = []

        async def on_progress(p: DownloadProgress) -> None:
            captured.append(p)

        d = AsyncDownloader()
        await d.download(
            url, tmp_path / "asset.zip", on_progress=on_progress, progress_interval=0.0
        )
        assert len(captured) >= 2

    def test_invalid_parallel_rejected(self) -> None:
        with pytest.raises(ValueError, match=">= 1"):
            AsyncDownloader(parallel=0)


# ── describe_request (diag log helper) ────────────────────────────────────────


class TestDescribeRequest:
    def test_redacts_authorization(self) -> None:
        from ghrel.downloader import describe_request

        line = describe_request("https://api.github.com/x", "ghp_secret_value")
        assert "ghp_secret_value" not in line
        assert "<redacted>" in line
        assert "https://api.github.com/x" in line

    def test_unauthenticated_request_omits_auth(self) -> None:
        from ghrel.downloader import describe_request

        line = describe_request("https://example.com", None)
        assert "Authorization" not in line
        assert "<redacted>" not in line
