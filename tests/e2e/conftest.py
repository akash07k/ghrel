"""Shared e2e test fixtures and helpers.

Pytest auto-discovers fixtures defined here for every test in
``tests/e2e/``. Helpers (functions and classes) are also exported via
``__all__`` for tests that want to call them directly.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from rich.console import Console

from ghrel.config import Config
from ghrel.downloader import DownloadError, DownloadProgress, DownloadResult
from ghrel.github_api import AssetInfo, GitHubApiError, ReleaseInfo
from ghrel.prompts import Prompts
from ghrel.state_machine import StateMachine

__all__ = [
    "FakeAsyncDownloader",
    "FakeDownloader",
    "build_machine",
    "make_asset",
    "make_release",
    "stdin_lines",
]


# ── Helpers ───────────────────────────────────────────────────────────────────


def make_release(
    *,
    tag: str = "v1.0",
    assets: list[AssetInfo] | None = None,
    is_prerelease: bool = False,
) -> ReleaseInfo:
    """Construct a frozen :class:`ReleaseInfo` for tests."""
    return ReleaseInfo(
        tag=tag,
        name=tag,
        is_prerelease=is_prerelease,
        published_at=datetime(2026, 4, 25, 14, 0, tzinfo=UTC),
        assets=tuple(assets or []),
    )


def make_asset(name: str, *, size: int = 1024, digest: str | None = None) -> AssetInfo:
    """Construct a frozen :class:`AssetInfo` for tests."""
    return AssetInfo(
        name=name,
        size=size,
        download_url=f"https://example.com/{name}",
        digest=digest,
    )


class FakeDownloader:
    """Minimal in-memory stand-in for :class:`Downloader`.

    On every :meth:`download`, writes 8 zero bytes to ``dest``, records the
    call, and returns a :class:`DownloadResult`. No network, no real disk
    streaming. ``fail_for`` makes it raise :class:`DownloadError` for URLs
    containing any of the listed substrings.
    """

    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.calls: list[tuple[str, Path]] = []
        self._fail_for = fail_for or set()

    def download(
        self,
        url: str,
        dest_path: Path,
        *,
        on_progress: Any = None,
        progress_interval: float = 0.5,
    ) -> DownloadResult:
        self.calls.append((url, dest_path))
        if any(needle in url for needle in self._fail_for):
            raise DownloadError(f"forced failure on {url}")
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(b"\x00" * 8)
        if on_progress is not None:
            on_progress(DownloadProgress(0, 8, 0.0))
            on_progress(DownloadProgress(8, 8, 0.01))
        return DownloadResult(url=url, dest_path=dest_path, bytes_written=8, elapsed_sec=0.01)


class FakeAsyncDownloader:
    """In-memory stand-in for :class:`AsyncDownloader`.

    Implements only the methods the state machine calls. Each
    :meth:`download_many` invocation writes 8 zero bytes per destination,
    records the calls, and returns a list of :class:`DownloadResult` /
    :class:`DownloadError` entries in input order. ``fail_for`` makes any
    URL containing one of the listed substrings come back as a
    :class:`DownloadError` (with siblings still succeeding).
    """

    def __init__(self, *, fail_for: set[str] | None = None) -> None:
        self.calls: list[tuple[str, Path]] = []
        self._fail_for = fail_for or set()

    async def download_many(
        self,
        items: Any,
        *,
        progress_factory: Any = None,
        progress_interval: float = 0.5,
    ) -> list[Any]:
        from ghrel.downloader import DownloadError, DownloadProgress, DownloadResult

        items_list = list(items)
        out: list[Any] = []
        for url, dest in items_list:
            self.calls.append((url, dest))
            # Mirror the real API: per-item callback resolved via factory.
            cb = progress_factory(url) if progress_factory is not None else None
            if any(needle in url for needle in self._fail_for):
                out.append(DownloadError(f"forced failure on {url}"))
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"\x00" * 8)
            if cb is not None:
                cb(DownloadProgress(0, 8, 0.0))
                cb(DownloadProgress(8, 8, 0.01))
            out.append(DownloadResult(url=url, dest_path=dest, bytes_written=8, elapsed_sec=0.01))
        return out


def build_machine(
    config: Config,
    captured: tuple[Console, io.StringIO],
    *,
    release: ReleaseInfo | None = None,
    fetch_error: GitHubApiError | None = None,
    downloader: FakeDownloader | None = None,
    async_downloader: FakeAsyncDownloader | None = None,
) -> tuple[StateMachine, MagicMock, FakeDownloader]:
    """Wire a :class:`StateMachine` with a mocked GitHub client + fake downloader."""
    console, _ = captured
    github = MagicMock()
    if fetch_error is not None:
        github.fetch_release.side_effect = fetch_error
    else:
        github.fetch_release.return_value = release or make_release()
    dl = downloader or FakeDownloader()
    machine = StateMachine(
        config=config,
        prompts=Prompts(console=console),
        github_client=github,
        downloader=dl,  # type: ignore[arg-type]  # FakeDownloader matches the duck interface
        async_downloader=async_downloader,  # type: ignore[arg-type]
        console=console,
    )
    return machine, github, dl


def stdin_lines(monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
    """Feed scripted lines into ``input()`` calls used by ``rich.prompt``."""
    queue = list(lines)

    def fake_input(prompt: str = "") -> str:
        if not queue:
            raise EOFError("test ran out of scripted input — likely an unexpected prompt")
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def captured_console() -> tuple[Console, io.StringIO]:
    """A rich Console writing to a StringIO so tests can read captured output."""
    buf = io.StringIO()
    return Console(file=buf, force_terminal=False, width=120, no_color=True), buf


@pytest.fixture
def base_config(tmp_path: Path) -> Config:
    """A baseline Config rooted at ``tmp_path/downloads``."""
    return Config(
        token=None,
        token_source="default",
        output_dir=tmp_path / "downloads",
        output_dir_raw="downloads",
        include_pre_release=False,
        parallel=1,
        force=False,
    )
