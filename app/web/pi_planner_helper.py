"""View model for the PI planner page (`/pi-planner`).

Assembles the output of `app/planetary/` into plain dicts a template can
render without doing any arithmetic of its own. The route parses query params,
calls `build_view_model`, and renders — nothing else.

Two things here are easy to get wrong and are worth stating up front:

* **The period quantity is resolved as one batch.** A blueprint target is
  resolved at the *whole period's* run count with `runs_per_job=None`, then
  divided down to a daily rate. ME is rounded per job in EVE, so resolving
  50,000 blocks and dividing gives the true rate, while resolving one run and
  multiplying inflates every material.
* **Section 2 and section 3 count different things, on purpose.** "PI demand
  by tier" is the full material pyramid, including the P1 and P0 a colony
  refines for itself. The colony plan counts only what has to be *supplied*
  (`external_demand`), so a P1 consumed inside a P2 extraction colony shows up
  in the tier table but never gets a colony of its own. The page says so.
"""
from __future__ import annotations

import datetime as dt
import sqlite3

from app.bom.resolver import BOMResolver
from app.planetary.colonies import DEFAULT_CCU_LEVEL, DEFAULT_DERATE, plan_colonies
from app.planetary.schematics import PIResolver, split_pi_leaves, whole_units

# period name → days. Month is 30 days, not a calendar month — this is a rate.
PERIODS: dict[str, int] = {"day": 1, "week": 7, "month": 30}
DEFAULT_PERIOD = "week"
DEFAULT_QUANTITY = 1000
DEFAULT_ME = 10          # fuel blocks and friends are planned at ME 10
CCU_LEVELS = (0, 1, 2, 3, 4, 5)

# PI is set-and-forget until a program runs dry, so 24h is the window that
# matters operationally — same threshold /planets uses for its "soon" state.
EXPIRY_WINDOW_SECS = 24 * 3600

TIER_LABELS: dict[int, str] = {
    4: "P4 — Advanced commodities",
    3: "P3 — Specialised commodities",
    2: "P2 — Refined commodities",
    1: "P1 — Processed materials",
    0: "P0 — Raw resources (extracted)",
}


# ── inputs ───────────────────────────────────────────────────────────────────

def parse_period(raw: str) -> str:
    return raw if raw in PERIODS else DEFAULT_PERIOD


def parse_quantity(raw: str) -> int:
    try:
        return max(1, int(float(raw)))
    except (TypeError, ValueError):
        return DEFAULT_QUANTITY


def parse_derate(raw: str) -> float:
    """Extraction derate as a fraction. Accepts "0.6" and "60" alike."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_DERATE
    if value > 1:                     # typed as a percentage
        value /= 100.0
    return min(1.0, max(0.05, value))


def parse_ccu_level(raw: str) -> int:
    try:
        level = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CCU_LEVEL
    return level if level in CCU_LEVELS else DEFAULT_CCU_LEVEL


def parse_me(raw: str) -> int:
    try:
        return min(10, max(0, int(raw)))
    except (TypeError, ValueError):
        return DEFAULT_ME


def target_choices(conn: sqlite3.Connection) -> list[str]:
    """Names for the type-ahead: PI commodities plus every blueprint product
    that consumes one.

    Both halves come from the SDE. The second is restricted to blueprints that
    actually list a PI commodity as a material — planning a target with no PI
    in its BOM would produce an empty page. Free text still works for anything
    else; this list is a convenience, not a whitelist.
    """
    rows = conn.execute("""
        SELECT t.name FROM sde_types t
        WHERE t.type_id IN (SELECT output_type_id FROM sde_planet_schematics)
        UNION
        SELECT DISTINCT t.name
        FROM sde_blueprint_products p
        JOIN sde_blueprint_materials m
          ON m.blueprint_type_id = p.blueprint_type_id AND m.activity = p.activity
        JOIN sde_types t ON t.type_id = p.product_type_id
        WHERE p.activity IN ('manufacturing', 'reaction')
          AND m.material_type_id IN (SELECT output_type_id FROM sde_planet_schematics)
        ORDER BY 1
    """).fetchall()
    return [r[0] for r in rows]


def _find_target(conn: sqlite3.Connection, target: str) -> sqlite3.Row | None:
    """Resolves the typed target to a type. Exact name first, then a prefix
    match, so "nitrogen fuel" finds the block without a full type-ahead."""
    text = target.strip()
    if not text:
        return None
    if text.isdigit():
        return conn.execute(
            "SELECT type_id, name FROM sde_types WHERE type_id=?", (int(text),)
        ).fetchone()
    row = conn.execute(
        "SELECT type_id, name FROM sde_types WHERE LOWER(name)=?", (text.lower(),)
    ).fetchone()
    if row:
        return row
    return conn.execute(
        "SELECT type_id, name FROM sde_types WHERE LOWER(name) LIKE ? ORDER BY LENGTH(name) LIMIT 1",
        (text.lower() + "%",),
    ).fetchone()


# ── output ───────────────────────────────────────────────────────────────────

def _quantity_row(type_id: int, name: str, per_day: float, period_days: int) -> dict:
    return {
        "type_id": type_id,
        "name": name,
        "per_day": per_day,
        "per_day_units": whole_units(per_day),
        "per_period": per_day * period_days,
        "per_period_units": whole_units(per_day * period_days),
    }


def _colony_row(req) -> dict:
    """Flattens a `ColonyRequirement` for the template."""
    return {
        "type_id": req.type_id,
        "name": req.name,
        "tier": req.tier,
        "tier_label": f"P{req.tier}",
        "demand_per_day": req.demand_per_day,
        "demand_units": whole_units(req.demand_per_day),
        "kind": req.kind,
        "layout_name": req.layout.name,
        "output_per_colony": req.output_per_colony,
        "output_units": whole_units(req.output_per_colony),
        "colonies": req.colonies,
        "facilities": req.facilities,
        "planet_types": req.planet_types,
        "factory_only": req.factory_only,
        "planet_restricted": req.planet_restricted,
        # An archetype that doesn't fit the chosen command centre is a real
        # answer ("you can't build this at CCU II"), not something to hide.
        "layout_fits": req.layout.fits(),
    }


# ── plan vs actual ───────────────────────────────────────────────────────────

def _pin_schematic_id(pin: dict) -> int | None:
    """The schematic a factory pin is running, or None for a non-factory pin.

    ESI carries it under `factory_details` and also repeats it at the top level
    of the pin; /planets reads the top-level copy. Read both, so either shape
    works and a payload that only has one of them still counts.
    """
    details = pin.get("factory_details") or {}
    return details.get("schematic_id") or pin.get("schematic_id")


def _remaining(expiry_iso: str, now: dt.datetime) -> tuple[str, int | None]:
    """Human countdown + seconds left. Mirrors the formatting on /planets."""
    try:
        end = dt.datetime.fromisoformat((expiry_iso or "").replace("Z", "+00:00"))
        secs = int((end - now).total_seconds())
    except (AttributeError, ValueError):
        return "", None
    if secs <= 0:
        return "Expired", secs
    days, rest = divmod(secs, 86400)
    hours, rest = divmod(rest, 3600)
    return ((f"{days}d " if days else "")
            + (f"{hours}h " if (days or hours) else "")
            + f"{rest // 60}m"), secs


def _schematic_outputs(conn: sqlite3.Connection, schematic_ids: set[int]) -> dict[int, int]:
    """schematic_id → output type_id, straight from the SDE."""
    if not schematic_ids:
        return {}
    placeholders = ",".join("?" * len(schematic_ids))
    return {r[0]: r[1] for r in conn.execute(
        f"SELECT schematic_id, output_type_id FROM sde_planet_schematics "
        f"WHERE schematic_id IN ({placeholders})", list(schematic_ids)
    ).fetchall()}


def _planet_names(conn: sqlite3.Connection, planet_ids: set[int]) -> dict[int, str]:
    """Names from the cache /planets already fills. Deliberately does not fetch:
    a planet this app has never rendered simply shows as its id."""
    if not planet_ids:
        return {}
    try:
        placeholders = ",".join("?" * len(planet_ids))
        return {r[0]: r[1] for r in conn.execute(
            f"SELECT planet_id, name FROM planet_name_cache "
            f"WHERE planet_id IN ({placeholders})", list(planet_ids)
        ).fetchall() if r[1]}
    except sqlite3.OperationalError:
        return {}          # cache table not created yet (never visited /planets)


def _type_names(conn: sqlite3.Connection, type_ids: set[int]) -> dict[int, str]:
    """Batch name lookup — one query instead of one per extractor."""
    ids = {t for t in type_ids if t}
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    return {r[0]: r[1] for r in conn.execute(
        f"SELECT type_id, name FROM sde_types WHERE type_id IN ({placeholders})", list(ids)
    ).fetchall()}


def build_plan_vs_actual(
    conn: sqlite3.Connection,
    results: list,
    char_names: dict[int, str],
    colonies_plan: list[dict],
    now: dt.datetime | None = None,
    planet_names: dict[int, str] | None = None,
) -> dict:
    """Cross-references the colony plan against live colonies.

    `results` is what `main._fetch_pi_colonies` returns — the same payload
    /planets renders, so this adds no ESI calls and no second cache.

    What a colony "produces" comes from its factory pins:
    `factory_details.schematic_id` → `sde_planet_schematics.output_type_id`.
    A P2 extraction colony therefore reports its P1 as well as its P2, which is
    correct — those P1 really are being made there.

    Extractor expiry is reported only for colonies the plan depends on, i.e.
    colonies producing at least one planned product. A colony that exports raw
    P0 has no factory pins and cannot be matched this way; that is a known
    limit, not an oversight — nothing in the pin data says which colony a P0
    shipment ends up feeding.

    `planet_names` comes from the caller (which owns the cached resolver). When
    omitted, names are read from the cache without fetching.

    Also returns `cache_entries` / `ok_char_ids` so the caller can refresh the
    shared PI extractor cache. That payload deliberately covers **every**
    extractor on every colony, not just the plan-relevant ones the page shows:
    the store replaces a character's rows wholesale, so handing it the filtered
    list would silently drop the rest from the dashboard tile and nav badge.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    planned = {row["type_id"]: row for row in colonies_plan}

    needs_relogin: list[str] = []
    ok_char_ids: list[int] = []
    fetched_any = False

    # Pass 1 — flatten ESI into one colony record per planet.
    raw_colonies: list[dict] = []
    schematic_ids: set[int] = set()
    planet_ids: set[int] = set()
    for char_id, result in results:
        name = char_names.get(char_id, str(char_id))
        if result == "forbidden":
            needs_relogin.append(name)
            continue
        if not result or isinstance(result, str):
            continue
        fetched_any = True
        ok_char_ids.append(char_id)
        colonies, details = result
        by_planet = {c["planet_id"]: (d if isinstance(d, dict) else None)
                     for c, d in zip(colonies, details)}
        for colony in colonies:
            planet_id = colony["planet_id"]
            planet_ids.add(planet_id)
            detail = by_planet.get(planet_id)
            facilities: dict[int, int] = {}      # schematic_id → factory pin count
            extractors: list[dict] = []
            for pin in ((detail or {}).get("pins") or []):
                schematic_id = _pin_schematic_id(pin)
                if schematic_id:
                    schematic_ids.add(schematic_id)
                    facilities[schematic_id] = facilities.get(schematic_id, 0) + 1
                extractor = pin.get("extractor_details")
                if extractor:
                    expiry = pin.get("expiry_time") or ""
                    text, secs = _remaining(expiry, now)
                    extractors.append({
                        "product_id": extractor.get("product_type_id"),
                        "expiry_iso": expiry,
                        "remaining": text,
                        "secs": secs,
                    })
            raw_colonies.append({
                "char_id": char_id, "char_name": name,
                "planet_id": planet_id,
                "facilities": facilities, "extractors": extractors,
            })

    outputs = _schematic_outputs(conn, schematic_ids)
    names = dict(_planet_names(conn, planet_ids))
    names.update({k: v for k, v in (planet_names or {}).items() if v})
    product_names = _type_names(conn, {
        e["product_id"] for c in raw_colonies for e in c["extractors"]
    })

    # Payload for the shared PI extractor cache: every extractor with an
    # expiry, unfiltered — see the docstring.
    cache_entries = [{
        "char_id": colony["char_id"], "char_name": colony["char_name"],
        "planet_id": colony["planet_id"],
        "planet_name": names.get(colony["planet_id"], f"Planet #{colony['planet_id']}"),
        "product_id": extractor["product_id"],
        "product": product_names.get(extractor["product_id"], "—"),
        "expiry_iso": extractor["expiry_iso"],
    } for colony in raw_colonies for extractor in colony["extractors"]
        if extractor["expiry_iso"] and extractor["product_id"]]

    # Pass 2 — what each colony makes, and whether the plan depends on it.
    have: dict[int, int] = {}                    # product → colonies making it
    have_facilities: dict[int, int] = {}         # product → factory pins making it
    sources: dict[int, list[str]] = {}           # product → where
    expiring: list[dict] = []
    matched = 0
    for colony in raw_colonies:
        produced: dict[int, int] = {}
        for schematic_id, count in colony["facilities"].items():
            type_id = outputs.get(schematic_id)
            if type_id:
                produced[type_id] = produced.get(type_id, 0) + count
        planet_name = names.get(colony["planet_id"], f"Planet #{colony['planet_id']}")
        relevant = [t for t in produced if t in planned]
        for type_id, count in produced.items():
            have[type_id] = have.get(type_id, 0) + 1
            have_facilities[type_id] = have_facilities.get(type_id, 0) + count
            sources.setdefault(type_id, []).append(f"{colony['char_name']} — {planet_name}")
        if not relevant:
            continue                             # the plan doesn't lean on this colony
        matched += 1
        for extractor in colony["extractors"]:
            secs = extractor["secs"]
            if secs is None or secs >= EXPIRY_WINDOW_SECS:
                continue
            expiring.append({
                "char_name": colony["char_name"],
                "planet_name": planet_name,
                "product": product_names.get(extractor["product_id"], "—"),
                "expiry_iso": extractor["expiry_iso"],
                "remaining": extractor["remaining"],
                "expired": secs <= 0,
                "for_products": sorted(planned[t]["name"] for t in relevant),
            })
    expiring.sort(key=lambda e: e["expiry_iso"] or "9999")

    rows = []
    for row in colonies_plan:
        type_id = row["type_id"]
        needed, owned = row["colonies"], have.get(type_id, 0)
        rows.append({
            "type_id": type_id,
            "name": row["name"],
            "tier_label": row["tier_label"],
            "needed": needed,
            "have": owned,
            "gap": max(0, needed - owned),
            "surplus": max(0, owned - needed),
            "facilities": have_facilities.get(type_id, 0),
            "sources": sources.get(type_id, []),
        })

    return {
        "rows": rows,
        "expiring": expiring,
        "needs_relogin": needs_relogin,
        "fetched_any": fetched_any,
        "cache_entries": cache_entries,
        "ok_char_ids": ok_char_ids,
        "total_gap": sum(r["gap"] for r in rows),
        "total_have": sum(r["have"] for r in rows),
        "total_needed": sum(r["needed"] for r in rows),
        "matched_colonies": matched,
        "unplanned": sorted(
            _type_names(conn, {t for t in have if t not in planned}).values()
        ),
    }


def build_view_model(
    db_path: str,
    target: str = "",
    quantity: int = DEFAULT_QUANTITY,
    period: str = DEFAULT_PERIOD,
    derate: float = DEFAULT_DERATE,
    ccu_level: int = DEFAULT_CCU_LEVEL,
    me: int = DEFAULT_ME,
) -> dict:
    """Builds the whole page. Never raises for bad user input — an unresolvable
    target comes back as `error` with the form intact."""
    period = parse_period(period)
    period_days = PERIODS[period]

    view: dict = {
        "form": {
            "target": target, "quantity": quantity, "period": period,
            "derate": derate, "derate_pct": round(derate * 100),
            "ccu_level": ccu_level, "me": me,
        },
        "periods": list(PERIODS),
        "ccu_levels": list(CCU_LEVELS),
        "target_choices": [],
        "show_me": True,
        "error": None,
        "result": None,
        # Filled in by the route once there is a plan to compare against —
        # the ESI half is the route's job, everything else here is pure.
        "actual": None,
        "signed_in": False,
    }

    pi = PIResolver(db_path)
    bom: BOMResolver | None = None
    try:
        view["target_choices"] = target_choices(pi.conn)
        if not target.strip():
            return view

        row = _find_target(pi.conn, target)
        if row is None:
            view["error"] = f"No type named “{target}”."
            return view

        type_id, name = row["type_id"], row["name"]
        from app.db.conn import connect_to_path
        sde = connect_to_path(db_path)                 # for the converted BOMResolver
        bom = BOMResolver(sde, runs_per_job=None)
        blueprint = bom.find_blueprint(type_id)
        is_pi = pi.is_pi_commodity(type_id)
        view["show_me"] = blueprint is not None

        if blueprint is None and not is_pi:
            view["error"] = (
                f"{name} is neither a manufactured item nor a PI commodity — "
                "nothing to plan. Raw P0 resources are extracted, not produced."
            )
            return view

        manufacturing = None
        if blueprint is not None:
            # One batched job over the whole period, then divided down: ME is
            # rounded per job, so this is the only way to get an exact rate.
            leaves = bom.resolve(type_id, quantity, me=me).aggregate_leaves()
            pi_leaves, other_leaves = split_pi_leaves(leaves, pi)
            if not pi_leaves:
                view["error"] = (
                    f"{name} has no planetary commodities in its bill of "
                    "materials — there is nothing for the PI planner to do."
                )
                return view
            demands = {t: q / period_days for t, (_n, q) in pi_leaves.items()}
            manufacturing = {
                "me": me,
                "pi": sorted(
                    (_quantity_row(t, n, q / period_days, period_days)
                     for t, (n, q) in pi_leaves.items()),
                    key=lambda r: -r["per_day"],
                ),
                "other": sorted(
                    (_quantity_row(t, n, q / period_days, period_days)
                     for t, (n, q) in other_leaves.items()),
                    key=lambda r: -r["per_day"],
                ),
            }
        else:
            demands = {type_id: quantity / period_days}

        roots = pi.resolve_many(demands)

        tiers = []
        by_tier = pi.aggregate_many_by_tier(roots)
        for tier in (4, 3, 2, 1, 0):
            per_type = by_tier.get(tier)
            if not per_type:
                continue
            tiers.append({
                "tier": tier,
                "label": TIER_LABELS[tier],
                "rows": sorted(
                    (_quantity_row(t, pi.get_type_name(t), q, period_days)
                     for t, q in per_type.items()),
                    key=lambda r: -r["per_day"],
                ),
            })

        colonies = [_colony_row(r)
                    for r in plan_colonies(pi, roots, derate=derate, ccu_level=ccu_level)]

        view["result"] = {
            "type_id": type_id,
            "name": name,
            "is_manufactured": blueprint is not None,
            "period": period,
            "period_days": period_days,
            "quantity": quantity,
            "manufacturing": manufacturing,
            "tiers": tiers,
            "colonies": colonies,
            "total_colonies": sum(c["colonies"] for c in colonies),
            "factory_only": [c["name"] for c in colonies if c["factory_only"]],
            "needs_p4_planet": any(c["planet_restricted"] for c in colonies),
            "unbuildable": [c["name"] for c in colonies if not c["layout_fits"]],
        }
        return view
    finally:
        if bom is not None:
            sde.close()
        pi.close()
