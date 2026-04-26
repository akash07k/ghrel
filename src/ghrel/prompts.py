"""Interactive prompts.

Design priority: **screen-reader friendly text I/O**. No live regions, no
cursor magic, no widget redraws. Every prompt is line-based: print message,
read one line, parse it. NVDA/JAWS/Narrator handle this natively (it's the
same interaction model as ``input()``).

We *do* use :mod:`rich` for **styling** (color, bold) — but not for the
prompt machinery. ``rich.prompt.Prompt.ask`` writes a single styled line then
calls ``input()``; the Console auto-detects non-TTY contexts and downgrades
to plain text, which is exactly what we want for piped input or CI.

Navigation shortcuts (``q`` / ``b`` / ``m`` / ``?``) are recognized at every
prompt. Recognition is *exact-match-only* so single-letter shortcuts don't
shadow real asset-name searches (typing ``b`` is "back"; typing ``bin`` is
the search term "bin").
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto

from rich.console import Console
from rich.prompt import Prompt

# ── Helpers ───────────────────────────────────────────────────────────────────


def _format_size(num_bytes: int) -> str:
    """Render a byte count in KB or MB depending on size.

    Uses the same 512 KiB threshold as the download-progress display so the
    file-existence prompt and the progress lines stay visually consistent.
    """
    if num_bytes < (1 << 19):  # < 512 KiB
        return f"{num_bytes / (1 << 10):.1f} KB"
    return f"{num_bytes / (1 << 20):.1f} MB"


# ── Navigation shortcuts ──────────────────────────────────────────────────────


_NAV_QUIT = frozenset({"q", "quit", "exit"})
_NAV_BACK = frozenset({"b", "back"})
_NAV_MENU = frozenset({"m", "menu", "home"})
_NAV_HELP = frozenset({"?", "h", "help"})


class NavAction(Enum):
    """A navigation shortcut typed by the user instead of regular input."""

    QUIT = auto()
    BACK = auto()
    MENU = auto()
    HELP = auto()


def parse_nav(text: str) -> NavAction | None:
    """Map ``text`` to a :class:`NavAction` if it exactly matches a shortcut.

    Returns ``None`` for non-matching input (caller treats it as content).
    Whitespace-trimmed and case-folded; the comparison is *exact* so ``bin``
    is not "back".
    """
    folded = text.strip().lower()
    if folded in _NAV_QUIT:
        return NavAction.QUIT
    if folded in _NAV_BACK:
        return NavAction.BACK
    if folded in _NAV_MENU:
        return NavAction.MENU
    if folded in _NAV_HELP:
        return NavAction.HELP
    return None


# ── Prompt result types ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class PromptResult:
    """Generic prompt outcome. Either a nav action or a literal string value."""

    nav: NavAction | None = None
    value: str | None = None

    @property
    def is_nav(self) -> bool:
        return self.nav is not None


# ── Prompts ───────────────────────────────────────────────────────────────────


class Prompts:
    """Bundle of accessible prompts driven by a single :class:`Console`.

    Construct one at the top of the state machine and pass it down. Tests can
    pass a :class:`Console` configured with ``file=io.StringIO()`` to capture
    output, plus monkey-patch :func:`input` for stdin.
    """

    def __init__(self, console: Console | None = None) -> None:
        # When ``console=None``, rich uses the global Console which writes to
        # stdout and respects $NO_COLOR / non-TTY detection automatically.
        self.console = console or Console()

    # ── Repo prompt ────────────────────────────────────────────────────────

    def repo(self, *, last_repo: str | None = None) -> PromptResult:
        """Ask the user for ``owner/repo`` or a GitHub URL.

        Returns:
            :class:`PromptResult` with a nav action *or* the typed value.
            Empty input with no ``last_repo`` returns ``value=""`` so the
            state machine can prompt again or show an error.
        """
        self.console.print()
        self.console.rule("[bold cyan]Main Menu[/]", align="left")
        if last_repo:
            self.console.print(
                f"[dim]Last repo: {last_repo} (Enter to reuse, or type a different one)[/]"
            )
        self.console.print("[dim]Shortcuts: q=quit  ?=help[/]")

        text = Prompt.ask("[bold]GitHub repo or URL[/] (e.g. ggml-org/llama.cpp)", default="")
        nav = parse_nav(text)
        if nav is not None:
            return PromptResult(nav=nav)
        return PromptResult(value=text.strip())

    # ── Asset selector ────────────────────────────────────────────────────

    def asset_choice(
        self,
        *,
        page_label: str,
        page_size: int,
        has_more: bool,
        prompt_label: str = "Choice",
    ) -> PromptResult:
        """Read user input from the paged asset list.

        The state machine prints the asset list itself (so it can apply
        formatting / colors). This method just reads the response.

        Args:
            page_label: ``"1-9 of 28"`` style label included in the prompt
                hint for screen readers.
            page_size: Number of items currently visible (for the help line).
            has_more: True when "0 = next page" should be shown as a hint.
            prompt_label: The text shown before the input cursor.

        Returns:
            :class:`PromptResult` with nav action or the raw input string.
        """
        more_hint = "  Type 0 for the next page." if has_more else ""
        self.console.print(
            f"[dim]Pick one number, or several (e.g. '1 3 6' or '1,3,6'),"
            f" or type words to filter.{more_hint}[/]"
        )
        self.console.print("[dim]Shortcuts: b=back  m=menu  q=quit  ?=help[/]")

        text = Prompt.ask(f"[bold]{prompt_label}[/] ({page_label})", default="")
        nav = parse_nav(text)
        if nav is not None:
            return PromptResult(nav=nav)
        return PromptResult(value=text.strip())

    # ── Overwrite prompt ───────────────────────────────────────────────────

    def overwrite(self, *, asset_name: str, existing_size_bytes: int) -> PromptResult:
        """Ask whether to overwrite an existing file.

        ``existing_size_bytes`` is rendered in MB or KB depending on size, so
        small files don't all read as ``"0.0 MB"`` (matches the progress-line
        display).

        Returns:
            :class:`PromptResult` with:

            - ``value="yes"`` — overwrite this file
            - ``value="no"`` — skip *this* file (queue continues for multi)
            - ``nav=...`` — abort the entire queue and navigate
        """
        size_str = _format_size(existing_size_bytes)
        self.console.print()
        self.console.print(f"[yellow]File already exists: {asset_name} ({size_str})[/]")
        self.console.print("[dim]Shortcuts: b=back to selector  m=main menu  q=quit  ?=help[/]")
        text = Prompt.ask("[bold]Overwrite?[/] [y/N]", default="n")
        nav = parse_nav(text)
        if nav is not None:
            return PromptResult(nav=nav)
        normalized = "yes" if text.strip().lower().startswith("y") else "no"
        return PromptResult(value=normalized)

    # ── Post-download menu ────────────────────────────────────────────────

    def post_download_menu(
        self,
        *,
        completed: list[tuple[str, int]],
        repo: str,
        tag: str,
        output_dir: str,
    ) -> PromptResult:
        """Show the post-download summary and ask what to do next.

        Args:
            completed: list of ``(asset_name, size_bytes)`` tuples for the
                summary block.
            repo: ``owner/repo`` of the current release (for option label).
            tag: release tag (for option label).
            output_dir: human-readable destination path.

        Returns:
            :class:`PromptResult` with ``value`` in
            ``{"another_asset", "another_repo", "quit"}`` or a nav action.
        """
        self.console.print()
        self.console.rule("[bold cyan]What's next?[/]", align="left")
        if len(completed) == 1:
            name, _size = completed[0]
            self.console.print(f"  Just downloaded: [green]{name}[/]")
        else:
            self.console.print(f"  [green]Just downloaded {len(completed)} assets:[/]")
            for name, size in completed:
                mb = size / (1 << 20)
                self.console.print(f"    - {name:<68} {mb:7.1f} MB")
        self.console.print(f"  [dim]Saved under: {output_dir}[/]")
        self.console.print()
        self.console.print(f"  [bold][1][/] Download another asset from {repo} @ {tag}")
        self.console.print("  [bold][2][/] Pick a different repo (main menu)")
        self.console.print("  [bold][3][/] Quit")
        self.console.print("[dim]Shortcuts: b=back to selector  m=main menu  q=quit  ?=help[/]")

        while True:
            text = Prompt.ask("[bold]Choice[/]", default="").strip().lower()
            nav = parse_nav(text)
            if nav is NavAction.HELP:
                # Help is shown by the state machine, then re-loop.
                return PromptResult(nav=NavAction.HELP)
            if nav is not None:
                return PromptResult(nav=nav)
            if text in {"1", "a", "asset", "another"}:
                return PromptResult(value="another_asset")
            if text in {"2", "r", "repo"}:
                return PromptResult(value="another_repo")
            if text in {"3"}:
                return PromptResult(value="quit")
            self.console.print("[yellow]Unrecognized choice. Enter 1, 2, 3, or ? for help.[/]")

    # ── Help screen ────────────────────────────────────────────────────────

    def show_help(self) -> None:
        """Print the help screen. No prompt — just renders to the console."""
        self.console.print()
        self.console.rule("[bold cyan]Help[/]", align="left")
        self.console.print("[bold yellow]Navigation shortcuts (recognized at most prompts):[/]")
        self.console.print("    q / quit / exit  — Quit the program")
        self.console.print("    b / back         — Go back one step")
        self.console.print("    m / menu / home  — Go to main menu (repo prompt)")
        self.console.print("    ? / h / help     — Show this help")
        self.console.print()
        self.console.print("[bold yellow]Asset selection:[/]")
        self.console.print("    Type a NUMBER to pick one asset.")
        self.console.print(
            "    Type SEVERAL NUMBERS to pick multiple at once (downloaded in order):"
        )
        self.console.print("        1 3 6        picks assets 1, 3, 6")
        self.console.print("        1,3,6        same")
        self.console.print("        8, 2, 4      picks 8 first, then 2, then 4")
        self.console.print("    Type '0' for the next page (when paginated).")
        self.console.print("    Type WORDS to filter the list:")
        self.console.print("        cuda win x64 zip          tokens, any order, case-insensitive")
        self.console.print("        cuda, win, x64 zip        commas/spaces both work")
        self.console.print("        *linux-x64*               glob fallback (with * or ?)")
        self.console.print()

    # ── Press-enter-to-continue ───────────────────────────────────────────

    def pause(self, *, message: str = "Press Enter to continue") -> PromptResult:
        """Generic pause. Returns nav action or empty value."""
        text = Prompt.ask(f"[dim]{message}[/]", default="")
        nav = parse_nav(text)
        if nav is not None:
            return PromptResult(nav=nav)
        return PromptResult(value=text.strip())
