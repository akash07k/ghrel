# ghrel — interactive GitHub release downloader

A cross-platform CLI for downloading release assets from any public (or
private, with a token) GitHub repository. Single-command install via `uv`,
single binary distribution via PyInstaller, no external runtime requirements.

## Features

- **Interactive asset browser** with pagination
- **Multi-asset selection** in one prompt: `1 3 6` or `1,3,6` or `8, 2, 4`
- **Token-based search** — type `cuda win x64` and matches any-order
  substrings in asset names; case-insensitive; separators are space,
  dash, dot, comma, slash
- **Glob fallback** — anything containing `*` or `?` falls through to
  `fnmatch`-style matching (`*linux-x64*`)
- **Navigation shortcuts** at every prompt: `q` (quit), `b` (back),
  `m` (main menu), `?` (help)
- **Loop after download** — pick another asset from the same release,
  switch to a different repo, or quit
- **Single-shot CLI mode** for scripting / CI:
  `ghrel --repo owner/x --asset-pattern '*linux-x64*' --force`
- **Integrity verification** — GitHub API `digest` field first, then
  release-bundled checksum file (GNU / BSD / `sha256sum -b` formats), then
  SHA256-for-records when neither is available
- **Async parallel downloads** for multi-asset queues (`--parallel N`,
  bounded by `asyncio.Semaphore`)
- **TLS-only** — `httpx` defaults give us TLS 1.2/1.3 with cert validation
- **Auth handling** — `GITHUB_TOKEN` env var or `config.toml` `token` field;
  the value is never written to the diag log
- **Per-run logging** to `logs/run_<YYYY-MM-DD>_<HH-MM-SS>_pid<pid>_diag.log`
  with human-readable timestamps and structured `LEVEL: message` entries
- **Distinct exit codes**: `0` success, `1` normal failure, `2` malformed
  argument
- **Accessibility** — every prompt is line-based plain text (no live
  regions, no widget redraws); works with NVDA / JAWS / Narrator / VoiceOver
- **Path safety** — asset destinations are resolved and verified to be
  inside the configured `output_dir`; rejects path-traversal, drive
  prefixes, alt data streams, and reserved DOS device names

## Quick start

### Windows — double-click `run.bat`

Easiest path. On first run, `run.bat` installs Python 3.13 + dependencies
into a local `.venv` via `uv`, then launches the tool. Subsequent runs
reuse the existing environment.

### Standalone `ghrel.exe` (Windows, no Python required)

Build a single-file executable:

```bash
uv sync --extra build
uv run python scripts/build_exe.py
```

Produces `dist/ghrel.exe` (~20 MB) with Python 3.13 and all dependencies
embedded. Copy it anywhere on `PATH` and run it directly:

```
ghrel.exe --repo "ggml-org/llama.cpp" --asset-pattern "*win-cuda-13.1-x64*"
```

### From any terminal (developer install)

```bash
# One-time: clone the repo and install dependencies
git clone https://github.com/akash07k/ghrel
cd ghrel
uv sync

# Interactive — drops you into the asset selector after asking for the repo
uv run ghrel

# Single-shot — for CI or scripting
uv run ghrel --repo "ggml-org/llama.cpp" --asset-pattern "*win-cuda-13.1-x64*"
```

### Install as a system command (uv tool)

```bash
uv tool install .
ghrel --version
```

## Configuration (optional)

Copy `config.example.toml` to `config.toml` next to the script and edit:

```toml
# GitHub Personal Access Token. Empty = unauthenticated (60 req/hour).
token = ""

# Where assets are saved. See "Output directory" below for accepted forms.
output_dir = ""

# Default for --include-pre-release (CLI flag overrides).
include_pre_release = false

# Max concurrent downloads when multi-selecting (1..16).
parallel = 1
```

`config.toml` is gitignored. Authentication priority: `--token` CLI flag >
`GITHUB_TOKEN` env var > `config.toml` > unauthenticated.

## CLI parameters

| Parameter | Description |
|---|---|
| `--repo` / `-r` | Repository as `owner/repo` or a full URL. If omitted, prompts interactively. |
| `--asset-pattern` / `-p` | Asset name filter. Setting this **enables single-shot mode** (no interactive loop). |
| `--output-dir` / `-o` | Override `config.toml`'s `output_dir`. |
| `--force` / `-f` | Skip the "overwrite?" prompt for existing files. |
| `--include-pre-release` | Include pre-releases when picking the latest. |
| `--parallel` | Max concurrent downloads (1..16). 1 = serial. Engages when downloading >1 asset and no destination conflicts (or `--force`); otherwise falls back to serial with per-asset prompts. |
| `--token` | GitHub PAT. Overrides `GITHUB_TOKEN` and `config.toml`. **Note:** visible in process listings; prefer the env var or config file on shared machines. |
| `--no-log` | Disable the per-run diag log. |
| `--log-dir` | Override the diag-log directory. |
| `--config` | Path to `config.toml`. |
| `--version` | Show version and exit. |

## Asset selection

Three input modes inside the selector:

| You type | Result |
|---|---|
| `3` | Pick asset 3 from the visible page |
| `1 3 6` or `1,3,6` | Multi-select, downloaded in your typed order |
| `0` | Next page (when paginated) |
| `cuda win x64` | Filter by tokens — any order, case-insensitive |
| `*linux-x64*` | Glob fallback (with `*` or `?`) |
| `q` / `b` / `m` / `?` | Navigation shortcuts (exact match only) |

Single-letter shortcuts are **exact-match-only** so typing `b` is "back" but
`bin` is the search term "bin".

## Output directory

The `output_dir` field accepts every common path form:

| Form | Example | Resolves to |
|---|---|---|
| Empty | `""` | `<script>/downloads` |
| Relative | `downloads`, `./foo`, `../shared` | Resolved against the **script directory** (predictable across launchers) |
| Windows absolute | `D:\Releases` or `D:/Releases` | as-is, slashes normalized |
| UNC | `\\fileserver\releases` | as-is |
| Tilde | `~`, `~/Downloads` | Expanded via `$USERPROFILE` |
| Env var | `%USERPROFILE%\Downloads` | Expanded via Windows convention |
| WSL bridge | `/mnt/d/releases` | `D:\releases` (auto-converted) |

## Logging

Each run produces a single diag log under `logs/`:

```
logs/run_2026-04-26_02-02-10_pid36608_diag.log
```

Format — each entry is two content lines plus a blank separator line, so a
long log scans like a paragraphed document:

```
INFO: Mode: single-shot (CLI) | 
26th April, 2026 at 02:02:10.188 AM

INFO: AssetPattern '*checksums*' matched 1 asset(s); picking fzf_0.71.0_checksums.txt | 
26th April, 2026 at 02:02:11.750 AM
```

Levels: `DEBUG`, `INFO`, `WARN`, `ERROR`. The `Authorization` header is
always redacted. Passing `--no-log` disables the file sink entirely.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Normal failure — network error, no asset matched, no releases, integrity mismatch |
| `2` | Malformed `--asset-pattern` (corrupt-arg guard tripped) |

## Development

### Windows: `dev.bat`

A single launcher that bundles every routine task:

```
dev.bat install        Sync deps (dev + build extras)
dev.bat test           Run pytest (any args forwarded: dev.bat test -k token)
dev.bat cov            Run pytest with coverage (HTML + term)
dev.bat lint           ruff check
dev.bat format         ruff format (modifies)   |  format-check (no modify)
dev.bat types          mypy + pyright
dev.bat check          All CI gates: lint + format-check + types + tests
dev.bat build          Build standalone ghrel.exe
dev.bat clean          Remove build/, dist/, caches
dev.bat run --version  Run ghrel directly (args forwarded)
dev.bat version        ghrel / Python / uv versions
dev.bat help           Show this list
```

### Any platform: raw uv commands

```bash
# install with all dev tools
uv sync --extra dev

# run tests
uv run pytest

# coverage
uv run pytest --cov=src/ghrel --cov-report=term-missing

# lint, format, type-check
uv run ruff check src tests
uv run ruff format src tests
uv run mypy src/ghrel
uv run pyright src/ghrel
```

The test suite has 280+ cases across unit / integration / end-to-end layers.
End-to-end tests script complete user sessions (input, prompts, multi-select
queue, navigation) using `monkeypatch.setattr("builtins.input", ...)` —
no terminal emulation needed.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the full version history.

## License

AGPL-3.0-or-later — see [LICENSE](LICENSE) for full text.
