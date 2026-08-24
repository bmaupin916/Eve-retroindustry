"""Helpers for computing EVE Online manufacturing fees."""
from __future__ import annotations
import time
import httpx
from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection
from app.esi.client import esi_client
# Authoritative rig group_id → affected product group_ids, generated from EVE Ref
# reference-data (see scripts/build_rig_affected_groups.py). Replaces the old
# name-based product classification, which produced ~74 false positives.
from app.web.rig_affected_groups import RIG_AFFECTED_GROUPS
from app.db.schema import ensure_schema as ensure_db_schema

ESI_BASE = "https://esi.evetech.net/latest"

# SCC Surcharge — raised on 2024-02-01 from 1.5 % to 4.0 % (third increase since Viridian 2023)
_SCC = 0.04


def ensure_industry_tables(conn: Connection) -> None:
    """Schema shim. The tables live in app/db/schema.py; this only guarantees
    they exist, and only on SQLite.

    The lazy create predates migrations and is SQLite-only by construction:
    `app/db/schema.py` memoises by asking `PRAGMA database_list`, which is a
    syntax error on Postgres. There the schema arrives through Alembic, so this
    is "nothing to do" rather than "create if missing".
    """
    if conn.engine.dialect.name != "sqlite":
        return
    ensure_db_schema(conn.connection.driver_connection)


def rig_applies_to_product(
    conn: Connection,
    rig_type_id: int,
    product_type_id: int,
) -> bool:
    """Return True if the given rig provides a bonus to manufacturing the given product.

    Looks the product's group up in the rig's authoritative affected-groups set
    (RIG_AFFECTED_GROUPS, generated from EVE Ref). Unknown combination → False
    (safe: a slight underestimate of savings beats a false overestimate).
    """
    rig_group_row = conn.execute(
        text("SELECT group_id FROM sde_types WHERE type_id=:type_id"),
        {"type_id": rig_type_id},
    ).fetchone()
    prod_row = conn.execute(
        text("SELECT group_id FROM sde_types WHERE type_id=:type_id"),
        {"type_id": product_type_id},
    ).fetchone()
    if not rig_group_row or not prod_row:
        return False
    affected = RIG_AFFECTED_GROUPS.get(rig_group_row[0])
    return bool(affected) and prod_row[0] in affected


# Group ID → (set_size, category) for Standup structure rigs
_RIG_GROUP_MAP: dict[int, tuple[str, str]] = {
    **{gid: ("M", "manufacturing") for gid in [
        1816, 1819, 1820, 1821, 1822, 1823, 1824, 1825,
        1826, 1827, 1828, 1829, 1830, 1831, 1832, 1833,
        1834, 1835, 1836, 1837, 1838, 1839, 1840, 1841,
    ]},
    **{gid: ("L", "manufacturing") for gid in [
        1850, 1851, 1852, 1853, 1854, 1855, 1856, 1857,
        1858, 1859, 1860, 1861, 1862,
    ]},
    **{gid: ("XL", "manufacturing") for gid in [1867, 1868, 1869]},
    **{gid: ("M", "reaction") for gid in [1933, 1934, 1935, 1936, 1937, 1938]},
    1939: ("L", "reaction"),
}

# Structure type → (set_size, category)
STRUCTURE_TYPE_MAP: dict[str, tuple[str, str]] = {
    "raitaru": ("M",  "manufacturing"),
    "azbel":   ("L",  "manufacturing"),
    "sotiyo":  ("XL", "manufacturing"),
    "athanor": ("M",  "reaction"),
    "tatara":  ("L",  "reaction"),
}

# Refineries run reactions — their rig bonuses scale with a DIFFERENT
# security table (lowsec ×1.0, null/WH ×1.1) than manufacturing rigs
# (lowsec ×1.9, null/WH ×2.1). Verified against EVE Ref API.
_RXN_STRUCTURE_TYPES = frozenset({"athanor", "tatara"})

# Structure type → TE bonus (% reduction of job time)
STRUCTURE_TE_BONUS: dict[str, float] = {
    "raitaru": 15.0,
    "azbel":   20.0,
    "sotiyo":  30.0,
    "athanor":  0.0,
    "tatara":  25.0,
}

# Structure type → base ME bonus (%) — engineering complexes give 1% ME, refineries 0%
STRUCTURE_ME_BONUS: dict[str, float] = {
    "raitaru": 1.0,
    "azbel":   1.0,
    "sotiyo":  1.0,
    "athanor": 0.0,
    "tatara":  0.0,
}

# Structure type → job-installation cost bonus (fraction; reduces SCI portion).
# Engineering complex role bonus: Raitaru 3%, Azbel 4%, Sotiyo 5%.
# Refineries have no SCI cost bonus.
STRUCTURE_COST_BONUS: dict[str, float] = {
    "raitaru": 0.03,
    "azbel":   0.04,
    "sotiyo":  0.05,
    "athanor": 0.0,
    "tatara":  0.0,
}


def get_station_cost_bonus(conn: Connection, location_id: int) -> float:
    """Return SCI cost reduction fraction (e.g. 0.03 for Raitaru, 0.0 for NPC)."""
    ensure_industry_tables(conn)
    row = conn.execute(
        text("SELECT structure_type FROM station_rigs WHERE location_id=:loc"),
        {"loc": location_id},
    ).fetchone()
    if not row or not row[0]:
        return 0.0
    return STRUCTURE_COST_BONUS.get(row[0], 0.0)


def populate_rig_bonuses(conn: Connection) -> None:
    """Populate rig_bonuses from local SDE. No-op if already populated."""
    if conn.execute(text("SELECT COUNT(*) FROM rig_bonuses")).fetchone()[0] > 0:
        return

    group_ids = list(_RIG_GROUP_MAP.keys())
    rows = conn.execute(
        text("SELECT type_id, name, group_id FROM sde_types"
             " WHERE group_id IN :group_ids AND published=1").bindparams(
                 bindparam("group_ids", expanding=True)),
        {"group_ids": group_ids},
    ).fetchall()

    entries = []
    for type_id, name, group_id in rows:
        if "Standup" not in name:
            continue  # skip non-rig items accidentally in these groups

        set_size, category = _RIG_GROUP_MAP[group_id]
        n = name.lower()
        is_t2 = name.endswith(" II")
        is_thukker = "thukker" in n
        enhanced = is_t2 or is_thukker

        me_base = 2.4 if enhanced else 2.0
        te_base = 24.0 if enhanced else 20.0

        has_mat = "material efficiency" in n
        has_time = "time efficiency" in n
        has_both = "efficiency" in n and not has_mat and not has_time

        me_bonus = me_base if (has_mat or has_both) else 0.0
        te_bonus = te_base if (has_time or has_both) else 0.0

        entries.append({"type_id": type_id, "name": name, "set_size": set_size,
                        "category": category, "me_bonus": me_bonus,
                        "te_bonus": te_bonus})

    # Guarded because an empty list is not a no-op here the way it was for
    # `executemany`: SQLAlchemy has no rows to infer the statement's parameter
    # shape from and raises instead of doing nothing.
    if entries:
        conn.execute(
            text("INSERT INTO rig_bonuses"
                 " (type_id, name, set_size, category, me_bonus, te_bonus)"
                 " VALUES (:type_id, :name, :set_size, :category, :me_bonus, :te_bonus)"
                 " ON CONFLICT (type_id) DO UPDATE SET name=excluded.name,"
                 " set_size=excluded.set_size, category=excluded.category,"
                 " me_bonus=excluded.me_bonus, te_bonus=excluded.te_bonus"),
            entries,
        )
    conn.commit()


def get_rig_types(conn: Connection, structure_type: str) -> list[dict]:
    """Return available rigs for the given structure type (raitaru/azbel/etc)."""
    mapping = STRUCTURE_TYPE_MAP.get(structure_type)
    if not mapping:
        return []
    set_size, category = mapping
    rows = conn.execute(
        text("SELECT type_id, name, me_bonus, te_bonus FROM rig_bonuses"
             " WHERE set_size=:set_size AND category=:category ORDER BY name"),
        {"set_size": set_size, "category": category},
    ).fetchall()
    return [{"type_id": r[0], "name": r[1], "me_bonus": r[2], "te_bonus": r[3]} for r in rows]


def save_station_rigs_full(
    conn: Connection,
    location_id: int,
    structure_type: str | None,
    rig1_type_id: int | None,
    rig2_type_id: int | None,
    rig3_type_id: int | None,
) -> float:
    """Save rig configuration for a station and return the computed ME bonus (%)."""
    rig_ids = [r for r in [rig1_type_id, rig2_type_id, rig3_type_id] if r]
    me_bonus = STRUCTURE_ME_BONUS.get(structure_type or "", 0.0)
    if rig_ids:
        # Look each distinct rig up once, then sum over the slots. Nobody fits
        # the same rig twice, so the two are the same total in practice; the
        # distinct lookup is just one query instead of three.
        unique_ids = list(set(rig_ids))
        bonus_map = {r[0]: r[1] for r in conn.execute(
            text("SELECT type_id, me_bonus FROM rig_bonuses"
                 " WHERE type_id IN :ids").bindparams(
                     bindparam("ids", expanding=True)),
            {"ids": unique_ids},
        ).fetchall()}
        me_bonus += sum(bonus_map.get(rid, 0.0) for rid in rig_ids)

    conn.execute(
        text("""INSERT INTO station_rigs
           (location_id, me_bonus_pct, updated_at, structure_type, rig1_type_id, rig2_type_id, rig3_type_id)
           VALUES (:loc, :me_bonus, :updated_at, :structure_type, :rig1, :rig2, :rig3)
           ON CONFLICT (location_id) DO UPDATE SET me_bonus_pct=excluded.me_bonus_pct,
           updated_at=excluded.updated_at, structure_type=excluded.structure_type,
           rig1_type_id=excluded.rig1_type_id, rig2_type_id=excluded.rig2_type_id,
           rig3_type_id=excluded.rig3_type_id"""),
        {"loc": location_id, "me_bonus": me_bonus, "updated_at": int(time.time()),
         "structure_type": structure_type or None, "rig1": rig1_type_id or None,
         "rig2": rig2_type_id or None, "rig3": rig3_type_id or None},
    )
    conn.commit()
    return me_bonus


def get_station_rigs_full(conn: Connection, location_id: int) -> dict:
    """Return rig configuration for a station."""
    row = conn.execute(
        text("SELECT me_bonus_pct, structure_type, rig1_type_id, rig2_type_id,"
             " rig3_type_id FROM station_rigs WHERE location_id=:loc"),
        {"loc": location_id},
    ).fetchone()
    if not row:
        return {"me_bonus_pct": 0.0, "structure_type": None, "rigs": [None, None, None]}
    return {
        "me_bonus_pct": float(row[0] or 0.0),
        "structure_type": row[1],
        "rigs": [row[2], row[3], row[4]],
    }


def get_station_te_multiplier(conn: Connection, location_id: int) -> float:
    """[DEPRECATED for calculation] Return the station's "global" TE multiplier — applies
    all rigs regardless of product category. Used only for the summary
    display % in the header (where we don't have a specific product anyway). For per-job
    calculation use `get_product_te_multiplier(...)`.
    """
    from app.web.location_resolver import get_station_security_multiplier

    ensure_industry_tables(conn)
    row = conn.execute(
        text("SELECT structure_type, rig1_type_id, rig2_type_id, rig3_type_id"
             " FROM station_rigs WHERE location_id=:loc"),
        {"loc": location_id},
    ).fetchone()
    if not row:
        return 1.0

    structure_type = row[0] or ""
    structure_te_pct = STRUCTURE_TE_BONUS.get(structure_type, 0.0)
    multiplier = 1.0 - structure_te_pct / 100

    rig_ids = [r for r in [row[1], row[2], row[3]] if r]
    if rig_ids:
        sec_mult = get_station_security_multiplier(
            conn, location_id, structure_type in _RXN_STRUCTURE_TYPES
        )
        unique_ids = list(set(rig_ids))
        rig_te_map = {r[0]: r[1] for r in conn.execute(
            text("SELECT type_id, te_bonus FROM rig_bonuses"
                 " WHERE type_id IN :ids").bindparams(
                     bindparam("ids", expanding=True)),
            {"ids": unique_ids},
        ).fetchall()}
        for rid in rig_ids:
            te_b = rig_te_map.get(rid, 0.0) * sec_mult / 100
            multiplier *= (1.0 - te_b)

    return max(0.01, multiplier)  # never negative


def get_product_te_multiplier(conn: Connection, facility, product_type_id: int) -> float:
    """Per-product TE multiplier — applies only rigs relevant to the product's
    category (an Equipment TE rig does not speed up ship construction, etc.).

    facility: StationFacility z app.bom.resolver (passed in to avoid circular import).
    """
    multiplier = 1.0 - facility.structure_te_pct / 100
    for rig_id, _me_b, te_b in facility.rigs:
        if te_b <= 0:
            continue
        if rig_applies_to_product(conn, rig_id, product_type_id):
            multiplier *= 1.0 - te_b * facility.sec_multiplier / 100
    return max(0.01, multiplier)


def get_station_facility(conn: Connection, location_id: int):
    """Return the StationFacility for the given location_id — structure role bonus,
    rig list (with ME/TE bonuses), and security multiplier.
    For NPC stations / unknown structures it returns an empty facility (1.0 multiplier).
    """
    from app.bom.resolver import StationFacility
    from app.web.location_resolver import get_station_security_multiplier

    ensure_industry_tables(conn)
    row = conn.execute(
        text("SELECT structure_type, rig1_type_id, rig2_type_id, rig3_type_id"
             " FROM station_rigs WHERE location_id=:loc"),
        {"loc": location_id},
    ).fetchone()
    if not row:
        return StationFacility()

    structure_type = row[0] or ""
    structure_pct = STRUCTURE_ME_BONUS.get(structure_type, 0.0)
    structure_te_pct = STRUCTURE_TE_BONUS.get(structure_type, 0.0)
    rig_ids = [r for r in [row[1], row[2], row[3]] if r]
    rigs: list[tuple[int, float, float]] = []
    if rig_ids:
        unique_ids = list(set(rig_ids))
        rig_map = {r[0]: (r[1], r[2]) for r in conn.execute(
            text("SELECT type_id, me_bonus, te_bonus FROM rig_bonuses"
                 " WHERE type_id IN :ids").bindparams(
                     bindparam("ids", expanding=True)),
            {"ids": unique_ids},
        ).fetchall()}
        for rid in rig_ids:
            me_b, te_b = rig_map.get(rid, (0.0, 0.0))
            rigs.append((rid, me_b, te_b))

    sec_mult = get_station_security_multiplier(
        conn, location_id, structure_type in _RXN_STRUCTURE_TYPES
    )
    return StationFacility(
        structure_pct=structure_pct,
        structure_te_pct=structure_te_pct,
        rigs=tuple(rigs),
        sec_multiplier=sec_mult,
    )


def get_station_me_multiplier(conn: Connection, location_id: int) -> float:
    """Return the station's combined ME multiplier (e.g. 0.87 = 13 % savings).

    Bonuses stack multiplicatively (per Fenris Creations):
        m = (1 − struct_role/100) × (1 − rig1×sec/100) × (1 − rig2×sec/100) × …
    where struct_role is 1 % for engineering complexes and 0 % for refineries,
    and sec is 1.0 / 1.9 / 2.1 depending on the system's security status.
    """
    from app.web.location_resolver import get_station_security_multiplier

    ensure_industry_tables(conn)
    row = conn.execute(
        text("SELECT structure_type, rig1_type_id, rig2_type_id, rig3_type_id"
             " FROM station_rigs WHERE location_id=:loc"),
        {"loc": location_id},
    ).fetchone()
    if not row:
        return 1.0

    structure_type = row[0] or ""
    structure_pct = STRUCTURE_ME_BONUS.get(structure_type, 0.0)
    multiplier = 1.0 - structure_pct / 100

    rig_ids = [r for r in [row[1], row[2], row[3]] if r]
    if rig_ids:
        sec_mult = get_station_security_multiplier(
            conn, location_id, structure_type in _RXN_STRUCTURE_TYPES
        )
        unique_ids = list(set(rig_ids))
        rig_me_map = {r[0]: r[1] for r in conn.execute(
            text("SELECT type_id, me_bonus FROM rig_bonuses"
                 " WHERE type_id IN :ids").bindparams(
                     bindparam("ids", expanding=True)),
            {"ids": unique_ids},
        ).fetchall()}
        for rid in rig_ids:
            me_b = rig_me_map.get(rid, 0.0) * sec_mult / 100
            multiplier *= (1.0 - me_b)

    return max(0.01, multiplier)


def get_station_me_bonus_pct(conn: Connection, location_id: int) -> float:
    """Effective ME savings as a percentage for the UI: (1 - multiplier) × 100.

    I.e. the multiplicatively combined savings, not the arithmetic sum of bonuses.
    """
    return round((1.0 - get_station_me_multiplier(conn, location_id)) * 100, 4)


def get_station_me_bonus(conn: Connection, location_id: int) -> float:
    """Return the stored ME bonus (%) for the given station/structure, or 0.0."""
    ensure_industry_tables(conn)
    row = conn.execute(
        text("SELECT me_bonus_pct FROM station_rigs WHERE location_id=:loc"),
        {"loc": location_id},
    ).fetchone()
    return float(row[0]) if row else 0.0


def save_station_me_bonus(conn: Connection, location_id: int, me_bonus_pct: float):
    ensure_industry_tables(conn)
    conn.execute(
        text("INSERT INTO station_rigs (location_id, me_bonus_pct, updated_at)"
             " VALUES (:loc, :me_bonus_pct, :updated_at)"
             " ON CONFLICT (location_id) DO UPDATE SET"
             " me_bonus_pct=excluded.me_bonus_pct, updated_at=excluded.updated_at"),
        {"loc": location_id, "me_bonus_pct": max(0.0, min(25.0, me_bonus_pct)),
         "updated_at": int(time.time())},
    )
    conn.commit()


def _adj_is_fresh(conn: Connection) -> bool:
    row = conn.execute(
        text("SELECT MIN(cached_at) FROM adjusted_price_cache")).fetchone()
    if not row or row[0] is None:
        return False
    return (time.time() - row[0]) < 86400  # 24 h


def _sci_is_fresh(conn: Connection, solar_system_id: int, activity: str) -> bool:
    row = conn.execute(
        text("SELECT cached_at FROM sci_cache"
             " WHERE solar_system_id=:sys AND activity=:activity"),
        {"sys": solar_system_id, "activity": activity},
    ).fetchone()
    if not row:
        return False
    return (time.time() - row[0]) < 3600  # 1 h


def get_adjusted_prices_cached(conn: Connection) -> dict[int, float]:
    """Cache-only sibling of `get_adjusted_prices` — never fetches.

    The margin tracker prices a whole watchlist on every page load; going to
    ESI for adjusted prices there would turn one page view into a market-wide
    refresh. Returns whatever is cached, stale or empty included, and lets the
    caller decide what to say about it.
    """
    ensure_industry_tables(conn)
    return {r[0]: r[1] for r in conn.execute(
        text("SELECT type_id, adjusted FROM adjusted_price_cache")).fetchall()}


async def get_adjusted_prices(conn: Connection) -> dict[int, float]:
    """Return {type_id: adjusted_price} from cache or ESI (GET /markets/prices/)."""
    ensure_industry_tables(conn)
    if _adj_is_fresh(conn):
        rows = conn.execute(
            text("SELECT type_id, adjusted FROM adjusted_price_cache")).fetchall()
        return {r[0]: r[1] for r in rows}
    try:
        async with esi_client() as client:
            r = await client.get(
                f"{ESI_BASE}/markets/prices/",
                params={"datasource": "tranquility"},
                timeout=30,
            )
        if r.status_code == 200:
            now = int(time.time())
            entries = [
                {"type_id": item["type_id"], "adjusted": item["adjusted_price"],
                 "cached_at": now}
                for item in r.json()
                if item.get("adjusted_price") is not None
            ]
            if entries:
                conn.execute(
                    text("INSERT INTO adjusted_price_cache"
                         " (type_id, adjusted, cached_at)"
                         " VALUES (:type_id, :adjusted, :cached_at)"
                         " ON CONFLICT (type_id) DO UPDATE SET"
                         " adjusted=excluded.adjusted, cached_at=excluded.cached_at"),
                    entries,
                )
            conn.commit()
            return {e["type_id"]: e["adjusted"] for e in entries}
    except Exception:
        pass
    # Return stale cache if ESI fails
    rows = conn.execute(
        text("SELECT type_id, adjusted FROM adjusted_price_cache")).fetchall()
    return {r[0]: r[1] for r in rows}


async def get_sci_for_system(
    conn: Connection,
    solar_system_id: int,
    activity: str,
) -> float:
    """
    Return the System Cost Index for the given system and activity.
    On a missing/expired cache it fetches the entire GET /industry/systems/ endpoint
    and stores all values at once.
    """
    ensure_industry_tables(conn)
    if _sci_is_fresh(conn, solar_system_id, activity):
        row = conn.execute(
            text("SELECT cost_index FROM sci_cache"
                 " WHERE solar_system_id=:sys AND activity=:activity"),
            {"sys": solar_system_id, "activity": activity},
        ).fetchone()
        if row:
            return row[0]
    try:
        async with esi_client() as client:
            r = await client.get(
                f"{ESI_BASE}/industry/systems/",
                params={"datasource": "tranquility"},
                timeout=30,
            )
        if r.status_code == 200:
            now = int(time.time())
            entries = [
                {"sys": sys["solar_system_id"], "activity": idx["activity"],
                 "cost_index": idx["cost_index"], "cached_at": now}
                for sys in r.json()
                for idx in sys.get("cost_indices", [])
            ]
            if entries:
                conn.execute(
                    text("INSERT INTO sci_cache"
                         " (solar_system_id, activity, cost_index, cached_at)"
                         " VALUES (:sys, :activity, :cost_index, :cached_at)"
                         " ON CONFLICT (solar_system_id, activity) DO UPDATE SET"
                         " cost_index=excluded.cost_index,"
                         " cached_at=excluded.cached_at"),
                    entries,
                )
            conn.commit()
    except Exception:
        pass
    row = conn.execute(
        text("SELECT cost_index FROM sci_cache"
             " WHERE solar_system_id=:sys AND activity=:activity"),
        {"sys": solar_system_id, "activity": activity},
    ).fetchone()
    return row[0] if row else 0.0


