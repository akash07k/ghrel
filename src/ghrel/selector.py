"""Asset selection logic.

Pure functions, no I/O. Two responsibilities:

1. **Multi-number parsing** — turn user input like ``"1 3 6"``, ``"1,3,6"``, or
   ``"8, 2, 4"`` into a validated, deduped, order-preserving list of indices.
   See :func:`parse_picked_numbers`.

2. **Token-based asset filtering** — given a query string and a list of assets,
   return the assets whose name contains *all* tokens (case-insensitive,
   order-independent). Wildcards (``*`` / ``?``) trigger a glob fallback. See
   :func:`find_matching_assets`.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

# ── Multi-number parsing ──────────────────────────────────────────────────────


class PickStatus(Enum):
    """Outcome of attempting to parse a number-list user input."""

    OK = "ok"
    NOT_NUMBERS = "not_numbers"
    """Input did not look like a number list. Caller should try other interpretations
    (e.g. token search, navigation shortcut)."""

    OUT_OF_RANGE = "out_of_range"
    """Input was numeric but at least one number falls outside ``[1..max_value]``."""


@dataclass(frozen=True)
class PickedNumbers:
    """Result of :func:`parse_picked_numbers`."""

    status: PickStatus
    numbers: tuple[int, ...] = ()
    """Validated, deduplicated, order-preserved picks. Populated only when
    ``status == OK``."""

    bad_number: int | None = None
    """The first number that failed range validation. Populated only when
    ``status == OUT_OF_RANGE``."""


# Splits on any combination of whitespace and commas (one or more).
_NUMBER_SEPARATOR = re.compile(r"[\s,]+")


def parse_picked_numbers(text: str, max_value: int) -> PickedNumbers:
    """Parse a multi-number selection string.

    Examples:
        ``"1"``           → ``PickedNumbers(OK, numbers=(1,))``
        ``"1 3 6"``       → ``PickedNumbers(OK, numbers=(1, 3, 6))``
        ``"1,3,6"``       → same
        ``"8, 2, 4"``     → ``PickedNumbers(OK, numbers=(8, 2, 4))`` (order preserved)
        ``"1 1 3"``       → ``PickedNumbers(OK, numbers=(1, 3))`` (duplicates after first dropped)
        ``"3 1 3"``       → ``PickedNumbers(OK, numbers=(3, 1))`` (the second ``3`` is dropped)
        ``"10"`` (max=9)  → ``PickedNumbers(OUT_OF_RANGE, bad_number=10)``
        ``"abc"``         → ``PickedNumbers(NOT_NUMBERS)`` (caller falls through)
        ``"1 abc 3"``     → ``PickedNumbers(NOT_NUMBERS)``  (any non-numeric token disqualifies)
        ``""``            → ``PickedNumbers(NOT_NUMBERS)``

    Args:
        text: User input from a prompt.
        max_value: Inclusive upper bound. Numbers must be in ``[1..max_value]``.

    Returns:
        A :class:`PickedNumbers` describing the parse outcome.
    """
    tokens = [t for t in _NUMBER_SEPARATOR.split(text) if t]
    if not tokens:
        return PickedNumbers(status=PickStatus.NOT_NUMBERS)
    if any(not t.isdigit() for t in tokens):
        return PickedNumbers(status=PickStatus.NOT_NUMBERS)

    seen: set[int] = set()
    ordered: list[int] = []
    for tok in tokens:
        n = int(tok)
        if not (1 <= n <= max_value):
            return PickedNumbers(status=PickStatus.OUT_OF_RANGE, bad_number=n)
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return PickedNumbers(status=PickStatus.OK, numbers=tuple(ordered))


# ── Token-based asset filtering ───────────────────────────────────────────────


class HasName(Protocol):
    """Structural type for anything with a ``name`` string attribute.

    GitHub release assets, dataclasses, plain dicts wrapped with a tiny
    name-accessor — anything works as long as it exposes ``.name``.

    The field is declared via ``@property`` so frozen dataclasses (which have
    read-only attributes) satisfy the protocol. A bare ``name: str`` annotation
    would imply mutability, which a frozen dataclass can't provide.
    """

    @property
    def name(self) -> str: ...


# Splits on space, dash, dot, comma, slash (any combination, one or more).
_TOKEN_SEPARATOR = re.compile(r"[-.,/\s]+")


def find_matching_assets[T: HasName](query: str, assets: Sequence[T]) -> list[T]:
    """Filter ``assets`` by ``query``.

    Two matching modes, automatically chosen based on the query:

    1. **Glob** — if the query contains ``*`` or ``?``, match via
       :func:`fnmatch.fnmatchcase` (case-insensitive). Power-user escape hatch
       and back-compat with the old PS ``-AssetPattern``.

    2. **Token AND-match** (default) — split the query on whitespace, dashes,
       dots, commas, slashes. Require *every* token to appear as a substring
       of ``asset.name`` (case-insensitive). Order-independent.

    Examples (asset name = ``"llama-b8929-bin-win-cuda-13.1-x64.zip"``):
        ``"cuda win x64 zip"``       → match (4 tokens, all present)
        ``"cuda, win, x64 zip"``     → match (commas as separators)
        ``"llama win cuda x64.zip"`` → match (mixed separators)
        ``"*linux-x64*"``            → glob fallback
        ``""``                       → empty list (no tokens)

    Args:
        query: User input from a prompt or CLI flag.
        assets: Iterable of objects with a ``.name`` attribute.

    Returns:
        Subset of ``assets`` matching the query, preserving input order.
    """
    if any(c in query for c in "*?"):
        glob_lower = query.lower()
        return [a for a in assets if fnmatch.fnmatchcase(a.name.lower(), glob_lower)]

    tokens = [t.lower() for t in _TOKEN_SEPARATOR.split(query) if t]
    if not tokens:
        return []

    return [a for a in assets if all(t in a.name.lower() for t in tokens)]
