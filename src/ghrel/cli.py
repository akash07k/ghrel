"""Typer CLI entrypoint.

Single command (no subcommands). All flags map to fields on :class:`Config`
or directly into :meth:`StateMachine.run`. The structure below is
**dependency-injection friendly**: e2e tests can call
:func:`build_state_machine` directly with stub deps and skip the Typer layer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer
from rich.console import Console

from ghrel import __version__
from ghrel.config import Config, ConfigError, load_config
from ghrel.downloader import AsyncDownloader, Downloader
from ghrel.github_api import GitHubClient
from ghrel.logging_setup import setup_logging
from ghrel.prompts import Prompts
from ghrel.state_machine import ExitCode, StateMachine

app = typer.Typer(
    name="ghrel",
    help="Interactive GitHub release downloader.",
    add_completion=False,
    no_args_is_help=False,
)


def _version_callback(show: bool) -> None:
    if show:
        typer.echo(f"ghrel {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    repo: str | None = typer.Option(
        None,
        "--repo",
        "-r",
        help="GitHub repo as 'owner/repo' or a full URL. If omitted, prompts interactively.",
    ),
    asset_pattern: str | None = typer.Option(
        None,
        "--asset-pattern",
        "-p",
        help=(
            "Asset name filter. Setting this enables single-shot mode: "
            "fetch, find first match, download, exit. Token search ('cuda win x64') "
            "and globs ('*linux-x64*') both work."
        ),
    ),
    output_dir: str | None = typer.Option(
        None,
        "--output-dir",
        "-o",
        help="Override config's output_dir. Same path forms accepted.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help="Overwrite existing files without prompting.",
    ),
    include_pre_release: bool = typer.Option(
        False,
        "--include-pre-release",
        help="Include pre-releases when picking the latest.",
    ),
    parallel: int | None = typer.Option(
        None,
        "--parallel",
        help="Max concurrent downloads in multi-asset mode. 1 = serial. Range 1-16.",
    ),
    token: str | None = typer.Option(
        None,
        "--token",
        help=(
            "GitHub Personal Access Token. Overrides GITHUB_TOKEN env var and "
            "config.toml. WARNING: visible in process listings (ps/Task Manager); "
            "prefer GITHUB_TOKEN env var or config.toml for shared machines."
        ),
    ),
    no_log: bool = typer.Option(
        False,
        "--no-log",
        help="Disable per-run diag log.",
    ),
    log_dir: str | None = typer.Option(
        None,
        "--log-dir",
        help="Override the diag-log directory (default: ./logs/ next to the script).",
    ),
    config_path: Path | None = typer.Option(
        None,
        "--config",
        help="Path to config.toml. Defaults to looking next to the script.",
    ),
    _version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """Entry point. Builds Config + dependencies, then runs the state machine."""
    base_dir = Path(__file__).resolve().parent.parent.parent

    actual_config_path = config_path if config_path else base_dir / "config.toml"

    try:
        config = load_config(
            config_path=actual_config_path if actual_config_path.exists() else None,
            base_dir=base_dir,
            cli_token=token,
            cli_output_dir=output_dir,
            cli_include_pre_release=include_pre_release if include_pre_release else None,
            cli_parallel=parallel,
        )
    except ConfigError as exc:
        typer.echo(f"Config error: {exc}", err=True)
        raise typer.Exit(code=ExitCode.FAILURE) from exc

    # Wire logging — setup_logging removes loguru's default sink and adds
    # ours, so subsequent imports of `loguru.logger` produce our format.
    log_dir_path = Path(log_dir) if log_dir else (base_dir / "logs")
    setup_logging(log_dir=log_dir_path, no_log=no_log)

    # Inject the CLI-level overrides we don't put in Config.
    machine = build_state_machine(config, force=force)
    exit_code = machine.run(cli_repo=repo, cli_asset_pattern=asset_pattern)
    raise typer.Exit(code=exit_code)


def build_state_machine(
    config: Config,
    *,
    force: bool = False,
    console: Console | None = None,
) -> StateMachine:
    """Construct the state machine and all its dependencies.

    Tests can call this directly with a tweaked :class:`Config` and inspect
    or mock the resulting machine.

    The ``force`` flag is glued onto the config via dataclass replace so the
    rest of the app sees a single, uniform :class:`Config` object instead of
    juggling parallel parameters.
    """
    from dataclasses import replace

    config = replace(config, force=force)

    actual_console = console or Console()
    prompts = Prompts(console=actual_console)
    github = GitHubClient(token=config.token)
    downloader = Downloader(token=config.token)
    # Construct the async downloader only when the user actually asked for
    # parallel downloads; saves a tiny bit of work on the default path.
    async_downloader: AsyncDownloader | None = (
        AsyncDownloader(token=config.token, parallel=config.parallel)
        if config.parallel > 1
        else None
    )

    return StateMachine(
        config=config,
        prompts=prompts,
        github_client=github,
        downloader=downloader,
        async_downloader=async_downloader,
        console=actual_console,
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app() or 0)
