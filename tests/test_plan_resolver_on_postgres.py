"""`_resolve_product_local` on both backends — the one thing SQLite cannot tell you.

This is the function that turns what you type in the plan form into a type id.
It searches exact → prefix → substring, and every stage of that cascade is
case-insensitive **for two independent SQLite-specific reasons**:

  1. the exact match carries `COLLATE NOCASE`, a collation Postgres does not have;
  2. the two `LIKE` stages rely on SQLite's default, where `LIKE` ignores ASCII
     case. **Postgres `LIKE` is case-sensitive.**

Only the first is visible by reading. The second is the dangerous one, and the
suite could never have found it: mutating `COLLATE NOCASE` out of the exact
match on SQLite changes nothing, because the prefix stage catches the query
anyway. Measured, not reasoned about —

    SELECT … WHERE name = 'bantam'                       -> None
    SELECT … WHERE name LIKE 'bantam%' ORDER BY LENGTH…  -> (582, 'Bantam')

So a conversion that faithfully translated `COLLATE NOCASE` and left the two
`LIKE`s alone would leave every mixed-case product search broken on Postgres,
with a green SQLite suite forever. That is the failure this file exists to
prevent, and it is why the test drives the real function against a real
Postgres rather than asserting on SQL strings.

**These tests fail against the pre-conversion code on the Postgres half, and
that is the point.** They were written first, they describe the behaviour the
conversion has to preserve, and the SQLite half passes throughout.

One limit, recorded rather than papered over: removing `LOWER()` from **one**
stage of the cascade is not detectable, on either backend. The three stages are
redundant — the needle is lowered once, up front, so an exact match that stops
folding is caught by the prefix stage, and a prefix stage that stops folding is
caught by the substring stage. What *is* caught is dropping the `q.lower()`
itself, which disables all three at once.

Making the single-stage cases decidable would need inputs where the stages
disagree about which type wins, and for this cascade they mostly agree by
construction — the preference rules are the same at every stage. A test forced
into existence there would be pinning an accident of the fixture rather than a
property of the code, which is the failure this file has already corrected
twice.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from app.web.routers.plan import _resolve_product_local
from tests.test_postgres_schema import URL as PG_URL, _reachable

PG_SCHEMA = "pytest_plan_resolver"

#: Deliberately mixed case, and deliberately a set where the stages disagree:
#: "Bantam" is an exact name AND a prefix of "Bantam Blueprint", so the
#: preference rules have something to choose between.
SEED = [
    (582, "Bantam", 1),
    (683, "Bantam Blueprint", 1),
    (34, "Tritanium", 1),
    (11393, "Widow", 1),
    (999001, "Unpublished Thing", 0),
]

#: Only 582 is producible; `_pick` prefers producible candidates, which is what
#: keeps "Bantam" from resolving to the blueprint.
PRODUCTS = [(683, "manufacturing", 582)]


@pytest.fixture(scope="module", params=["sqlite", "postgres"])
def engine(request, tmp_path_factory):
    from app.db.migrate import upgrade_to_head
    from app.db.schema import SDE_TABLES, metadata

    def _build_sde(eng):
        metadata.create_all(
            eng, tables=[metadata.tables[n] for n in sorted(SDE_TABLES)])

    if request.param == "sqlite":
        url = f"sqlite:///{tmp_path_factory.mktemp('db') / 'resolver.db'}"
        upgrade_to_head(url)
        eng = create_engine(url)
        _build_sde(eng)
        yield eng
        eng.dispose()
        return

    if not _reachable(PG_URL):
        pytest.skip(f"no Postgres at {PG_URL} — see tests/test_postgres_schema.py")

    admin = create_engine(PG_URL)
    with admin.connect() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {PG_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {PG_SCHEMA}"))
        c.commit()
    admin.dispose()

    scoped = PG_URL + ("&" if "?" in PG_URL else "?") + \
        f"options=-csearch_path%3D{PG_SCHEMA}"
    upgrade_to_head(scoped)
    eng = create_engine(scoped)
    _build_sde(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def conn(engine):
    with engine.connect() as c:
        c.execute(text("DELETE FROM sde_blueprint_products"))
        c.execute(text("DELETE FROM sde_types"))
        c.execute(
            text("INSERT INTO sde_types (type_id, name, published)"
                 " VALUES (:tid, :name, :pub)"),
            [{"tid": t, "name": n, "pub": p} for t, n, p in SEED])
        c.execute(
            text("INSERT INTO sde_blueprint_products"
                 " (blueprint_type_id, activity, product_type_id, quantity)"
                 " VALUES (:bp, :act, :prod, 1)"),
            [{"bp": b, "act": a, "prod": p} for b, a, p in PRODUCTS])
        c.commit()
        yield c


# ── the case-folding the whole form depends on ───────────────────────────────

@pytest.mark.parametrize("typed", ["Bantam", "bantam", "BANTAM", "BaNtAm"])
def test_a_product_resolves_whatever_case_you_type(conn, typed):
    """Four spellings, one answer, on both backends.

    A single lowercase probe cannot tell case-folding from a lucky exact match,
    which is how three other tests in this codebase came to be vacuous. Four
    spellings including the correct one make the difference decidable: the
    correct-case probe passes even when folding is broken, so it is the control
    and the other three are the measurement.
    """
    got = _resolve_product_local(conn, typed)

    assert got is not None, (
        f"typing {typed!r} resolved to nothing — on Postgres this is what a "
        f"`LIKE` left case-sensitive looks like")
    assert got[0] == 582, f"{typed!r} resolved to {got}"


def test_a_lowercase_prefix_still_reaches_the_prefix_stage(conn):
    """The middle stage of the cascade, isolated.

    "banta" is nobody's exact name, so the exact match must miss and the prefix
    match must carry it — in lower case. On Postgres a bare `LIKE 'banta%'`
    matches nothing at all.
    """
    got = _resolve_product_local(conn, "banta")

    assert got is not None, "the prefix stage found nothing for a lowercase stem"
    assert got[0] == 582


def test_a_lowercase_substring_still_reaches_the_substring_stage(conn):
    """The last stage. "antam" is neither a name nor a prefix of one, so only
    the substring pass can find it."""
    got = _resolve_product_local(conn, "antam")

    assert got is not None, "the substring stage found nothing for a lowercase stem"
    assert got[0] == 582


# ── the preference rules, which decide *which* match wins ────────────────────

def test_the_producible_product_beats_its_blueprint(conn):
    """`_pick` prefers candidates with a manufacturing or reaction recipe.

    "Bantam" prefix-matches "Bantam Blueprint" too, and planning a build *of the
    blueprint* is a plan for the wrong item.

    On its own this proves less than it looks: the blueprint is also the longer
    name, so the length tie-break would pick right even with the producible
    preference gone. The test below is the one that separates them.
    """
    got = _resolve_product_local(conn, "Bantam")

    assert got == (582, "Bantam")


def test_producible_beats_shorter(conn):
    """The producible preference, made decidable.

    The mutation battery scored the test above as not catching a `_pick` that
    ignores its producible list — because in that fixture producible and
    shortest are the same row, so the two rules cannot disagree. Same trap as
    the "hobgoblin" ordering test earlier in this conversion: an expected value
    that the broken code also produces.

    Here they disagree on purpose. "Bantam Mk2" is buildable; "Bantam Mk" is
    shorter and is not. Preference honoured → Mk2; preference dropped → the
    length tie-break wins and the form offers something with no recipe.

    **The search term has to miss the exact stage.** Asking for "Bantam Mk"
    resolves to "Bantam Mk" on both backends and always will — it is somebody's
    exact name, so `_pick` receives a single row and never compares anything.
    That is right, and it is not what this test is about. "Bantam M" is nobody's
    name, so the prefix stage hands `_pick` both candidates and the preference
    rules actually run.
    """
    with conn.begin_nested():
        conn.execute(
            text("INSERT INTO sde_types (type_id, name, published)"
                 " VALUES (:tid, :name, 1)"),
            [{"tid": 999010, "name": "Bantam Mk"},      # shorter, not producible
             {"tid": 999011, "name": "Bantam Mk2"}])    # longer, producible
        conn.execute(
            text("INSERT INTO sde_blueprint_products"
                 " (blueprint_type_id, activity, product_type_id, quantity)"
                 " VALUES (:bp, 'manufacturing', :prod, 1)"),
            {"bp": 999012, "prod": 999011})

        got = _resolve_product_local(conn, "Bantam M")

    assert got is not None
    assert got[0] == 999011, (
        f"resolved to {got} — the shorter, unbuildable name won, so the "
        f"producible preference is not being applied")


def test_an_unpublished_type_loses_to_a_published_one(conn):
    """`published` is the second sort key. Unpublished types are removed content
    and things that never shipped; offering one as a build target is offering
    something you cannot make."""
    with conn.begin_nested():
        conn.execute(
            text("INSERT INTO sde_types (type_id, name, published)"
                 " VALUES (:tid, :name, :pub)"),
            {"tid": 999002, "name": "Widowx", "pub": 0})

        got = _resolve_product_local(conn, "widow")

    assert got is not None
    assert got[0] == 11393, (
        f"an unpublished type won the match: {got}")


def test_an_unknown_name_resolves_to_nothing(conn):
    """The negative control. Without it every assertion above could be satisfied
    by a function that returned the Bantam for any input at all."""
    assert _resolve_product_local(conn, "zzzz not a real item") is None
