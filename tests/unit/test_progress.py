"""Tests for ``ghrel.progress``.

Pure formatting helpers + the :class:`LineProgressReporter` cadence logic.
"""

from __future__ import annotations

import io

from rich.console import Console

from ghrel.downloader import DownloadProgress
from ghrel.progress import LineProgressReporter, _format_eta, _format_progress_line

# ── Pure formatters ───────────────────────────────────────────────────────────


class TestFormatProgressLine:
    def test_with_total(self) -> None:
        line = _format_progress_line(
            DownloadProgress(bytes_done=1 << 20, bytes_total=4 << 20, elapsed_sec=1.0)
        )
        assert "25%" in line
        assert "1.0 / 4.0 MB" in line
        assert "ETA:" in line

    def test_without_total(self) -> None:
        line = _format_progress_line(
            DownloadProgress(bytes_done=1 << 20, bytes_total=None, elapsed_sec=1.0)
        )
        # No percent / total / ETA when content-length is missing
        assert "%" not in line
        assert "MB at" in line

    def test_with_asset_label(self) -> None:
        line = _format_progress_line(
            DownloadProgress(bytes_done=10, bytes_total=100, elapsed_sec=1.0),
            asset_label="foo.zip",
        )
        assert "[foo.zip]" in line

    # ── KB display for tiny files ──────────────────────────────────────────

    def test_tiny_file_renders_in_kb(self) -> None:
        """A 100 KB file should show KB units, not '0.0 MB'."""
        line = _format_progress_line(
            DownloadProgress(bytes_done=100 * 1024, bytes_total=100 * 1024, elapsed_sec=1.0)
        )
        assert "100.0 KB" in line
        assert "MB" not in line  # no mixed units
        assert "100%" in line

    def test_500_byte_file_kb_units(self) -> None:
        """The realistic case from live tests: a ~500 B PGP signature."""
        line = _format_progress_line(
            DownloadProgress(bytes_done=512, bytes_total=512, elapsed_sec=0.1)
        )
        assert "0.5 KB" in line
        assert "MB" not in line

    def test_units_match_total_not_progress(self) -> None:
        """A 1 MB download shouldn't flip to KB just because we're at the start."""
        line = _format_progress_line(
            DownloadProgress(bytes_done=1024, bytes_total=4 << 20, elapsed_sec=1.0)
        )
        # 4 MB total -> MB units throughout, even at 1 KB done.
        assert "MB" in line
        assert "0.0 / 4.0 MB" in line

    def test_threshold_boundary_at_512_kib(self) -> None:
        """Files at exactly the 512 KiB threshold cross to MB units."""
        # Just below threshold: KB
        below = _format_progress_line(
            DownloadProgress(bytes_done=0, bytes_total=(1 << 19) - 1, elapsed_sec=0.1)
        )
        assert "KB" in below and "MB" not in below
        # At threshold: MB
        at = _format_progress_line(
            DownloadProgress(bytes_done=0, bytes_total=1 << 19, elapsed_sec=0.1)
        )
        assert "MB" in at

    def test_kb_rate_conversion(self) -> None:
        """Rate should also be in KB/s when overall units are KB."""
        line = _format_progress_line(
            DownloadProgress(bytes_done=10 * 1024, bytes_total=100 * 1024, elapsed_sec=1.0)
        )
        # 10 KB downloaded in 1 sec -> 10 KB/s rate display.
        assert "KB/s" in line
        assert "MB/s" not in line


class TestFormatEta:
    def test_under_one_minute(self) -> None:
        # 1 MB remaining at 1 MB/s -> 1 second
        assert _format_eta(bytes_done=0, bytes_total=1 << 20, rate_mb_per_sec=1.0) == "1s"

    def test_minutes_and_seconds(self) -> None:
        # 100 MB remaining at 1 MB/s -> 100s -> "1m 40s"
        eta = _format_eta(bytes_done=0, bytes_total=100 << 20, rate_mb_per_sec=1.0)
        assert eta == "1m 40s"

    def test_zero_rate(self) -> None:
        assert _format_eta(bytes_done=0, bytes_total=100, rate_mb_per_sec=0.0) == "—"


# ── Cadence logic ─────────────────────────────────────────────────────────────


class TestLineProgressReporterCadence:
    def test_first_call_always_emits(self) -> None:
        buf = io.StringIO()
        reporter = LineProgressReporter(Console(file=buf, no_color=True, width=120))
        reporter(DownloadProgress(bytes_done=0, bytes_total=100, elapsed_sec=0.0))
        assert buf.getvalue() != ""

    def test_subsequent_below_interval_skipped(self) -> None:
        buf = io.StringIO()
        # Big interval ensures only the very first / last calls qualify.
        reporter = LineProgressReporter(
            Console(file=buf, no_color=True, width=120),
            min_interval_sec=999.0,
            pct_step=999,
        )
        reporter(DownloadProgress(bytes_done=0, bytes_total=100, elapsed_sec=0.0))
        first = buf.getvalue()
        reporter(DownloadProgress(bytes_done=20, bytes_total=100, elapsed_sec=0.001))
        # No new content — interval not elapsed and % step too large
        assert buf.getvalue() == first

    def test_completion_always_emits(self) -> None:
        buf = io.StringIO()
        reporter = LineProgressReporter(
            Console(file=buf, no_color=True, width=120),
            min_interval_sec=999.0,
            pct_step=999,
        )
        reporter(DownloadProgress(bytes_done=0, bytes_total=100, elapsed_sec=0.0))
        first = buf.getvalue()
        # 100% must always emit, regardless of interval / pct_step.
        reporter(DownloadProgress(bytes_done=100, bytes_total=100, elapsed_sec=0.001))
        assert buf.getvalue() != first
        assert "100%" in buf.getvalue()
