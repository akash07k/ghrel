"""Interactive state machine driving the asset-download flow.

.. code-block:: text

    enter_repo  ──>  fetch_release  ──>  select_asset  ──>  do_download
       ^                                       |              (loops on queue)
       |                                       v                    |
       |                                                       post_download
       └─── back / "main menu" ────────────────────────────────────┘

Single-shot mode (CLI ``--asset-pattern`` provided) skips ``select_asset``'s
interactive UI: the pattern is fed into :func:`find_matching_assets`, the
first match is downloaded, and the program exits with the resulting code
(no post-download menu).

The implementation is constructor-injection-friendly so e2e tests can drive
the machine with mocked dependencies (fake :class:`Prompts`, mocked
:class:`GitHubClient`, fake :class:`Downloader`).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

from loguru import logger
from rich.console import Console

from ghrel.config import Config
from ghrel.downloader import AsyncDownloader, Downloader, DownloadError, DownloadResult
from ghrel.formatters import format_published_date, format_relative_time
from ghrel.github_api import (
    AssetInfo,
    GitHubApiError,
    GitHubClient,
    InvalidRepoError,
    ReleaseInfo,
    resolve_github_repo,
)
from ghrel.path_utils import UnsafeAssetNameError, get_safe_asset_path
from ghrel.progress import LineProgressReporter
from ghrel.prompts import NavAction, Prompts
from ghrel.selector import (
    PickStatus,
    find_matching_assets,
    parse_picked_numbers,
)
from ghrel.verifier import (
    AssetLike,
    ChecksumLoader,
    VerifyOutcome,
    verify_asset,
)

# ── Exit codes ────────────────────────────────────────────────────────────────


class ExitCode:
    """Distinct exit codes so callers (CI, scripts) can branch on failure type.

    Contract: 0 = success, 1 = normal failure, 2 = malformed argument
    (corrupt-arg guard tripped).
    """

    SUCCESS = 0
    FAILURE = 1
    BAD_ARGUMENT = 2


# ── Defensive corrupt-arg guard ───────────────────────────────────────────────


# These regexes target the *signature shape* of cmd-batch substitution leaks
# (``"= `` and bare ``=``); they are intentionally narrow. Anything that
# doesn't match falls through to :func:`find_matching_assets`, which simply
# returns no results — so a corrupt pattern that slips past here ends in a
# clean "no asset matched" failure, not a crash.
_CORRUPT_PATTERNS = (
    re.compile(r"^\s*=\s*$"),
    re.compile(r'^"?='),
)


def is_corrupt_asset_pattern(pattern: str) -> bool:
    """Detect the cmd-batch ``set VAR=!VAR:s=r!`` corruption signature.

    Some Windows cmd-batch wrappers, when the user presses Enter at an
    unfilled ``set /p`` prompt, leak the literal substitution syntax fragments
    (``"= ``-style values) instead of an empty string. If a wrapper invokes
    ``ghrel`` with a corrupt ``--asset-pattern`` value, we catch it here and
    exit with a clear error before any network call is made.

    This is a *signature detector*, not a full validator: only the two
    canonical leak shapes are matched. Patterns that don't match fall through
    to normal asset matching, which will simply return zero results for
    nonsense input.
    """
    if not pattern:
        return False
    return any(p.search(pattern) for p in _CORRUPT_PATTERNS)


# ── Page constants ────────────────────────────────────────────────────────────


PAGE_SIZE: int = 9
"""Items per page in the interactive selector. Single-digit page picks ([1]
through [9]) plus '0' for next page — keeps the input space tight and
eliminates the visual ambiguity of two-digit picks adjacent to multi-select
inputs like ``"1 2"``."""


# ── State enum ────────────────────────────────────────────────────────────────


class State(Enum):
    ENTER_REPO = auto()
    FETCH_RELEASE = auto()
    SELECT_ASSET = auto()
    DO_DOWNLOAD = auto()
    POST_DOWNLOAD = auto()
    QUIT = auto()


# ── Machine state container ───────────────────────────────────────────────────


@dataclass
class _Context:
    """Mutable state held across state-machine iterations.

    Kept private to this module — tests inspect it via :class:`StateMachine`'s
    methods, never directly.
    """

    current_repo: str | None = None
    """Resolved ``owner/repo`` (post-Resolve-GitHubRepo)."""

    current_release: ReleaseInfo | None = None
    current_assets: list[AssetInfo] = field(default_factory=list)

    download_queue: list[AssetInfo] = field(default_factory=list)
    completed: list[tuple[AssetInfo, Path]] = field(default_factory=list)
    total_queued: int = 0
    """Snapshot of queue size at the start of the run, for "X of Y" display."""

    page: int = 0
    """Current page in the asset selector (0-indexed)."""

    exit_code: int = ExitCode.SUCCESS

    single_shot: bool = False
    """Set by ``run()`` from the presence of ``cli_asset_pattern``. Used by
    state handlers that need to choose between "interactive recovery"
    (return to a previous prompt) and "fail fast and exit" (single-shot)."""


# ── State machine ─────────────────────────────────────────────────────────────


class StateMachine:
    """Drives the interactive flow.

    Construct with config + dependencies, then call :meth:`run` with optional
    CLI overrides (``cli_repo`` / ``cli_asset_pattern``). The machine manages
    its own state; ``run`` returns the final exit code.
    """

    def __init__(
        self,
        *,
        config: Config,
        prompts: Prompts,
        github_client: GitHubClient,
        downloader: Downloader,
        async_downloader: AsyncDownloader | None = None,
        console: Console | None = None,
    ) -> None:
        self._config = config
        self._prompts = prompts
        self._github = github_client
        self._downloader = downloader
        # The async downloader is only consulted when parallel > 1 *and* the
        # current queue has more than one asset. If unset (e.g. in unit tests
        # that pass parallel=1), the sync path is always used.
        self._async_downloader = async_downloader
        self._console = console or Console()
        self._ctx = _Context()

    # ── Public entry point ────────────────────────────────────────────────

    def run(
        self,
        *,
        cli_repo: str | None = None,
        cli_asset_pattern: str | None = None,
    ) -> int:
        """Run the state machine and return the final exit code.

        Args:
            cli_repo: Optional pre-filled repo from a CLI flag. Goes through
                :func:`resolve_github_repo` so URLs work.
            cli_asset_pattern: When provided, switches the machine into
                "single-shot" mode (no interactive selector, no post-download
                loop).

        Returns:
            One of the :class:`ExitCode` values.
        """
        # Defensive corrupt-arg guard — fires before any network call.
        if cli_asset_pattern and is_corrupt_asset_pattern(cli_asset_pattern):
            self._console.print(
                f"[red]ERROR:[/] Received a malformed --asset-pattern value: {cli_asset_pattern!r}"
            )
            self._console.print(
                "[dim]This usually means a wrapper script passed a corrupted argument.[/]"
            )
            logger.error(
                f"AssetPattern looks corrupt (cmd-batch substitution bug?): {cli_asset_pattern!r}"
            )
            return ExitCode.BAD_ARGUMENT

        single_shot = bool(cli_asset_pattern)
        self._ctx.single_shot = single_shot
        logger.info(f"Mode: {'single-shot (CLI)' if single_shot else 'interactive (loop)'}")
        # Auth state — logged so `did my token even load?` is answerable from the diag log.
        # The token *value* is never logged — only its source and length.
        if self._config.token:
            token_source = self._config.token_source
            logger.info(
                f"Authenticated via {token_source} (token length={len(self._config.token)})"
            )
        else:
            logger.info("Running unauthenticated (60 req/hour rate limit)")

        # Normalize repo if CLI provided one.
        if cli_repo:
            try:
                self._ctx.current_repo = resolve_github_repo(cli_repo)
                if self._ctx.current_repo != cli_repo:
                    logger.info(
                        f"Repo resolved at startup: '{cli_repo}' -> '{self._ctx.current_repo}'"
                    )
            except InvalidRepoError as exc:
                self._console.print(f"[red]ERROR:[/] {exc}")
                logger.error(f"Invalid repo argument: {exc}")
                return ExitCode.FAILURE

        state = State.FETCH_RELEASE if self._ctx.current_repo else State.ENTER_REPO

        # Direct match dispatch so mypy/pyright can statically verify the
        # state-handler signatures (a dict-of-lambdas obscured the types).
        while state is not State.QUIT:
            logger.debug(f"State: {state.name}")
            match state:
                case State.ENTER_REPO:
                    state = self._enter_repo()
                case State.FETCH_RELEASE:
                    state = self._fetch_release()
                case State.SELECT_ASSET:
                    state = self._select_asset(
                        cli_asset_pattern=cli_asset_pattern,
                        single_shot=single_shot,
                    )
                case State.DO_DOWNLOAD:
                    state = self._do_download(single_shot=single_shot)
                case State.POST_DOWNLOAD:
                    state = self._post_download(single_shot=single_shot)
                case State.QUIT:
                    break

        return self._ctx.exit_code

    # ── State handlers ────────────────────────────────────────────────────

    def _enter_repo(self) -> State:
        result = self._prompts.repo(last_repo=self._ctx.current_repo)
        if result.is_nav:
            if result.nav is NavAction.QUIT:
                return State.QUIT
            if result.nav is NavAction.HELP:
                self._prompts.show_help()
                return State.ENTER_REPO
            # BACK / MENU at the main menu are no-ops; just re-prompt.
            self._console.print("[yellow]Already at the main menu.[/]")
            return State.ENTER_REPO

        text = (result.value or "").strip()
        if not text:
            if self._ctx.current_repo:
                # Empty input + a previous repo = "reuse last".
                return State.FETCH_RELEASE
            self._console.print(
                "[yellow]Repository is required. Type 'q' to quit or '?' for help.[/]"
            )
            return State.ENTER_REPO

        try:
            self._ctx.current_repo = resolve_github_repo(text)
        except InvalidRepoError as exc:
            self._console.print(f"[yellow]Could not parse {text!r} as a repo or URL: {exc}[/]")
            return State.ENTER_REPO

        logger.info(f"Repo resolved: '{text}' -> '{self._ctx.current_repo}'")
        return State.FETCH_RELEASE

    def _fetch_release(self) -> State:
        repo = self._ctx.current_repo
        assert repo is not None, "fetch_release entered without a repo"

        label = (
            "latest release (including pre-releases)"
            if self._config.include_pre_release
            else "latest stable release"
        )
        self._console.print(f"\n  Fetching {label} from [yellow]{repo}[/] ...")
        logger.info(
            f"API GET releases for {repo} (include_prerelease={self._config.include_pre_release})"
        )

        try:
            release = self._github.fetch_release(
                repo, include_prerelease=self._config.include_pre_release
            )
        except GitHubApiError as exc:
            self._console.print(f"[red]ERROR:[/] Could not fetch release: {exc}")
            logger.error(f"API call failed: {exc}")
            self._ctx.exit_code = ExitCode.FAILURE
            # In single-shot the run is over; in interactive offer to retry.
            return self._after_fetch_failure()

        self._ctx.current_release = release
        self._ctx.current_assets = list(release.assets)
        self._ctx.page = 0

        prerelease_label = " (pre-release)" if release.is_prerelease else ""
        self._console.print(f"\n  Latest release : [green]{release.tag}{prerelease_label}[/]")
        if release.name and release.name != release.tag:
            self._console.print(f"  Release name   : {release.name}")
        published_long = format_published_date(release.published_at)
        published_relative = format_relative_time(release.published_at)
        self._console.print(f"  Published      : {published_long} ({published_relative})")
        self._console.print(f"  Total assets   : {len(release.assets)}")
        published_iso = (
            release.published_at.isoformat() if release.published_at is not None else "unknown"
        )
        logger.info(
            f"Release: tag={release.tag} prerelease={release.is_prerelease} "
            f"published={published_iso} "
            f"assets={len(release.assets)}"
        )

        if not release.assets:
            self._console.print("[red]ERROR:[/] This release has no downloadable assets.")
            logger.error(f"Release {release.tag} has zero assets")
            self._ctx.exit_code = ExitCode.FAILURE
            return self._after_fetch_failure()

        return State.SELECT_ASSET

    def _after_fetch_failure(self) -> State:
        """Decide where to go after fetch_release fails or yields no assets.

        Single-shot mode quits with the failure code already in
        ``self._ctx.exit_code``; interactive mode drops back to the repo
        prompt so the user can try a different repo without re-launching.
        """
        if self._ctx.single_shot:
            return State.QUIT
        # Clear the cached repo so the prompt asks fresh (otherwise the
        # "Enter to reuse <bad-repo>" hint would point at the failing one).
        self._ctx.current_repo = None
        return State.ENTER_REPO

    # ── Selector ──────────────────────────────────────────────────────────

    def _select_asset(
        self,
        *,
        cli_asset_pattern: str | None,
        single_shot: bool,
    ) -> State:
        # Reset per-batch state.
        self._ctx.download_queue = []
        self._ctx.completed = []

        if single_shot and cli_asset_pattern:
            matches = find_matching_assets(cli_asset_pattern, self._ctx.current_assets)
            if not matches:
                self._console.print(f"[red]No asset matching {cli_asset_pattern!r} found.[/]")
                self._console.print("  Available assets:")
                for a in self._ctx.current_assets:
                    self._console.print(f"    - {a.name}")
                logger.error(f"AssetPattern '{cli_asset_pattern}' matched no assets")
                self._ctx.exit_code = ExitCode.FAILURE
                return State.QUIT
            self._ctx.download_queue = [matches[0]]
            self._ctx.total_queued = 1
            logger.info(
                f"AssetPattern '{cli_asset_pattern}' matched {len(matches)} asset(s); "
                f"picking {matches[0].name}"
            )
            return State.DO_DOWNLOAD

        return self._select_asset_interactive()

    def _select_asset_interactive(self) -> State:
        """Show the paged asset list and read user input until something useful happens."""
        while True:
            assets = self._ctx.current_assets
            total = len(assets)
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            self._ctx.page = min(self._ctx.page, pages - 1)

            start = self._ctx.page * PAGE_SIZE
            end = min(start + PAGE_SIZE, total)
            page_items = assets[start:end]
            has_more = (self._ctx.page + 1) < pages

            self._render_asset_list(start, end, total, page_items, has_more)

            result = self._prompts.asset_choice(
                page_label=f"{start + 1}-{end} of {total}",
                page_size=len(page_items),
                has_more=has_more,
            )

            if result.is_nav:
                if result.nav is NavAction.QUIT:
                    return State.QUIT
                if result.nav in {NavAction.BACK, NavAction.MENU}:
                    return State.ENTER_REPO
                if result.nav is NavAction.HELP:
                    self._prompts.show_help()
                    continue

            user_input = (result.value or "").strip()
            if not user_input:
                continue

            # "0" means "next page" on a paginated list (single-token only).
            if user_input == "0":
                if has_more:
                    self._ctx.page += 1
                    continue
                self._console.print("[yellow]No more pages.[/]")
                continue

            # Try multi-number parse.
            picked = parse_picked_numbers(user_input, max_value=len(page_items))
            if picked.status is PickStatus.OK:
                self._ctx.download_queue = [page_items[i - 1] for i in picked.numbers]
                self._ctx.total_queued = len(self._ctx.download_queue)
                names = ", ".join(a.name for a in self._ctx.download_queue)
                logger.info(f"Multi-select picked {self._ctx.total_queued}: {names}")
                if self._ctx.total_queued > 1:
                    self._console.print(
                        f"  [green]Picked {self._ctx.total_queued} assets:[/] {names}"
                    )
                return State.DO_DOWNLOAD
            if picked.status is PickStatus.OUT_OF_RANGE:
                hint = " or 0 for more" if has_more else ""
                self._console.print(
                    f"[yellow]Number {picked.bad_number} is out of range. "
                    f"Valid: 1-{len(page_items)}{hint}.[/]"
                )
                continue

            # Fall through: treat as text filter.
            filtered = find_matching_assets(user_input, assets)
            if not filtered:
                self._console.print(
                    f"[yellow]No assets match {user_input!r}. Try different/fewer tokens.[/]"
                )
                continue
            if len(filtered) == 1:
                self._console.print(f"  [green]Matched:[/] {filtered[0].name}")
                self._ctx.download_queue = [filtered[0]]
                self._ctx.total_queued = 1
                return State.DO_DOWNLOAD

            sub_state = self._select_from_filtered(filtered)
            if sub_state is not None:
                return sub_state
            # sub_state == None means "go back to full list" — re-loop.

    def _select_from_filtered(self, filtered: list[AssetInfo]) -> State | None:
        """Show a filtered sub-list, paginated with the same single-digit
        scheme as the main selector. Returns ``None`` to mean "user wants the
        full list again" and re-loop to the caller's selector.
        """
        page = 0
        while True:
            total = len(filtered)
            pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(page, pages - 1)
            start = page * PAGE_SIZE
            end = min(start + PAGE_SIZE, total)
            page_items = filtered[start:end]
            has_more = (page + 1) < pages

            self._console.print()
            page_label = f"{start + 1}-{end} of {total}"
            self._console.print(f"  [cyan]{total} matches ({page_label}):[/]")
            for i, a in enumerate(page_items, start=1):
                mb = a.size / (1 << 20)
                self._console.print(f"  [bold][{i}][/] {a.name:<68} {mb:7.1f} MB")
            if has_more:
                self._console.print("  [bold][0][/] Show more...")
            self._console.print()

            result = self._prompts.asset_choice(
                page_label=page_label,
                page_size=len(page_items),
                has_more=has_more,
                prompt_label="Pick number(s)",
            )
            if result.is_nav:
                if result.nav is NavAction.QUIT:
                    return State.QUIT
                if result.nav is NavAction.MENU:
                    return State.ENTER_REPO
                if result.nav is NavAction.HELP:
                    self._prompts.show_help()
                    continue
                # BACK = back to full list
                return None

            text = (result.value or "").strip()
            if not text:
                return None  # empty Enter = back to full list

            if text == "0":
                if has_more:
                    page += 1
                    continue
                self._console.print("[yellow]No more pages.[/]")
                continue

            picked = parse_picked_numbers(text, max_value=len(page_items))
            if picked.status is PickStatus.OK:
                self._ctx.download_queue = [page_items[i - 1] for i in picked.numbers]
                self._ctx.total_queued = len(self._ctx.download_queue)
                names = ", ".join(a.name for a in self._ctx.download_queue)
                logger.info(f"Multi-select (filtered) picked {self._ctx.total_queued}: {names}")
                if self._ctx.total_queued > 1:
                    self._console.print(
                        f"  [green]Picked {self._ctx.total_queued} assets:[/] {names}"
                    )
                return State.DO_DOWNLOAD
            if picked.status is PickStatus.OUT_OF_RANGE:
                hint = " or 0 for more" if has_more else ""
                self._console.print(
                    f"[yellow]Number {picked.bad_number} is out of range. "
                    f"Valid: 1-{len(page_items)}{hint}.[/]"
                )
                continue

            self._console.print("[yellow]Returning to full list.[/]")
            return None

    def _render_asset_list(
        self,
        start: int,
        end: int,
        total: int,
        page_items: Sequence[AssetInfo],
        has_more: bool,
    ) -> None:
        self._console.print()
        self._console.print(f"  [cyan]Available assets ({start + 1}-{end} of {total}):[/]")
        for i, a in enumerate(page_items, start=1):
            mb = a.size / (1 << 20)
            self._console.print(f"  [bold][{i}][/] {a.name:<68} {mb:7.1f} MB")
        if has_more:
            self._console.print("  [bold][0][/] Show more...")

    # ── Download ──────────────────────────────────────────────────────────

    def _do_download(self, *, single_shot: bool) -> State:
        if not self._ctx.download_queue:
            return State.POST_DOWNLOAD

        # Parallel fast-path: when configured for >1 simultaneous downloads
        # and the queue has more than one asset, batch-download via the async
        # client. The sync per-asset path below still handles single-asset
        # queues, conflict prompts, and the parallel-but-conflicting fallback.
        if (
            self._async_downloader is not None
            and self._config.parallel > 1
            and len(self._ctx.download_queue) > 1
            and self._can_use_parallel()
        ):
            return self._do_download_parallel(single_shot=single_shot)

        # Pop head.
        asset = self._ctx.download_queue[0]
        self._ctx.download_queue = self._ctx.download_queue[1:]
        index = self._ctx.total_queued - len(self._ctx.download_queue)

        if self._ctx.total_queued > 1:
            self._console.print()
            self._console.rule(
                f"[bold cyan]Downloading {index} of {self._ctx.total_queued}[/]",
                align="left",
            )

        # Resolve safe destination.
        try:
            dest = get_safe_asset_path(asset.name, self._config.output_dir)
        except UnsafeAssetNameError as exc:
            self._console.print(f"[red]ERROR:[/] {exc}")
            logger.error(f"Asset name rejected: {exc}")
            return self._after_download_skip(single_shot)

        logger.info(f"Asset: {asset.name} size={asset.size} url={asset.download_url}")
        logger.info(f"Destination: {dest}")

        # Overwrite check.
        if dest.exists() and not self._config.force:
            existing_bytes = dest.stat().st_size
            ow = self._prompts.overwrite(asset_name=asset.name, existing_size_bytes=existing_bytes)
            if ow.is_nav:
                if ow.nav is NavAction.QUIT:
                    self._ctx.download_queue = []
                    return State.QUIT
                if ow.nav is NavAction.BACK:
                    self._ctx.download_queue = []
                    return State.SELECT_ASSET
                if ow.nav is NavAction.MENU:
                    self._ctx.download_queue = []
                    return State.ENTER_REPO
                if ow.nav is NavAction.HELP:
                    self._prompts.show_help()
                    # Re-add this asset to the front of the queue and re-loop.
                    self._ctx.download_queue.insert(0, asset)
                    return State.DO_DOWNLOAD
            if ow.value != "yes":
                logger.info(f"Skipped file (declined overwrite): {asset.name}")
                return self._after_download_skip(single_shot)
        elif dest.exists() and self._config.force:
            logger.info(f"Existing file overwritten via --force: {asset.name}")

        # Actually download.
        self._console.print(f"\n  Downloading: [cyan]{asset.name}[/]")
        reporter = LineProgressReporter(
            self._console,
            asset_label=asset.name if self._ctx.total_queued > 1 else None,
        )
        try:
            self._downloader.download(asset.download_url, dest, on_progress=reporter)
        except DownloadError as exc:
            self._console.print(f"[red]ERROR:[/] {exc}")
            logger.error(f"Download failed: {exc}")
            self._ctx.exit_code = ExitCode.FAILURE
            return State.QUIT

        self._console.print(f"  [green]Saved:[/] {dest}")
        logger.info(f"Saved to {dest}")

        # Verify integrity. Returns False on MISMATCH (file already deleted,
        # exit_code set, queue cleared); we go straight to QUIT in that case
        # rather than offering a post-download menu over corrupt evidence.
        if not self._verify_and_report(asset, dest):
            return State.QUIT

        self._ctx.completed.append((asset, dest))

        if self._ctx.download_queue:
            return State.DO_DOWNLOAD
        return State.QUIT if single_shot else State.POST_DOWNLOAD

    def _after_download_skip(self, single_shot: bool) -> State:
        """Decide next state after skipping (declining overwrite OR rejecting an unsafe name)."""
        if self._ctx.download_queue:
            return State.DO_DOWNLOAD
        if not self._ctx.completed:
            return State.QUIT if single_shot else State.SELECT_ASSET
        return State.QUIT if single_shot else State.POST_DOWNLOAD

    def _can_use_parallel(self) -> bool:
        """Whether the current queue is eligible for the parallel fast-path.

        Parallel mode skips per-asset interactive prompts (overwrite, etc.),
        so it's only safe when:

        - all destination paths resolve (no unsafe names), AND
        - either no destination already exists, OR ``--force`` was passed.

        Otherwise we fall back to the sequential path so the user still gets
        a per-asset overwrite prompt and unsafe names are surfaced one at a
        time. This sidesteps the "interleaved prompts during parallel download"
        UX problem without sacrificing safety.
        """
        for asset in self._ctx.download_queue:
            try:
                dest = get_safe_asset_path(asset.name, self._config.output_dir)
            except UnsafeAssetNameError:
                return False
            if dest.exists() and not self._config.force:
                return False
        return True

    def _do_download_parallel(self, *, single_shot: bool) -> State:
        """Download the entire queue concurrently via :class:`AsyncDownloader`.

        Pre-conditions enforced by ``_can_use_parallel``: every asset name
        resolves to a safe path, and either no destination conflicts exist
        or ``--force`` is set. So this method does not need overwrite
        prompts — it just dispatches and reports per-asset outcomes.

        Verification still runs sequentially after dispatch (verify_asset
        does file I/O and we want the MISMATCH-aborts-the-queue semantics,
        even though the queue is already drained at this point).
        """
        assert self._async_downloader is not None  # checked by caller

        queue = self._ctx.download_queue
        self._ctx.download_queue = []

        # Resolve destinations once. ``_can_use_parallel`` already verified
        # safety, so any error here would be a logic bug; surface it loudly.
        items: list[tuple[AssetInfo, str, Path]] = []
        for asset in queue:
            dest = get_safe_asset_path(asset.name, self._config.output_dir)
            if dest.exists() and self._config.force:
                logger.info(f"Existing file overwritten via --force: {asset.name}")
            items.append((asset, asset.download_url, dest))
            logger.info(f"Asset: {asset.name} size={asset.size} url={asset.download_url}")
            logger.info(f"Destination: {dest}")

        self._console.print()
        self._console.rule(
            f"[bold cyan]Downloading {len(items)} assets (parallel={self._config.parallel})[/]",
            align="left",
        )
        for asset, _, _ in items:
            self._console.print(f"  Queued: [cyan]{asset.name}[/]")

        # Per-asset progress reporters keyed by URL — each prints lines
        # prefixed with ``[<asset-name>]`` so concurrent reports stay
        # individually readable when they interleave (asyncio cooperative
        # scheduling guarantees a single ``console.print`` call is atomic;
        # only whole lines interleave, never characters within a line).
        reporters: dict[str, LineProgressReporter] = {
            url: LineProgressReporter(self._console, asset_label=asset.name)
            for asset, url, _ in items
        }

        async def run_batch() -> list[DownloadResult | DownloadError]:
            assert self._async_downloader is not None
            return await self._async_downloader.download_many(
                ((url, dest) for _, url, dest in items),
                progress_factory=reporters.get,
                progress_interval=0.5,
            )

        try:
            results = asyncio.run(run_batch())
        except RuntimeError as exc:
            # Surface a clear message if a caller is already running an event
            # loop (shouldn't happen via the CLI, but tests might).
            self._console.print(f"[red]ERROR:[/] Parallel dispatch failed: {exc}")
            logger.error(f"asyncio.run failed in parallel dispatch: {exc}")
            self._ctx.exit_code = ExitCode.FAILURE
            return State.QUIT

        # Sequential post-processing: print each outcome, run verifier on
        # successes. MISMATCH still aborts the rest of the verification loop
        # (and any sibling files we'd otherwise treat as completed).
        any_failure = False
        for (asset, _, dest), result in zip(items, results, strict=True):
            if isinstance(result, DownloadError):
                self._console.print(f"  [red]Failed:[/] {asset.name}: {result}")
                logger.error(f"Download failed: {asset.name}: {result}")
                any_failure = True
                continue
            self._console.print(
                f"  [green]Saved:[/] {asset.name} "
                f"([dim]{result.bytes_written / (1 << 20):.1f} MB in "
                f"{result.elapsed_sec:.1f} s[/])"
            )
            logger.info(f"Saved to {dest}")

            if not self._verify_and_report(asset, dest):
                # MISMATCH: file deleted, exit_code already set. We still
                # treat earlier successful completions as kept on disk.
                any_failure = True
                break

            self._ctx.completed.append((asset, dest))

        if any_failure:
            if self._ctx.exit_code == ExitCode.SUCCESS:
                self._ctx.exit_code = ExitCode.FAILURE
            return State.QUIT

        return State.QUIT if single_shot else State.POST_DOWNLOAD

    def _make_checksum_loader(self) -> ChecksumLoader:
        """Build a checksum-file loader for :func:`verify_asset`.

        The loader downloads the checksum file via our existing
        :class:`Downloader` (so it shares the auth/redirect handling) into a
        temp file, reads it as UTF-8 text, and returns the contents. On any
        download or decode failure it returns ``None`` — :func:`verify_asset`
        treats ``None`` as "no expected hash available" and falls through to
        the SHA256-for-records branch.
        """
        import tempfile

        downloader = self._downloader

        def loader(asset: AssetLike) -> str | None:
            # Use the system temp dir for the checksum file rather than our
            # configured output_dir — we're going to delete it immediately,
            # and we don't want to clutter the user's downloads/.
            tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115
                mode="wb",
                delete=False,
                suffix=".checksum",
                prefix="ghrel-",
            )
            tmp.close()
            tmp_path = Path(tmp.name)
            try:
                downloader.download(asset.download_url, tmp_path)
                return tmp_path.read_text(encoding="utf-8", errors="replace")
            except (DownloadError, OSError, UnicodeError) as exc:
                logger.warning(f"Could not load checksum file '{asset.name}': {exc}")
                return None
            finally:
                # Best-effort cleanup; never raises.
                with contextlib.suppress(OSError):
                    tmp_path.unlink(missing_ok=True)

        return loader

    def _verify_and_report(self, asset: AssetInfo, dest: Path) -> bool:
        """Run the verification chain and print a one-line outcome.

        Returns ``True`` if the file is safe to keep (OK, NO_EXPECTED_HASH, or
        an unverifiable error); ``False`` only on confirmed MISMATCH. On
        MISMATCH the corrupt file is deleted, ``self._ctx.exit_code`` is set
        to FAILURE, and the rest of the queue is cleared — a tampered or
        corrupted release is more likely than a single-asset hash collision,
        so continuing to download more potentially-compromised binaries is
        the wrong default.
        """
        loader: ChecksumLoader | None = self._make_checksum_loader()
        try:
            report = verify_asset(
                asset, dest, list(self._ctx.current_assets), checksum_loader=loader
            )
        except OSError as exc:
            self._console.print(f"[yellow]Could not verify integrity: {exc}[/]")
            logger.warning(f"Verification I/O error: {exc}")
            return True  # Couldn't check; assume safe (matches prior behavior).

        algo = report.algorithm or "?"
        if report.outcome is VerifyOutcome.OK:
            self._console.print(f"  [green]Integrity OK[/] ({algo})")
            logger.info(f"Integrity OK ({algo}={report.actual_hash})")
            return True
        if report.outcome is VerifyOutcome.MISMATCH:
            self._console.print(
                f"  [red]Integrity MISMATCH[/] ({algo}): "
                f"expected {report.expected_hash}, got {report.actual_hash}"
            )
            logger.error(
                f"Integrity mismatch ({algo}): expected={report.expected_hash} "
                f"actual={report.actual_hash} file={dest}"
            )
            with contextlib.suppress(OSError):
                dest.unlink(missing_ok=True)
            self._console.print(f"  [red]Removed corrupt file:[/] {dest}")
            self._ctx.exit_code = ExitCode.FAILURE
            # Abort any remaining downloads in the queue — a poisoned release
            # is more likely than a one-off hash collision.
            if self._ctx.download_queue:
                remaining = len(self._ctx.download_queue)
                self._console.print(
                    f"  [yellow]Aborting remaining {remaining} download(s) in the queue.[/]"
                )
                logger.warning(f"Aborting queue after MISMATCH; {remaining} remaining")
                self._ctx.download_queue = []
            return False
        if report.outcome is VerifyOutcome.NO_EXPECTED_HASH:
            self._console.print(
                f"  [dim]No digest available; SHA256={report.actual_hash} ({dest})[/]"
            )
            logger.info(
                f"No digest available; computed SHA256={report.actual_hash} "
                f"file={dest} (source={report.source.value})"
            )
            return True
        # PARSE_ERROR or any future outcome — surface the note, keep the file.
        self._console.print(f"  [yellow]Integrity check skipped: {report.note}[/]")
        logger.warning(f"Integrity {report.outcome.value}: {report.note}")
        return True

    # ── Post-download menu ────────────────────────────────────────────────

    def _post_download(self, *, single_shot: bool) -> State:
        if single_shot:
            return State.QUIT
        if not self._ctx.completed:
            return State.SELECT_ASSET
        if self._ctx.current_release is None or self._ctx.current_repo is None:
            return State.ENTER_REPO

        # Materialize once; the prompt re-loops for HELP and the summary block
        # must remain visible across iterations. (A generator would be drained
        # on the first ``list(...)`` and the second iteration would show
        # "Just downloaded 0 assets:".)
        completed_pairs: list[tuple[str, int]] = [(a.name, a.size) for a, _ in self._ctx.completed]

        while True:
            result = self._prompts.post_download_menu(
                completed=completed_pairs,
                repo=self._ctx.current_repo,
                tag=self._ctx.current_release.tag,
                output_dir=str(self._config.output_dir),
            )
            if result.is_nav:
                if result.nav is NavAction.QUIT:
                    return State.QUIT
                if result.nav is NavAction.BACK:
                    return State.SELECT_ASSET
                if result.nav is NavAction.MENU:
                    return State.ENTER_REPO
                if result.nav is NavAction.HELP:
                    self._prompts.show_help()
                    continue
                # Any future NavAction added without an explicit branch lands
                # here. Surfacing it as an error beats silently re-looping.
                raise AssertionError(f"Unhandled NavAction in post_download: {result.nav!r}")
            choice = result.value or ""
            logger.info(
                f"Post-download choice: {choice} (after {len(self._ctx.completed)} download(s))"
            )
            if choice == "another_asset":
                return State.SELECT_ASSET
            if choice == "another_repo":
                return State.ENTER_REPO
            if choice == "quit":
                return State.QUIT
            # Prompts.post_download_menu only returns the three values above
            # or a NavAction; reaching here means the prompt contract drifted.
            raise AssertionError(f"Unhandled post-download choice: {choice!r}")
