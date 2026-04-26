"""Smoke tests for the Typer entrypoint via :class:`CliRunner`.

The state machine itself is exhaustively tested in
``test_state_machine_e2e.py``; this file just verifies that the Typer layer
parses arguments correctly, wires up dependencies, and propagates the exit
code from the state machine to the process. No real network IO.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from ghrel import __version__
from ghrel.cli import app


@pytest.fixture
def runner() -> CliRunner:
    # `mix_stderr` was removed from Click 8.2's CliRunner; we don't need to
    # separate streams for these tests, so default behavior is fine.
    return CliRunner()


class TestCliVersion:
    def test_version_flag(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.stdout


class TestCliCorruptArg:
    def test_corrupt_pattern_exits_with_code_2(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Run with --no-log so no logs/ dir is created.
        # Override --output-dir to keep tmp_path-isolated.
        result = runner.invoke(
            app,
            [
                "--repo",
                "owner/repo",
                "--asset-pattern",
                "= ",
                "--no-log",
                "--output-dir",
                str(tmp_path / "out"),
            ],
        )
        # The corrupt-arg guard fires before any network call, so this is
        # deterministic and offline-safe.
        assert result.exit_code == 2


class TestCliInvalidRepo:
    def test_unparseable_repo_exits_with_failure(
        self,
        runner: CliRunner,
        tmp_path: Path,
    ) -> None:
        result = runner.invoke(
            app,
            [
                "--repo",
                "not a repo",
                "--asset-pattern",
                "foo",
                "--no-log",
                "--output-dir",
                str(tmp_path / "out"),
            ],
        )
        assert result.exit_code == 1
