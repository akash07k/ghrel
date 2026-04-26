"""Line-based download-progress reporter.

We *deliberately don't* use rich's animated `Progress` widget here:

- Animated progress bars use cursor save/restore + line redraws, which screen
  readers announce as constant flicker (or not at all). NVDA's "live region"
  detection is hit-or-miss with terminal cursor magic.
- A simple ``print("  25% -- 100/400 MB at 5.2 MB/s")`` line writes a fresh
  text line per update. Screen readers handle that the same as any normal
  output. Sighted users still get the same information.

The cadence — one line every ~0.5 s OR every 5% of progress — keeps the
output frequent enough to feel responsive without flooding a slow terminal.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from rich.console import Console

from ghrel.downloader import DownloadProgress


@dataclass
class _LineProgressState:
    """Mutable state kept between callback invocations."""

    last_emit_at: float = 0.0
    last_pct: int = -1


class LineProgressReporter:
    """Callable that consumes :class:`DownloadProgress` events and prints them
    as discrete lines. Construct one *per download* — state isn't shared.

    Use as the ``on_progress`` argument to :meth:`Downloader.download`::

        reporter = LineProgressReporter(console, asset_label="foo.zip")
        downloader.download(url, dest, on_progress=reporter)
    """

    def __init__(
        self,
        console: Console,
        *,
        asset_label: str | None = None,
        min_interval_sec: float = 0.5,
        pct_step: int = 5,
    ) -> None:
        """
        Args:
            console: Where to render progress lines.
            asset_label: Optional prefix for each line (helpful when several
                downloads are running in parallel — each gets a distinct prefix).
            min_interval_sec: Minimum seconds between emitted lines.
            pct_step: Always emit when crossing this percent boundary, even
                if the time interval hasn't elapsed (keeps a steady cadence
                for fast downloads).
        """
        self._console = console
        self._asset_label = asset_label
        self._min_interval = min_interval_sec
        self._pct_step = pct_step
        self._state = _LineProgressState()

    def __call__(self, progress: DownloadProgress) -> None:
        now = time.monotonic()
        pct = progress.percent
        emit = (
            self._state.last_pct < 0  # first call
            or (now - self._state.last_emit_at) >= self._min_interval
            or (pct is not None and pct - self._state.last_pct >= self._pct_step)
            or (pct is not None and pct >= 100 and self._state.last_pct < 100)
        )
        if not emit:
            return
        self._state.last_emit_at = now
        if pct is not None:
            self._state.last_pct = pct

        line = _format_progress_line(progress, asset_label=self._asset_label)
        self._console.print(line)


# Below this many bytes, the progress line shows KB instead of MB so tiny
# files (signatures, checksum files, small binaries) don't all read as "0.0 MB".
# 0.5 MiB is the boundary: anything smaller looks better in KB.
_KB_DISPLAY_THRESHOLD: int = 1 << 19  # 512 KiB


def _format_size_and_rate(
    bytes_done: int,
    bytes_total: int | None,
    rate_mb_per_sec: float,
) -> tuple[str, str]:
    """Pick MB or KB units and format the size + rate strings.

    Returns ``(size_str, rate_str)``. When ``bytes_total`` is known, the
    size string is ``"<done> / <total> <UNIT>"`` (single unit suffix).
    When unknown, ``"<done> <UNIT>"``.
    """
    # Pick units based on the *total* size when known (so units don't switch
    # mid-download); otherwise base it on bytes-done.
    pivot = bytes_total if bytes_total else bytes_done
    use_kb = pivot < _KB_DISPLAY_THRESHOLD

    if use_kb:
        unit = "KB"
        done_n = bytes_done / (1 << 10)
        total_n = bytes_total / (1 << 10) if bytes_total else None
        # MB/s → KB/s for the rate display.
        rate_str = f"{rate_mb_per_sec * (1 << 20) / (1 << 10):.1f} KB/s"
    else:
        unit = "MB"
        done_n = bytes_done / (1 << 20)
        total_n = bytes_total / (1 << 20) if bytes_total else None
        rate_str = f"{rate_mb_per_sec:.1f} MB/s"

    if total_n is not None:
        size_str = f"{done_n:.1f} / {total_n:.1f} {unit}"
    else:
        size_str = f"{done_n:.1f} {unit}"
    return size_str, rate_str


def _format_progress_line(
    progress: DownloadProgress,
    *,
    asset_label: str | None = None,
) -> str:
    """Render one progress line. Pure for testability.

    Switches to KB units for downloads under 512 KiB so tiny files
    (signatures, small checksum files) don't all read as ``"0.0 MB"``.
    """
    size_str, rate_str = _format_size_and_rate(
        progress.bytes_done, progress.bytes_total, progress.rate_mb_per_sec
    )

    if progress.bytes_total is not None and progress.bytes_total > 0:
        pct = progress.percent or 0
        eta_str = _format_eta(progress.bytes_done, progress.bytes_total, progress.rate_mb_per_sec)
        body = f"{pct:3d}% — {size_str} at {rate_str} (ETA: {eta_str})"
    else:
        body = f"{size_str} at {rate_str}"

    if asset_label:
        return f"  [{asset_label}] {body}"
    return f"  {body}"


def _format_eta(bytes_done: int, bytes_total: int, rate_mb_per_sec: float) -> str:
    """Format the ETA in mm:ss or s form. Pure for testability."""
    if rate_mb_per_sec <= 0:
        return "—"
    remaining_mb = max(0.0, (bytes_total - bytes_done) / (1 << 20))
    eta_sec = int(remaining_mb / rate_mb_per_sec)
    if eta_sec >= 60:
        return f"{eta_sec // 60}m {eta_sec % 60}s"
    return f"{eta_sec}s"
