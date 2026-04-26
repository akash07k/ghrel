"""User-facing string formatters.

Pure functions, no I/O. Two responsibilities:

- :func:`format_published_date` — turn a :class:`datetime` (typically the
  release ``published_at``) into a long, human-friendly string in the user's
  *local* timezone, e.g. ``"Saturday, April 26, 2026 at 8:24 PM"``.
- :func:`format_relative_time` — render a relative phrase such as
  ``"11 minutes ago"`` / ``"3 days ago"`` for the same datetime.

Both intentionally avoid pulling in heavy date-time libraries (humanize,
pendulum) — the small grammar-driven implementation here is plenty for the
release-info banner and stays fully locale-independent.
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo

# Time-bucket thresholds in seconds. The "year" boundary uses 365 days
# rather than 365.25 because the difference is irrelevant once you're at
# the "N years ago" bucket and the simpler value is easier to reason about.
_SEC_PER_MIN = 60
_SEC_PER_HOUR = 60 * 60
_SEC_PER_DAY = 24 * _SEC_PER_HOUR
_SEC_PER_MONTH = 30 * _SEC_PER_DAY
_SEC_PER_YEAR = 365 * _SEC_PER_DAY


def format_published_date(dt: datetime | None, *, tz: tzinfo | None = None) -> str:
    """Render ``dt`` as ``"Weekday, Month D, YYYY at H:MM AM/PM"``.

    Args:
        dt: The datetime to render, or ``None`` for releases that have no
            published timestamp (returns ``"unknown"`` in that case).
            Naive datetimes are kept as-is (treated as already-local) to
            avoid silent timezone shifts.
        tz: Target timezone for display. Defaults to the *system local*
            timezone — that's what end users want to see. Tests that need
            deterministic output should pass an explicit timezone.

    Examples:
        >>> from datetime import datetime, UTC
        >>> dt = datetime(2026, 4, 25, 14, 24, tzinfo=UTC)
        >>> format_published_date(dt, tz=UTC)
        'Saturday, April 25, 2026 at 2:24 PM'
        >>> format_published_date(None)
        'unknown'

    The hour is rendered without a leading zero (``"2:24 PM"``, not
    ``"02:24 PM"``) and the day without one (``"April 5"``, not
    ``"April 05"``) — matches the way English speakers naturally read
    dates aloud.
    """
    if dt is None:
        return "unknown"
    # Convert to the requested (or system-local) timezone for display.
    # PyGithub returns timezone-aware datetimes (UTC) for ``published_at``;
    # if a caller passes a naive datetime we keep it as-is rather than
    # silently re-interpreting it.
    if dt.tzinfo is None:
        local = dt
    elif tz is not None:
        local = dt.astimezone(tz)
    else:
        local = dt.astimezone()

    weekday = local.strftime("%A")
    month = local.strftime("%B")
    day = local.day
    year = local.year

    # strftime("%I") gives 01..12. Strip leading zero, keep "12" intact.
    hour_12 = local.strftime("%I").lstrip("0") or "12"
    minute = local.strftime("%M")
    ampm = local.strftime("%p")

    return f"{weekday}, {month} {day}, {year} at {hour_12}:{minute} {ampm}"


def format_relative_time(dt: datetime | None, *, now: datetime | None = None) -> str:
    """Render ``dt`` as a human-relative phrase like ``"11 minutes ago"``.

    Buckets: ``just now`` (<60 s), minutes (<1 h), hours (<1 d), days
    (<30 d), months (<1 y), years (else). Singular/plural is grammatical
    (``"1 minute ago"`` vs ``"5 minutes ago"``).

    Args:
        dt: The reference time, or ``None`` (returns ``"unknown"``).
            Naive datetimes are treated as UTC.
        now: Override "now" for testability. Defaults to current UTC time.
    """
    if dt is None:
        return "unknown"
    reference = now if now is not None else datetime.now(UTC)
    target = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)

    delta = reference - target
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        # Future date — degenerate but possible with clock skew. Fall back
        # to "just now" rather than confusingly producing "1 minute ago"
        # for a future timestamp.
        return "just now"

    if total_seconds < _SEC_PER_MIN:
        return "just now"
    if total_seconds < _SEC_PER_HOUR:
        n = total_seconds // _SEC_PER_MIN
        return f"{n} minute{'s' if n != 1 else ''} ago"
    if total_seconds < _SEC_PER_DAY:
        n = total_seconds // _SEC_PER_HOUR
        return f"{n} hour{'s' if n != 1 else ''} ago"
    if total_seconds < _SEC_PER_MONTH:
        n = total_seconds // _SEC_PER_DAY
        return f"{n} day{'s' if n != 1 else ''} ago"
    if total_seconds < _SEC_PER_YEAR:
        n = total_seconds // _SEC_PER_MONTH
        return f"{n} month{'s' if n != 1 else ''} ago"
    n = total_seconds // _SEC_PER_YEAR
    return f"{n} year{'s' if n != 1 else ''} ago"
