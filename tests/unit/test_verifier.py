"""Tests for ``ghrel.verifier``.

Covers the integrity-verification helpers:

- :func:`parse_api_digest` — GitHub API ``"sha256:hex"`` parsing
- :func:`find_checksum_line` — locating the right line in a checksum file,
  including the ``foo.zip`` vs ``foo.zip.sig`` prefix-collision case
- :func:`parse_hash_from_line` — GNU and BSD format extraction
- :func:`algorithm_for_hash_length` — length → algorithm mapping
- :func:`compute_file_hash` — round-trip with hashlib
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ghrel.verifier import (
    algorithm_for_hash_length,
    compute_file_hash,
    find_checksum_line,
    parse_api_digest,
    parse_hash_from_line,
)

# ── parse_api_digest ──────────────────────────────────────────────────────────


class TestParseApiDigest:
    def test_sha256(self) -> None:
        result = parse_api_digest(
            "sha256:f96935e7e385e3b2d0189239077c10fe8fd7e95690fea4afec455b1b6c7e3f18"
        )
        assert result is not None
        assert result.algorithm == "SHA256"
        assert (
            result.expected_hash
            == "F96935E7E385E3B2D0189239077C10FE8FD7E95690FEA4AFEC455B1B6C7E3F18"
        )

    def test_sha512(self) -> None:
        hex512 = "a" * 128
        result = parse_api_digest(f"sha512:{hex512}")
        assert result is not None
        assert result.algorithm == "SHA512"
        assert result.expected_hash == hex512.upper()

    def test_uppercase_algo_normalized(self) -> None:
        result = parse_api_digest("SHA256:abcdef0123456789")
        assert result is not None
        assert result.algorithm == "SHA256"

    @pytest.mark.parametrize(
        "value",
        [None, "", "no-colon", "sha256:", ":abcdef", "sha256:notHexZZZZ", "sha256/badseparator"],
        ids=["none", "empty", "no-colon", "no-hash", "no-algo", "non-hex", "wrong-separator"],
    )
    def test_invalid_returns_none(self, value: str | None) -> None:
        assert parse_api_digest(value) is None


# ── find_checksum_line ────────────────────────────────────────────────────────


class TestFindChecksumLine:
    def test_gnu_format(self) -> None:
        lines = [
            f"{'a' * 64}  llama-bin-win-cuda-13.1-x64.zip",
            f"{'b' * 64}  other.zip",
        ]
        result = find_checksum_line("llama-bin-win-cuda-13.1-x64.zip", lines)
        assert result is not None
        assert "llama-bin-win-cuda-13.1-x64.zip" in result
        assert result.startswith("a" * 64)

    def test_bsd_format(self) -> None:
        lines = [
            f"SHA256 (llama-bin-win-cuda-13.1-x64.zip) = {'a' * 64}",
            f"SHA256 (other.zip) = {'b' * 64}",
        ]
        result = find_checksum_line("llama-bin-win-cuda-13.1-x64.zip", lines)
        assert result is not None
        assert "(llama-bin-win-cuda-13.1-x64.zip)" in result

    def test_sha256sum_binary_format(self) -> None:
        """``sha256sum -b`` produces ``<hash> *filename`` (asterisk-prefixed)."""
        lines = [f"{'a' * 64} *llama-bin-win-cuda-13.1-x64.zip"]
        result = find_checksum_line("llama-bin-win-cuda-13.1-x64.zip", lines)
        assert result is not None

    def test_prefix_collision_zip_vs_zip_sig(self) -> None:
        """The GNU regression: looking for ``foo.zip`` must NOT match the
        ``foo.zip.sig`` line, because the substring would otherwise hit it
        first when ``foo.zip.sig`` appears earlier in the file.
        """
        lines = [
            f"{'b' * 64}  llama-bin-win-cuda-13.1-x64.zip.sig",  # appears first
            f"{'a' * 64}  llama-bin-win-cuda-13.1-x64.zip",  # the one we want
        ]
        result = find_checksum_line("llama-bin-win-cuda-13.1-x64.zip", lines)
        assert result is not None
        assert result.startswith("a" * 64), "Should match the .zip line, not the .zip.sig line"

    def test_prefix_collision_bsd(self) -> None:
        """Same prefix-collision protection in BSD format."""
        lines = [
            f"SHA256 (llama-bin-win-cuda-13.1-x64.zip.sig) = {'b' * 64}",
            f"SHA256 (llama-bin-win-cuda-13.1-x64.zip) = {'a' * 64}",
        ]
        result = find_checksum_line("llama-bin-win-cuda-13.1-x64.zip", lines)
        assert result is not None
        assert result.endswith("a" * 64)

    def test_no_match_returns_none(self) -> None:
        lines = [f"{'a' * 64}  other.zip"]
        assert find_checksum_line("missing.zip", lines) is None

    def test_subdir_prefixed_filename(self) -> None:
        """``./subdir/filename`` style lines should still match via the / prefix."""
        lines = [f"{'a' * 64}  ./subdir/foo.zip"]
        result = find_checksum_line("foo.zip", lines)
        assert result is not None


# ── parse_hash_from_line ──────────────────────────────────────────────────────


class TestParseHashFromLine:
    def test_gnu(self) -> None:
        line = f"{'a' * 64}  filename.zip"
        assert parse_hash_from_line(line) == "A" * 64

    def test_bsd(self) -> None:
        line = f"SHA256 (filename.zip) = {'b' * 64}"
        assert parse_hash_from_line(line) == "B" * 64

    @pytest.mark.parametrize("hash_len", [32, 40, 64, 96, 128])
    def test_various_lengths(self, hash_len: int) -> None:
        line = f"{'c' * hash_len}  filename.zip"
        assert parse_hash_from_line(line) == "C" * hash_len

    def test_unrecognized_returns_none(self) -> None:
        assert parse_hash_from_line("no hash here") is None
        assert parse_hash_from_line("") is None


# ── algorithm_for_hash_length ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("length", "algorithm"),
    [
        (32, "MD5"),
        (40, "SHA1"),
        (64, "SHA256"),
        (96, "SHA384"),
        (128, "SHA512"),
        (10, None),
        (0, None),
        (200, None),
    ],
    ids=[
        "md5",
        "sha1",
        "sha256",
        "sha384",
        "sha512",
        "too-short",
        "zero",
        "too-long",
    ],
)
def test_algorithm_for_hash_length(length: int, algorithm: str | None) -> None:
    assert algorithm_for_hash_length(length) == algorithm


# ── compute_file_hash ─────────────────────────────────────────────────────────


class TestComputeFileHash:
    def test_round_trip_sha256(self, tmp_path: Path) -> None:
        target = tmp_path / "data.bin"
        payload = b"the quick brown fox jumps over the lazy dog"
        target.write_bytes(payload)

        expected = hashlib.sha256(payload).hexdigest().upper()
        assert compute_file_hash(target, "SHA256") == expected

    def test_round_trip_md5(self, tmp_path: Path) -> None:
        target = tmp_path / "data.bin"
        payload = b"hello world" * 1000
        target.write_bytes(payload)

        expected = hashlib.md5(payload, usedforsecurity=False).hexdigest().upper()
        assert compute_file_hash(target, "MD5") == expected

    def test_lowercase_algorithm_accepted(self, tmp_path: Path) -> None:
        target = tmp_path / "data.bin"
        target.write_bytes(b"x")
        upper = compute_file_hash(target, "SHA256")
        lower = compute_file_hash(target, "sha256")
        assert upper == lower

    def test_large_file_streams_correctly(self, tmp_path: Path) -> None:
        """Hash a >1 MiB file to ensure the chunked read works correctly.

        Note operator precedence: `<<` is *lower* than `+` in Python, so
        ``(1 << 20) + 100`` must be parenthesized — ``1 << 20 + 100`` would
        try to compute ``1 << 120`` (a number too big for an index).
        """
        target = tmp_path / "big.bin"
        payload = b"x" * ((1 << 20) + 100)  # 1 MiB + 100 bytes
        target.write_bytes(payload)

        expected = hashlib.sha256(payload).hexdigest().upper()
        assert compute_file_hash(target, "SHA256") == expected

    def test_unknown_algorithm_raises(self, tmp_path: Path) -> None:
        target = tmp_path / "data.bin"
        target.write_bytes(b"x")
        with pytest.raises(ValueError):
            compute_file_hash(target, "NOT_A_REAL_ALGO")
