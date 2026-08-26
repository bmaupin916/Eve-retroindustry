"""`import_sde.py --fresh` throws away the static data and nothing else.

`_drop_static_data` had no test on either backend. That is the wrong function to
leave uncovered: its own docstring says the alternative "would take
`characters` and every refresh token with it, which is a defect this project has
shipped once already, from a different button" — so the failure mode is silent
loss of the credentials the whole app depends on, at the hands of a flag whose
name sounds harmless.

**The two branches do opposite things, and both are correct.** For a SQLite
*file* the whole file is deleted, because that path exists to build
`sde_base.db` and a bundle carrying somebody's tokens would be a data leak
rather than a fixture. For any other target — which today means Postgres, i.e.
the deployment — only the fourteen SDE tables are dropped. A test that asserted
one behaviour on both backends would be asserting a bug on one of them, so the
asymmetry is the thing under test rather than an inconvenience to paper over.

The Postgres half is the half that matters and it is the half that skips when
the container is down, so `test_the_postgres_half_is_not_silently_skipping`
exists to make that visible: without it a stopped container reads as a green
file whose SQLite half never touches the branch at issue.
"""
from __future__ import annotations


import pytest
from sqlalchemy import create_engine, inspect, text

import import_sde
from app.db.schema import SDE_TABLES, create_sde_schema, metadata
from tests.test_postgres_schema import URL as PG_URL, _reachable

PG_SCHEMA = "pytest_sde_fresh"

CANARY_ID = 2_200_000_001
CANARY_TOKEN = "refresh-token-that-must-survive"


# ── Postgres: drop the static data, keep everything else ─────────────────────

@pytest.fixture
def pg():
    """A Postgres database holding both static data and runtime data.

    Built rather than migrated: `upgrade_to_head` would work and takes seconds,
    and every one of those seconds would be spent proving something
    `tests/test_migrations.py` already proves. What this file needs is one
    declared runtime table with a row in it.
    """
    if not _reachable(PG_URL):
        pytest.skip(f"no Postgres at {PG_URL} — see tests/test_postgres_schema.py")

    admin = create_engine(PG_URL)
    with admin.connect() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {PG_SCHEMA} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {PG_SCHEMA}"))
        c.commit()
    admin.dispose()

    url = PG_URL + ("&" if "?" in PG_URL else "?") + \
        f"options=-csearch_path%3D{PG_SCHEMA}"
    engine = create_engine(url)

    with engine.connect() as c:
        create_sde_schema(c)
        c.commit()
    metadata.tables["characters"].create(engine)
    with engine.connect() as c:
        c.execute(
            text("INSERT INTO characters (character_id, character_name,"
                 " refresh_token, added_at) VALUES (:i, :n, :t, :a)"),
            {"i": CANARY_ID, "n": "Canary Pilot", "t": CANARY_TOKEN,
             "a": 1_787_775_700})
        c.commit()

    yield engine, url

    engine.dispose()
    admin = create_engine(PG_URL)
    with admin.connect() as c:
        c.execute(text(f"DROP SCHEMA IF EXISTS {PG_SCHEMA} CASCADE"))
        c.commit()
    admin.dispose()


def test_the_postgres_half_is_not_silently_skipping(pg):
    """The SQLite tests below never reach the branch this file is about."""
    engine, _ = pg
    assert engine.dialect.name == "postgresql"


def test_fresh_keeps_the_refresh_token(pg):
    """The defect in `_drop_static_data`'s docstring, asserted rather than
    described. A character's refresh token is the only thing in this database
    that cannot be re-fetched from CCP."""
    engine, url = pg
    import_sde._drop_static_data(engine, url)

    with engine.connect() as c:
        row = c.execute(
            text("SELECT character_name, refresh_token FROM characters"
                 " WHERE character_id = :i"), {"i": CANARY_ID}).fetchone()
    assert row is not None, "--fresh took the characters table with it"
    assert row[0] == "Canary Pilot"
    assert row[1] == CANARY_TOKEN


def test_fresh_leaves_the_runtime_table_itself_in_place(pg):
    """Distinct from the row surviving: a `DELETE` that spared the schema and a
    `DROP` that spared nothing both fail the test above, and only one of them is
    recoverable by re-running migrations."""
    engine, url = pg
    import_sde._drop_static_data(engine, url)

    with engine.connect() as c:
        assert inspect(c).has_table("characters")


def test_fresh_really_does_drop_the_static_data(pg):
    """The positive control. Every assertion above is also satisfied by a
    `_drop_static_data` that does nothing at all."""
    engine, url = pg
    with engine.connect() as c:
        before = set(inspect(c).get_table_names()) & set(SDE_TABLES)
    assert before == set(SDE_TABLES), "the fixture did not build the static data"

    import_sde._drop_static_data(engine, url)

    with engine.connect() as c:
        after = set(inspect(c).get_table_names()) & set(SDE_TABLES)
    assert after == set()


def test_the_message_counts_what_was_dropped_not_what_was_asked_for(pg, capsys):
    """`drop_all(checkfirst=True)` skips what is not there, so reporting the
    length of the list is a number nobody measured. It read "Dropped 14
    static-data tables" against a database that had none."""
    engine, url = pg
    import_sde._drop_static_data(engine, url)          # the 14 really go
    capsys.readouterr()

    import_sde._drop_static_data(engine, url)          # now there are none
    out = capsys.readouterr().out
    assert "No static-data tables to drop" in out
    assert "Dropped 14" not in out


def test_dropping_twice_is_not_an_error(pg):
    """`--fresh` is the flag people re-run after an import fails partway."""
    engine, url = pg
    import_sde._drop_static_data(engine, url)
    import_sde._drop_static_data(engine, url)


# ── SQLite: the file goes, because that is what builds the bundle ────────────

def test_fresh_on_a_sqlite_file_removes_the_whole_file(tmp_path):
    """`--out sde_base.db --fresh` builds the shipped bundle. Dropping tables
    would leave whatever else the file held — and the bundle is committed to the
    repository, so "whatever else" would be published."""
    path = tmp_path / "sde_base.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    with engine.connect() as c:
        create_sde_schema(c)
        c.commit()
    assert path.exists()

    import_sde._drop_static_data(engine, url)

    assert not path.exists()


def test_fresh_on_a_sqlite_path_that_is_not_there_yet_is_not_an_error(tmp_path):
    """The first build of a bundle has no file to remove."""
    path = tmp_path / "does-not-exist.db"
    url = f"sqlite:///{path}"
    import_sde._drop_static_data(create_engine(url), url)
    assert not path.exists()


def test_the_sqlite_branch_releases_the_file_before_removing_it(tmp_path):
    """Windows refuses to unlink an open file, so the `engine.dispose()` in
    that branch is load-bearing on the platform this is developed on — and its
    absence would show up as an occasional failure rather than a clean one."""
    path = tmp_path / "held.db"
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    with engine.connect() as c:
        create_sde_schema(c)
        c.commit()
    engine.connect()                     # a pooled connection left open

    import_sde._drop_static_data(engine, url)

    assert not path.exists()


# ── the two branches are chosen by the URL, not by anything ambient ──────────

@pytest.mark.parametrize("url", ["sqlite:///x.db", "sqlite+pysqlite:///x.db"])
def test_every_sqlite_spelling_takes_the_delete_branch(url, monkeypatch):
    """The branch is `url.startswith("sqlite")` on a raw string, so the driver
    suffix decides it. `sqlite+pysqlite://` is the form SQLAlchemy itself
    renders when a URL is round-tripped through `make_url`, and it must not
    start dropping tables out of a file it was asked to replace.

    The other direction — a Postgres URL keeping its runtime tables — is not
    faked here; it is the `pg` fixture's tests above, against a real server.
    """
    removed, dropped = [], []
    monkeypatch.setattr(import_sde.os, "remove", lambda p: removed.append(p))
    monkeypatch.setattr(import_sde.os.path, "exists", lambda p: True)
    monkeypatch.setattr(import_sde.metadata, "drop_all",
                        lambda *a, **k: dropped.append(k.get("tables")))

    class _Engine:
        def dispose(self):
            pass

        def connect(self):
            raise AssertionError("the sqlite branch must not open a connection")

    import_sde._drop_static_data(_Engine(), url)

    assert removed == ["x.db"]
    assert dropped == []
