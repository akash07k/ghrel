"""Build a single-file ``ghrel.exe`` via PyInstaller.

Run from the project root:

    uv sync --extra build
    uv run python scripts/build_exe.py

Produces ``dist/ghrel.exe`` (Windows) or ``dist/ghrel`` (Linux/macOS).

The build is driven by ``ghrel.spec`` (sibling file), which carries the
hidden-imports list and PyInstaller knobs.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parent.parent
    spec_file = project_root / "ghrel.spec"
    if not spec_file.exists():
        print(f"ERROR: spec file not found: {spec_file}", file=sys.stderr)
        return 1

    # Clean previous build artefacts so we don't ship stale modules.
    for stale in (project_root / "build", project_root / "dist"):
        if stale.exists():
            print(f"  Cleaning {stale}")
            shutil.rmtree(stale, ignore_errors=True)

    print(f"  Running PyInstaller against {spec_file.name}...")
    result = subprocess.run(
        ["pyinstaller", "--clean", "--noconfirm", str(spec_file)],
        cwd=project_root,
        check=False,
    )
    if result.returncode != 0:
        print("ERROR: PyInstaller exited non-zero.", file=sys.stderr)
        return result.returncode

    # Surface the artefact path so the user knows where the binary landed.
    suffix = ".exe" if sys.platform == "win32" else ""
    artifact = project_root / "dist" / f"ghrel{suffix}"
    if artifact.exists():
        size_mb = artifact.stat().st_size / (1 << 20)
        print(f"\n  Built: {artifact} ({size_mb:.1f} MB)")
    else:
        print("\n  WARNING: Expected artifact not found at dist/ghrel(.exe).")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
