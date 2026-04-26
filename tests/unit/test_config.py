"""Tests for ``ghrel.config``.

Covers:

- TOML parsing (well-formed, malformed, missing fields)
- Resolution priority: CLI > env > config file > default
- Type validation (wrong type → ConfigError)
- ``parallel`` bounds (1..16)
- Default config-path discovery on different platforms
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from ghrel.config import Config, ConfigError, default_config_paths, load_config

# ── load_config: basic flows ──────────────────────────────────────────────────


class TestLoadConfigDefaults:
    def test_no_config_file_no_cli_no_env_uses_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        result = load_config(config_path=None, base_dir=tmp_path)
        assert isinstance(result, Config)
        assert result.token is None
        assert result.token_source == "default"
        assert result.output_dir == (tmp_path / "downloads").resolve()
        assert result.include_pre_release is False
        assert result.parallel == 1

    def test_nonexistent_config_path_treated_as_no_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        result = load_config(
            config_path=tmp_path / "missing.toml",
            base_dir=tmp_path,
        )
        assert result.token is None
        assert result.parallel == 1


class TestLoadConfigFromFile:
    def test_full_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text(
            'token = "ghp_x"\noutput_dir = "custom"\ninclude_pre_release = true\nparallel = 4\n',
            encoding="utf-8",
        )
        result = load_config(config_path=cfg_path, base_dir=tmp_path)
        assert result.token == "ghp_x"
        assert result.token_source == "config"
        assert result.output_dir == (tmp_path / "custom").resolve()
        assert result.output_dir_raw == "custom"
        assert result.include_pre_release is True
        assert result.parallel == 4

    def test_empty_token_in_file_treated_as_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text('token = ""\n', encoding="utf-8")
        result = load_config(config_path=cfg_path, base_dir=tmp_path)
        assert result.token is None
        assert result.token_source == "default"

    def test_partial_file_uses_defaults_for_missing_fields(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text("parallel = 3\n", encoding="utf-8")
        result = load_config(config_path=cfg_path, base_dir=tmp_path)
        assert result.parallel == 3
        assert result.token is None
        assert result.include_pre_release is False
        assert result.output_dir == (tmp_path / "downloads").resolve()


# ── Resolution priority ───────────────────────────────────────────────────────


class TestResolutionPriority:
    def test_cli_token_overrides_env_and_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "from-env")
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text('token = "from-file"\n', encoding="utf-8")

        result = load_config(
            config_path=cfg_path,
            base_dir=tmp_path,
            cli_token="from-cli",
        )
        assert result.token == "from-cli"
        assert result.token_source == "cli"

    def test_env_token_overrides_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "from-env")
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text('token = "from-file"\n', encoding="utf-8")
        result = load_config(config_path=cfg_path, base_dir=tmp_path)
        assert result.token == "from-env"
        assert result.token_source == "env"

    def test_cli_output_dir_overrides_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text('output_dir = "from-file"\n', encoding="utf-8")
        result = load_config(
            config_path=cfg_path,
            base_dir=tmp_path,
            cli_output_dir="from-cli",
        )
        assert result.output_dir == (tmp_path / "from-cli").resolve()
        assert result.output_dir_raw == "from-cli"

    def test_cli_parallel_overrides_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text("parallel = 2\n", encoding="utf-8")
        result = load_config(
            config_path=cfg_path,
            base_dir=tmp_path,
            cli_parallel=8,
        )
        assert result.parallel == 8

    def test_cli_include_pre_release_overrides_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text("include_pre_release = false\n", encoding="utf-8")
        result = load_config(
            config_path=cfg_path,
            base_dir=tmp_path,
            cli_include_pre_release=True,
        )
        assert result.include_pre_release is True


# ── Validation errors ─────────────────────────────────────────────────────────


class TestValidationErrors:
    def test_malformed_toml(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        cfg_path = tmp_path / "bad.toml"
        cfg_path.write_text("this is = not valid = toml [", encoding="utf-8")
        with pytest.raises(ConfigError, match="not valid TOML"):
            load_config(config_path=cfg_path, base_dir=tmp_path)

    def test_wrong_type_for_token(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text("token = 42\n", encoding="utf-8")
        with pytest.raises(ConfigError, match="expected str"):
            load_config(config_path=cfg_path, base_dir=tmp_path)

    def test_wrong_type_for_parallel(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        cfg_path = tmp_path / "config.toml"
        cfg_path.write_text('parallel = "four"\n', encoding="utf-8")
        with pytest.raises(ConfigError, match="expected int"):
            load_config(config_path=cfg_path, base_dir=tmp_path)

    def test_parallel_below_one_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with pytest.raises(ConfigError, match=">= 1"):
            load_config(config_path=None, base_dir=tmp_path, cli_parallel=0)

    def test_parallel_above_max_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with pytest.raises(ConfigError, match="<= 16"):
            load_config(config_path=None, base_dir=tmp_path, cli_parallel=100)


# ── default_config_paths ──────────────────────────────────────────────────────


class TestDefaultConfigPaths:
    def test_first_path_is_base_dir_config(self, tmp_path: Path) -> None:
        paths = default_config_paths(tmp_path)
        assert paths[0] == tmp_path / "config.toml"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_windows_includes_appdata_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        paths = default_config_paths(tmp_path)
        assert any("appdata" in str(p).lower() and "ghrel" in str(p) for p in paths)

    @pytest.mark.skipif(sys.platform == "win32", reason="Unix-only test")
    def test_unix_includes_xdg_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        paths = default_config_paths(tmp_path)
        assert any("xdg" in str(p) and "ghrel" in str(p) for p in paths)
