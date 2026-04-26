"""Configuration loading.

Resolution priority for every setting (highest wins):

1. CLI flag (passed by the Typer entrypoint)
2. Environment variable (``GITHUB_TOKEN`` only; the others are CLI/file only)
3. ``config.toml`` next to the script (or in a platform-appropriate location)
4. Built-in default

The TOML file uses snake_case keys; the schema is validated with explicit
field-by-field reads (no auto-mapping libraries needed for a 4-field config).

We use TOML rather than JSON because it supports comments, has stricter
typing, and is the standard for Python tooling (``pyproject.toml`` itself).
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ghrel.path_utils import resolve_output_dir

# ── Config dataclass ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Config:
    """Effective configuration after merging file / env / defaults.

    All fields are post-resolution: ``output_dir`` is an absolute :class:`Path`,
    not a raw string.
    """

    token: str | None = None
    """GitHub PAT or None for unauthenticated."""

    token_source: str = "default"  # noqa: S105 (provenance label, not a credential)
    """Provenance of the token, for logging: ``"env"``, ``"config"``, or
    ``"default"`` (none)."""

    output_dir: Path = field(default_factory=Path.cwd)
    """Absolute, canonical destination directory for downloaded assets."""

    output_dir_raw: str = ""
    """Original unresolved value, useful for diagnostic logging
    (``raw='X' -> resolved='Y'``)."""

    include_pre_release: bool = False

    parallel: int = 1
    """Maximum simultaneous downloads when multi-selecting. ``1`` = serial."""

    force: bool = False
    """Skip the overwrite prompt for existing files. CLI-only; not loaded
    from config.toml because forcing should be an explicit per-run choice."""


# ── Loading ───────────────────────────────────────────────────────────────────


class ConfigError(ValueError):
    """Raised on malformed config (bad TOML, wrong types, invalid values)."""


def _read_toml(path: Path) -> dict[str, Any]:
    """Read a TOML file. Raise :class:`ConfigError` on parse failure."""
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except OSError as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


def _expect_type(value: Any, expected: type, key: str, source: str) -> Any:
    """Type-check a value loaded from TOML, with a helpful error."""
    if not isinstance(value, expected):
        raise ConfigError(
            f"{source}: expected {expected.__name__} for '{key}', "
            f"got {type(value).__name__}: {value!r}"
        )
    return value


def load_config(
    config_path: Path | None,
    base_dir: Path,
    cli_token: str | None = None,
    cli_output_dir: str | None = None,
    cli_include_pre_release: bool | None = None,
    cli_parallel: int | None = None,
) -> Config:
    """Build the effective :class:`Config` from all sources.

    Resolution order for each field:

    - **token**: ``cli_token`` > ``GITHUB_TOKEN`` env > config file > ``None``
    - **output_dir**: ``cli_output_dir`` > config file > built-in default
    - **include_pre_release**: ``cli_include_pre_release`` > config file > ``False``
    - **parallel**: ``cli_parallel`` > config file > ``1``

    Args:
        config_path: Path to ``config.toml``. ``None`` or non-existent path
            means "no file; use defaults".
        base_dir: Anchor for resolving relative ``output_dir`` values
            (typically the directory containing the script).
        cli_token, cli_output_dir, cli_include_pre_release, cli_parallel:
            Values from CLI flags. ``None`` means "not provided".

    Returns:
        Resolved :class:`Config`.

    Raises:
        ConfigError: On malformed TOML, wrong field types, or invalid values
            (e.g. ``parallel < 1``).
    """
    file_data: dict[str, Any] = {}
    if config_path is not None and config_path.exists():
        file_data = _read_toml(config_path)

    # ── Token ─────────────────────────────────────────────────────────────
    # The "cli" / "env" / "config" / "default" strings below are provenance
    # labels for logging — not credentials. Bandit's S105 false-positives on
    # them, so we tag with noqa.
    if cli_token:
        token: str | None = cli_token
        token_source = "cli"  # noqa: S105
    elif env_token := os.environ.get("GITHUB_TOKEN"):
        token = env_token
        token_source = "env"  # noqa: S105
    elif file_token := file_data.get("token"):
        _expect_type(file_token, str, "token", str(config_path))
        token = file_token if file_token else None
        token_source = "config" if token else "default"
    else:
        token = None
        token_source = "default"  # noqa: S105

    # ── Output dir ────────────────────────────────────────────────────────
    if cli_output_dir is not None:
        raw_output = cli_output_dir
    elif "output_dir" in file_data:
        raw_output = _expect_type(file_data["output_dir"], str, "output_dir", str(config_path))
    else:
        raw_output = ""
    resolved_output = resolve_output_dir(raw_output, base_dir)

    # ── Include pre-release ───────────────────────────────────────────────
    if cli_include_pre_release is not None:
        include_pre_release = cli_include_pre_release
    elif "include_pre_release" in file_data:
        include_pre_release = _expect_type(
            file_data["include_pre_release"],
            bool,
            "include_pre_release",
            str(config_path),
        )
    else:
        include_pre_release = False

    # ── Parallel ──────────────────────────────────────────────────────────
    if cli_parallel is not None:
        parallel = cli_parallel
    elif "parallel" in file_data:
        parallel = _expect_type(file_data["parallel"], int, "parallel", str(config_path))
    else:
        parallel = 1

    if parallel < 1:
        raise ConfigError(f"'parallel' must be >= 1, got {parallel}")
    if parallel > 16:
        # Soft cap — anything higher is almost certainly a mistake and would
        # exhaust connection limits / look like abuse to GitHub's CDN.
        raise ConfigError(f"'parallel' must be <= 16, got {parallel}")

    return Config(
        token=token,
        token_source=token_source,
        output_dir=resolved_output,
        output_dir_raw=raw_output,
        include_pre_release=include_pre_release,
        parallel=parallel,
    )


def default_config_paths(base_dir: Path) -> list[Path]:
    """Return candidate config locations in priority order (first existing wins).

    1. ``<base_dir>/config.toml`` — sibling to the script.
    2. ``$XDG_CONFIG_HOME/ghrel/config.toml`` (or ``~/.config/ghrel/config.toml`` on Unix)
    3. ``%APPDATA%/ghrel/config.toml`` on Windows
    """
    paths = [base_dir / "config.toml"]

    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            paths.append(Path(appdata) / "ghrel" / "config.toml")
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
        paths.append(Path(xdg) / "ghrel" / "config.toml")

    return paths
