"""Remove rows a test run wrote into a real database.

Until the fix in `tests/conftest.py`, `EVE_APP_DIR` was set inside the
`app_module` fixture — after pytest collection, which imports every test
module. The first test module with a module-level `from app.db...` or
`from app.web...` import therefore made the app bind the developer's own
`eve_cache.db`, and the whole run read and wrote there.

This finds what that left behind and takes it out. It is deliberately narrow:
it removes rows it can identify as synthetic, and nothing else. Anything it is
not sure about, it reports and leaves alone.

    python scripts/clean_test_contamination.py            # report only
    python scripts/clean_test_contamination.py --apply    # and delete

A timestamped copy is made before any delete. `--db` points it at a different
file; the default is the `eve_cache.db` beside this checkout.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import time

# tests/conftest.py::_seed
TEST_CHARACTERS = (900000001, 900000002)
TEST_CORPORATION = 98000001
# tests/test_pi_planner.py
TEST_PLANETS = (4001, 4002, 4003)
# _seed() overwrites these five minerals with sell 5.0 / buy 4.0
SEEDED_TYPES = (34, 35, 36, 37, 38)


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def plan(conn) -> list[tuple[str, str, tuple]]:
    """(label, sql, params) for everything that is going to be removed."""
    steps: list[tuple[str, str, tuple]] = []
    chars = ",".join("?" * len(TEST_CHARACTERS))

    # The instance-owner claim. This is the one that actually locks the real
    # user out: security.claim_owner() pins the app to one character, and
    # conftest's _login() claims it for Test Pilot Alpha.
    if _table_exists(conn, "app_owner"):
        steps.append((
            "app_owner: the instance claim made by a test character",
            f"DELETE FROM app_owner WHERE character_id IN ({chars})",
            TEST_CHARACTERS,
        ))

    if _table_exists(conn, "app_sessions"):
        steps.append((
            "app_sessions: sessions minted for test characters",
            f"DELETE FROM app_sessions WHERE character_id IN ({chars})",
            TEST_CHARACTERS,
        ))

    # Characters and everything keyed to them. The real characters' rows were
    # deleted by _seed()'s `DELETE FROM characters`; their *caches* survived,
    # so only the synthetic ids are removed here.
    for table in ("characters", "char_assets_cache", "char_blueprints_cache",
                  "char_wallet_cache", "char_skills_cache"):
        if _table_exists(conn, table) and "character_id" in _columns(conn, table):
            steps.append((
                f"{table}: rows belonging to the seeded test pilots",
                f"DELETE FROM {table} WHERE character_id IN ({chars})",
                TEST_CHARACTERS,
            ))

    if _table_exists(conn, "corp_assets_cache"):
        steps.append((
            "corp_assets_cache: the synthetic corporation",
            "DELETE FROM corp_assets_cache WHERE corporation_id = ?",
            (TEST_CORPORATION,),
        ))

    if _table_exists(conn, "planet_name_cache"):
        steps.append((
            "planet_name_cache: the PI planner's Testworlds",
            "DELETE FROM planet_name_cache WHERE planet_id IN (?,?,?)",
            TEST_PLANETS,
        ))

    # Not deleted for being wrong — deleted so they refetch. _seed() writes
    # sell 5.0 / buy 4.0 over the real mineral prices, and a stale-looking row
    # is repriced on the next refresh while a fresh-looking wrong one is not.
    if _table_exists(conn, "market_price_cache"):
        steps.append((
            "market_price_cache: the five minerals _seed() overwrites, so they refetch",
            "DELETE FROM market_price_cache WHERE type_id IN (?,?,?,?,?)"
            " AND sell_price = 5.0",
            SEEDED_TYPES,
        ))

    return steps


def report_unsure(conn) -> list[str]:
    """Things that look test-written but cannot be identified with certainty."""
    notes = []
    if _table_exists(conn, "margin_watchlist") and _table_exists(conn, "app_owner"):
        claimed = conn.execute(
            "SELECT claimed_at FROM app_owner WHERE character_id IN (?,?)",
            TEST_CHARACTERS).fetchone()
        if claimed:
            # Everything written after the claim is from that run.
            since = claimed[0]
            rows = conn.execute(
                "SELECT id, type_id, me, te FROM margin_watchlist WHERE added_at >= ?",
                (since - 3600,)).fetchall()
            for r in rows:
                notes.append(
                    f"margin_watchlist id={r[0]} type_id={r[1]} ME{r[2]}/TE{r[3]} "
                    "was added during the contaminated window "
                    "(tests/test_margins.py adds Crane at ME 5 / TE 20)")
    return notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "eve_cache.db"))
    ap.add_argument("--apply", action="store_true",
                    help="actually delete; without it, only report")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"no such database: {args.db}")
        return 1

    conn = sqlite3.connect(args.db)
    steps = plan(conn)

    total = 0
    counts = []
    for label, sql, params in steps:
        count_sql = sql.replace("DELETE FROM", "SELECT COUNT(*) FROM", 1)
        n = conn.execute(count_sql, params).fetchone()[0]
        counts.append((label, sql, params, n))
        total += n

    print(f"{args.db}\n")
    for label, _, _, n in counts:
        mark = "  " if n == 0 else "* "
        print(f"{mark}{n:5d}  {label}")
    print(f"\n{total} rows to remove")

    notes = report_unsure(conn)
    if notes:
        print("\nnot removed automatically — check these yourself:")
        for n in notes:
            print(f"  - {n}")

    if not args.apply:
        print("\nreport only. Re-run with --apply to delete.")
        conn.close()
        return 0

    if total == 0:
        print("\nnothing to do.")
        conn.close()
        return 0

    backup = f"{args.db}.bak-{time.strftime('%Y%m%d-%H%M%S')}-before-cleanup"
    conn.close()
    shutil.copy2(args.db, backup)
    print(f"\nbacked up to {backup}")

    conn = sqlite3.connect(args.db)
    for _, sql, params, n in counts:
        if n:
            conn.execute(sql, params)
    conn.commit()
    conn.close()
    print(f"removed {total} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
