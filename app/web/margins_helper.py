"""View model for the Margin Tracker page (`/margins`).

Owns the watchlist (which products you're tracking, at what ME/TE) and the daily
snapshot history that powers the change and 7-day-average columns. The profit
maths itself lives in `app/manufacturing/margins.py`; this module is persistence
plus presentation.

**Why snapshots rather than back-computation.** "Change in profit %" and the
7-day average need yesterday's margin, and margin is a function of the product
price *and* every material price *and* your station config on that day. The
price history cache only covers types someone has charted, and none of it knows
what your station config was last Tuesday. So the tracker records what it
computed, once per UTC day, and averages its own readings. The honest
consequence: the 7-day average is thin until the page has been open on seven
separate days, and the UI says so rather than averaging one reading and calling
it a week.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

from app.manufacturing.margins import MarginRow, compute_margin, _station_context
from app.web.app_defaults import get_defaults, is_configured
from app.market.taxes import selling_costs

# Days of history behind the rolling average.
AVG_WINDOW_DAYS = 7


def ensure_margin_tables(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS margin_watchlist (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            type_id  INTEGER NOT NULL,
            me       INTEGER NOT NULL DEFAULT 0,
            te       INTEGER NOT NULL DEFAULT 0,
            added_at REAL,
            -- The same product at two ME levels is two genuinely different
            -- propositions, so (type, ME, TE) is the identity, not the type.
            UNIQUE (type_id, me, te)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS margin_snapshot (
            item_id     INTEGER NOT NULL,
            day         TEXT NOT NULL,          -- YYYY-MM-DD, UTC
            margin_pct  REAL,
            profit      REAL,
            sell_price  REAL,
            captured_at REAL,
            PRIMARY KEY (item_id, day)
        )
    """)
    conn.commit()


def _today() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")


# ── watchlist ────────────────────────────────────────────────────────────────

def add_item(conn: sqlite3.Connection, type_id: int, me: int, te: int) -> tuple[bool, str]:
    """Adds a product. Returns (ok, message)."""
    ensure_margin_tables(conn)
    row = conn.execute("SELECT name FROM sde_types WHERE type_id=?", (type_id,)).fetchone()
    if not row:
        return False, "Unknown item."
    buildable = conn.execute(
        "SELECT 1 FROM sde_blueprint_products WHERE product_type_id=? "
        "AND activity IN ('manufacturing','reaction') LIMIT 1", (type_id,)
    ).fetchone()
    if not buildable:
        return False, f"{row[0]} has no blueprint — there is no build margin to track."
    try:
        conn.execute(
            "INSERT INTO margin_watchlist (type_id, me, te, added_at) VALUES (?,?,?,?)",
            (type_id, me, te, dt.datetime.now(dt.timezone.utc).timestamp()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return False, f"{row[0]} at ME {me} / TE {te} is already tracked."
    return True, f"Added {row[0]} (ME {me}, TE {te})."


def remove_item(conn: sqlite3.Connection, item_id: int) -> None:
    ensure_margin_tables(conn)
    conn.execute("DELETE FROM margin_watchlist WHERE id=?", (item_id,))
    conn.execute("DELETE FROM margin_snapshot WHERE item_id=?", (item_id,))
    conn.commit()


def clear_all(conn: sqlite3.Connection) -> None:
    ensure_margin_tables(conn)
    conn.execute("DELETE FROM margin_watchlist")
    conn.execute("DELETE FROM margin_snapshot")
    conn.commit()


def list_items(conn: sqlite3.Connection) -> list[dict]:
    """Watchlist rows as dicts — `get_conn()` hands back plain tuples, so this
    does not rely on the caller having set a row factory."""
    ensure_margin_tables(conn)
    return [{"id": r[0], "type_id": r[1], "me": r[2], "te": r[3]} for r in conn.execute(
        "SELECT id, type_id, me, te FROM margin_watchlist ORDER BY added_at"
    ).fetchall()]


# ── history ──────────────────────────────────────────────────────────────────

def record_snapshot(conn: sqlite3.Connection, item_id: int, row: MarginRow) -> None:
    """Stores today's reading, replacing any earlier reading from today.

    Replacing rather than skipping means today's row always reflects the latest
    prices; only *previous* days are frozen.
    """
    if row.margin_pct is None:
        return                      # an unpriced row is not a data point
    conn.execute(
        "INSERT INTO margin_snapshot (item_id, day, margin_pct, profit, sell_price, captured_at) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(item_id, day) DO UPDATE SET "
        "margin_pct=excluded.margin_pct, profit=excluded.profit, "
        "sell_price=excluded.sell_price, captured_at=excluded.captured_at",
        (item_id, _today(), row.margin_pct, row.profit, row.sell_price,
         dt.datetime.now(dt.timezone.utc).timestamp()),
    )


def history_for(conn: sqlite3.Connection, item_id: int) -> dict:
    """Change since the previous reading, plus the rolling average.

    `change` compares against the most recent day *before* today, so it reads as
    "since last time you looked", not "since this morning's first page load".
    """
    today = _today()
    prev = conn.execute(
        "SELECT margin_pct FROM margin_snapshot WHERE item_id=? AND day<? "
        "ORDER BY day DESC LIMIT 1", (item_id, today),
    ).fetchone()
    window = conn.execute(
        "SELECT margin_pct FROM margin_snapshot WHERE item_id=? "
        "ORDER BY day DESC LIMIT ?", (item_id, AVG_WINDOW_DAYS),
    ).fetchall()
    values = [r[0] for r in window if r[0] is not None]
    return {
        "prev_margin": prev[0] if prev else None,
        "avg_margin": (sum(values) / len(values)) if values else None,
        "days": len(values),
        "full_window": len(values) >= AVG_WINDOW_DAYS,
    }


# ── view model ───────────────────────────────────────────────────────────────

def build_view_model(conn: sqlite3.Connection, db_path: str,
                     message: str | None = None) -> dict:
    """Prices the whole watchlist and assembles the page."""
    ensure_margin_tables(conn)
    defaults = get_defaults(conn)
    items = list_items(conn)

    view: dict = {
        "defaults": defaults,
        "configured": is_configured(defaults),
        "rows": [],
        "message": message,
        "avg_window": AVG_WINDOW_DAYS,
        "station_name": _station_name(conn, defaults.get("build_station_id")),
        "reaction_station_name": _station_name(conn, defaults.get("reaction_station_id")),
        "volume_supported": _volume_column_present(conn),
        "sci_cached": True,
        "any_unpriced": False,
        # What selling costs under the current settings. Shown in the footer so
        # the deduction is visible rather than buried in each row's profit.
        "selling": selling_costs(defaults),
    }
    if not items:
        return view

    ctx = _station_context(conn, defaults)
    view["sci_cached"] = ctx["sci_cached"]
    blueprints = _all_blueprints(conn)

    for item in items:
        row = compute_margin(conn, db_path, item["type_id"], item["me"], item["te"],
                             defaults, blueprints=blueprints, ctx=ctx)
        record_snapshot(conn, item["id"], row)
        hist = history_for(conn, item["id"])
        change = None
        if row.margin_pct is not None and hist["prev_margin"] is not None:
            change = row.margin_pct - hist["prev_margin"]
        view["rows"].append({
            "item_id": item["id"],
            "row": row,
            "change": change,
            "avg_margin": hist["avg_margin"],
            "avg_days": hist["days"],
            "avg_full": hist["full_window"],
        })
        if row.unpriced or row.error:
            view["any_unpriced"] = True
    conn.commit()
    return view


def _station_name(conn: sqlite3.Connection, location_id) -> str:
    if not location_id:
        return ""
    row = conn.execute(
        "SELECT name FROM location_name_cache WHERE location_id=?", (location_id,)
    ).fetchone()
    return row[0] if row and row[0] else str(location_id)


def _volume_column_present(conn: sqlite3.Connection) -> bool:
    """False on an SDE imported before `sde_types.volume` existed — the page
    hides profit-per-m³ rather than showing a column of dashes."""
    return "volume" in {r[1] for r in conn.execute("PRAGMA table_info(sde_types)")}


def _all_blueprints(conn: sqlite3.Connection) -> list:
    """Every character's blueprints, so intermediate components are costed at
    the ME you actually own rather than at 0.

    The tracked ME/TE belongs to the product's own blueprint; components fall
    back to whatever is in your hangars, matching what /plan does.
    """
    import json

    from app.character.blueprints import _parse_blueprints

    raw: list[dict] = []
    try:
        rows = conn.execute("SELECT data_json FROM char_blueprints_cache").fetchall()
    except sqlite3.OperationalError:
        return []
    for (blob,) in rows:
        try:
            entries = json.loads(blob)
        except (ValueError, TypeError):
            continue
        if isinstance(entries, list):
            raw.extend(e for e in entries if isinstance(e, dict) and "type_id" in e)
    try:
        return _parse_blueprints(raw)
    except KeyError:
        return []              # a cache entry missing required keys — skip the lot
