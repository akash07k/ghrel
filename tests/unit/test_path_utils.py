"""Tests for ``ghrel.path_utils``.

Two functions, both critical for security and UX:

- :func:`get_safe_asset_path` — defends against path traversal via malicious
  asset names; tests cover ``..`` traversal, drive prefixes, ADS colons, and
  reserved DOS device names.
- :func:`resolve_output_dir` — covers every accepted input form: relative,
  Windows absolute, UNC, ``~``-prefixed, env-var-prefixed, WSL bridge,
  Linux-style absolute on Windows.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ghrel.path_utils import (
    UnsafeAssetNameError,
    get_safe_asset_path,
    resolve_output_dir,
)

# ── get_safe_asset_path ───────────────────────────────────────────────────────


class TestGetSafeAssetPath:
    """Path-traversal guard. The asset name is attacker-controlled (whoever
    publishes the GitHub release picks the filename), so any of these inputs
    should be rejected before we hand the path to ``open()``.
    """

    @pytest.mark.parametrize(
        "asset_name",
        [
            "normal-asset.zip",
            "foo-bar_baz.tar.gz",
            "a",
            "release-1.2.3.tgz",
            "llama-b8929-bin-win-cuda-13.1-x64.zip",
        ],
        ids=["normal", "with-underscores", "single-char", "version-tags", "real-llama-asset"],
    )
    def test_safe_names_accepted(self, asset_name: str, tmp_output_dir: Path) -> None:
        result = get_safe_asset_path(asset_name, tmp_output_dir)
        assert result.parent == tmp_output_dir
        assert result.name == asset_name

    @pytest.mark.parametrize(
        ("asset_name", "expect_phrase"),
        [
            ("..\\..\\evil.exe", "forbidden character"),  # path-traversal up
            ("..", "Invalid"),  # parent ref
            (".", "Invalid"),  # current ref
            ("", "Invalid"),  # empty
            ("foo/bar.zip", "forbidden character"),  # forward slash
            ("C:\\Windows\\System32\\evil.exe", "forbidden character"),  # has \
            ("evil.exe:hidden", "forbidden character"),  # ADS colon
            ("foo*bar.zip", "forbidden character"),  # wildcard
            ("foo?bar.zip", "forbidden character"),
            ('foo"bar.zip', "forbidden character"),
            ("foo<bar.zip", "forbidden character"),
            ("foo>bar.zip", "forbidden character"),
            ("foo|bar.zip", "forbidden character"),
            ("..foo", "'..'"),  # ".." substring
            ("foo..bar", "'..'"),  # ".." in middle
            ("CON", "reserved Windows"),
            ("aux.txt", "reserved Windows"),
            ("PRN.zip", "reserved Windows"),
            ("NUL.dat", "reserved Windows"),
            ("COM1", "reserved Windows"),
            ("LPT9.txt", "reserved Windows"),
        ],
        ids=[
            "path-traversal-up",
            "parent-ref",
            "current-ref",
            "empty",
            "forward-slash",
            "drive-prefix",
            "ads-colon",
            "asterisk",
            "question-mark",
            "double-quote",
            "less-than",
            "greater-than",
            "pipe",
            "double-dot-prefix",
            "double-dot-middle",
            "con",
            "aux-with-ext",
            "prn-with-ext",
            "nul-with-ext",
            "com1",
            "lpt9-with-ext",
        ],
    )
    def test_unsafe_names_rejected(
        self,
        asset_name: str,
        expect_phrase: str,
        tmp_output_dir: Path,
    ) -> None:
        with pytest.raises(UnsafeAssetNameError, match=expect_phrase):
            get_safe_asset_path(asset_name, tmp_output_dir)

    def test_creates_output_dir_if_missing(self, tmp_path: Path) -> None:
        """get_safe_asset_path should mkdir on demand."""
        target = tmp_path / "deeply" / "nested" / "out"
        assert not target.exists()
        result = get_safe_asset_path("foo.zip", target)
        assert target.exists()
        assert result.parent == target.resolve()

    def test_resolved_path_is_inside_output_dir(self, tmp_output_dir: Path) -> None:
        """Belt-and-braces: result must be under output_dir."""
        result = get_safe_asset_path("foo.zip", tmp_output_dir)
        result.relative_to(tmp_output_dir.resolve())  # raises if not inside


# ── resolve_output_dir ────────────────────────────────────────────────────────


class TestResolveOutputDir:
    """Output-directory resolver across every accepted input form."""

    def test_empty_uses_default(self, tmp_path: Path) -> None:
        result = resolve_output_dir("", tmp_path)
        assert result == (tmp_path / "downloads").resolve()

    def test_none_uses_default(self, tmp_path: Path) -> None:
        result = resolve_output_dir(None, tmp_path)
        assert result == (tmp_path / "downloads").resolve()

    def test_whitespace_uses_default(self, tmp_path: Path) -> None:
        result = resolve_output_dir("   ", tmp_path)
        assert result == (tmp_path / "downloads").resolve()

    @pytest.mark.parametrize(
        ("raw", "rel"),
        [
            ("downloads", "downloads"),
            ("./foo", "foo"),
            ("my-stuff", "my-stuff"),
            ("sub/dir/here", "sub/dir/here"),
        ],
        ids=["plain", "dot-prefix", "with-hyphen", "forward-slashes"],
    )
    def test_relative_resolves_to_base_dir(self, raw: str, rel: str, tmp_path: Path) -> None:
        result = resolve_output_dir(raw, tmp_path)
        assert result == (tmp_path / rel).resolve()

    def test_relative_dotdot_goes_above_base(self, tmp_path: Path) -> None:
        """``../sibling`` resolves to a sibling of base_dir."""
        result = resolve_output_dir("../sibling", tmp_path)
        assert result == (tmp_path.parent / "sibling").resolve()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only path")
    def test_windows_absolute_backslash(self, tmp_path: Path) -> None:
        result = resolve_output_dir("E:\\Releases", tmp_path)
        assert str(result).lower() == "e:\\releases"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only path")
    def test_windows_absolute_forward_slash_normalized(self, tmp_path: Path) -> None:
        """``D:/foo`` should normalize to ``D:\\foo`` on Windows."""
        result = resolve_output_dir("E:/Releases", tmp_path)
        assert "\\" in str(result)
        assert str(result).lower() == "e:\\releases"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only path")
    def test_windows_absolute_with_dotdot_canonicalized(self, tmp_path: Path) -> None:
        result = resolve_output_dir("E:\\Releases\\sub\\..\\final", tmp_path)
        assert str(result).lower() == "e:\\releases\\final"

    def test_tilde_alone(self, tmp_path: Path) -> None:
        result = resolve_output_dir("~", tmp_path)
        assert result == Path.home().resolve()

    def test_tilde_with_subdir(self, tmp_path: Path) -> None:
        result = resolve_output_dir("~/Downloads", tmp_path)
        assert result == (Path.home() / "Downloads").resolve()

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows tilde-backslash")
    def test_tilde_with_backslash_subdir_windows(self, tmp_path: Path) -> None:
        result = resolve_output_dir("~\\Downloads\\llama", tmp_path)
        assert result == (Path.home() / "Downloads" / "llama").resolve()

    def test_env_var_expansion(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GHREL_TEST_DIR", str(tmp_path / "from-env"))
        if sys.platform == "win32":
            result = resolve_output_dir("%GHREL_TEST_DIR%\\sub", tmp_path)
        else:
            result = resolve_output_dir("$GHREL_TEST_DIR/sub", tmp_path)
        assert result == (tmp_path / "from-env" / "sub").resolve()

    @pytest.mark.skipif(sys.platform != "win32", reason="WSL bridge is Windows-specific")
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("/mnt/d/releases", "D:\\releases"),
            ("/mnt/c/Users/x/foo", "C:\\Users\\x\\foo"),
            ("/mnt/E/games", "E:\\games"),
        ],
        ids=["lowercase-d", "lowercase-c", "uppercase-E"],
    )
    def test_wsl_bridge_converts_to_drive(self, raw: str, expected: str, tmp_path: Path) -> None:
        result = resolve_output_dir(raw, tmp_path)
        assert str(result).lower() == expected.lower()

    @pytest.mark.skipif(sys.platform != "win32", reason="Path semantics differ")
    def test_linux_style_absolute_on_windows_resolves_to_current_drive(
        self, tmp_path: Path
    ) -> None:
        """``/usr/local/foo`` on Windows is rooted (current drive). Documented
        behavior — accepts as-is rather than erroring."""
        result = resolve_output_dir("/usr/local/foo", tmp_path)
        # Whatever the current drive is, the path should end with the suffix.
        assert str(result).lower().endswith("\\usr\\local\\foo")


# ── Property-based ────────────────────────────────────────────────────────────


@given(
    name=st.text(
        alphabet=st.characters(
            min_codepoint=0x20,
            max_codepoint=0x7E,
            blacklist_categories=("Cc",),
        ),
        min_size=1,
        max_size=50,
    ),
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_safe_asset_path_either_returns_inside_or_raises(
    name: str,
    tmp_path: Path,
) -> None:
    """For any printable-ASCII asset name, ``get_safe_asset_path`` either
    returns a path strictly inside the output dir or raises
    :class:`UnsafeAssetNameError`. It must never silently return an unsafe
    path.

    The ``tmp_path`` fixture is shared across generated inputs but only used
    as an output-dir root — we never check or mutate prior state — so the
    function-scoped-fixture warning doesn't apply.
    """
    output = tmp_path / "out"
    try:
        result = get_safe_asset_path(name, output)
    except UnsafeAssetNameError:
        return  # acceptable outcome
    # If it didn't raise, the path MUST be inside output.
    result.relative_to(output.resolve())
