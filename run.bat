@echo off
setlocal EnableDelayedExpansion

REM ===========================================================================
REM run.bat -- robust launcher for ghrel.
REM
REM Verifies uv is installed, ensures the project's virtual environment is
REM up-to-date with pyproject.toml on the first run, then forwards every
REM argument to "uv run ghrel".
REM
REM Usage:
REM   run.bat                              -- interactive mode
REM   run.bat --repo owner/repo            -- interactive selector for repo
REM   run.bat --repo owner/x -p "*.zip"    -- single-shot, downloads first match
REM   run.bat --help                       -- show ghrel's full CLI help
REM ===========================================================================

REM Anchor working dir to the script location so relative paths in the project
REM (config.toml, logs/, downloads/) resolve predictably even when invoked
REM from elsewhere (double-click, scheduled task, etc.).
cd /d "%~dp0"

REM ---------------------------------------------------------------------------
REM 1. Verify uv is installed and on PATH.
REM ---------------------------------------------------------------------------
where uv >nul 2>&1
if errorlevel 1 (
    echo.
    echo   ERROR: 'uv' is not installed or not on PATH.
    echo.
    echo   Install uv from https://docs.astral.sh/uv/getting-started/installation/
    echo   On Windows the easiest option is:
    echo       powershell -ExecutionPolicy Bypass -c "irm https://astral.sh/uv/install.ps1 ^| iex"
    echo.
    pause
    exit /b 127
)

REM ---------------------------------------------------------------------------
REM 2. First-run setup: build the virtual environment and install dependencies.
REM    "uv sync" is idempotent and fast on subsequent runs (no-op when
REM    up-to-date), so we only show the "first run" banner if the venv does
REM    not exist yet.
REM ---------------------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo   First run detected -- setting up the Python environment with uv...
    echo   This installs Python 3.13 if missing, then resolves dependencies.
    echo.
    uv sync
    if errorlevel 1 (
        echo.
        echo   ERROR: 'uv sync' failed. See messages above.
        echo.
        pause
        exit /b 1
    )
    echo.
    echo   Setup complete. Launching ghrel...
    echo.
)

REM ---------------------------------------------------------------------------
REM 3. Run ghrel with every forwarded argument. "uv run" re-checks the lock
REM    file each invocation, so any pyproject.toml edit auto-syncs before run.
REM ---------------------------------------------------------------------------
uv run ghrel %*
set "GHREL_EXIT=!ERRORLEVEL!"

REM ---------------------------------------------------------------------------
REM 4. If a user double-clicked the bat (no args, no console attached
REM    otherwise), keep the window open so they can read errors.
REM ---------------------------------------------------------------------------
if "%~1"=="" (
    echo.
    pause
)

exit /b !GHREL_EXIT!
