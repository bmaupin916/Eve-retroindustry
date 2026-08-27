"""The documented scope count is a claim about the code, so assert it.

Four documents said "all 24 requested scopes are read-only". `SCOPES` has 23,
and `git log` says it has had 23 since the scope list was written — nobody ever
counted, and nothing was watching. That is the same shape as every other defect
this project has found in a sentence long marked correct: **a name that claims
something about the world needs an assertion about the world.**

The count is about to move — §5 of the design wants
`esi-corporations.read_divisions.v1` so a corp warehouse can be called what the
corp calls it — and the point of this file is that adding it will fail here
until the four documents are updated with it.

One deliberate hazard, guarded below: a regex that finds *no* claims passes
every per-claim assertion vacuously, and a scan that silently under-reports
looks exactly like a clean result. `test_the_scan_still_finds_what_it_guards`
pins the number of claims and the files carrying them.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.auth.esi_oauth import SCOPES

ROOT = Path(__file__).resolve().parent.parent

#: "all 24 requested scopes", "all 23 scopes" — the shapes the docs actually use.
CLAIM = re.compile(r"\b(\d+)\s+(?:requested\s+)?scopes\b")

#: Every prose file that may carry the claim. Not a glob: a new document making
#: the claim should be added here deliberately, and a glob would hide a rename.
DOCS = (
    Path("README.md"),
    Path("docs/deploy-vps.md"),
    Path("docs/design-hosted-v2.md"),
)


def _claims() -> list[tuple[Path, int, int]]:
    """(file, line number, claimed count) for every scope-count claim found."""
    found = []
    for rel in DOCS:
        path = ROOT / rel
        assert path.exists(), f"{rel} is gone — fix the list, do not delete the check"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in CLAIM.finditer(line):
                found.append((rel, n, int(m.group(1))))
    return found


def test_scopes_has_no_duplicates():
    """A duplicate would make len() disagree with what CCP is actually asked for."""
    assert len(SCOPES) == len(set(SCOPES))


def test_every_scope_is_a_well_formed_esi_scope():
    """`esi-<group>.<action>.v<n>` — a typo here is a 400 at the consent screen."""
    shape = re.compile(r"^esi-[a-z]+\.[a-z_]+\.v\d+$")
    bad = [s for s in SCOPES if not shape.match(s)]
    assert not bad, f"malformed scope strings: {bad}"


def test_documented_scope_counts_match_the_code():
    wrong = [(str(f), n, claimed) for f, n, claimed in _claims() if claimed != len(SCOPES)]
    assert not wrong, (
        f"SCOPES has {len(SCOPES)} entries; these say otherwise: {wrong}. "
        "Update the documents, or the scope list, so they agree."
    )


def test_the_scan_still_finds_what_it_guards():
    """The under-reporting guard.

    Without this, deleting the regex's body or renaming a document leaves every
    assertion above passing over an empty list.
    """
    found = _claims()
    assert len(found) >= 4, f"expected at least 4 scope-count claims, found {found}"
    covered = {f for f, _, _ in found}
    assert covered == set(DOCS), f"claims found only in {covered}, expected all of {set(DOCS)}"


@pytest.mark.parametrize("sample,expected", [
    ("All 23 requested scopes are read-only", 23),
    ("all 23 scopes are read-only", 23),
    ("all 24 requested scopes are read-only", 24),
])
def test_the_regex_matches_the_shapes_the_docs_use(sample, expected):
    """Pins the matcher itself, so a narrowed pattern fails here and not silently."""
    m = CLAIM.search(sample)
    assert m is not None and int(m.group(1)) == expected
