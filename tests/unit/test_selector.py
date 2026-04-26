"""Tests for ``ghrel.selector``.

Covers two pure functions that are critical to UX correctness:

- :func:`parse_picked_numbers` — multi-number input parsing
- :func:`find_matching_assets` — token-based / glob asset filtering

The dataset (see ``llama_assets`` fixture) is sized and shaped to give the
token matcher discriminating cases — overlapping prefixes, repeated
fragments, and a mix of archive types.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from ghrel.selector import (
    PickStatus,
    find_matching_assets,
    parse_picked_numbers,
)
from tests.conftest import FakeAsset

# ── parse_picked_numbers ──────────────────────────────────────────────────────


class TestParsePickedNumbers:
    """Number-list parser: separators, dedupe, range validation."""

    @pytest.mark.parametrize(
        ("text", "max_value", "expected"),
        [
            ("1", 9, (1,)),
            ("1 3 6", 9, (1, 3, 6)),
            ("1,3,6", 9, (1, 3, 6)),
            ("1, 3, 6", 9, (1, 3, 6)),
            ("8, 2, 4", 9, (8, 2, 4)),  # order preserved
            ("  3  1  2  ", 9, (3, 1, 2)),  # extra whitespace
            ("1,,3", 9, (1, 3)),  # double-comma collapsed
            ("1 1 3", 9, (1, 3)),  # dedupe; first kept
            ("3 1 3", 9, (3, 1)),  # later dup dropped
        ],
        ids=[
            "single",
            "space-separated",
            "comma-separated",
            "comma-space-mixed",
            "non-sorted-order-preserved",
            "leading-trailing-whitespace",
            "double-comma",
            "dedupe-first-kept",
            "dedupe-later-dropped",
        ],
    )
    def test_ok_cases(self, text: str, max_value: int, expected: tuple[int, ...]) -> None:
        result = parse_picked_numbers(text, max_value)
        assert result.status is PickStatus.OK
        assert result.numbers == expected
        assert result.bad_number is None

    @pytest.mark.parametrize(
        ("text", "max_value", "expected_bad"),
        [
            ("10", 9, 10),
            ("0", 9, 0),
            ("1 2 99", 9, 99),
            ("100", 5, 100),
        ],
        ids=["above-max", "below-min", "mixed-with-out-of-range", "way-above-max"],
    )
    def test_out_of_range(self, text: str, max_value: int, expected_bad: int) -> None:
        result = parse_picked_numbers(text, max_value)
        assert result.status is PickStatus.OUT_OF_RANGE
        assert result.bad_number == expected_bad
        assert result.numbers == ()

    @pytest.mark.parametrize(
        "text",
        ["abc", "1 abc 3", "cuda win x64", "*.zip", "", "   ", "1.5", "+1"],
        ids=[
            "letters-only",
            "mixed-numbers-and-letters",
            "token-search-style",
            "glob",
            "empty",
            "whitespace-only",
            "decimal",  # isdigit() rejects '.'
            "leading-plus",
        ],
    )
    def test_not_numbers(self, text: str) -> None:
        """Non-numeric input must not be confused with a number list — caller
        falls through to text-search interpretation."""
        result = parse_picked_numbers(text, 9)
        assert result.status is PickStatus.NOT_NUMBERS
        assert result.numbers == ()
        assert result.bad_number is None

    @given(
        nums=st.lists(st.integers(min_value=1, max_value=99), min_size=1, max_size=10),
    )
    def test_property_dedupe_preserves_first_occurrence(self, nums: list[int]) -> None:
        """For any list of numbers in range, the parser dedupes while keeping
        the first occurrence — mathematically: ``out == seen-in-order(input)``.
        """
        text = ", ".join(str(n) for n in nums)
        result = parse_picked_numbers(text, max_value=99)
        assert result.status is PickStatus.OK

        seen: list[int] = []
        for n in nums:
            if n not in seen:
                seen.append(n)
        assert result.numbers == tuple(seen)

    @given(text=st.text(alphabet="0123456789, ", min_size=1, max_size=30))
    def test_property_no_crash_on_random_numeric_text(self, text: str) -> None:
        """Hypothesis: any string of digits/commas/spaces must produce a valid
        result without raising."""
        result = parse_picked_numbers(text, max_value=99)
        assert result.status in {
            PickStatus.OK,
            PickStatus.NOT_NUMBERS,
            PickStatus.OUT_OF_RANGE,
        }


# ── find_matching_assets ──────────────────────────────────────────────────────


class TestFindMatchingAssets:
    """Token search (default) and glob fallback (when query has * or ?)."""

    @pytest.mark.parametrize(
        ("query", "expected_count"),
        [
            ("cuda win x64 zip", 4),  # 4 cuda+win+x64+zip files
            ("llama win x64 zip", 5),  # all 5 win zips contain "llama" substring
            ("cuda, win, x64 zip", 4),  # commas equivalent to spaces
            ("llama win cuda x64.zip", 4),  # dots are separators too
            ("CUDA WIN X64 ZIP", 4),  # case-insensitive
            ("*linux-x64*", 0),  # glob fallback, no match
            ("*ubuntu*", 1),  # glob fallback, single match
            ("macos arm64", 1),  # token AND-match, single result
            ("nonexistent token", 0),  # no match
            ("win", 5),  # all 5 win-prefixed zips
            ("llama bin macos arm64", 1),  # narrow with 4 tokens
            ("", 0),  # empty query → empty result
            ("   ", 0),  # whitespace-only → empty
        ],
        ids=[
            "cuda-win-x64-zip",
            "llama-win-x64-zip-greedy",
            "comma-separator",
            "dot-separator",
            "case-insensitive",
            "glob-no-match",
            "glob-single-match",
            "tokens-single-match",
            "no-match",
            "single-token-broad",
            "narrow-via-many-tokens",
            "empty",
            "whitespace-only",
        ],
    )
    def test_count(
        self,
        llama_assets: list[FakeAsset],
        query: str,
        expected_count: int,
    ) -> None:
        result = find_matching_assets(query, llama_assets)
        assert len(result) == expected_count, (
            f"Query {query!r} expected {expected_count} matches, got {len(result)}: "
            f"{[a.name for a in result]}"
        )

    def test_glob_returns_actual_assets(self, llama_assets: list[FakeAsset]) -> None:
        """Glob match should return the right asset objects, not just count."""
        result = find_matching_assets("*ubuntu*", llama_assets)
        assert len(result) == 1
        assert result[0].name == "llama-b8929-bin-ubuntu-x64.tar.gz"

    def test_token_match_preserves_input_order(self, llama_assets: list[FakeAsset]) -> None:
        """Output order matches input order — important for the page selector."""
        result = find_matching_assets("cuda zip", llama_assets)
        names = [a.name for a in result]
        # Should appear in the same order as in llama_assets
        original_indices = [llama_assets.index(a) for a in result]
        assert original_indices == sorted(original_indices)
        # Sanity: at least cudart entries are first since they come first in the input
        assert names[0].startswith("cudart-")

    @given(query=st.text(min_size=0, max_size=30))
    @settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_property_no_crash_on_random_query(
        self, query: str, llama_assets: list[FakeAsset]
    ) -> None:
        """Hypothesis: arbitrary query strings should never raise.

        The fixture is read-only — we only filter from it, never mutate —
        so it's safe to share across hypothesis-generated examples.
        """
        result = find_matching_assets(query, llama_assets)
        assert isinstance(result, list)
        # All results must be from the input
        for asset in result:
            assert asset in llama_assets
