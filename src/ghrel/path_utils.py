"""Path safety helpers.

Two responsibilities:

1. :func:`get_safe_asset_path` — turn a user-controlled GitHub asset name into
   a destination :class:`Path` that is *guaranteed* to live inside the
   configured output directory. Defends against names like ``..\\..\\evil.exe``
   that a malicious release publisher could craft to write outside the output
   tree.

2. :func:`resolve_output_dir` — accept an output-directory string in any of
   the common forms (relative, Windows absolute, UNC, ``~``-prefixed,
   ``%VAR%``-style env vars, WSL ``/mnt/<letter>/`` bridge) and produce a
   canonical absolute :class:`Path`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# ── Windows-forbidden filename characters ─────────────────────────────────────
# Backslash, forward slash, colon, asterisk, question mark, double-quote,
# less-than, greater-than, pipe.
_FORBIDDEN_FILENAME_CHARS = frozenset('\\/:*?"<>|')

# Reserved DOS device names. The match is case-insensitive and applies whether
# the name has an extension (``CON.txt``) or not (``CON``).
_RESERVED_DOS_NAMES = re.compile(
    r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\.|$)",
    re.IGNORECASE,
)

# WSL bridge: /mnt/<drive-letter>/<rest> → <drive>:/<rest>
_WSL_BRIDGE = re.compile(r"^/mnt/([a-zA-Z])(/.*)?$")


class UnsafeAssetNameError(ValueError):
    """Raised by :func:`get_safe_asset_path` when an asset name fails validation.

    Subclass of :class:`ValueError` so callers can catch with either type.
    """


def get_safe_asset_path(asset_name: str, output_dir: Path | str) -> Path:
    """Build a safe destination path for ``asset_name`` inside ``output_dir``.

    Layered defense:

    1. **Reject characters that have special meaning** in Windows filesystems
       (``\\ / : * ? " < > |``).
    2. **Reject parent-directory and current-directory references** (``..``,
       ``.``).
    3. **Reject reserved DOS device names** (``CON``, ``PRN``, ``AUX``,
       ``NUL``, ``COM1``-``COM9``, ``LPT1``-``LPT9``), with or without
       extension.
    4. **Resolve the full destination path** and verify it lives inside
       the resolved ``output_dir``. Catches anything the character-class
       checks miss.

    The output directory is created if it does not exist.

    Args:
        asset_name: The asset name as published by GitHub (untrusted input).
        output_dir: Directory the file should be written to.

    Returns:
        Absolute :class:`Path` for the asset.

    Raises:
        UnsafeAssetNameError: If the asset name fails any validation step.
    """
    trimmed = asset_name.strip()
    if trimmed in {"", ".", ".."}:
        raise UnsafeAssetNameError(f"Invalid asset name: {asset_name!r}")

    bad_chars = [c for c in asset_name if c in _FORBIDDEN_FILENAME_CHARS]
    if bad_chars:
        raise UnsafeAssetNameError(
            f"Asset name {asset_name!r} contains a forbidden character: {bad_chars[0]!r}"
        )

    if ".." in asset_name:
        raise UnsafeAssetNameError(f"Asset name {asset_name!r} contains '..'")

    if _RESERVED_DOS_NAMES.match(trimmed):
        raise UnsafeAssetNameError(f"Asset name {asset_name!r} is a reserved Windows device name")

    # Defense in depth: even if the character class missed something, ensure
    # the resolved destination is inside the resolved output root.
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    candidate = (output_root / asset_name).resolve()
    try:
        candidate.relative_to(output_root)
    except ValueError as exc:
        raise UnsafeAssetNameError(
            f"Resolved path {str(candidate)!r} escapes output directory {str(output_root)!r}"
        ) from exc

    return candidate


def resolve_output_dir(raw: str | None, base_dir: Path | str) -> Path:
    """Resolve a configured ``output_dir`` string to a canonical absolute path.

    Accepts every common path form. Resolution order matters — WSL bridge runs
    *before* slash normalization, env-var expansion runs *before* tilde so
    ``%USERPROFILE%`` works even if the value contains ``~``.

    ============================  ===========================================
    Input                         Resolves to
    ============================  ===========================================
    ``None`` / empty / whitespace ``base_dir / "downloads"``
    ``"downloads"``               ``base_dir / "downloads"``
    ``"./foo"``                   ``base_dir / "foo"``
    ``"../sibling"``              parent of ``base_dir`` ``/ sibling``
    ``"sub/dir/here"``            ``base_dir / sub / dir / here``
    ``"D:\\Releases"``            ``D:\\Releases`` (as-is)
    ``"D:/Releases"``             ``D:\\Releases`` (slashes normalized)
    ``"\\\\server\\share"``       UNC, as-is
    ``"~"``                       user home directory
    ``"~/Downloads"``             user home ``/`` Downloads
    ``"%USERPROFILE%\\foo"``      env-var-expanded path
    ``"/mnt/d/releases"``         ``D:\\releases`` (WSL bridge)
    ``"/usr/local/foo"``          on Windows: current-drive root ``/usr/local/foo``
    ============================  ===========================================

    Relative paths anchor to ``base_dir`` (typically the script directory),
    *not* the current working directory — that makes config behavior
    predictable regardless of how the script is launched.

    Args:
        raw: Raw value from config / CLI flag / default.
        base_dir: Anchor for resolving relative paths.

    Returns:
        Canonical absolute :class:`Path`. Does *not* create the directory.
    """
    base = Path(base_dir).resolve()

    if raw is None or not raw.strip():
        return base / "downloads"

    p = raw.strip()

    # 1. Expand %VAR% / $VAR env vars (Windows + Unix conventions both work).
    p = os.path.expandvars(p)

    # 2. WSL bridge: do BEFORE slash normalization (the regex matches forward
    #    slashes; converting to backslashes first would break it).
    wsl_match = _WSL_BRIDGE.match(p)
    if wsl_match:
        drive = wsl_match.group(1).upper()
        rest = wsl_match.group(2) or ""
        p = f"{drive}:{rest}"

    # 3. Tilde expansion. We use os.path.expanduser here (rather than
    # Path(p).expanduser() per ruff's PTH111 suggestion) because the surrounding
    # logic operates on strings — switching to Path mid-stream and back is
    # awkward and gains nothing in clarity. The function is identical.
    p = os.path.expanduser(p)  # noqa: PTH111

    # 4. Build a Path. Path() auto-normalizes separators per platform.
    path_obj = Path(p)

    # 5. Anchor relative paths to base_dir.
    if not path_obj.is_absolute():
        path_obj = base / path_obj

    # 6. Canonicalize. resolve(strict=False) collapses ``..`` segments and
    #    normalizes separators without requiring the directory to exist.
    return path_obj.resolve()
