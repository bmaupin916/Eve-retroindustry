"""App-wide industry defaults — the build/reaction stations and job-cost inputs
that every profit calculation needs.

Before this existed, `/plan` took the station, taxes and ME bonus as per-request
form fields and nothing remembered them. The margin tracker needs a standing
answer to "where do you build?", because it prices a whole watchlist in the
background with nobody filling in a form. These are those answers: one row per
key, edited on the Settings page, read by the tracker and pre-filled into the
`/plan` form.

Deliberately a key/value table rather than typed columns — the set of defaults
will grow (price hub, skill levels, implants) and a migration per addition is
not worth it for single-user config.
"""
from __future__ import annotations

import sqlite3

# key → (default value, coercer). The coercer also validates: anything that
# fails to parse falls back to the default rather than raising, so a hand-edited
# DB row can't take the whole page down.
DEFAULTS: dict[str, tuple[object, type]] = {
    "build_station_id":      (0, int),      # 0 = none chosen yet
    "reaction_station_id":   (0, int),      # 0 = reactions run at the build station
    "facility_tax":          (2.5, float),  # %
    "reaction_facility_tax": (2.5, float),  # %
    "facility_me_bonus":     (0.0, float),  # % structure ME role bonus
    "reaction_me_bonus":     (0.0, float),
    "industry_skill":        (5, int),
    "adv_industry_skill":    (5, int),
    "input_basis":           ("sell", str), # "sell" = instant-buy, "buy" = place orders
    "price_hub":             ("jita", str), # only Jita for now; configurable later

    # ── Job splitting and slots ──────────────────────────────────────────
    # Longest a single job may run before it is split into several. 0 = never
    # split. Splitting raises material cost (ME rounds per job), so this feeds
    # the bill of materials, not just the schedule.
    "max_job_days":          (0.0, float),
    # Concurrent slots. 0 = unlimited, which reproduces the old "every job in a
    # level runs at once" estimate.
    "manufacturing_slots":   (0, int),
    "reaction_slots":        (0, int),
    # How many of the manufacturing slots can run capital components. A subset
    # of `manufacturing_slots`, never an addition to it: 20 manufacturing slots
    # with 10 capital-capable means at most 10 concurrent capital jobs out of
    # those 20 — not 30 slots.
    "capital_slots":         (0, int),

    # ── Selling costs ────────────────────────────────────────────────────
    # Between 4.4% and 10.5% of the sale price never reaches your wallet. See
    # app/market/taxes.py. Defaults are the *pessimistic* end — untrained
    # skills, no standings — so an unconfigured install understates profit
    # rather than overstating it. Every other default here follows the same
    # rule, because a tool that flatters you is worse than one that does not.
    #
    # "orders"    — you list a sell order: broker fee AND sales tax
    # "immediate" — you sell into existing buy orders: sales tax only
    "sales_method":          ("orders", str),
    "accounting_skill":      (0, int),      # −11% of the sales tax base per level
    "broker_relations_skill": (0, int),     # −0.3% broker fee per level
    # Where you list. NPC stations take skills and standings into account;
    # Upwell structures charge a flat SCC surcharge plus the owner's cut and
    # ignore skills entirely.
    "sell_venue":            ("npc", str),  # "npc" | "upwell"
    "faction_standing":      (0.0, float),  # −0.03% broker fee per point
    "corp_standing":         (0.0, float),  # −0.02% broker fee per point
    "structure_broker_pct":  (0.0, float),  # owner-set %, Upwell only
}


def ensure_defaults_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_defaults (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()


def get_defaults(conn: sqlite3.Connection) -> dict:
    """Every default, with stored values coerced and unset keys filled in."""
    ensure_defaults_table(conn)
    stored = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM app_defaults")}
    out: dict = {}
    for key, (fallback, cast) in DEFAULTS.items():
        raw = stored.get(key)
        if raw is None:
            out[key] = fallback
            continue
        try:
            out[key] = cast(raw)
        except (TypeError, ValueError):
            out[key] = fallback
    return out


def save_defaults(conn: sqlite3.Connection, values: dict) -> dict:
    """Writes the recognised keys and returns the resulting full set.

    Unknown keys are ignored rather than stored — this table is read back with
    `DEFAULTS` as the schema, so an unrecognised row would be dead weight.
    """
    ensure_defaults_table(conn)
    for key, raw in values.items():
        if key not in DEFAULTS:
            continue
        fallback, cast = DEFAULTS[key]
        try:
            coerced = cast(raw)
        except (TypeError, ValueError):
            coerced = fallback
        conn.execute(
            "INSERT INTO app_defaults (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(coerced)),
        )
    conn.commit()
    return get_defaults(conn)


def is_configured(defaults: dict) -> bool:
    """True once a build station is set — the one default with no sane fallback.

    Everything else has a usable default; without a station there is no system
    cost index and no structure bonuses, so a profit figure would be fiction.
    """
    return bool(defaults.get("build_station_id"))
