"""Tests for ``ghrel.prompts``.

Focus on the pure-logic helpers (:func:`parse_nav` and the
:class:`PromptResult` dataclass). The :class:`Prompts` *methods* are exercised
end-to-end in ``tests/e2e/test_state_machine_e2e.py`` where we drive real
:func:`input` via monkeypatching — that's a more honest test than mocking
``rich.prompt.Prompt.ask``.
"""

from __future__ import annotations

import pytest

from ghrel.prompts import NavAction, PromptResult, parse_nav


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("q", NavAction.QUIT),
        ("Q", NavAction.QUIT),
        ("quit", NavAction.QUIT),
        ("EXIT", NavAction.QUIT),
        ("b", NavAction.BACK),
        ("Back", NavAction.BACK),
        ("m", NavAction.MENU),
        ("menu", NavAction.MENU),
        ("home", NavAction.MENU),
        ("?", NavAction.HELP),
        ("h", NavAction.HELP),
        ("help", NavAction.HELP),
        ("  q  ", NavAction.QUIT),  # whitespace-trimmed
    ],
)
def test_parse_nav_accepts(text: str, expected: NavAction) -> None:
    assert parse_nav(text) is expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "  ",
        "bin",  # contains 'b' but isn't 'b'
        "mac",  # contains 'm' but isn't 'm'
        "1 3 6",
        "cuda win",
        "?? help me",
        "qwerty",
        "exit-strategy",
    ],
)
def test_parse_nav_rejects(text: str) -> None:
    assert parse_nav(text) is None


def test_prompt_result_is_nav_property() -> None:
    nav_result = PromptResult(nav=NavAction.QUIT)
    value_result = PromptResult(value="something")

    assert nav_result.is_nav is True
    assert value_result.is_nav is False
