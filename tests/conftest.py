"""Shared pytest fixtures."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest


@dataclass
class FakeAsset:
    """Minimal stand-in for a GitHub release asset for selector tests."""

    name: str
    size: int = 0


@pytest.fixture
def llama_assets() -> list[FakeAsset]:
    """Realistic asset list modeled on a ggml-org/llama.cpp release.

    Used to exercise token search and multi-select against the same shape of
    data the live API returns: a handful of platform-tagged binaries with
    overlapping name fragments (``cuda``, ``win``, ``x64``, ``llama``,
    ``cudart``) so token-AND matching has interesting cases to discriminate.
    """
    names = [
        "cudart-llama-bin-win-cuda-12.4-x64.zip",
        "cudart-llama-bin-win-cuda-13.1-x64.zip",
        "llama-b8929-bin-win-cuda-12.4-x64.zip",
        "llama-b8929-bin-win-cuda-13.1-x64.zip",
        "llama-b8929-bin-win-cpu-x64.zip",
        "llama-b8929-bin-ubuntu-x64.tar.gz",
        "llama-b8929-bin-macos-arm64.tar.gz",
        "llama-b8929-xcframework.zip",
    ]
    return [FakeAsset(name=n, size=1024 * 1024) for n in names]


@pytest.fixture
def tmp_output_dir(tmp_path: Path) -> Path:
    """Per-test output directory that won't collide with real downloads."""
    out = tmp_path / "downloads"
    out.mkdir(parents=True, exist_ok=True)
    return out
