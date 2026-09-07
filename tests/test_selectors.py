"""Guards on the Meet selector module.

Meet's DOM is the most volatile dependency in this project. These tests do not
verify that the selectors *work* - only a live meeting can do that - but they
do enforce the property that makes fixing them cheap: every Meet selector
lives in one file.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from meetbot import selectors

PACKAGE_ROOT = Path(selectors.__file__).parent

#: Every public selector list in the module.
SELECTOR_LISTS = {
    name: getattr(selectors, name)
    for name in selectors.__all__
}

#: Markers that indicate a Meet DOM selector is being written inline.
_SELECTOR_MARKERS = re.compile(r"aria-label|jsname|role=\"button\"|:has-text\(")


def test_every_selector_list_is_non_empty() -> None:
    for name, candidates in SELECTOR_LISTS.items():
        assert candidates, f"{name} has no candidates"


def test_every_selector_is_a_non_empty_string() -> None:
    for name, candidates in SELECTOR_LISTS.items():
        for candidate in candidates:
            assert isinstance(candidate, str), f"{name} contains a non-string"
            assert candidate.strip(), f"{name} contains a blank selector"


def test_selector_lists_have_no_duplicates() -> None:
    """Duplicates mean a wasted probe on every fallback walk."""
    for name, candidates in SELECTOR_LISTS.items():
        assert len(candidates) == len(set(candidates)), f"{name} has duplicates"


def test_fallback_candidates_are_provided() -> None:
    """Single-candidate lists have no resilience when Google ships a change."""
    for name in ("NAME_INPUT", "JOIN_BUTTON", "IN_CALL_MARKERS", "MEETING_ENDED_MARKERS"):
        assert len(SELECTOR_LISTS[name]) >= 2, f"{name} needs fallback candidates"


def test_module_documents_that_selectors_need_maintenance() -> None:
    doc = selectors.__doc__ or ""
    assert "WILL break" in doc
    assert "Last verified" in doc


@pytest.mark.parametrize(
    "module_path",
    sorted(
        p
        for p in PACKAGE_ROOT.rglob("*.py")
        if p.name not in ("selectors.py", "__init__.py")
    ),
    ids=lambda p: p.name,
)
def test_no_meet_selectors_are_written_inline(module_path: Path) -> None:
    """Meet selectors must live in selectors.py, not scattered through the code.

    If this fails, move the string into ``meetbot/selectors.py`` and reference
    it by name - that is the whole point of the module.
    """
    offenders = [
        f"{module_path.name}:{lineno}: {line.strip()}"
        for lineno, line in enumerate(
            module_path.read_text(encoding="utf-8").splitlines(), start=1
        )
        if _SELECTOR_MARKERS.search(line) and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        "Inline Meet selector(s) found outside selectors.py:\n  "
        + "\n  ".join(offenders)
    )


def test_join_denied_markers_cover_the_anonymous_refusal() -> None:
    """The page Meet shows when it will not let a guest even ask to join.

    Observed live: the pre-join screen is replaced entirely by "You can't
    join this video call", so there is no join button to find. Without this
    marker the failure is misreported as a broken JOIN_BUTTON selector, which
    sends you off to fix selectors that are fine.
    """
    joined = " ".join(selectors.JOIN_DENIED_MARKERS)
    assert "can't join this video call" in joined
