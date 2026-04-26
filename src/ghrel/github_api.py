"""GitHub API client.

Thin wrapper over `PyGithub`_ that:

1. Translates ``GitRelease`` / ``GitReleaseAsset`` objects into our own frozen
   :class:`ReleaseInfo` / :class:`AssetInfo` dataclasses. We don't expose
   PyGithub objects to the rest of the application — they are mutable, lazy
   (some fields trigger an extra HTTP fetch on access), and tied to a live
   client. Frozen dataclasses give the state machine and verifier predictable,
   serializable inputs.
2. Provides :func:`resolve_github_repo` — accepts ``owner/repo``, full
   URLs (``https://github.com/owner/repo``), URLs with trailing path
   segments (``/releases``), ``.git``-suffixed forms, and SSH-style
   ``git@github.com:owner/repo`` URLs.
3. Centralizes auth: a single optional token is used for both REST API calls
   *and* the asset CDN fetch. Token never leaks into logs.

.. _PyGithub: https://github.com/PyGithub/PyGithub
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

from github import Auth, Github, GithubException

if TYPE_CHECKING:
    from github.GitRelease import GitRelease
    from github.GitReleaseAsset import GitReleaseAsset


# ── Repository identifier resolution ──────────────────────────────────────────


# Matches github.com URLs (HTTPS) and git@github.com:owner/repo.git (SSH).
# After "github.com", accept either "/" (HTTPS) or ":" (SSH) as the separator.
_REPO_FROM_URL = re.compile(r"github\.com[/:]([^/\s:]+/[^/\s]+?)(?:\.git)?(?:/.*)?$")


class InvalidRepoError(ValueError):
    """Raised when a string can't be parsed as a GitHub repo identifier."""


def resolve_github_repo(raw: str) -> str:
    """Normalize a repo input to ``owner/repo`` form.

    Accepts:

    - ``"owner/repo"`` — passes through unchanged
    - ``"owner/repo.git"`` — ``.git`` suffix stripped
    - ``"https://github.com/owner/repo"`` — extracted
    - ``"https://github.com/owner/repo/releases"`` — trailing path stripped
    - ``"https://github.com/owner/repo.git"`` — both stripped
    - leading/trailing whitespace — trimmed

    The function is idempotent: ``resolve_github_repo(resolve_github_repo(x))``
    always equals ``resolve_github_repo(x)``. Callers can re-invoke it
    defensively without worrying about double-stripping.

    Args:
        raw: Raw user input from a CLI flag, prompt, or config.

    Returns:
        ``"owner/repo"``.

    Raises:
        InvalidRepoError: If the input is empty or doesn't match the expected
            shape (no slash separator after stripping).
    """
    text = raw.strip()
    if not text:
        raise InvalidRepoError("Repository identifier is empty.")

    match = _REPO_FROM_URL.search(text)
    candidate = match.group(1) if match else text
    candidate = re.sub(r"\.git$", "", candidate.strip())

    if "/" not in candidate or candidate.count("/") != 1:
        raise InvalidRepoError(
            f"{raw!r} does not look like a GitHub repo (expected 'owner/repo' or a github.com URL)."
        )
    owner, name = candidate.split("/", 1)
    if not owner or not name:
        raise InvalidRepoError(f"{raw!r} has an empty owner or name.")

    return candidate


# ── Result dataclasses ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AssetInfo:
    """Frozen snapshot of a release asset.

    Decoupled from PyGithub's ``GitReleaseAsset`` so the rest of the app
    sees immutable, easily-mocked, easily-serialized data.
    """

    name: str
    size: int
    download_url: str
    """The ``browser_download_url`` from the API. The asset CDN will redirect
    here from the API URL after auth handshake."""

    digest: str | None = None
    """``"sha256:hex"`` if the API exposes it, else ``None``."""

    content_type: str | None = None


@dataclass(frozen=True)
class ReleaseInfo:
    """Frozen snapshot of a release."""

    tag: str
    name: str
    is_prerelease: bool
    published_at: datetime | None
    """``None`` when the release has no published timestamp (rare, but possible
    for scheduled-but-not-yet-public releases surfaced by the prerelease
    iteration path). Display formatters accept ``None`` and render
    ``"unknown"`` rather than crashing."""
    assets: tuple[AssetInfo, ...] = field(default_factory=tuple)
    body: str | None = None
    html_url: str | None = None


# ── Exceptions ────────────────────────────────────────────────────────────────


class GitHubApiError(RuntimeError):
    """Network or API-level failure (404, 401, 403, 5xx, timeout, etc.).

    The underlying ``GithubException`` is preserved as ``__cause__`` for
    callers that want to inspect headers or raw response data.
    """

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def _human_message_for(exc: GithubException) -> str:
    """Translate a ``GithubException`` into a user-friendly one-liner.

    The standard 4xx statuses get tailored guidance (404 → check the repo
    name; 403 → set ``GITHUB_TOKEN``; 401 → token invalid). Everything else
    falls back to ``"HTTP <status> — <message>"`` so the user has at least
    a status code to search for.
    """
    status = exc.status
    if status == 404:
        return "Repository not found. Check the owner/repo name."
    if status == 403:
        return (
            "Rate limit exceeded or access denied. Set the GITHUB_TOKEN "
            "environment variable to authenticate (raises limit to 5000 req/hour)."
        )
    if status == 401:
        return "Authentication failed. Check your GITHUB_TOKEN value."
    msg = getattr(exc, "data", {}).get("message") if hasattr(exc, "data") else None
    base = msg or str(exc)
    return f"HTTP {status} — {base}" if status else base


# ── Client ────────────────────────────────────────────────────────────────────


class GitHubClient:
    """Thin wrapper around :class:`github.Github`.

    Holds a single :class:`Github` instance per client (connection pooling) and
    exposes the two operations we actually need: get the latest release, and
    the latest *non-draft* release including pre-releases.
    """

    def __init__(self, token: str | None = None, *, timeout: float = 60.0) -> None:
        """Construct a client.

        Args:
            token: GitHub PAT for authenticated requests. ``None`` falls back
                to the unauthenticated 60 req/hour rate limit.
            timeout: Connection / response-headers timeout in seconds.
        """
        auth = Auth.Token(token) if token else None
        self._client = Github(auth=auth, timeout=int(timeout))
        self._token = token  # exposed via property; never logged directly

    @property
    def is_authenticated(self) -> bool:
        return self._token is not None

    @property
    def token(self) -> str | None:
        """The bearer token, for use in the asset downloader. Never log directly."""
        return self._token

    def fetch_release(
        self,
        repo: str,
        *,
        include_prerelease: bool = False,
    ) -> ReleaseInfo:
        """Fetch the latest release of a repository.

        Args:
            repo: ``owner/repo`` (already normalized via
                :func:`resolve_github_repo`).
            include_prerelease: When ``True``, return the most recent
                non-draft release (including pre-releases); when ``False``,
                only the latest *stable* release (PyGithub's
                ``get_latest_release`` excludes drafts and prereleases).

        Returns:
            :class:`ReleaseInfo` snapshot.

        Raises:
            GitHubApiError: On any GitHub-side failure — repo not found,
                rate-limited, no published releases, network error, etc.
        """
        try:
            repository = self._client.get_repo(repo)
        except GithubException as exc:
            raise GitHubApiError(_human_message_for(exc), status=exc.status) from exc

        try:
            if include_prerelease:
                # Iterate the paginated list and pick the first non-draft.
                # PyGithub's PaginatedList lazily fetches a page at a time;
                # this rarely needs more than the first page.
                release: GitRelease | None = next(
                    (r for r in repository.get_releases() if not r.draft),
                    None,
                )
                if release is None:
                    raise GitHubApiError(
                        "No published releases found (all are drafts).",
                        status=0,
                    )
            else:
                release = repository.get_latest_release()
        except GithubException as exc:
            raise GitHubApiError(_human_message_for(exc), status=exc.status) from exc

        return _release_to_info(release)


def _release_to_info(release: GitRelease) -> ReleaseInfo:
    """Project a PyGithub :class:`GitRelease` into a frozen :class:`ReleaseInfo`."""
    return ReleaseInfo(
        tag=release.tag_name,
        name=release.title or release.tag_name,
        is_prerelease=release.prerelease,
        published_at=release.published_at,
        body=release.body,
        html_url=release.html_url,
        assets=tuple(_asset_to_info(a) for a in release.get_assets()),
    )


def _asset_to_info(asset: GitReleaseAsset) -> AssetInfo:
    """Project a PyGithub :class:`GitReleaseAsset` into a frozen :class:`AssetInfo`."""
    digest: str | None = getattr(asset, "digest", None) or None
    return AssetInfo(
        name=asset.name,
        size=asset.size,
        download_url=asset.browser_download_url,
        digest=digest,
        content_type=asset.content_type,
    )
