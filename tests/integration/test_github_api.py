"""Integration tests for ``ghrel.github_api``.

PyGithub uses ``requests`` internally (not httpx), so we mock at the
:class:`Github` class level rather than the HTTP layer. That keeps tests
fast (no real network) and avoids pulling in ``responses`` just for these
checks — every interesting branch is exercised through PyGithub's public
surface area.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from github import GithubException

from ghrel.github_api import (
    AssetInfo,
    GitHubApiError,
    GitHubClient,
    InvalidRepoError,
    ReleaseInfo,
    resolve_github_repo,
)

# ── resolve_github_repo ───────────────────────────────────────────────────────


class TestResolveGithubRepo:
    """Repo-identifier normalization across HTTPS / SSH / shorthand inputs."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("owner/repo", "owner/repo"),
            ("  owner/repo  ", "owner/repo"),
            ("owner/repo.git", "owner/repo"),
            ("https://github.com/ggml-org/llama.cpp", "ggml-org/llama.cpp"),
            ("https://github.com/ggml-org/llama.cpp/releases", "ggml-org/llama.cpp"),
            ("https://github.com/ggml-org/llama.cpp/releases/tag/b8929", "ggml-org/llama.cpp"),
            ("https://github.com/owner/repo.git", "owner/repo"),
            ("http://github.com/owner/repo", "owner/repo"),
            ("git@github.com:owner/repo.git", "owner/repo"),  # SSH-style URL
        ],
        ids=[
            "plain",
            "leading-trailing-whitespace",
            "with-git-suffix",
            "full-url",
            "url-with-trailing-releases",
            "url-with-tag-path",
            "url-with-git-suffix",
            "http-url",
            "ssh-url",
        ],
    )
    def test_accepts(self, raw: str, expected: str) -> None:
        assert resolve_github_repo(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "no-slash",
            "owner/",
            "/repo",
            "https://github.com/owner",  # missing repo segment
            "owner/repo/extra",  # too many slashes after stripping
        ],
        ids=[
            "empty",
            "whitespace",
            "no-slash",
            "trailing-slash-no-repo",
            "leading-slash-no-owner",
            "url-missing-repo",
            "extra-slashes",
        ],
    )
    def test_rejects(self, raw: str) -> None:
        with pytest.raises(InvalidRepoError):
            resolve_github_repo(raw)

    def test_idempotent(self) -> None:
        """Calling resolve twice produces the same result as calling once."""
        first = resolve_github_repo("https://github.com/ggml-org/llama.cpp/releases")
        second = resolve_github_repo(first)
        assert first == second == "ggml-org/llama.cpp"


# ── GitHubClient ──────────────────────────────────────────────────────────────


def _make_mock_asset(
    name: str,
    *,
    size: int = 1024,
    digest: str | None = "sha256:abcd",
    content_type: str = "application/zip",
) -> MagicMock:
    """Build a fake PyGithub ``GitReleaseAsset``."""
    asset = MagicMock()
    asset.name = name
    asset.size = size
    asset.browser_download_url = f"https://github.com/owner/repo/releases/download/v1.0/{name}"
    asset.digest = digest
    asset.content_type = content_type
    return asset


def _make_mock_release(
    *,
    tag: str = "v1.0",
    name: str | None = "v1.0",
    prerelease: bool = False,
    draft: bool = False,
    assets: list[MagicMock] | None = None,
    body: str = "Release notes",
    html_url: str = "https://github.com/owner/repo/releases/tag/v1.0",
    published_at: datetime | None = None,
) -> MagicMock:
    """Build a fake PyGithub ``GitRelease``."""
    release = MagicMock()
    release.tag_name = tag
    release.title = name
    release.prerelease = prerelease
    release.draft = draft
    release.body = body
    release.html_url = html_url
    release.published_at = published_at or datetime(2026, 4, 25, 14, 54, 29, tzinfo=UTC)
    release.get_assets.return_value = assets or []
    return release


class TestFetchRelease:
    def test_fetch_latest_stable(self, mocker: Any) -> None:
        mock_release = _make_mock_release(
            tag="b8931",
            assets=[
                _make_mock_asset("llama-bin-win-cuda-13.1-x64.zip", size=1024 * 1024),
                _make_mock_asset("llama-bin-macos-arm64.tar.gz", size=512 * 1024),
            ],
        )
        mock_repo = MagicMock()
        mock_repo.get_latest_release.return_value = mock_release

        gh_class = mocker.patch("ghrel.github_api.Github")
        gh_class.return_value.get_repo.return_value = mock_repo

        client = GitHubClient(token=None)
        result = client.fetch_release("owner/repo", include_prerelease=False)

        assert isinstance(result, ReleaseInfo)
        assert result.tag == "b8931"
        assert result.is_prerelease is False
        assert len(result.assets) == 2
        assert all(isinstance(a, AssetInfo) for a in result.assets)
        assert result.assets[0].name == "llama-bin-win-cuda-13.1-x64.zip"
        assert result.assets[0].size == 1024 * 1024
        assert result.assets[0].digest == "sha256:abcd"

    def test_fetch_includes_prerelease_picks_first_non_draft(self, mocker: Any) -> None:
        draft = _make_mock_release(tag="v2.0-draft", draft=True)
        prerelease = _make_mock_release(
            tag="v2.0-rc1", prerelease=True, draft=False, name="v2.0-rc1"
        )
        stable = _make_mock_release(tag="v1.0")
        mock_repo = MagicMock()
        mock_repo.get_releases.return_value = [draft, prerelease, stable]

        gh_class = mocker.patch("ghrel.github_api.Github")
        gh_class.return_value.get_repo.return_value = mock_repo

        client = GitHubClient(token="ghp_x")
        result = client.fetch_release("owner/repo", include_prerelease=True)

        assert result.tag == "v2.0-rc1"
        assert result.is_prerelease is True

    def test_fetch_includes_prerelease_no_published_releases(self, mocker: Any) -> None:
        only_drafts = [_make_mock_release(draft=True), _make_mock_release(draft=True)]
        mock_repo = MagicMock()
        mock_repo.get_releases.return_value = only_drafts

        gh_class = mocker.patch("ghrel.github_api.Github")
        gh_class.return_value.get_repo.return_value = mock_repo

        client = GitHubClient()
        with pytest.raises(GitHubApiError, match="No published releases"):
            client.fetch_release("owner/repo", include_prerelease=True)

    @pytest.mark.parametrize(
        ("status", "expect_phrase"),
        [
            (404, "Repository not found"),
            (403, "Rate limit exceeded"),
            (401, "Authentication failed"),
            (500, "HTTP 500"),
        ],
    )
    def test_get_repo_errors_translated(self, mocker: Any, status: int, expect_phrase: str) -> None:
        gh_class = mocker.patch("ghrel.github_api.Github")
        gh_class.return_value.get_repo.side_effect = GithubException(
            status=status, data={"message": "boom"}, headers={}
        )

        client = GitHubClient()
        with pytest.raises(GitHubApiError, match=expect_phrase) as excinfo:
            client.fetch_release("nope/nope", include_prerelease=False)
        assert excinfo.value.status == status

    def test_get_release_errors_translated(self, mocker: Any) -> None:
        mock_repo = MagicMock()
        mock_repo.get_latest_release.side_effect = GithubException(
            status=404, data={"message": "no releases"}, headers={}
        )
        gh_class = mocker.patch("ghrel.github_api.Github")
        gh_class.return_value.get_repo.return_value = mock_repo

        client = GitHubClient()
        with pytest.raises(GitHubApiError):
            client.fetch_release("owner/repo", include_prerelease=False)


class TestAuthState:
    def test_unauthenticated(self, mocker: Any) -> None:
        mocker.patch("ghrel.github_api.Github")
        client = GitHubClient(token=None)
        assert client.is_authenticated is False
        assert client.token is None

    def test_authenticated(self, mocker: Any) -> None:
        mocker.patch("ghrel.github_api.Github")
        client = GitHubClient(token="ghp_x")
        assert client.is_authenticated is True
        assert client.token == "ghp_x"
