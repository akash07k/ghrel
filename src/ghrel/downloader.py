"""Asset download — sync and async, with manual redirect handling.

Two parallel implementations:

- :class:`Downloader` — synchronous, ``httpx.Client`` based. Used by the
  state machine when ``parallel == 1`` (default).
- :class:`AsyncDownloader` — async, ``httpx.AsyncClient`` based. Used when
  ``parallel > 1`` to run multiple asset downloads concurrently with a
  bounded :class:`asyncio.Semaphore`.

Both handle the **redirect / auth-strip quirk** that GitHub asset URLs
require: the asset endpoint accepts a ``Bearer`` token, but it returns a
302 to a pre-signed S3/CDN URL. The CDN URL has its own auth in query
params and *rejects* additional ``Authorization`` headers with a 403. So
we disable auto-redirect, strip the Authorization header on the second
request, and follow manually.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

from ghrel.logging_setup import redact_headers

# ── Constants ─────────────────────────────────────────────────────────────────


DEFAULT_TIMEOUT_SEC: Final[float] = 60.0
"""Connection / response-headers timeout. The streaming body itself has no
timeout (GitHub assets can be hundreds of MB)."""

CHUNK_SIZE: Final[int] = 1 << 20  # 1 MiB
"""Read buffer size during streaming. 1 MiB strikes a good balance between
syscall overhead (smaller = more) and progress-callback granularity
(bigger = laggier feedback for slow connections)."""

USER_AGENT: Final[str] = "ghrel/1.0 (+https://github.com/akash07k/ghrel)"

REDIRECT_STATUSES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DownloadProgress:
    """Progress snapshot pushed to the optional callback."""

    bytes_done: int
    bytes_total: int | None
    """``None`` when the server didn't send ``Content-Length``."""
    elapsed_sec: float

    @property
    def percent(self) -> int | None:
        if self.bytes_total is None or self.bytes_total <= 0:
            return None
        return int(self.bytes_done * 100 / self.bytes_total)

    @property
    def rate_mb_per_sec(self) -> float:
        if self.elapsed_sec <= 0:
            return 0.0
        return self.bytes_done / (1 << 20) / self.elapsed_sec


@dataclass(frozen=True)
class DownloadResult:
    """Successful download outcome."""

    url: str
    dest_path: Path
    bytes_written: int
    elapsed_sec: float


# Type aliases
ProgressCallback = Callable[[DownloadProgress], None]
AsyncProgressCallback = Callable[[DownloadProgress], Awaitable[None] | None]
# Maps a URL to the progress callback to use for that download — used by
# :meth:`AsyncDownloader.download_many` so parallel downloads can each have
# their own reporter (a single shared callback can't distinguish which
# download is reporting because :class:`DownloadProgress` has no URL field).
# Returning ``None`` means "no progress for this URL".
ProgressFactory = Callable[[str], "AsyncProgressCallback | None"]


class DownloadError(RuntimeError):
    """Raised when a download fails (network, non-2xx, IO, etc.)."""


# ── Helpers ───────────────────────────────────────────────────────────────────


def _build_headers(token: str | None) -> dict[str, str]:
    """Build the request headers used for the *initial* request.

    The token (if any) is included here. On redirect we issue a fresh request
    *without* this dict so the CDN URL doesn't see the bearer.

    ``Accept-Encoding: identity`` asks the server NOT to compress the
    response. Without it, httpx auto-decompresses on the read side, so
    ``Content-Length`` (compressed transfer size) and the bytes we count
    via ``iter_bytes`` (decompressed body) disagree — the progress meter
    would then climb past 100%. GitHub's release CDN honors ``identity``
    for binary assets; for text-ish assets that the CDN gzips anyway the
    smart Content-Length detector below falls back to "unknown size".
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/octet-stream",
        "Accept-Encoding": "identity",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _cdn_headers() -> dict[str, str]:
    """Headers for the redirect-followed CDN request — explicitly *no* auth."""
    return {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}


def _resolve_redirect_url(response: httpx.Response) -> str:
    """Extract the absolute redirect URL from a 3xx response.

    Raises :class:`DownloadError` if the server returned a redirect status
    without a ``Location`` header (rare but seen in misbehaving proxies).
    """
    location = response.headers.get("location")
    if not location:
        raise DownloadError(
            f"Server returned redirect ({response.status_code}) without a Location header."
        )
    return str(httpx.URL(response.url).join(location))


def _safe_remove(path: Path) -> None:
    """Best-effort partial-file cleanup. Never raises."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


# ── Synchronous downloader ────────────────────────────────────────────────────


class Downloader:
    """Synchronous downloader. One instance covers many sequential downloads;
    the underlying :class:`httpx.Client` reuses connections.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SEC,
    ) -> None:
        """Construct a synchronous downloader.

        Args:
            token: GitHub PAT for authenticated requests, or ``None`` for
                unauthenticated. Sent as ``Bearer`` on the initial request
                and stripped on the redirect-followed CDN request.
            timeout: Base timeout in seconds. Applied as the connect / write
                / pool timeout directly. The streaming **read** timeout is
                ``timeout * 5`` — that's the maximum allowed gap *between
                chunks* during the body transfer (not the total wall clock).
                With the default ``60`` it tolerates a 300-second pause
                between 1 MiB chunks, which covers most slow-connection
                scenarios without waiting forever on a fully-stalled stream.
        """
        self._token = token
        # The headers timeout fires if the server takes too long to send
        # response headers; the read timeout is the time between *chunks*
        # during streaming. We give read a long ceiling because release
        # assets can be hundreds of MB on slow connections.
        self._timeout = httpx.Timeout(
            connect=timeout, read=timeout * 5, write=timeout, pool=timeout
        )
        self._client: httpx.Client | None = None

    def __enter__(self) -> Downloader:
        self._client = httpx.Client(timeout=self._timeout, follow_redirects=False)
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout, follow_redirects=False)
        return self._client

    def download(
        self,
        url: str,
        dest_path: Path,
        *,
        on_progress: ProgressCallback | None = None,
        progress_interval: float = 0.5,
    ) -> DownloadResult:
        """Download ``url`` to ``dest_path``.

        Calls ``on_progress`` at most every ``progress_interval`` seconds (and
        always at the start and end). Removes a partial file on any failure
        before re-raising.

        Args:
            url: Initial URL (typically ``asset.download_url``).
            dest_path: Output path. Parent directory must exist.
            on_progress: Optional callback invoked with
                :class:`DownloadProgress` snapshots.
            progress_interval: Minimum seconds between callbacks.

        Returns:
            :class:`DownloadResult` on success.

        Raises:
            DownloadError: On HTTP non-2xx, network failure, or IO error.
        """
        client = self._ensure_client()
        start = time.monotonic()
        bytes_written = 0
        response: httpx.Response | None = None
        try:
            response = self._open_stream(client, url)
            total = self._content_length(response)
            if on_progress is not None:
                on_progress(DownloadProgress(0, total, 0.0))

            with dest_path.open("wb") as f:
                last_emit = start
                for chunk in response.iter_bytes(CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        bytes_written += len(chunk)
                        now = time.monotonic()
                        if on_progress is not None and (now - last_emit) >= progress_interval:
                            on_progress(DownloadProgress(bytes_written, total, now - start))
                            last_emit = now

            elapsed = time.monotonic() - start
            if on_progress is not None:
                on_progress(DownloadProgress(bytes_written, total, elapsed))

            return DownloadResult(
                url=url,
                dest_path=dest_path,
                bytes_written=bytes_written,
                elapsed_sec=elapsed,
            )
        except (httpx.HTTPError, OSError) as exc:
            _safe_remove(dest_path)
            raise DownloadError(f"Download failed: {exc}") from exc
        except Exception:
            _safe_remove(dest_path)
            raise
        finally:
            if response is not None:
                response.close()

    def _open_stream(self, client: httpx.Client, url: str) -> httpx.Response:
        """Open a streaming response, handling the auth-on-redirect dance."""
        request = client.build_request("GET", url, headers=_build_headers(self._token))
        response = client.send(request, stream=True)

        if response.status_code in REDIRECT_STATUSES:
            redirect_url = _resolve_redirect_url(response)
            response.close()
            # Fresh request to the CDN, *without* Authorization. We let httpx
            # follow any further redirects automatically here — the CDN may
            # itself redirect to a regional edge, and those don't care about
            # auth either way.
            cdn_request = client.build_request("GET", redirect_url, headers=_cdn_headers())
            response = client.send(cdn_request, stream=True, follow_redirects=True)

        if response.status_code >= 400:
            status = response.status_code
            response.close()
            raise DownloadError(
                f"Server returned HTTP {status}. The download URL may have expired "
                f"or access was denied."
            )
        return response

    @staticmethod
    def _content_length(response: httpx.Response) -> int | None:
        """Return the Content-Length the *body iterator* will produce, or
        ``None`` if it can't be known up front.

        Returns ``None`` when:

        - The header is missing / unparseable, OR
        - The server sent ``Content-Encoding`` other than ``identity``.
          In that case Content-Length is the *compressed* transfer size
          while ``iter_bytes`` yields *decompressed* bytes, so the two
          don't describe the same thing — better to show "X.X MB at Y MB/s"
          (no percent, no ETA) than a bogus "412%".
        """
        encoding = response.headers.get("content-encoding", "").strip().lower()
        if encoding and encoding != "identity":
            return None
        cl = response.headers.get("content-length")
        if cl is None:
            return None
        try:
            return int(cl)
        except ValueError:
            return None


# ── Async downloader ──────────────────────────────────────────────────────────


class AsyncDownloader:
    """Async downloader for parallel multi-asset workflows.

    Constructed with a target ``parallel`` ceiling; calls to
    :meth:`download_many` run individual downloads concurrently up to that
    bound via an :class:`asyncio.Semaphore`. Each download still has its own
    redirect / auth-strip dance, identical to the sync path.
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        parallel: int = 1,
    ) -> None:
        if parallel < 1:
            raise ValueError(f"parallel must be >= 1, got {parallel}")
        self._token = token
        self._parallel = parallel
        self._timeout = httpx.Timeout(
            connect=timeout, read=timeout * 5, write=timeout, pool=timeout
        )

    async def download(
        self,
        url: str,
        dest_path: Path,
        *,
        on_progress: AsyncProgressCallback | None = None,
        progress_interval: float = 0.5,
    ) -> DownloadResult:
        """Download a single asset asynchronously."""
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:
            return await self._download_with_client(
                client,
                url,
                dest_path,
                on_progress=on_progress,
                progress_interval=progress_interval,
            )

    async def download_many(
        self,
        items: Iterable[tuple[str, Path]],
        *,
        progress_factory: ProgressFactory | None = None,
        progress_interval: float = 0.5,
    ) -> list[DownloadResult | DownloadError]:
        """Download multiple assets concurrently, bounded by ``parallel``.

        ``progress_factory`` (when provided) is called once per item with the
        item's URL and must return the callback to use for *that* download
        (or ``None`` to skip progress for it). This is how callers wire up
        per-item progress reporters — one shared callback can't tell which
        download is reporting because :class:`DownloadProgress` has no URL
        field.

        Returns a list of length ``len(items)``, one entry per input item, in
        the same order. Each entry is either a :class:`DownloadResult` (on
        success) or a :class:`DownloadError` (on failure) — failures do **not**
        cancel siblings, and the caller decides per-asset what to do with each
        outcome (retry, log, report). Other exception types (asyncio.CancelledError,
        unexpected runtime errors) propagate normally.
        """
        items_list = list(items)
        if not items_list:
            return []

        sem = asyncio.Semaphore(self._parallel)
        # Single shared client across the whole batch — cheaper than one per
        # download, and httpx is happy to multiplex.
        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=False) as client:

            async def bounded(url: str, dest: Path) -> DownloadResult:
                # Resolve the per-item callback *outside* the semaphore so the
                # factory call doesn't count against the concurrency limit.
                cb = progress_factory(url) if progress_factory is not None else None
                async with sem:
                    return await self._download_with_client(
                        client,
                        url,
                        dest,
                        on_progress=cb,
                        progress_interval=progress_interval,
                    )

            # ``return_exceptions=True`` collects per-item exceptions in place
            # of results so one failed download doesn't cancel the others.
            raw = await asyncio.gather(
                *(bounded(url, dest) for url, dest in items_list),
                return_exceptions=True,
            )

        # Narrow the typing: gather returns BaseException, but we only catch
        # DownloadError below; anything else (CancelledError, programmer
        # bugs) re-raises so the caller still sees real failures.
        out: list[DownloadResult | DownloadError] = []
        for entry in raw:
            if isinstance(entry, DownloadError):
                out.append(entry)
            elif isinstance(entry, BaseException):
                raise entry
            else:
                out.append(entry)
        return out

    async def _download_with_client(
        self,
        client: httpx.AsyncClient,
        url: str,
        dest_path: Path,
        *,
        on_progress: AsyncProgressCallback | None,
        progress_interval: float,
    ) -> DownloadResult:
        start = time.monotonic()
        bytes_written = 0
        response: httpx.Response | None = None
        try:
            response = await self._open_stream(client, url)
            total = Downloader._content_length(response)
            await _emit_progress(on_progress, DownloadProgress(0, total, 0.0))

            with dest_path.open("wb") as f:
                last_emit = start
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    if chunk:
                        f.write(chunk)
                        bytes_written += len(chunk)
                        now = time.monotonic()
                        if (now - last_emit) >= progress_interval:
                            await _emit_progress(
                                on_progress,
                                DownloadProgress(bytes_written, total, now - start),
                            )
                            last_emit = now

            elapsed = time.monotonic() - start
            await _emit_progress(on_progress, DownloadProgress(bytes_written, total, elapsed))
            return DownloadResult(
                url=url,
                dest_path=dest_path,
                bytes_written=bytes_written,
                elapsed_sec=elapsed,
            )
        except (httpx.HTTPError, OSError) as exc:
            _safe_remove(dest_path)
            raise DownloadError(f"Download failed: {exc}") from exc
        except Exception:
            _safe_remove(dest_path)
            raise
        finally:
            if response is not None:
                await response.aclose()

    async def _open_stream(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        """Async equivalent of :meth:`Downloader._open_stream`."""
        request = client.build_request("GET", url, headers=_build_headers(self._token))
        response = await client.send(request, stream=True)

        if response.status_code in REDIRECT_STATUSES:
            redirect_url = _resolve_redirect_url(response)
            await response.aclose()
            cdn_request = client.build_request("GET", redirect_url, headers=_cdn_headers())
            response = await client.send(cdn_request, stream=True, follow_redirects=True)

        if response.status_code >= 400:
            status = response.status_code
            await response.aclose()
            raise DownloadError(
                f"Server returned HTTP {status}. The download URL may have expired "
                f"or access was denied."
            )
        return response


async def _emit_progress(
    callback: AsyncProgressCallback | None,
    progress: DownloadProgress,
) -> None:
    """Invoke an optional async progress callback, awaiting if it returned a coroutine."""
    if callback is None:
        return
    result = callback(progress)
    if asyncio.iscoroutine(result):
        await result


# ── Public helper for diag logging ────────────────────────────────────────────


def describe_request(url: str, token: str | None) -> str:
    """Render a request line suitable for the diag log (no token leakage).

    Used by :mod:`ghrel.state_machine` when logging the download attempt.
    """
    headers = _build_headers(token)
    return f"GET {url} headers={redact_headers(headers)}"
