# PyInstaller spec for ghrel.
# Build with:  uv run pyinstaller ghrel.spec
# Or via the helper script: uv run python scripts/build_exe.py

# This file is executed by PyInstaller; the conventional structure here is
# `Analysis -> PYZ -> EXE`. We use `--onefile` (single executable) and add
# explicit hidden-imports for PyGithub's submodules — its lazy-import
# pattern defeats PyInstaller's static analysis on some platforms, so we
# pull them in manually.

# ruff: noqa: F821
# pyright: reportUndefinedVariable=false

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hidden_imports: list[str] = []
# PyGithub registers its endpoint classes via ``__init_subclass__``; if any
# of them isn't imported by name from the entry-point chain, PyInstaller
# misses it. ``collect_submodules`` walks the package and adds them all.
hidden_imports += collect_submodules("github")
# Loguru handlers / lazy-loaded sinks.
hidden_imports += collect_submodules("loguru")
# Typer registers click command groups via metaclass dispatch.
hidden_imports += collect_submodules("typer")

a = Analysis(
    ["src/ghrel/__main__.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # We don't ship test code or dev-only deps in the binary.
        "pytest",
        "respx",
        "hypothesis",
        "mypy",
        "pyright",
        "ruff",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="ghrel",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX often trips antivirus heuristics; skip
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,              # ghrel is a CLI; keep the console attached
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
