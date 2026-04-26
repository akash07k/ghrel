"""Asset integrity verification.

Verification chain (in order of preference):

1. **GitHub API ``digest`` field** — most authoritative; the API returns
   ``"sha256:<hex>"`` for assets when available. See :func:`parse_api_digest`.

2. **Release-bundled checksum file** — a per-file ``foo.zip.sha256`` or a
   shared ``SHA256SUMS`` / ``checksums.txt`` etc. Supports the GNU format
   (``<hash>  filename``), BSD format (``ALGO (filename) = hash``), and
   ``sha256sum -b`` binary-mode format (``<hash> *filename``). See
   :func:`find_checksum_line` and :func:`parse_hash_from_line`.

3. **SHA256-for-records** — when no digest or checksum file exists, just
   compute and surface a SHA256 so the user can verify out-of-band.

The line-matching regex anchors with non-name boundary chars so that
``foo.zip`` does *not* match the ``foo.zip.sig`` line first when both are in
the same checksum file. Verified by the test suite.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

# ── Algorithm mapping ─────────────────────────────────────────────────────────


_HASH_LENGTH_TO_ALGORITHM: Final[dict[int, str]] = {
    32: "MD5",
    40: "SHA1",
    64: "SHA256",
    96: "SHA384",
    128: "SHA512",
}


def algorithm_for_hash_length(hex_length: int) -> str | None:
    """Map a hex-string length to a hash algorithm name.

    Returns ``None`` for unrecognized lengths so the caller can fall back to
    a manual-verification flow.
    """
    return _HASH_LENGTH_TO_ALGORITHM.get(hex_length)


# ── API digest parsing ────────────────────────────────────────────────────────


_API_DIGEST = re.compile(r"^(\w+):([a-fA-F0-9]+)$")


@dataclass(frozen=True)
class ApiDigest:
    """A successfully parsed ``digest`` value from the GitHub API."""

    algorithm: str
    """Algorithm name, uppercased (e.g. ``"SHA256"``)."""

    expected_hash: str
    """Hex hash, uppercased."""


def parse_api_digest(digest: str | None) -> ApiDigest | None:
    """Parse the ``digest`` field returned by the GitHub releases API.

    The field is formatted as ``"<algorithm>:<hex>"``, e.g.
    ``"sha256:f96935e7..."``. Returns ``None`` if the value is missing or
    does not match the expected shape (caller should then try the checksum
    file fallback).
    """
    if not digest:
        return None
    match = _API_DIGEST.match(digest)
    if not match:
        return None
    return ApiDigest(
        algorithm=match.group(1).upper(),
        expected_hash=match.group(2).upper(),
    )


# ── Checksum file line matching ───────────────────────────────────────────────
#
# The asset name must be preceded by start-of-line OR one of {whitespace,
# `(`, `*`, `/`} and followed by end-of-line OR one of {whitespace, `)`}.
# This covers:
#   GNU format            "<hash>  filename"
#   BSD format            "ALGO (filename) = <hash>"
#   sha256sum -b          "<hash> *binary-filename"
#   subdir-prefixed       "<hash>  ./subdir/filename"   (allowed via the / prefix)
# and prevents "foo.zip" from spuriously matching the "foo.zip.sig" line.


def find_checksum_line(asset_name: str, lines: list[str]) -> str | None:
    """Return the first line in ``lines`` that references ``asset_name``.

    Anchors the asset name with non-name boundary characters so that prefix
    collisions like ``foo.zip`` vs ``foo.zip.sig`` resolve correctly.

    Returns ``None`` if no line matches.
    """
    pattern = re.compile(rf"(?:^|[\s(*/]){re.escape(asset_name)}(?:$|[\s)])")
    for line in lines:
        if pattern.search(line):
            return line
    return None


# Hex hash patterns. Allow 32-128 hex chars to cover MD5/SHA1/SHA256/SHA384/SHA512.
_GNU_HASH = re.compile(r"^([a-fA-F0-9]{32,128})\s")
_BSD_HASH = re.compile(r"=\s*([a-fA-F0-9]{32,128})\s*$")


def parse_hash_from_line(line: str) -> str | None:
    """Extract a hex hash from a single line of a checksum file.

    Tries GNU style first (``<hash>  filename``), then BSD style
    (``ALGO (filename) = <hash>``). Returns ``None`` if neither matches.

    The returned hash is uppercased for stable comparison.
    """
    match = _GNU_HASH.match(line)
    if match:
        return match.group(1).upper()
    match = _BSD_HASH.search(line)
    if match:
        return match.group(1).upper()
    return None


# ── File hashing ──────────────────────────────────────────────────────────────


_HASH_CHUNK_SIZE: Final[int] = 1 << 16  # 64 KiB


def compute_file_hash(path: Path | str, algorithm: str) -> str:
    """Compute the hex digest of a file using the named algorithm.

    The result is uppercased for stable comparison against parsed expected
    hashes (which are also uppercased).

    Args:
        path: Path to the file.
        algorithm: Algorithm name accepted by :func:`hashlib.new` (case-
            insensitive).

    Returns:
        Uppercased hex digest.

    Raises:
        ValueError: If the algorithm is not supported by ``hashlib``.
        OSError: If the file cannot be read.
    """
    hasher = hashlib.new(algorithm.lower())
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest().upper()


# ── Verification result types ─────────────────────────────────────────────────


class VerifySource(Enum):
    """Where the expected hash came from."""

    API_DIGEST = "api_digest"
    CHECKSUM_FILE = "checksum_file"
    COMPUTED_FOR_RECORDS = "computed_for_records"


class VerifyOutcome(Enum):
    """Outcome of an integrity check."""

    OK = "ok"
    MISMATCH = "mismatch"
    """The expected and computed hashes differed."""

    NO_EXPECTED_HASH = "no_expected_hash"
    """No digest or checksum file was available; we just computed and reported
    a hash for manual verification."""

    PARSE_ERROR = "parse_error"
    """A digest or checksum line could not be parsed."""


@dataclass(frozen=True)
class VerifyReport:
    """Detailed outcome of :func:`verify_file`."""

    outcome: VerifyOutcome
    source: VerifySource
    algorithm: str | None = None
    expected_hash: str | None = None
    actual_hash: str | None = None
    note: str | None = None
    """Optional human-readable note (e.g. parse error details)."""


# ── Orchestrator ──────────────────────────────────────────────────────────────


# Type aliases for the orchestrator's pluggable dependencies. We keep them
# string-based to avoid a circular import on github_api / downloader.
from collections.abc import Callable, Sequence  # noqa: E402
from typing import Protocol  # noqa: E402


class AssetLike(Protocol):
    """Structural type for any asset object the verifier accepts.

    Used as the parameter type of :func:`verify_asset` so callers can pass
    in their own asset shape without coupling to PyGithub or our concrete
    :class:`AssetInfo` dataclass. Declared with ``@property`` so frozen
    dataclasses (which have read-only attributes) satisfy the protocol.
    """

    @property
    def name(self) -> str: ...
    @property
    def size(self) -> int: ...
    @property
    def download_url(self) -> str: ...
    @property
    def digest(self) -> str | None: ...


# A function that accepts an asset and returns its checksum-file *contents*
# (already fetched and decoded as text). ``None`` means the loader couldn't
# fetch it (network failure, 403, etc.) — :func:`verify_asset` falls through.
ChecksumLoader = Callable[[AssetLike], "str | None"]


_CHECKSUM_NAME_PATTERNS = (
    "{name}.sha256",
    "{name}.sha512",
    "{name}.md5",
)
_GENERAL_CHECKSUM_REGEX = re.compile(
    r"(?i)(sha256sums?|sha512sums?|checksums?|SUMS|\.sha256|\.sha512)"
)


def _find_checksum_asset(target: AssetLike, available: Sequence[AssetLike]) -> AssetLike | None:
    """Pick the most specific checksum-bearing asset for ``target``.

    Preference:
      1. ``<target.name>.sha256`` / ``.sha512`` / ``.md5`` (per-file)
      2. Any asset whose name looks like a generic checksum bundle
    """
    for tmpl in _CHECKSUM_NAME_PATTERNS:
        candidate_name = tmpl.format(name=target.name)
        for asset in available:
            if asset.name == candidate_name:
                return asset

    for asset in available:
        if asset.name == target.name:
            continue
        if _GENERAL_CHECKSUM_REGEX.search(asset.name):
            return asset
    return None


def verify_asset(
    asset: AssetLike,
    dest_path: Path,
    available_assets: Sequence[AssetLike],
    *,
    checksum_loader: ChecksumLoader | None = None,
) -> VerifyReport:
    """Run the full verification chain on a downloaded asset.

    Order of attempts:

    1. **API digest** — if ``asset.digest`` is a parseable
       ``"<algo>:<hex>"`` value, compute and compare. This is the most
       authoritative source.
    2. **Bundled checksum file** — look for ``<asset>.sha256`` etc., or a
       generic ``SHA256SUMS``/``checksums.txt`` file. If ``checksum_loader``
       is provided, fetch the file via it; locate the line for our asset;
       compare. The line-matching uses anchored prefix-collision-safe regex
       (the ``foo.zip`` vs ``foo.zip.sig`` case).
    3. **SHA256-for-records** — if neither digest nor checksum file is
       available, just compute SHA256 and report it. Outcome
       :attr:`VerifyOutcome.NO_EXPECTED_HASH` so the caller knows there was
       no comparison done.

    Args:
        asset: The downloaded asset (typically an :class:`AssetInfo`).
        dest_path: Path to the file on disk.
        available_assets: All assets in the release (so we can find checksum
            files among them).
        checksum_loader: Optional function that fetches a checksum file's
            text content. ``None`` = skip the checksum-file step (still try
            digest and SHA256-for-records).

    Returns:
        :class:`VerifyReport` describing the outcome.
    """
    # ── 1. API digest ─────────────────────────────────────────────────────
    digest = parse_api_digest(asset.digest)
    if digest is not None:
        actual = compute_file_hash(dest_path, digest.algorithm)
        outcome = VerifyOutcome.OK if actual == digest.expected_hash else VerifyOutcome.MISMATCH
        return VerifyReport(
            outcome=outcome,
            source=VerifySource.API_DIGEST,
            algorithm=digest.algorithm,
            expected_hash=digest.expected_hash,
            actual_hash=actual,
        )

    # ── 2. Checksum file ──────────────────────────────────────────────────
    checksum_asset = _find_checksum_asset(asset, available_assets) if checksum_loader else None
    if checksum_asset is not None and checksum_loader is not None:
        text = checksum_loader(checksum_asset)
        if text is not None:
            line = find_checksum_line(asset.name, text.splitlines())
            if line is None:
                # File exists but our asset isn't in it — fall through to step 3.
                pass
            else:
                expected = parse_hash_from_line(line)
                if expected is None:
                    return VerifyReport(
                        outcome=VerifyOutcome.PARSE_ERROR,
                        source=VerifySource.CHECKSUM_FILE,
                        note=f"could not parse a hash from line: {line!r}",
                    )
                algorithm = algorithm_for_hash_length(len(expected))
                if algorithm is None:
                    return VerifyReport(
                        outcome=VerifyOutcome.PARSE_ERROR,
                        source=VerifySource.CHECKSUM_FILE,
                        expected_hash=expected,
                        note=f"unrecognized hash length ({len(expected)} hex chars)",
                    )
                actual = compute_file_hash(dest_path, algorithm)
                outcome = VerifyOutcome.OK if actual == expected else VerifyOutcome.MISMATCH
                return VerifyReport(
                    outcome=outcome,
                    source=VerifySource.CHECKSUM_FILE,
                    algorithm=algorithm,
                    expected_hash=expected,
                    actual_hash=actual,
                )

    # ── 3. SHA256-for-records ─────────────────────────────────────────────
    actual = compute_file_hash(dest_path, "SHA256")
    return VerifyReport(
        outcome=VerifyOutcome.NO_EXPECTED_HASH,
        source=VerifySource.COMPUTED_FOR_RECORDS,
        algorithm="SHA256",
        actual_hash=actual,
        note="No digest or checksum file available; SHA256 reported for manual verification.",
    )
