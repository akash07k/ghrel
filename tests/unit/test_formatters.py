"""Tests for ``ghrel.formatters``."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from ghrel.formatters import format_published_date, format_relative_time

# ── format_published_date ─────────────────────────────────────────────────────


class TestFormatPublishedDate:
    """Long-form publish-date rendering.

    Tests pass ``tz=UTC`` so assertions don't depend on the runner's local
    timezone. Production callers omit ``tz`` and get the system-local TZ.
    """

    def test_basic_format(self) -> None:
        dt = datetime(2026, 4, 25, 14, 24, tzinfo=UTC)
        assert format_published_date(dt, tz=UTC) == "Saturday, April 25, 2026 at 2:24 PM"

    def test_no_leading_zero_on_hour(self) -> None:
        dt = datetime(2026, 4, 5, 9, 5, tzinfo=UTC)
        out = format_published_date(dt, tz=UTC)
        # Hour is 9, not 09.
        assert "at 9:05 AM" in out

    def test_no_leading_zero_on_day(self) -> None:
        dt = datetime(2026, 4, 5, 13, 24, tzinfo=UTC)
        out = format_published_date(dt, tz=UTC)
        # Day is 5, not 05.
        assert "April 5, 2026" in out

    def test_midnight_renders_as_12_am(self) -> None:
        dt = datetime(2026, 4, 25, 0, 5, tzinfo=UTC)
        out = format_published_date(dt, tz=UTC)
        assert "12:05 AM" in out

    def test_none_returns_unknown(self) -> None:
        # Releases without a published_at (e.g. scheduled but not yet public)
        # must render gracefully rather than crashing the banner.
        assert format_published_date(None) == "unknown"
        assert format_published_date(None, tz=UTC) == "unknown"

    def test_noon_renders_as_12_pm(self) -> None:
        dt = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
        out = format_published_date(dt, tz=UTC)
        assert "12:00 PM" in out

    def test_explicit_non_utc_timezone(self) -> None:
        """Demonstrate that ``tz`` is honored — same UTC time, different display."""
        dt = datetime(2026, 4, 25, 14, 24, tzinfo=UTC)
        # +5:30 puts the local time at 7:54 PM on the same day.
        ist = timezone(timedelta(hours=5, minutes=30))
        assert format_published_date(dt, tz=ist) == "Saturday, April 25, 2026 at 7:54 PM"

    def test_naive_datetime_kept_as_local(self) -> None:
        """A naive datetime must not be silently re-interpreted as UTC."""
        dt = datetime(2026, 4, 25, 14, 24)  # naive
        # No conversion happens; we get the input values back, formatted.
        assert format_published_date(dt) == "Saturday, April 25, 2026 at 2:24 PM"

    def test_default_tz_is_system_local(self) -> None:
        """When ``tz`` is omitted, the system local timezone is used.

        We can't assert the exact output (TZ-dependent), but the function
        must not raise and must produce a parseable structure.
        """
        dt = datetime(2026, 4, 25, 14, 24, tzinfo=UTC)
        out = format_published_date(dt)
        assert "2026" in out
        assert ("AM" in out) or ("PM" in out)
        assert out.startswith(("Saturday", "Friday"))  # may roll a day in extreme TZs


# ── format_relative_time ──────────────────────────────────────────────────────


@pytest.fixture
def fixed_now() -> datetime:
    """A deterministic 'now' for relative-time tests."""
    return datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)


class TestFormatRelativeTime:
    @pytest.mark.parametrize(
        ("offset_seconds", "expected"),
        [
            (0, "just now"),
            (10, "just now"),
            (59, "just now"),
            (60, "1 minute ago"),
            (61, "1 minute ago"),
            (120, "2 minutes ago"),
            (59 * 60, "59 minutes ago"),
            (60 * 60, "1 hour ago"),
            (60 * 60 + 1, "1 hour ago"),
            (2 * 60 * 60, "2 hours ago"),
            (23 * 60 * 60 + 59 * 60, "23 hours ago"),
            (24 * 60 * 60, "1 day ago"),
            (2 * 24 * 60 * 60, "2 days ago"),
            (29 * 24 * 60 * 60, "29 days ago"),
            (30 * 24 * 60 * 60, "1 month ago"),
            (60 * 24 * 60 * 60, "2 months ago"),
            (364 * 24 * 60 * 60, "12 months ago"),
            (365 * 24 * 60 * 60, "1 year ago"),
            (3 * 365 * 24 * 60 * 60, "3 years ago"),
        ],
        ids=[
            "0s",
            "10s",
            "59s",
            "1m",
            "61s",
            "2m",
            "59m",
            "1h",
            "1h-1s",
            "2h",
            "23h59m",
            "1d",
            "2d",
            "29d",
            "1mo",
            "2mo",
            "12mo-just-under-year",
            "1y",
            "3y",
        ],
    )
    def test_buckets(
        self,
        fixed_now: datetime,
        offset_seconds: int,
        expected: str,
    ) -> None:
        target = fixed_now - timedelta(seconds=offset_seconds)
        assert format_relative_time(target, now=fixed_now) == expected

    def test_future_timestamp_returns_just_now(self, fixed_now: datetime) -> None:
        """Clock skew can produce future timestamps; we degrade gracefully."""
        future = fixed_now + timedelta(minutes=5)
        assert format_relative_time(future, now=fixed_now) == "just now"

    def test_naive_input_treated_as_utc(self, fixed_now: datetime) -> None:
        naive = datetime(2026, 5, 1, 11, 0)  # 1 hour before fixed_now (UTC)
        assert format_relative_time(naive, now=fixed_now) == "1 hour ago"

    def test_no_now_uses_current_time(self) -> None:
        """When ``now`` is omitted, the current time is used. Just verify
        the function doesn't raise and returns *some* valid bucket label."""
        result = format_relative_time(datetime(2020, 1, 1, tzinfo=UTC))
        assert "year" in result and "ago" in result

    def test_none_returns_unknown(self, fixed_now: datetime) -> None:
        """``None`` published_at must not crash the relative-time helper."""
        assert format_relative_time(None, now=fixed_now) == "unknown"
        assert format_relative_time(None) == "unknown"
