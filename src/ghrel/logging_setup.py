"""Loguru configuration.

Two sinks per run:

- **Console (stderr)** — colorized, INFO+, just the message (no timestamp,
  prompt-friendly).
- **Diag log file** — DEBUG+, two-line entries with a blank line between
  records, in the format::

      LEVEL: <message> |
      <human-readable timestamp>
      <blank line>

  e.g.::

      INFO: Repo resolved at startup: 'https://github.com/owner/repo' -> 'owner/repo' |
      26th April, 2026 at 09:12:25.331 PM

      INFO: Fetching latest stable release from ggml-org/llama.cpp |
      26th April, 2026 at 09:12:25.412 PM

The two-line layout puts the message first (what you scan for) and the
timestamp on its own line for readability — easier to skim in a long log
than a packed single-line ``[timestamp] [LEVEL] message`` format. The
trailing blank line separates entries visually so a long log scans more
like a paragraphed document than a wall of text.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    # ``Record`` is declared in loguru's type stubs but NOT exported at
    # runtime. Importing it conditionally keeps the type annotations honest
    # without breaking the import chain at module load time.
    from loguru import Record

# ── Format constants ──────────────────────────────────────────────────────────


CONSOLE_FORMAT = "<level>{level: <5}</level> {message}"
"""Short, colored, no timestamp — keeps the prompt screen clean."""

# We do NOT use loguru's `{time:Do MMMM, YYYY...}` token here. Loguru renders
# `Do` via its underlying time library, whose ordinal-suffix output is
# locale-sensitive — on systems with a non-English locale you get "26o" or
# "26°" instead of "26th". We format the timestamp ourselves through a
# ``patcher`` that injects an English-locale ``human_date`` field into the
# log record's ``extra`` dict.
#
# The trailing ``\n`` is what separates entries with a blank line on disk:
# loguru appends its own newline after each record, so the format produces
#   ``message-line\n``  ``timestamp-line\n``  ``\n`` (from us)  ``\n`` (loguru)
# which renders as two content lines + one blank line per entry.
DIAG_FORMAT = "{level}: {message} | \n{extra[human_date]}\n"


# ── Setup ─────────────────────────────────────────────────────────────────────


def _ordinal_suffix(day: int) -> str:
    """Return the English ordinal suffix for a 1-31 day-of-month value.

    Special-cases 11/12/13 (which always take ``"th"`` regardless of last
    digit); otherwise the last digit decides: 1→st, 2→nd, 3→rd, else th.
    """
    if 11 <= day % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _attach_human_date(record: Record) -> None:
    """Loguru patcher — adds ``record["extra"]["human_date"]``.

    Renders timestamps as ``"26th April, 2026 at 02:01:02.021 AM"``,
    locale-independently (we control the suffix and the strftime format).
    """
    dt = record["time"]
    suffix = _ordinal_suffix(dt.day)
    record["extra"]["human_date"] = (
        f"{dt.day}{suffix} {dt.strftime('%B')}, {dt.year} at "
        f"{dt.strftime('%I:%M:%S')}.{dt.microsecond // 1000:03d} {dt.strftime('%p')}"
    )


def setup_logging(
    log_dir: Path | None,
    no_log: bool = False,
    console_level: str = "INFO",
    diag_level: str = "DEBUG",
) -> Path | None:
    """Configure the global loguru logger and return the diag log path.

    The default loguru sink is removed so the only sinks are the ones we add
    here (otherwise we'd get duplicate output on stderr).

    Args:
        log_dir: Directory to place the per-run diag log in. Will be created
            if missing. Set to ``None`` (or pass ``no_log=True``) to skip the
            file sink entirely.
        no_log: If ``True``, only the console sink is added.
        console_level: Minimum level for the console sink.
        diag_level: Minimum level for the diag file sink.

    Returns:
        Absolute :class:`Path` to the diag log file, or ``None`` if logging
        was disabled.
    """
    logger.remove()

    # The patcher fires before each record is rendered. It runs cheaply
    # (O(1) string formatting) and lets every sink reference
    # `{extra[human_date]}` if it wants the English-locale ordinal date.
    logger.configure(patcher=_attach_human_date)

    logger.add(
        sys.stderr,
        format=CONSOLE_FORMAT,
        level=console_level,
        colorize=True,
        backtrace=False,
        diagnose=False,
    )

    if no_log or log_dir is None:
        return None

    log_dir.mkdir(parents=True, exist_ok=True)
    # Format: run_YYYY-MM-DD_HH-MM-SS_pidNNNNN_diag.log
    # Dashes in the time component (instead of mashing 020210 together) make
    # the filename scannable, while keeping ISO-style chronological sorting.
    # The ``pid`` prefix labels the otherwise-mystery digits as a process id.
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    diag_path = log_dir / f"run_{timestamp}_pid{os.getpid()}_diag.log"

    logger.add(
        diag_path,
        format=DIAG_FORMAT,
        level=diag_level,
        rotation=None,
        retention=None,
        encoding="utf-8",
        # Always-flush so a Ctrl-C doesn't leave the last lines buffered.
        enqueue=False,
        backtrace=True,
        # ``diagnose=False`` is a deliberate security choice. Loguru's
        # ``diagnose`` enables ``better_exceptions``-style frame-locals
        # dumping for any exception logged via ``logger.exception(...)`` or
        # ``logger.opt(exception=True)``. Our ``Downloader`` and
        # ``GitHubClient`` instances hold the bearer token as ``self._token``;
        # a future ``logger.exception("download failed")`` near that code
        # would dump the token straight to disk. Backtraces alone are enough
        # for diagnostics.
        diagnose=False,
    )

    return diag_path


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of ``headers`` with the Authorization value replaced.

    Used in DEBUG log entries so we can record the *shape* of API requests
    without ever putting the bearer token on disk.
    """
    return {k: ("<redacted>" if k.lower() == "authorization" else v) for k, v in headers.items()}
