"""Targeted coverage tests for ``ghrel.state_machine``.

These exercise branches that the main e2e suite doesn't naturally hit:
include-pre-release, zero-asset releases, pagination, force-overwrite,
post-download help, filtered-list back, repo-prompt edge cases, auth
logging.

The fixtures (``base_config``, ``captured_console``) and helpers
(``make_release``, ``make_asset``, ``build_machine``, ``stdin_lines``)
come from ``tests/e2e/conftest.py`` — pytest auto-discovers fixtures from
conftest, and the helpers are explicit imports.
"""

from __future__ import annotations

import io
from dataclasses import replace
from pathlib import Path

import pytest
from rich.console import Console

from ghrel.config import Config
from ghrel.state_machine import ExitCode
from tests.e2e.conftest import build_machine, make_asset, make_release, stdin_lines

# ── --include-pre-release ────────────────────────────────────────────────────


class TestIncludePreRelease:
    def test_pre_release_flag_routes_to_include_prerelease_path(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
    ) -> None:
        """include_pre_release=True flips the API call signature."""
        cfg = replace(base_config, include_pre_release=True)
        release = make_release(assets=[make_asset("foo.zip")], is_prerelease=True)
        machine, github, _ = build_machine(cfg, captured_console, release=release)
        machine.run(cli_repo="owner/repo", cli_asset_pattern="foo")
        github.fetch_release.assert_called_once_with("owner/repo", include_prerelease=True)


# ── Zero-asset release ────────────────────────────────────────────────────────


class TestZeroAssetRelease:
    def test_release_with_no_assets_in_single_shot_exits_failure(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
    ) -> None:
        release = make_release(assets=[])
        machine, _, dl = build_machine(base_config, captured_console, release=release)
        exit_code = machine.run(cli_repo="owner/repo", cli_asset_pattern="anything")
        assert exit_code == ExitCode.FAILURE
        assert len(dl.calls) == 0
        _, buf = captured_console
        assert "no downloadable assets" in buf.getvalue()


# ── Pagination ────────────────────────────────────────────────────────────────


class TestPagination:
    def test_zero_advances_to_next_page(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 12 assets -> page 1 has 9, page 2 has 3.
        assets = [make_asset(f"asset-{i:02d}.zip") for i in range(1, 13)]
        release = make_release(assets=assets)
        machine, _, dl = build_machine(base_config, captured_console, release=release)
        # "0" -> next page; pick #1 (== asset-10); quit.
        stdin_lines(monkeypatch, ["0", "1", "3"])
        exit_code = machine.run(cli_repo="owner/repo")
        assert exit_code == ExitCode.SUCCESS
        assert dl.calls[0][1].name == "asset-10.zip"

    def test_zero_on_single_page_prints_no_more_pages(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        release = make_release(assets=[make_asset("only.zip")])
        machine, _, _ = build_machine(base_config, captured_console, release=release)
        # "0" with only one page -> hint shown, prompt re-runs; then quit.
        stdin_lines(monkeypatch, ["0", "q"])
        exit_code = machine.run(cli_repo="owner/repo")
        assert exit_code == ExitCode.SUCCESS
        _, buf = captured_console
        assert "No more pages" in buf.getvalue()


# ── Force overwrite ───────────────────────────────────────────────────────────


class TestForceOverwrite:
    def test_force_skips_overwrite_prompt(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
    ) -> None:
        cfg = replace(base_config, force=True)
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        (cfg.output_dir / "foo.zip").write_bytes(b"old")

        release = make_release(assets=[make_asset("foo.zip")])
        machine, _, dl = build_machine(cfg, captured_console, release=release)
        # No interactive overwrite — force=True bypasses the prompt.
        exit_code = machine.run(cli_repo="owner/repo", cli_asset_pattern="foo.zip")
        assert exit_code == ExitCode.SUCCESS
        assert len(dl.calls) == 1


# ── Post-download menu help ───────────────────────────────────────────────────


class TestPostDownloadHelp:
    def test_help_at_post_download_re_prompts(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        release = make_release(assets=[make_asset("a.zip")])
        machine, _, _ = build_machine(base_config, captured_console, release=release)
        # Pick asset 1, "?" at post-download (help screen renders, prompt re-runs), then quit.
        stdin_lines(monkeypatch, ["1", "?", "3"])
        exit_code = machine.run(cli_repo="owner/repo")
        assert exit_code == ExitCode.SUCCESS
        _, buf = captured_console
        assert "Navigation shortcuts" in buf.getvalue()


# ── Filtered list — "back" returns to full list ──────────────────────────────


class TestFilteredListBack:
    def test_back_from_filtered_returns_to_full_list(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assets = [make_asset(f"file-{i}.zip") for i in range(1, 6)]
        release = make_release(assets=assets)
        machine, _, dl = build_machine(base_config, captured_console, release=release)
        # Search "file" -> N matches -> "b" back to full list -> pick #2 -> quit.
        stdin_lines(monkeypatch, ["file", "b", "2", "3"])
        exit_code = machine.run(cli_repo="owner/repo")
        assert exit_code == ExitCode.SUCCESS
        assert dl.calls[0][1].name == "file-2.zip"


# ── enter_repo edge cases ─────────────────────────────────────────────────────


class TestEnterRepoEdgeCases:
    def test_invalid_repo_text_re_prompts(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine, _, _ = build_machine(base_config, captured_console)
        stdin_lines(monkeypatch, ["not a repo at all", "q"])
        exit_code = machine.run()
        assert exit_code == ExitCode.SUCCESS
        _, buf = captured_console
        assert "Could not parse" in buf.getvalue()

    def test_back_at_main_menu_is_no_op(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine, _, _ = build_machine(base_config, captured_console)
        stdin_lines(monkeypatch, ["b", "q"])
        exit_code = machine.run()
        assert exit_code == ExitCode.SUCCESS
        _, buf = captured_console
        assert "Already at the main menu" in buf.getvalue()

    def test_empty_input_with_no_last_repo_re_prompts(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine, _, _ = build_machine(base_config, captured_console)
        stdin_lines(monkeypatch, ["", "q"])
        exit_code = machine.run()
        assert exit_code == ExitCode.SUCCESS
        _, buf = captured_console
        assert "Repository is required" in buf.getvalue()


# ── Auth logging — token source and length, never the value ──────────────────


class TestAuthLogging:
    def test_authenticated_logs_source_and_length_no_value(
        self,
        captured_console: tuple[Console, io.StringIO],
        tmp_path: Path,
    ) -> None:
        cfg = Config(
            token="ghp_secretxyz12345",  # 18 chars
            token_source="env",
            output_dir=tmp_path / "downloads",
            output_dir_raw="downloads",
            include_pre_release=False,
            parallel=1,
            force=False,
        )
        from loguru import logger

        captured_log: list[str] = []
        sink_id = logger.add(captured_log.append, format="{message}", level="INFO")
        try:
            release = make_release(assets=[make_asset("foo.zip")])
            machine, _, _ = build_machine(cfg, captured_console, release=release)
            machine.run(cli_repo="owner/repo", cli_asset_pattern="foo")
        finally:
            logger.remove(sink_id)

        joined = "\n".join(captured_log)
        assert "Authenticated via env" in joined
        assert "token length=18" in joined
        # The token VALUE must never appear in the log stream.
        assert "ghp_secretxyz12345" not in joined
