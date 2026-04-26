"""Tests for ``ghrel.logging_setup``.

These tests pin two on-disk format guarantees:

1. The diag-log filename uses dash-separated time (``HH-MM-SS``) and a
   ``pid``-prefixed process id, so a casual reader can recognize the
   timestamp at a glance.
2. Each log record produces two content lines plus a trailing blank
   separator line, so consecutive entries are visually distinct in the
   resulting file.
"""

from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

from ghrel.logging_setup import setup_logging


class TestDiagLogFilename:
    """Lock in the human-readable filename pattern.

    Pattern: ``run_YYYY-MM-DD_HH-MM-SS_pidNNN_diag.log``
    """

    def test_filename_uses_dashed_time_and_pid_prefix(self, tmp_path: Path) -> None:
        diag_path = setup_logging(log_dir=tmp_path, no_log=False)
        # Ensure cleanup so a later test doesn't see this sink.
        logger.remove()

        assert diag_path is not None
        name = diag_path.name
        # Sanity: lives in the requested directory.
        assert diag_path.parent == tmp_path
        # Shape check: ``run_<date>_<dashed-time>_pid<digits>_diag.log``.
        match = re.fullmatch(
            r"run_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_pid\d+_diag\.log",
            name,
        )
        assert match is not None, f"Filename did not match expected pattern: {name!r}"

    def test_no_log_returns_none(self, tmp_path: Path) -> None:
        diag_path = setup_logging(log_dir=tmp_path, no_log=True)
        logger.remove()
        assert diag_path is None
        # No file should have been created in tmp_path either.
        assert not list(tmp_path.iterdir())


class TestDiagLogEntrySeparation:
    """Lock in the blank-line-between-entries format.

    Each record renders as::

        LEVEL: message |
        <human-readable timestamp>
        <blank line>

    so two consecutive INFO records produce six lines: 2 content + 1 blank,
    twice. The blank line is what makes long logs scan as paragraphed text
    instead of a wall.
    """

    def test_two_records_have_blank_line_between_them(self, tmp_path: Path) -> None:
        diag_path = setup_logging(log_dir=tmp_path, no_log=False)
        try:
            logger.info("first message")
            logger.info("second message")
        finally:
            # ``logger.remove()`` flushes and closes the file sink, releasing
            # the file handle so Windows can read it back without contention.
            logger.remove()

        assert diag_path is not None
        text = diag_path.read_text(encoding="utf-8")
        lines = text.split("\n")

        # Find the two message lines so we can assert what's between them.
        first_idx = next(i for i, line in enumerate(lines) if "first message" in line)
        second_idx = next(i for i, line in enumerate(lines) if "second message" in line)

        # Layout: <message> | <newline> <timestamp> <newline> <blank> <newline>
        # So between ``first message`` (line N) and ``second message`` we expect:
        #   N+1 = timestamp line
        #   N+2 = blank line
        #   N+3 = "second message" line
        assert second_idx == first_idx + 3, (
            f"Expected exactly one blank line between entries; got "
            f"{second_idx - first_idx - 1} non-message line(s) between them. "
            f"Lines: {lines[first_idx : second_idx + 1]!r}"
        )
        # The blank line really is empty.
        assert lines[first_idx + 2] == ""
        # The line after the first message holds the timestamp; sanity check
        # that it parses as our human-date format.
        assert " at " in lines[first_idx + 1]
        assert ("AM" in lines[first_idx + 1]) or ("PM" in lines[first_idx + 1])
