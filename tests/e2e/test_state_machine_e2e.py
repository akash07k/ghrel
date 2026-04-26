"""End-to-end tests for the state machine.

These tests instantiate :class:`StateMachine` with **real** :class:`Prompts`
and :class:`Console` (so the prompt code paths run, and rich actually renders
output to a captured stream), but **mocked** :class:`GitHubClient` and a
**fake** downloader (so no network IO and no real disk-side downloads).

We script entire user sessions by feeding scripted lines into stdin via the
``capsys`` / ``monkeypatch`` fixtures and asserting on:

- Final exit code
- Captured stdout for evidence of state transitions
- Side effects (files written, ``StateMachine._ctx.completed`` populated)

This is the layer where the multi-select queue, navigation shortcuts, and
the post-download menu all become observable end-to-end.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from ghrel.config import Config
from ghrel.github_api import GitHubApiError
from ghrel.state_machine import ExitCode
from tests.e2e.conftest import (
    FakeAsyncDownloader,
    build_machine,
    make_asset,
    make_release,
    stdin_lines,
)

# ── Single-shot mode ──────────────────────────────────────────────────────────


class TestSingleShotMode:
    def test_match_and_download(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
    ) -> None:
        release = make_release(
            assets=[make_asset("foo-linux-x64.tar.gz"), make_asset("foo-win-x64.zip")]
        )
        machine, github, dl = build_machine(base_config, captured_console, release=release)

        exit_code = machine.run(cli_repo="owner/repo", cli_asset_pattern="*linux-x64*")

        assert exit_code == ExitCode.SUCCESS
        assert len(dl.calls) == 1
        assert dl.calls[0][0].endswith("foo-linux-x64.tar.gz")
        github.fetch_release.assert_called_once_with("owner/repo", include_prerelease=False)

    def test_no_match_exits_with_failure(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
    ) -> None:
        release = make_release(assets=[make_asset("foo-linux-x64.tar.gz")])
        machine, _, dl = build_machine(base_config, captured_console, release=release)

        exit_code = machine.run(cli_repo="owner/repo", cli_asset_pattern="nonexistent_qqq")

        assert exit_code == ExitCode.FAILURE
        assert len(dl.calls) == 0
        _, buf = captured_console
        assert "No asset matching" in buf.getvalue()

    def test_corrupt_arg_exits_with_code_2(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
    ) -> None:
        machine, github, dl = build_machine(base_config, captured_console)

        exit_code = machine.run(cli_repo="owner/repo", cli_asset_pattern="= ")

        assert exit_code == ExitCode.BAD_ARGUMENT
        # Corrupt-arg guard fires BEFORE any API call.
        github.fetch_release.assert_not_called()
        assert len(dl.calls) == 0

    def test_url_repo_is_resolved(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
    ) -> None:
        release = make_release(assets=[make_asset("foo.zip")])
        machine, github, _ = build_machine(base_config, captured_console, release=release)

        machine.run(
            cli_repo="https://github.com/ggml-org/llama.cpp/releases",
            cli_asset_pattern="foo",
        )
        github.fetch_release.assert_called_once_with("ggml-org/llama.cpp", include_prerelease=False)

    def test_invalid_repo_exits_with_failure(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
    ) -> None:
        machine, _, _ = build_machine(base_config, captured_console)
        exit_code = machine.run(cli_repo="not a repo", cli_asset_pattern="foo")
        assert exit_code == ExitCode.FAILURE

    def test_api_error_exits_with_failure(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
    ) -> None:
        machine, _, _ = build_machine(
            base_config,
            captured_console,
            fetch_error=GitHubApiError("Repository not found.", status=404),
        )
        exit_code = machine.run(cli_repo="owner/nope", cli_asset_pattern="foo")
        assert exit_code == ExitCode.FAILURE
        _, buf = captured_console
        assert "Repository not found" in buf.getvalue()


# ── Interactive mode ──────────────────────────────────────────────────────────


class TestInteractiveSingleAsset:
    def test_pick_first_asset_and_quit(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        release = make_release(
            assets=[make_asset("alpha.zip"), make_asset("beta.zip"), make_asset("gamma.zip")]
        )
        machine, _, dl = build_machine(base_config, captured_console, release=release)
        # Pick #2 from the page, then quit at the post-download menu.
        stdin_lines(monkeypatch, ["2", "3"])  # "3" = quit option

        exit_code = machine.run(cli_repo="owner/repo")

        assert exit_code == ExitCode.SUCCESS
        assert len(dl.calls) == 1
        assert dl.calls[0][1].name == "beta.zip"

    def test_quit_at_repo_prompt(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine, github, dl = build_machine(base_config, captured_console)
        stdin_lines(monkeypatch, ["q"])

        exit_code = machine.run()  # no cli_repo -> goes to enter_repo state

        assert exit_code == ExitCode.SUCCESS
        github.fetch_release.assert_not_called()
        assert len(dl.calls) == 0

    def test_quit_at_asset_selector(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        release = make_release(assets=[make_asset("a.zip"), make_asset("b.zip")])
        machine, _, dl = build_machine(base_config, captured_console, release=release)
        stdin_lines(monkeypatch, ["q"])

        exit_code = machine.run(cli_repo="owner/repo")

        assert exit_code == ExitCode.SUCCESS
        assert len(dl.calls) == 0


class TestInteractiveMultiSelect:
    def test_pick_three_assets_in_one_go(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assets = [make_asset(f"file-{i}.zip") for i in range(1, 6)]
        release = make_release(assets=assets)
        machine, _, dl = build_machine(base_config, captured_console, release=release)
        # Pick 1, 3, 5 then quit at post-download.
        stdin_lines(monkeypatch, ["1 3 5", "3"])

        exit_code = machine.run(cli_repo="owner/repo")

        assert exit_code == ExitCode.SUCCESS
        assert [c[1].name for c in dl.calls] == ["file-1.zip", "file-3.zip", "file-5.zip"]

    def test_picked_order_preserved(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        assets = [make_asset(f"file-{i}.zip") for i in range(1, 6)]
        release = make_release(assets=assets)
        machine, _, dl = build_machine(base_config, captured_console, release=release)
        # User-typed order: 4, 1, 3 — should download in that exact order.
        stdin_lines(monkeypatch, ["4, 1, 3", "3"])

        exit_code = machine.run(cli_repo="owner/repo")

        assert exit_code == ExitCode.SUCCESS
        assert [c[1].name for c in dl.calls] == ["file-4.zip", "file-1.zip", "file-3.zip"]

    def test_token_search_then_multi_select(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # 4 cuda+win+x64 zips + a few others
        assets = [
            make_asset("cudart-bin-win-cuda-12.4-x64.zip"),
            make_asset("cudart-bin-win-cuda-13.1-x64.zip"),
            make_asset("foo-bin-win-cuda-12.4-x64.zip"),
            make_asset("foo-bin-win-cuda-13.1-x64.zip"),
            make_asset("foo-bin-macos-arm64.tar.gz"),
        ]
        release = make_release(assets=assets)
        machine, _, dl = build_machine(base_config, captured_console, release=release)
        # Type "cuda win x64 zip" -> 4 matches -> pick 1 and 3 from filtered -> quit
        stdin_lines(monkeypatch, ["cuda win x64 zip", "1 3", "3"])

        exit_code = machine.run(cli_repo="owner/repo")

        assert exit_code == ExitCode.SUCCESS
        # Filtered list preserves input order -> picks 1 and 3 are
        # cudart-bin-win-cuda-12.4-x64.zip and foo-bin-win-cuda-12.4-x64.zip.
        names = [c[1].name for c in dl.calls]
        assert names == [
            "cudart-bin-win-cuda-12.4-x64.zip",
            "foo-bin-win-cuda-12.4-x64.zip",
        ]


class TestInteractivePostDownloadMenu:
    def test_download_another_asset_loops_back(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        release = make_release(assets=[make_asset("a.zip"), make_asset("b.zip")])
        machine, _, dl = build_machine(base_config, captured_console, release=release)
        # Pick asset 1, then "1" (download another) at post-download, pick asset 2, then quit.
        stdin_lines(monkeypatch, ["1", "1", "2", "3"])

        exit_code = machine.run(cli_repo="owner/repo")

        assert exit_code == ExitCode.SUCCESS
        assert [c[1].name for c in dl.calls] == ["a.zip", "b.zip"]

    def test_pick_different_repo_returns_to_main_menu(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        release = make_release(assets=[make_asset("a.zip")])
        machine, github, _ = build_machine(base_config, captured_console, release=release)
        # Pick #1, "2" (different repo) at post-download, then quit at the main menu.
        stdin_lines(monkeypatch, ["1", "2", "q"])

        exit_code = machine.run(cli_repo="owner/repo")
        assert exit_code == ExitCode.SUCCESS
        # GitHub was hit only once — the second main-menu prompt was a quit.
        assert github.fetch_release.call_count == 1


class TestInteractiveOverwrite:
    def test_decline_overwrite_skips_just_this_file(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # Pre-create both target files so the overwrite prompt fires for both.
        (base_config.output_dir).mkdir(parents=True, exist_ok=True)
        (base_config.output_dir / "a.zip").write_bytes(b"existing")
        (base_config.output_dir / "b.zip").write_bytes(b"existing")

        release = make_release(assets=[make_asset("a.zip"), make_asset("b.zip")])
        machine, _, dl = build_machine(base_config, captured_console, release=release)
        # Pick both; decline overwrite for the first; accept for the second; quit.
        stdin_lines(monkeypatch, ["1 2", "n", "y", "3"])

        exit_code = machine.run(cli_repo="owner/repo")

        assert exit_code == ExitCode.SUCCESS
        # Only the second asset was actually downloaded.
        assert [c[1].name for c in dl.calls] == ["b.zip"]


class TestNonePublishedAt:
    """Regression: a ``ReleaseInfo`` with ``published_at=None`` must not
    crash the release-info banner (PyGithub can return None for scheduled
    releases that haven't actually been published yet).
    """

    def test_none_published_at_renders_unknown(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
    ) -> None:
        from dataclasses import replace as dc_replace

        # Build a release with no published_at; everything else is normal.
        release = make_release(assets=[make_asset("ok.zip")])
        release = dc_replace(release, published_at=None)
        machine, _, dl = build_machine(base_config, captured_console, release=release)

        exit_code = machine.run(cli_repo="owner/repo", cli_asset_pattern="ok")

        assert exit_code == ExitCode.SUCCESS
        assert len(dl.calls) == 1
        # The banner should have rendered "unknown" rather than crashing.
        assert "unknown" in captured_console[1].getvalue()


class TestIntegrityMismatch:
    """Regression tests: a digest-mismatch on a downloaded asset must:

    1. Set the exit code to FAILURE (so CI ``ghrel ... && installer.exe`` fails).
    2. Delete the corrupt file (so the bad bytes don't sit on disk waiting
       to be opened later).
    3. Abort any remaining queued downloads (a poisoned release affects all
       its assets, not just one).
    """

    def test_mismatch_fails_run_and_deletes_file(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
    ) -> None:
        # The fake downloader writes 8 zero bytes; SHA256 of those bytes
        # does NOT match this digest, so verify_asset will return MISMATCH.
        bad_digest = "sha256:" + ("a" * 64)
        release = make_release(
            assets=[make_asset("payload.zip", digest=bad_digest)],
        )
        machine, _, dl = build_machine(base_config, captured_console, release=release)

        exit_code = machine.run(cli_repo="owner/repo", cli_asset_pattern="payload")

        assert exit_code == ExitCode.FAILURE
        assert len(dl.calls) == 1
        # The corrupt file must be deleted.
        dest = base_config.output_dir / "payload.zip"
        assert not dest.exists(), "Corrupt file must be removed on MISMATCH"

        _, buf = captured_console
        text = buf.getvalue()
        assert "Integrity MISMATCH" in text
        assert "Removed corrupt file" in text

    def test_mismatch_aborts_remaining_queue(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        bad_digest = "sha256:" + ("b" * 64)
        # First asset has a mismatching digest; second asset comes after.
        release = make_release(
            assets=[
                make_asset("first.zip", digest=bad_digest),
                make_asset("second.zip"),
            ]
        )
        machine, _, dl = build_machine(base_config, captured_console, release=release)
        # Multi-select both assets.
        stdin_lines(monkeypatch, ["1 2"])

        exit_code = machine.run(cli_repo="owner/repo")

        assert exit_code == ExitCode.FAILURE
        # Only the first download was attempted; the second was aborted.
        assert [c[1].name for c in dl.calls] == ["first.zip"]
        assert not (base_config.output_dir / "first.zip").exists()


class TestPostDownloadMenuReentry:
    """Regression test for the ``completed_pairs`` generator-exhaustion bug:

    On the second iteration of the post-download loop (e.g. after the user
    typed ``?`` for help), the summary list must still display the assets
    just downloaded — not "Just downloaded 0 assets:".
    """

    def test_help_then_choice_keeps_summary_visible(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        release = make_release(
            assets=[make_asset(f"file-{i}.zip") for i in range(1, 4)],
        )
        machine, _, _ = build_machine(base_config, captured_console, release=release)
        # Pick all 3 → at the post-download menu type "?" (help) then "3" (quit).
        stdin_lines(monkeypatch, ["1 2 3", "?", "3"])

        exit_code = machine.run(cli_repo="owner/repo")

        assert exit_code == ExitCode.SUCCESS
        text = captured_console[1].getvalue()
        # The summary block prints "Just downloaded N assets:"; 0 would be
        # the bug. Count occurrences — should appear at least once with N=3.
        assert "Just downloaded 3 assets" in text
        assert "Just downloaded 0 assets" not in text


class TestParallelDownload:
    """The ``--parallel`` flag must actually engage the AsyncDownloader path.

    We can't easily exercise the real httpx async client in an e2e test, but
    we can verify that:
      - With parallel=1 the sync downloader is used (existing tests cover this).
      - With parallel>1 the async downloader is constructed.
      - A multi-asset queue with conflicts and no --force falls back to the
        sequential path so per-asset overwrite prompts still fire.
    """

    def test_parallel_with_no_async_downloader_falls_back_to_sync(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When ``async_downloader`` is None (e.g. unit-test wiring), even
        ``parallel > 1`` and a multi-asset queue must use the sync path.
        """
        from dataclasses import replace as dc_replace

        cfg = dc_replace(base_config, parallel=4)
        release = make_release(assets=[make_asset(f"f-{i}.zip") for i in range(1, 4)])
        machine, _, dl = build_machine(cfg, captured_console, release=release)
        # ``build_machine`` doesn't wire an async_downloader — the sync path
        # must still complete the queue without error.
        stdin_lines(monkeypatch, ["1 2 3", "3"])

        exit_code = machine.run(cli_repo="owner/repo")
        assert exit_code == ExitCode.SUCCESS
        assert len(dl.calls) == 3

    def test_parallel_dispatches_through_async_downloader(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With parallel > 1, multi-asset queue, and an async_downloader
        wired up, the entire queue must go through the async path in one
        ``download_many`` call — not the sync per-asset loop.
        """
        from dataclasses import replace as dc_replace

        cfg = dc_replace(base_config, parallel=4)
        release = make_release(assets=[make_asset(f"f-{i}.zip") for i in range(1, 4)])
        async_dl = FakeAsyncDownloader()
        machine, _, sync_dl = build_machine(
            cfg, captured_console, release=release, async_downloader=async_dl
        )
        stdin_lines(monkeypatch, ["1 2 3", "3"])

        exit_code = machine.run(cli_repo="owner/repo")
        assert exit_code == ExitCode.SUCCESS
        # Sync downloader was NEVER called.
        assert len(sync_dl.calls) == 0
        # All three went through the async path.
        assert len(async_dl.calls) == 3
        assert {c[1].name for c in async_dl.calls} == {"f-1.zip", "f-2.zip", "f-3.zip"}

    def test_parallel_with_existing_files_falls_back_to_sequential_prompts(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Parallel mode skips per-asset overwrite prompts. If any destination
        already exists and ``--force`` isn't set, the queue must fall back to
        the sequential path so the user still gets a per-asset overwrite
        prompt — rather than silently overwriting in parallel.
        """
        from dataclasses import replace as dc_replace

        cfg = dc_replace(base_config, parallel=4)
        cfg.output_dir.mkdir(parents=True, exist_ok=True)
        # Pre-create one of the targets to force a conflict.
        (cfg.output_dir / "f-2.zip").write_bytes(b"existing")

        release = make_release(assets=[make_asset(f"f-{i}.zip") for i in range(1, 4)])
        async_dl = FakeAsyncDownloader()
        machine, _, sync_dl = build_machine(
            cfg, captured_console, release=release, async_downloader=async_dl
        )
        # Multi-select all three; for the conflicting #2 the prompt asks
        # overwrite — answer "y". Then quit at post-download.
        stdin_lines(monkeypatch, ["1 2 3", "y", "3"])

        exit_code = machine.run(cli_repo="owner/repo")
        assert exit_code == ExitCode.SUCCESS
        # Async path was NOT used (conflict + no --force forced sync fallback).
        assert len(async_dl.calls) == 0
        # All three went through the sync path.
        assert [c[1].name for c in sync_dl.calls] == ["f-1.zip", "f-2.zip", "f-3.zip"]


class TestInteractiveNavigationShortcuts:
    def test_help_shortcut_at_repo_prompt(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        machine, _, _ = build_machine(base_config, captured_console)
        stdin_lines(monkeypatch, ["?", "q"])  # help, then quit

        exit_code = machine.run()
        assert exit_code == ExitCode.SUCCESS
        _, buf = captured_console
        # Help screen content visible
        assert "Navigation shortcuts" in buf.getvalue()
        assert "Asset selection" in buf.getvalue()

    def test_back_from_asset_selector_returns_to_repo_prompt(
        self,
        base_config: Config,
        captured_console: tuple[Console, io.StringIO],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        release = make_release(assets=[make_asset("a.zip")])
        machine, github, _ = build_machine(base_config, captured_console, release=release)
        # In selector: type 'b' to go back. Then quit at the (now reached) repo prompt.
        stdin_lines(monkeypatch, ["b", "q"])

        exit_code = machine.run(cli_repo="owner/repo")
        assert exit_code == ExitCode.SUCCESS
        # Fetch was still done once (initially), and we never downloaded.
        assert github.fetch_release.call_count == 1
