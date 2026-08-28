"""The manufacturing plan: the form, the result, and the machinery under it.

Moved out of `main.py` unchanged (W6), and the largest single move of the
split. `plan_result` is the biggest handler in the app, and everything it
leans on comes with it: resolving a product name locally, rolling stock up per
station, deriving job splits against slot limits, scheduling the steps, and
flattening the result into a template context.

The implant tables and the science-skill multiplier are here rather than in
`deps.py` because nothing else uses them — `_science_skill_mult` in particular
memoizes on its own function object, so a second importer would be a second
cache.
"""
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from app.bom.resolver import BOMResolver
from app.cache.blueprint_cache import resolve_type
from app.character.assets import load_cached_assets
from app.character.blueprints import load_cached_blueprints
from app.character.skills import get_cached_skills
from app.db.database import get_session
from app.esi.client import esi_client, esi_error_message, search_type_by_name
from app.manufacturing.margins import build_invention_params
from app.manufacturing.planner import (
    MFG_IMPLANTS,
    MFG_IMPLANT_PCTS,
    build_plan,
    calc_job_time,
    find_blueprint_for_product,
    format_duration,
)
from app.market.prices import get_cached_station_volumes
from app.market.taxes import selling_costs
from app.web import app_defaults
from app.db.location import database_path
from app.web.deps import (
    character_row,
    _load_assets_from_cache,
    _valid_token_async,
    _tr,
    get_active_character_id,
    get_conn,
)
from app.db.conn import connect as _connect
from app.web.industry_helper import (
    _SCC,
    get_adjusted_prices,
    get_product_te_multiplier,
    get_sci_for_system,
    get_station_cost_bonus,
    get_station_facility,
    get_station_me_multiplier,
    get_station_te_multiplier,
)
from app.web.location_resolver import (
    load_location_names_from_db,
    resolve_station_names_bulk,
)
from app.web.prices_helper import get_prices_for_ids

router = APIRouter()


def _science_skill_mult(
    conn: Connection,
    bp_type_id: int,
    activity: str,
    skills: dict[int, int],
    preloaded: list[tuple[int, int]] | None = None,
) -> tuple[float, list[tuple[str, int, float, int]]]:
    """Return (multiplier, [(skill_name, char_level, bonus_pct, required_level), ...]).

    Each required skill with a time bonus contributes (1 - level * bonus_pct/100).
    Industry and AdvIndustry are handled separately — we skip them here.

    `preloaded`: [(skill_id, required_level), …] from the bulk fetch in plan_result.
    If passed, we avoid per-bp DB queries — we only look up names and bonus_pct
    from (process-level cached) lookup tables.
    """
    if preloaded is not None:
        # Fast path: bulk-prefetched in caller. Resolve names + bonus_pct
        # from small joined tables; cache them on the function for the rest
        # of the process — sde_skill_time_bonus has only ~27 rows.
        if not hasattr(_science_skill_mult, "_bonus_cache"):
            with _connect() as _bc:
                _science_skill_mult._bonus_cache = {  # type: ignore[attr-defined]
                    r[0]: r[1] for r in _bc.execute(text(
                        "SELECT skill_type_id, time_bonus_pct"
                        " FROM sde_skill_time_bonus")).fetchall()
                }
        if not hasattr(_science_skill_mult, "_name_cache"):
            _science_skill_mult._name_cache = {}  # type: ignore[attr-defined]
        bonus_cache = _science_skill_mult._bonus_cache  # type: ignore[attr-defined]
        name_cache: dict[int, str] = _science_skill_mult._name_cache  # type: ignore[attr-defined]
        # Lazily resolve names for skill_ids we haven't seen yet.
        missing = [sid for sid, _ in preloaded if sid not in name_cache]
        if missing:
            with _connect() as _nc:
                for sid, name in _nc.execute(
                    text("SELECT type_id, name FROM sde_types"
                         " WHERE type_id IN :ids")
                    .bindparams(bindparam("ids", expanding=True)),
                    {"ids": missing},
                ).fetchall():
                    name_cache[sid] = name
        mult = 1.0
        details: list[tuple[str, int, float, int]] = []
        for sid, req_level in preloaded:
            level = skills.get(sid, 0)
            bonus_pct = bonus_cache.get(sid)
            if bonus_pct is not None:
                mult *= 1.0 - level * bonus_pct / 100
            details.append(
                (name_cache.get(sid, f"Skill {sid}"), level,
                 float(bonus_pct or 0), int(req_level))
            )
        return max(0.01, mult), details

    # Slow path — preloaded not available (single-blueprint callers).
    try:
        with _connect() as _sc:
            rows = _sc.execute(
                text("""SELECT bs.skill_type_id,
                      COALESCE(st.skill_name, t.name) AS skill_name,
                      bs.required_level,
                      st.time_bonus_pct
               FROM sde_blueprint_skills bs
               LEFT JOIN sde_skill_time_bonus st ON st.skill_type_id = bs.skill_type_id
               LEFT JOIN sde_types t              ON t.type_id       = bs.skill_type_id
               WHERE bs.blueprint_type_id = :bp AND bs.activity = :activity
                 AND bs.skill_type_id NOT IN (3380, 3388)"""),
                {"bp": bp_type_id, "activity": activity},
            ).fetchall()
    except Exception:
        return 1.0, []

    mult = 1.0
    details: list[tuple[str, int, float, int]] = []
    for skill_id, skill_name, req_level, bonus_pct in rows:
        level = skills.get(skill_id, 0)
        if bonus_pct is not None:
            mult *= 1.0 - level * bonus_pct / 100
        details.append((skill_name or f"Skill {skill_id}", level, float(bonus_pct or 0), int(req_level)))
    return max(0.01, mult), details


def _collect_type_ids(node) -> list[int]:
    ids = [node.type_id]
    for child in node.children:
        ids.extend(_collect_type_ids(child))
    return ids


# ---------------------------------------------------------------------------
# Manufacturing plan
# ---------------------------------------------------------------------------

@router.get("/plan", response_class=HTMLResponse)
async def plan_form(request: Request, char: str = "", station: str = ""):
    conn = get_conn()
    # Determine which character drives the form (URL ?char= overrides active cookie)
    plan_char_id: int | None = None
    if char.isdigit():
        plan_char_id = int(char)
        if not character_row(plan_char_id):
            plan_char_id = None
    if plan_char_id is None:
        plan_char_id = get_active_character_id(request)
    char_row = character_row(plan_char_id) if plan_char_id else None
    token = await _valid_token_async(plan_char_id) if plan_char_id else None

    location_ids = []
    char_skills: dict[int, int] = {}
    if char_row:
        with _connect() as _ac:
            raw = _load_assets_from_cache(_ac, char_row["character_id"])
        location_ids = sorted({a["location_id"] for a in raw if not a.get("is_singleton", False)})
        # Cached either way now. The two branches used to differ — a signed-in
        # character got a live fetch and a token-less one got the cache — which
        # meant the page was fast exactly when the data was least likely to
        # matter, and slow on every normal load. The worker keeps this warm.
        with _connect() as _sc:
            char_skills = get_cached_skills(_sc, char_row["character_id"])
    product_param = request.query_params.get("product", "")
    if product_param.strip().isdigit():
        with _connect() as _pc:
            row = _pc.execute(
                text("SELECT name FROM sde_types WHERE type_id=:tid"),
                {"tid": int(product_param)}).fetchone()
        if row:
            product_param = row[0]
    # Preserve station when switching character; otherwise fall back to the
    # app-wide default from Settings so the form opens ready to run.
    prefill_station = station.strip() if station.strip().isdigit() else ""
    # `app_defaults` is converted and this router is not, so it gets a
    # connection from the engine. Deliberately `connect()` and not the
    # `connect_to_path(database_path())` used further down for the resolver:
    # that one names the SQLite file, and the defaults must come from whatever
    # database the app is actually configured to read. `_connect` is imported at
    # module level; a local `from ... import connect as _connect` here would
    # make the name local to the whole function and leave every earlier use
    # unbound.
    with _connect() as _dc:
        _defaults = app_defaults.get_defaults(_dc)
    if not prefill_station and _defaults.get("build_station_id"):
        prefill_station = str(_defaults["build_station_id"])
    prefill_station_name = ""
    if prefill_station:
        with _connect() as _lc:
            row = _lc.execute(
                text("SELECT name FROM location_name_cache WHERE location_id=:lid"),
                {"lid": int(prefill_station)}).fetchone()
        if row:
            prefill_station_name = row[0]
    stock_default = int(prefill_station) if prefill_station else 0
    stock_station_options = await _build_stock_station_options(
        conn, plan_char_id, token,
        selected_ids=set(), default_station=stock_default, explicit=False,
    )
    conn.close()
    return _tr("plan.html", request, {
        "locations": location_ids,
        "stock_station_options": stock_station_options,
        "form_stock_stations": "",
        "result": None,
        "error": None,
        "form_product": product_param,
        "form_station": prefill_station,
        "form_station_name": prefill_station_name,
        "form_industry":     str(char_skills.get(3380, 0)),
        "form_adv_industry": str(char_skills.get(3388, 0)),
        "form_implant_mfg":  "0",
        "mfg_implant_options": _MFG_IMPLANT_OPTIONS,
        "plan_char_id": plan_char_id,
        "form_facility_tax": str(_defaults.get("facility_tax", 2.5)),
        "form_reaction_station": str(_defaults.get("reaction_station_id") or ""),
    })


def _resolve_product_local(conn, query: str) -> tuple[int, str] | None:
    """Find a product's type_id by name in the local SDE.

    Strategy: exact → prefix → substring. Among candidates it prefers
    producible ones (have a manufacturing/reaction recipe), then published,
    then the shortest name. That way "Industrial Jump Portal Generator" hits
    "…Generator I" instead of its blueprint or a longer variant.
    Returns None if nothing matches.

    **Every stage folds case explicitly, and that is a real change rather than a
    translation.** This used to be case-insensitive for two separate
    SQLite-specific reasons: `COLLATE NOCASE` on the exact match, and SQLite's
    default where `LIKE` ignores ASCII case on the other two. Postgres has no
    `NOCASE` collation *and* its `LIKE` is case-sensitive — so translating only
    the visible half would have left every mixed-case product search broken
    there, silently, with a green SQLite suite.

    That was not reasoned out; it is what the measurement said. On SQLite,
    dropping `COLLATE NOCASE` from the exact match changes nothing, because the
    prefix stage catches the query anyway:

        WHERE name = 'bantam'                        -> None
        WHERE name LIKE 'bantam%' ORDER BY LENGTH…   -> (582, 'Bantam')

    `LOWER()` on both sides is the portable form. It also widens the folding
    slightly: SQLite's `NOCASE` folds ASCII only, while Postgres `LOWER()` is
    locale-aware and folds Unicode. EVE type names are effectively ASCII so
    nothing observable changes, but it is a difference, and it is written down
    here rather than discovered later.
    """
    q = query.strip()
    if not q:
        return None
    needle = q.lower()

    def _pick(rows: list[tuple]) -> tuple[int, str] | None:
        if not rows:
            return None
        producible = {
            r[0] for r in conn.execute(
                text("SELECT DISTINCT product_type_id FROM sde_blueprint_products"
                     " WHERE activity IN ('manufacturing','reaction')"
                     "   AND product_type_id IN :ids")
                .bindparams(bindparam("ids", expanding=True)),
                {"ids": [r[0] for r in rows]},
            ).fetchall()
        }
        # best = producible > published > shorter name > lower type_id
        rows = sorted(rows, key=lambda r: (
            0 if r[0] in producible else 1,
            0 if r[2] else 1,
            len(r[1]),
            r[0],
        ))
        return rows[0][0], rows[0][1]

    # 1) exact
    exact = conn.execute(
        text("SELECT type_id, name, published FROM sde_types"
             " WHERE LOWER(name) = :needle"),
        {"needle": needle},
    ).fetchall()
    hit = _pick(exact)
    if hit:
        return hit
    # 2) prefix (limited so it doesn't explode on generic words)
    pref = conn.execute(
        text("SELECT type_id, name, published FROM sde_types"
             " WHERE LOWER(name) LIKE :pattern LIMIT 200"),
        {"pattern": needle + "%"},
    ).fetchall()
    hit = _pick(pref)
    if hit:
        return hit
    # 3) substring
    sub = conn.execute(
        text("SELECT type_id, name, published FROM sde_types"
             " WHERE LOWER(name) LIKE :pattern LIMIT 200"),
        {"pattern": "%" + needle + "%"},
    ).fetchall()
    return _pick(sub)


# Dropdown entries for the manufacturing implant, cheapest bonus first. Built from
# planner.MFG_IMPLANTS so the UI can never drift from what calc_job_time accepts.
_MFG_IMPLANT_OPTIONS: list[dict] = [
    {"pct": f"{pct:g}", "label": f"{name} (−{pct:g}%)"}
    for _tid, (name, pct) in sorted(MFG_IMPLANTS.items(), key=lambda kv: kv[1][1])
]


def _implant_name_for_pct(pct: float) -> str | None:
    """Name of the BX implant matching a reduction, or None when no implant."""
    for name, p in MFG_IMPLANTS.values():
        if p == pct:
            return name
    return None


@router.post("/plan", response_class=HTMLResponse)
async def plan_result(
    request: Request,
    product: str = Form(...),
    station: str = Form(""),
    reaction_station: int = Form(0),
    qty: int = Form(1),
    mode: str = Form("full"),
    form_me: str = Form(""),
    form_te: str = Form(""),
    facility_tax: str = Form("2.5"),
    reaction_facility_tax: str = Form(""),
    facility_me_bonus: str = Form("0"),
    reaction_me_bonus: str = Form("0"),
    selling_station: int = Form(0),
    form_industry: str = Form("0"),
    form_adv_industry: str = Form("0"),
    plan_char_id: str = Form(""),
    runs_per_job: str = Form("1"),
    stock_stations: str = Form(""),
    input_basis: str = Form("sell"),
    implant_mfg: str = Form("0"),
):
    conn = get_conn()
    input_basis = "buy" if input_basis == "buy" else "sell"
    # Zainou 'Beancounter' Industry implant — the UI sends the reduction in %.
    # Anything not in the real BX-80x set collapses to 0 (no implant).
    try:
        implant_mfg_pct = float(implant_mfg.replace(",", "."))
    except (ValueError, AttributeError):
        implant_mfg_pct = 0.0
    if implant_mfg_pct not in MFG_IMPLANT_PCTS:
        implant_mfg_pct = 0.0
    implant_mfg = f"{implant_mfg_pct:g}"
    error = None
    plan_data = None
    # Selection of stations the stock level is computed from. Empty = default
    # to the manufacturing station (backwards compatible). CSV location IDs from checkboxes.
    stock_station_ids: set[int] = {
        int(x) for x in stock_stations.split(",") if x.strip().lstrip("-").isdigit()
    }
    stock_explicit = bool(stock_stations.strip())
    # How many runs one BPC copy has — ME is rounded per job.
    # 1 (default) = parallel 1-run copies; K = copies of K runs each;
    # empty/0 = one batched job (in-game multi-run window).
    rpj_int: int | None = None
    if runs_per_job.strip().isdigit() and int(runs_per_job.strip()) > 0:
        rpj_int = int(runs_per_job.strip())
    # Resolve plan character from form, fall back to active char.
    plan_char_id_int: int | None = None
    if plan_char_id.strip().isdigit():
        candidate = int(plan_char_id.strip())
        if character_row(candidate):
            plan_char_id_int = candidate
    if plan_char_id_int is None:
        plan_char_id_int = get_active_character_id(request)

    # Parse station — friendly error instead of 422 (if missing, raise ValueError below)
    try:
        station = int(station.strip()) if isinstance(station, str) and station.strip() else 0
    except ValueError:
        station = 0

    # Convert ME/TE to int if provided
    me_override: int | None = int(form_me) if form_me.strip().isdigit() else None
    te_override: int | None = int(form_te) if form_te.strip().isdigit() else None
    # Safe defaults — overwritten inside try block once BP is known
    me: float = float(me_override) if me_override is not None else 0.0
    te: int   = te_override if te_override is not None else 0

    def _clamp_skill(s: str, max_val: int = 5) -> int:
        try:
            return max(0, min(max_val, int(s.strip())))
        except (ValueError, AttributeError):
            return 0

    industry_level     = _clamp_skill(form_industry)
    adv_industry_level = _clamp_skill(form_adv_industry)

    def _parse_pct(s: str) -> float:
        try:
            return max(0.0, min(25.0, float(s.replace(",", "."))))
        except (ValueError, AttributeError):
            return 0.0

    # facility_me_bonus / reaction_me_bonus from the form are now display-only
    # (form_facility_me_bonus is passed back to the template). The actual ME
    # multiplier is computed from station_rigs in get_station_me_multiplier.

    try:
        if plan_char_id_int is None:
            raise ValueError("You are not signed in.")
        token = await _valid_token_async(plan_char_id_int)
        row = character_row(plan_char_id_int)
        if not token or not row:
            raise ValueError("You are not signed in.")
        if not station:
            raise ValueError("Select a manufacturing station.")
        char = (row["character_id"], row["character_name"])
        char_id, _ = char

        # Resolving what the user typed. Local first, and for anything
        # buildable that is the whole story: every product with a blueprint is
        # in the SDE by definition. The ESI fallback below exists for a name the
        # static data has never heard of — a typo, or a type added since the
        # last `import_sde.py` — and is the reason this handler keeps its
        # exemption in tests/test_cache_only_routes.py rather than being
        # declared cache-only. It is the one fetch here that *is* the answer.
        local = None
        if not product.strip().isdigit():
            # Exact → prefix → substring; prefers producible, published,
            # shortest name (so "Industrial Jump Portal Generator" hits
            # "…Generator I", not its blueprint).
            with _connect() as _rc:
                local = _resolve_product_local(_rc, product.strip())

        if local:
            type_id, type_name = local
        else:
            async with esi_client() as client:
                session = get_session()
                try:
                    if product.strip().isdigit():
                        type_id = int(product.strip())
                        type_name = await resolve_type(client, session, type_id)
                    else:
                        results = await search_type_by_name(client, product.strip())
                        if not results:
                            raise ValueError(f"Product '{product}' not found.")
                        type_id = results[0]
                        type_name = await resolve_type(client, session, type_id)
                finally:
                    session.close()

        # All three come from the caches the sync worker fills. This was three
        # paginated ESI calls on every plan submission — and a plan is submitted
        # repeatedly while somebody tunes ME, runs and stations, so the same
        # three lists were fetched over and over to compute a different number
        # from identical inputs.
        with _connect() as _ac:
            blueprints, _bp_at = load_cached_blueprints(_ac, char_id)
            all_assets, _as_at = load_cached_assets(_ac, char_id)
            char_skills = get_cached_skills(_ac, char_id)
        if blueprints is None or all_assets is None:
            raise ValueError(
                "This character has not been synced yet — the background worker "
                "fills its blueprints and assets within a few minutes of signing "
                "in. Planning now would price the build as if you owned nothing.")

        # Industry/AdvIndustry always from current char_skills (the form_industry
        # field is hidden and may come from an old character after switching).
        industry_level     = max(0, min(5, int(char_skills.get(3380, 0))))
        adv_industry_level = max(0, min(5, int(char_skills.get(3388, 0))))
        form_industry      = str(industry_level)
        form_adv_industry  = str(adv_industry_level)

        # Stock sources: if the user picked stations, use them; otherwise default
        # to the manufacturing station. Roll up containers to their station +
        # exclude ship cargo/fittings via _rollup_stock (so selecting a station
        # also counts container contents, but not a ship's fit/cargo).
        effective_stock_ids = stock_station_ids if stock_explicit else {station}
        _station_types = _rollup_stock_from_charassets(all_assets)
        available = {}
        for sid in effective_stock_ids:
            for tid, q in _station_types.get(sid, {}).items():
                available[tid] = available.get(tid, 0) + q

        with _connect() as _bc:
            bp = find_blueprint_for_product(blueprints, type_id, _bc)
        me = float(me_override if me_override is not None else (bp.material_efficiency if bp else 0))
        te = int(te_override if te_override is not None else (bp.time_efficiency if bp else 0))

        # Station ME multiplier — per-product (a rig applies only to products
        # matching its category: Ship rig to ships, Equipment rig to modules, etc.).
        eff_rxn_station_for_me = reaction_station if reaction_station else station
        with _connect() as _ic:
            mfg_facility = get_station_facility(_ic, station)
            rxn_facility = get_station_facility(_ic, eff_rxn_station_for_me)
            # Aggregated savings for the ROOT product (for display)
            mfg_me_mult = get_station_me_multiplier(_ic, station)
            rxn_me_mult = get_station_me_multiplier(_ic, eff_rxn_station_for_me)

        # === Manufacturing fee parameters, computed up-front ===
        # The make-vs-buy optimizer needs each job's install fee, not just its
        # material cost — otherwise it "makes" components whose real install
        # fees then quietly erase the paper savings. So resolve fee inputs
        # (SCI, tax, structure bonus, adjusted prices) BEFORE building the plan.
        def _safe_pct(s: str, default: float) -> float:
            try:
                return float(s.replace(",", "."))
            except (ValueError, AttributeError):
                return default

        fac_tax_pct  = _safe_pct(facility_tax, 2.5)
        fac_tax_rate = fac_tax_pct / 100

        # Reaction station — 0 means use the same one as manufacturing
        eff_rxn_station = reaction_station if reaction_station else station
        sep_rxn_station = eff_rxn_station != station

        rxn_fac_tax_pct  = _safe_pct(reaction_facility_tax, fac_tax_pct) if reaction_facility_tax.strip() else fac_tax_pct
        rxn_fac_tax_rate = rxn_fac_tax_pct / 100

        # Solar system ID of the manufacturing station
        with _connect() as _sc:
            sys_row = _sc.execute(
                text("SELECT solar_system_id FROM location_name_cache"
                     " WHERE location_id=:lid"), {"lid": station}).fetchone()
        solar_system_id: int | None = sys_row[0] if sys_row and sys_row[0] else None

        # Solar system ID of the reaction station
        if sep_rxn_station:
            with _connect() as _sc:
                rxn_sys_row = _sc.execute(
                    text("SELECT solar_system_id FROM location_name_cache"
                         " WHERE location_id=:lid"),
                    {"lid": eff_rxn_station}).fetchone()
            rxn_solar_system_id: int | None = rxn_sys_row[0] if rxn_sys_row and rxn_sys_row[0] else None
        else:
            rxn_solar_system_id = solar_system_id

        with _connect() as _ic:
            adj_prices = await get_adjusted_prices(_ic)

            mfg_sci = await get_sci_for_system(_ic, solar_system_id, "manufacturing") if solar_system_id else 0.0
            rxn_sci = await get_sci_for_system(_ic, rxn_solar_system_id, "reaction") if rxn_solar_system_id else 0.0

            # TE multipliers for the stations (structure + rigs)
            mfg_te_mult = get_station_te_multiplier(_ic, station)
            rxn_te_mult = get_station_te_multiplier(_ic, eff_rxn_station) if sep_rxn_station else mfg_te_mult

            # Cost bonus na SCI (Raitaru −3 %, Azbel −4 %, Sotiyo −5 %)
            mfg_cost_bonus = get_station_cost_bonus(_ic, station)
            rxn_cost_bonus = get_station_cost_bonus(_ic, eff_rxn_station) if sep_rxn_station else mfg_cost_bonus

        # Combined install-fee rate per activity: SCI×(1−structure bonus) + tax + SCC.
        rate_mfg = mfg_sci * (1.0 - mfg_cost_bonus) + fac_tax_rate + _SCC
        rate_rxn = rxn_sci * (1.0 - rxn_cost_bonus) + rxn_fac_tax_rate + _SCC

        # Pass 1 — a structural resolve purely to discover which products the
        # tree contains, so their per-run times can be turned into per-product
        # job splits. Skipped entirely when splitting is off (the default), so
        # an unconfigured install pays nothing for it.
        from app.db.conn import connect_to_path
        # `with`, not a bare open/close: every call between here and the
        # end of the block can raise — an unknown type, a blueprint with no
        # product, a split that divides by a zero job time — and each of
        # those paths used to skip the `close()` that sat at the bottom.
        # NullPool means one fresh sqlite3 handle per call, so the leak was
        # a file handle per failed plan run, held until the process exited.
        # Nothing below the block needs it open: rows leave BOMResolver as
        # plain dicts, and the defaults were already read into one.
        with connect_to_path(database_path()) as sde_conn:   # the converted BOMResolver
            # Read once, into a plain dict, and close. The defaults are wanted as
            # data in three places further down; holding a pooled connection open
            # across all of them would leak one per plan run on any path that
            # raises. Not `sde_conn` either: that one is pinned to the SQLite file
            # by path, and the defaults belong to the configured database.
            with _connect() as _dc:
                _plan_defaults_all = app_defaults.get_defaults(_dc)
            _max_job_days = float(_plan_defaults_all.get("max_job_days") or 0)
            _job_splits: dict[int, int] = {}
            if _max_job_days > 0:
                probe = BOMResolver(sde_conn, blueprints=blueprints, runs_per_job=rpj_int)
                _job_splits = _derive_job_splits(
                    conn,
                    probe.resolve(type_id, qty, me=me, mfg_facility=mfg_facility,
                                  rxn_facility=rxn_facility),
                    max_days=_max_job_days, te=te,
                    industry_level=industry_level, adv_industry_level=adv_industry_level,
                    mfg_facility=mfg_facility, rxn_facility=rxn_facility,
                    char_skills=char_skills,
                )

            # Invention: a T2 item needs an invented BPC, and until v0.9.29 this page
            # charged nothing for it — not on nested components and not even on the
            # product itself, since only the margin tracker ever called that code.
            # Same builder the tracker uses, so the two pages price datacores alike.
            # `margins` is converted; this router is not. It gets the SQLAlchemy
            # connection already open for the resolver, and the defaults come from
            # the engine connection opened above. All three are the same database.
            inv_params, inv_warnings = build_invention_params(
                sde_conn, _plan_defaults_all, input_basis, database_path())

            # Pass 2 — the real resolve, with the splits applied. ME rounds once per
            # job, so the splits reach the material totals here and in build_plan
            # below; both resolutions must use them or the two views disagree.
            # The resolver gets all of the character's blueprints → per-product ME is
            # looked up for each intermediate step (Capital Armor Plates ME may differ from root ME).
            resolver = BOMResolver(sde_conn, blueprints=blueprints, runs_per_job=rpj_int,
                                   adjusted_prices=adj_prices, rate_mfg=rate_mfg, rate_rxn=rate_rxn,
                                   runs_per_job_by_product=_job_splits,
                                   invention=inv_params)
            root = resolver.resolve(type_id, qty, me=me,
                                    mfg_facility=mfg_facility,
                                    rxn_facility=rxn_facility)

        all_ids = list(set(_collect_type_ids(root) + [type_id]))
        with _connect() as _pc:
            prices = await get_prices_for_ids(_pc, all_ids)

        plan = build_plan(
            product_type_id=type_id,
            quantity=qty,
            location_id=station,
            available_assets=available,
            blueprints=blueprints,
            db_path=database_path(),
            mode=mode,
            prices=prices,
            mfg_facility=mfg_facility,
            rxn_facility=rxn_facility,
            runs_per_job=rpj_int,
            # Same splits as the steps resolution above — the Materials tab and
            # the Jobs list are two separate BOM resolutions and must not disagree.
            runs_per_job_by_product=_job_splits,
            adjusted_prices=adj_prices,
            rate_mfg=rate_mfg,
            rate_rxn=rate_rxn,
            input_basis=input_basis,
            # Hand over the SAME root ME/TE used for `root` above, so the Materials
            # tab and the Manufacturing steps cannot disagree (they are two separate
            # BOM resolutions of the same product).
            me_override=me,
            te_override=te,
            invention=inv_params,
        )
        plan.invention_unpriced = inv_warnings + plan.invention_unpriced
        plan_data = _plan_to_dict(plan, prices, type_name, conn=conn, input_basis=input_basis)
        # Override ME/TE in plan_data if entered manually
        if plan_data.get("blueprint"):
            plan_data["blueprint"]["me"] = int(me)
            plan_data["blueprint"]["te"] = te
        elif me_override is not None:
            plan_data["blueprint"] = {"kind": "—", "me": int(me), "te": te, "runs": "—", "manual": True}

        # Make-vs-buy decisions go to the UI in every mode (informational
        # tab). Only optimal mode acts on them: bought components are pruned
        # out of the manufacturing-steps tree — you don't run (or pay job
        # fees for) jobs whose output you buy off market.
        if plan.opt_decisions:
            plan_data["opt_decisions"] = [
                {
                    "type_id":    d.type_id,
                    "name":       d.name,
                    "quantity":   d.quantity,
                    "unit_price": (d.buy_cost / d.quantity)
                                  if (d.buy_cost is not None and d.quantity) else None,
                    "make_cost":  d.make_cost,
                    "buy_cost":   d.buy_cost,
                    "action":     d.action,
                    "savings":    d.savings,
                }
                for d in plan.opt_decisions
            ]
        if mode == "optimal" and plan.opt_decisions:
            buy_type_ids = {d.type_id for d in plan.opt_decisions if d.action == "buy"}
            if buy_type_ids:
                def _prune_bought(node):
                    kept = []
                    for c in node.children:
                        if c.type_id in buy_type_ids:
                            # bought → becomes a leaf (market purchase), no sub-jobs
                            c.children = []
                            c.is_leaf = True
                            c.activity = "raw"
                            kept.append(c)
                        else:
                            _prune_bought(c)
                            kept.append(c)
                    node.children = kept
                _prune_bought(root)

        plan_data["manufacturing_steps"] = _build_manufacturing_steps(root, prices, available, input_basis)
        # The materials above were costed as split jobs, so the job list has to
        # show the same split — otherwise the two views describe different builds.
        if _job_splits:
            _split_step_jobs(plan_data["manufacturing_steps"], _job_splits)

        # === Manufacturing fees === (fee parameters were resolved up-front,
        # before build_plan, so the make-vs-buy optimizer could weigh them.)

        total_job_fee = 0.0
        total_mfg_time_s = 0   # time of all manufacturing steps (sequential)
        total_rxn_time_s = 0

        # Bulk-fetch all blueprint data referenced by the manufacturing steps,
        # so each job doesn't hit DB 3× for its bp_id (materials, time, skills).
        all_bp_ids: set[int] = set()
        for step in plan_data["manufacturing_steps"]:
            for job in step["jobs"]:
                bp = job.get("blueprint_type_id")
                if bp:
                    all_bp_ids.add(bp)

        bp_materials_idx: dict[tuple[int, str], list] = {}
        bp_time_idx: dict[int, tuple[int, int]] = {}
        bp_skills_idx: dict[tuple[int, str], list] = {}
        if all_bp_ids:
            ids_list = list(all_bp_ids)
            _ids = bindparam("ids", expanding=True)
            with _connect() as _bp:
                for mid, q, act, bp in _bp.execute(
                    text("SELECT material_type_id, quantity, activity,"
                         " blueprint_type_id FROM sde_blueprint_materials"
                         " WHERE blueprint_type_id IN :ids").bindparams(_ids),
                    {"ids": ids_list},
                ).fetchall():
                    bp_materials_idx.setdefault((bp, act), []).append((mid, q))
                for bp, mtime, rtime in _bp.execute(
                    text("SELECT blueprint_type_id, manufacturing_time,"
                         " reaction_time FROM sde_blueprints"
                         " WHERE blueprint_type_id IN :ids").bindparams(_ids),
                    {"ids": ids_list},
                ).fetchall():
                    bp_time_idx[bp] = (mtime, rtime)
                # Industry (3380) and Advanced Industry (3388) are excluded on
                # purpose: they scale job time for the whole plan and are read
                # separately below. Left in, the science-skill multiplier
                # applies them a second time and the plan reports a build
                # faster than the game allows.
                for bp, act, sk_id, req_lvl in _bp.execute(
                    text("SELECT blueprint_type_id, activity, skill_type_id,"
                         " required_level FROM sde_blueprint_skills"
                         " WHERE blueprint_type_id IN :ids"
                         "   AND skill_type_id NOT IN (3380, 3388)")
                    .bindparams(_ids),
                    {"ids": ids_list},
                ).fetchall():
                    bp_skills_idx.setdefault((bp, act), []).append((sk_id, req_lvl))

        # Memoize get_product_te_multiplier per (facility-id, type_id).
        # Same product appears across multiple steps when the resolver
        # aggregates duplicates — without the cache we re-classify it each
        # time and pay the rig_applies_to_product cost again.
        te_mult_cache: dict[tuple[int, int], float] = {}
        def _te_mult_for(prod_facility, type_id: int) -> float:
            key = (id(prod_facility), type_id)
            cached = te_mult_cache.get(key)
            if cached is not None:
                return cached
            # Inside the memo, so this opens once per distinct
            # (facility, product) pair rather than once per step. A facility
            # with no rigs never reaches the database at all.
            with _connect() as _ic:
                val = get_product_te_multiplier(_ic, prod_facility, type_id)
            te_mult_cache[key] = val
            return val

        # Slot capacity from the app-wide defaults. All zero (the default) means
        # unlimited, which reproduces the previous longest-job-per-level estimate.
        from app.manufacturing.schedule import SlotLimits as _SlotLimits
        _plan_defaults = _plan_defaults_all
        _slot_limits = _SlotLimits(
            manufacturing=int(_plan_defaults.get("manufacturing_slots") or 0),
            reaction=int(_plan_defaults.get("reaction_slots") or 0),
            capital=int(_plan_defaults.get("capital_slots") or 0),
        )
        _capital_groups = _capital_group_lookup(conn, plan_data["manufacturing_steps"])

        for step in plan_data["manufacturing_steps"]:
            step_mfg_time = 0
            step_rxn_time = 0
            # In "components" mode we buy the 1st level from the market — we pay
            # install fees only for the final job (assembling the product itself).
            skip_fee = (mode == "components" and not step.get("is_final"))
            for job in step["jobs"]:
                is_rxn   = job.get("activity") == "reaction"
                sci      = rxn_sci      if is_rxn else mfg_sci
                tax_rate = rxn_fac_tax_rate if is_rxn else fac_tax_rate
                cost_bonus = rxn_cost_bonus if is_rxn else mfg_cost_bonus

                # EIV must use the BASE quantities from the SDE (not ME-reduced)
                bp_id = job.get("blueprint_type_id")
                runs  = job.get("runs", 1) or 1
                if bp_id:
                    base_mats = bp_materials_idx.get(
                        (bp_id, job.get("activity", "manufacturing")), []
                    )
                    eiv = sum(adj_prices.get(m[0], 0.0) * m[1] * runs for m in base_mats)
                else:
                    eiv = sum(adj_prices.get(inp["type_id"], 0.0) * inp["quantity"]
                              for inp in job["inputs"])

                # Official formula: TIF = EIV × ((SCI × (1 - structure_cost_bonus)) + tax + SCC)
                job_fee = eiv * (sci * (1.0 - cost_bonus) + tax_rate + _SCC)
                job["eiv"] = eiv
                job["sci"] = sci
                job["tax_pct"] = round(tax_rate * 100, 2)
                job["job_fee"] = job_fee
                if not skip_fee:
                    total_job_fee += job_fee

                # Job duration
                if bp_id:
                    bp_time_row = bp_time_idx.get(bp_id)
                    base_time = (bp_time_row[1] if is_rxn else bp_time_row[0]) if bp_time_row else None
                    if base_time:
                        activity_name = job.get("activity", "manufacturing")
                        sci_mult, sci_details = _science_skill_mult(
                            conn, bp_id, activity_name, char_skills,
                            preloaded=bp_skills_idx.get((bp_id, activity_name)),
                        )
                        job_te = te if not is_rxn else 0
                        # Per-product TE multiplier — an Equipment TE rig doesn't speed up building a ship
                        prod_facility = rxn_facility if is_rxn else mfg_facility
                        prod_te_mult = _te_mult_for(prod_facility, job["type_id"])
                        job_secs = calc_job_time(
                            base_time=base_time,
                            runs=runs,
                            te=job_te,
                            industry_level=industry_level,
                            adv_industry_level=adv_industry_level,
                            facility_te_multiplier=prod_te_mult,
                            is_reaction=is_rxn,
                            science_skill_mult=sci_mult,
                            implant_time_pct=implant_mfg_pct,
                        )
                        job["facility_te_mult"] = prod_te_mult
                        job["job_duration_seconds"] = job_secs
                        job["job_duration"] = format_duration(job_secs)
                        job["science_skills"] = sci_details  # [(name, level, bonus_pct)]
                        if is_rxn:
                            step_rxn_time = max(step_rxn_time, job_secs)
                        else:
                            step_mfg_time = max(step_mfg_time, job_secs)

            # With slot limits configured, a level is not "as long as its
            # longest job" — that assumed unlimited parallel slots. Schedule the
            # level's jobs across the pools instead. With no limits set,
            # `schedule_level` returns the same longest-job figure as before.
            if _slot_limits.manufacturing or _slot_limits.reaction or _slot_limits.capital:
                step_mfg_time, step_rxn_time = _schedule_step(
                    step, _slot_limits, _capital_groups)

            total_mfg_time_s += step_mfg_time
            total_rxn_time_s += step_rxn_time

        # "Jobs to Run" — the same jobs bucketed by what they are, so a build
        # that expands into hundreds of installs stays readable.
        from app.manufacturing.schedule import group_jobs as _group_jobs
        plan_data["job_groups"] = _group_jobs(
            [job for step in plan_data["manufacturing_steps"] for job in step["jobs"]],
            _plan_group_ids(conn, plan_data["manufacturing_steps"]),
            end_product_id=type_id,
        )
        plan_data["total_job_count"] = sum(g["job_count"] for g in plan_data["job_groups"])

        # Collect unique science skills across all jobs for display in the header.
        # For the same skill across jobs we take the max required_level.
        _seen: dict[str, tuple[int, float, int]] = {}
        for step in plan_data.get("manufacturing_steps", []):
            for job in step.get("jobs", []):
                for sname, slevel, spct, sreq in job.get("science_skills", []):
                    prev = _seen.get(sname)
                    if prev is None:
                        _seen[sname] = (slevel, spct, sreq)
                    else:
                        _seen[sname] = (slevel, spct, max(prev[2], sreq))
        plan_data["all_science_skills"] = [
            (n, l, p, r) for n, (l, p, r) in sorted(_seen.items())
        ]

        # Required Industry / Adv Industry levels — max across all BPs in the plan
        bp_ids_in_plan: set[int] = set()
        for step in plan_data.get("manufacturing_steps", []):
            for job in step.get("jobs", []):
                bp_id_j = job.get("blueprint_type_id")
                if bp_id_j:
                    bp_ids_in_plan.add(int(bp_id_j))
        industry_required = 0
        adv_industry_required = 0
        if bp_ids_in_plan:
            with _connect() as _sk:
                req_rows = _sk.execute(
                    text("SELECT skill_type_id, MAX(required_level)"
                         " FROM sde_blueprint_skills"
                         " WHERE blueprint_type_id IN :ids"
                         "   AND skill_type_id IN (3380, 3388)"
                         " GROUP BY skill_type_id")
                    .bindparams(bindparam("ids", expanding=True)),
                    {"ids": list(bp_ids_in_plan)},
                ).fetchall()
            for sid, lvl in req_rows:
                if sid == 3380:
                    industry_required = int(lvl)
                elif sid == 3388:
                    adv_industry_required = int(lvl)
        plan_data["industry_required"] = industry_required
        plan_data["adv_industry_required"] = adv_industry_required

        # Market price of all materials (regardless of stock)
        full_mat_cost = sum(
            m.get("total_price") or 0.0 for m in plan_data.get("materials", [])
        )
        # Price of only the missing materials (what needs to be bought)
        buy_cost = plan_data.get("total_buy") or 0.0
        rev = plan_data.get("revenue")

        # Freight: a flat fee on what crosses the boundary of the operation.
        # What you haul in, and what you send out — never anything moving
        # between your own jobs, because an intermediate you build never
        # travels. Both rates default to 0, which is right for anyone building
        # and selling in one station.
        _import_rate = float(_plan_defaults.get("freight_import_isk_m3") or 0.0)
        _export_rate = float(_plan_defaults.get("freight_export_isk_m3") or 0.0)
        import_m3 = import_m3_stock = export_m3 = 0.0
        if _import_rate or _export_rate:
            _mat_ids = [m["type_id"] for m in plan_data.get("materials", [])]
            _vol_ids = _mat_ids + [plan_data.get("product_type_id")]
            with _connect() as _vc:
                _vols = {r[0]: (r[1] or 0.0) for r in _vc.execute(
                    text("SELECT type_id, packaged_volume FROM sde_types"
                         " WHERE type_id IN :ids")
                    .bindparams(bindparam("ids", expanding=True)),
                    {"ids": [i for i in _vol_ids if i]})}
            for m in plan_data.get("materials", []):
                unit_m3 = _vols.get(m["type_id"], 0.0)
                # Two figures for the two profit lines below: buying every
                # material, and buying only what you are short of.
                import_m3 += (m.get("required") or 0) * unit_m3
                import_m3_stock += (m.get("missing") or 0) * unit_m3
            export_m3 = (plan_data.get("quantity") or 0) * _vols.get(
                plan_data.get("product_type_id"), 0.0)

        import_cost = import_m3 * _import_rate
        import_cost_stock = import_m3_stock * _import_rate
        export_cost = export_m3 * _export_rate

        # Selling is not free: sales tax always, broker's fee when listing an
        # order. Between 4.4% and 10.5% of revenue, which on a thin margin is
        # the whole margin — so it comes off both profit figures, not just the
        # headline one.
        _sell_costs = selling_costs(_plan_defaults)
        selling_cost = _sell_costs.on(rev) if rev is not None else 0.0

        # Freight joins the job fee: both are costs of producing, not of the
        # materials themselves, so they sit on the same side of the sum.
        # Import differs between the two lines for the same reason the material
        # cost does — you only haul what you actually buy.
        profit_market = (rev - full_mat_cost - total_job_fee - selling_cost
                         - import_cost - export_cost) if rev is not None else None
        profit_stock = (rev - buy_cost - total_job_fee - selling_cost
                        - import_cost_stock - export_cost) if rev is not None else None

        total_time_s = total_mfg_time_s + total_rxn_time_s
        plan_data["fees"] = {
            "solar_system_id":     solar_system_id,
            "rxn_solar_system_id": rxn_solar_system_id,
            "sep_rxn_station":     sep_rxn_station,
            "mfg_sci":             mfg_sci,
            "rxn_sci":             rxn_sci,
            "facility_tax":        fac_tax_pct,
            "rxn_facility_tax":    rxn_fac_tax_pct,
            "mfg_cost_bonus_pct":  round(mfg_cost_bonus * 100, 1),
            "rxn_cost_bonus_pct":  round(rxn_cost_bonus * 100, 1) if sep_rxn_station else None,
            "total_job_fee":       total_job_fee,
            # Shown as their own lines rather than folded into materials: a
            # cost you cannot see is one you cannot check, and freight is the
            # one number here that comes from a setting rather than the market.
            "import_m3":           import_m3,
            "import_m3_stock":     import_m3_stock,
            "import_cost":         import_cost,
            "import_cost_stock":   import_cost_stock,
            "export_m3":           export_m3,
            "export_cost":         export_cost,
            "total_time_s":        total_time_s,
            "total_time":          format_duration(total_time_s) if total_time_s else None,
            "implant_mfg_pct":     implant_mfg_pct,
            "implant_mfg_name":    _implant_name_for_pct(implant_mfg_pct),
            "mfg_te_pct":          round((1 - mfg_te_mult) * 100, 1),
            "rxn_te_pct":          round((1 - rxn_te_mult) * 100, 1) if sep_rxn_station else None,
            "mfg_me_pct":          round((1 - mfg_me_mult) * 100, 2),
            "rxn_me_pct":          round((1 - rxn_me_mult) * 100, 2) if sep_rxn_station else None,
            "full_mat_cost":       full_mat_cost,
            "selling_cost":        selling_cost,
            "selling_cost_pct":    _sell_costs.pct,
            "sales_tax_pct":       _sell_costs.sales_tax * 100,
            "broker_fee_pct":      _sell_costs.broker_fee * 100,
            "sales_method":        _sell_costs.method,
            "profit_market":       profit_market,
            "profit_stock":        profit_stock,
        }

    except Exception as e:
        error = esi_error_message(e) or str(e)

    # Stock-source options (names via ESI, not bare IDs). Default = manufacturing
    # station unless the user selected explicitly.
    _stock_token = (await _valid_token_async(plan_char_id_int)
                    if plan_char_id_int else None)
    stock_station_options = await _build_stock_station_options(
        conn, plan_char_id_int, _stock_token,
        selected_ids=stock_station_ids, default_station=station, explicit=stock_explicit,
    )
    location_ids = [o["location_id"] for o in stock_station_options]

    # Load the station name for display in the form
    with _connect() as _lc:
        loc_names = load_location_names_from_db(_lc)
    station_name = loc_names.get(station, str(station))
    rxn_station_name = loc_names.get(reaction_station, str(reaction_station)) if reaction_station else ""

    # Best sell price of the product at the selling station (from station_volume_cache)
    sell_loc = selling_station if selling_station else station
    station_sell_price: float | None = None
    if plan_data and plan_data.get("product_type_id"):
        # `app/market/prices.py` moved onto the portable layer, so this takes an
        # engine connection now rather than the router's raw `get_conn()` handle.
        with _connect() as _vc:
            svols = get_cached_station_volumes(_vc, sell_loc)
        if svols:
            entry = svols.get(plan_data["product_type_id"])
            if entry and entry[1]:
                station_sell_price = entry[1]
    selling_station_name = loc_names.get(sell_loc, str(sell_loc)) if sell_loc else ""

    conn.close()

    return _tr("plan.html", request, {
        "locations": location_ids,
        "stock_station_options": stock_station_options,
        "form_stock_stations": stock_stations,
        "result": plan_data,
        "error": error,
        "form_product": product,
        "form_station": station,
        "form_station_name": station_name,
        "form_rxn_station": reaction_station or "",
        "form_rxn_station_name": rxn_station_name,
        "form_qty": qty,
        "form_mode": mode,
        "form_input_basis": input_basis,
        "form_runs_per_job": runs_per_job,
        # After computing, always show the ROOT BP ME/TE (the actual values used in the plan) —
        # the user sees a concrete number instead of a placeholder.
        "form_me": str(int(me)),
        "form_te": str(int(te)),
        "form_facility_tax": facility_tax,
        "form_rxn_facility_tax": reaction_facility_tax if reaction_facility_tax.strip() else facility_tax,
        "form_facility_me_bonus": facility_me_bonus,
        "form_rxn_me_bonus": reaction_me_bonus,
        "station_sell_price": station_sell_price,
        "station_name": station_name,
        "selling_station_name": selling_station_name,
        "form_selling_station": selling_station or "",
        "form_selling_station_name": selling_station_name if selling_station else "",
        "form_industry":     form_industry,
        "form_adv_industry": form_adv_industry,
        "form_implant_mfg":  implant_mfg,
        "mfg_implant_options": _MFG_IMPLANT_OPTIONS,
        "plan_char_id":      plan_char_id_int,
    })


# location_flag values that mean "inside a ship" (fitted modules, cargo,
# drone/fighter bay, specialized bay). Such items are NOT counted as manufacturing
# stock — it makes no sense to strip individual ships' fit/cargo. Hangar,
# AutoFit/Unlocked/Locked (contents of hangar containers), on the other hand, are.
_SHIP_INTERNAL_FLAGS: frozenset[str] = frozenset({
    "Cargo", "DroneBay", "FleetHangar", "ShipHangar", "FighterBay",
    "FighterTube0", "FighterTube1", "FighterTube2", "FighterTube3", "FighterTube4",
    "HiddenModifiers",
    *(f"HiSlot{i}" for i in range(8)),
    *(f"MedSlot{i}" for i in range(8)),
    *(f"LoSlot{i}" for i in range(8)),
    *(f"RigSlot{i}" for i in range(8)),
    *(f"SubSystemSlot{i}" for i in range(8)),
})


def _is_ship_internal_flag(flag: str) -> bool:
    return flag in _SHIP_INTERNAL_FLAGS or flag.startswith("Specialized")


def _rollup_stock(rows: list[tuple]) -> dict[int, dict[int, int]]:
    """rows: (item_id, location_id, location_flag, type_id, quantity, is_singleton).

    Return {station_id: {type_id: total_qty}} — items rolled up to a real
    station/structure (container contents are summed onto their station),
    EXCLUDING singletons (ships, unique items) and everything inside ships
    (ship cargo / fittings / bays). station_id = the first ancestor in the chain
    that is no longer an owned item (= a real station or structure).
    """
    by_id = {r[0]: r for r in rows}
    result: dict[int, dict[int, int]] = {}
    for r in rows:
        item_id, loc_id, flag, type_id, qty, singleton = r
        if singleton:
            continue
        # Walk up the chain; if you hit a ship-internal flag anywhere,
        # it's ship contents → skip.
        node = r
        seen: set[int] = set()
        station = loc_id
        excluded = False
        for _ in range(32):
            if _is_ship_internal_flag(node[2]):
                excluded = True
                break
            parent_id = node[1]
            parent = by_id.get(parent_id)
            if parent is None:
                station = parent_id   # real station/structure
                break
            if parent_id in seen:
                station = parent_id
                break
            seen.add(parent_id)
            node = parent
        if excluded:
            continue
        d = result.setdefault(station, {})
        d[type_id] = d.get(type_id, 0) + qty
    return result


def _rollup_stock_from_charassets(assets) -> dict[int, dict[int, int]]:
    rows = [(a.item_id, a.location_id, a.location_flag, a.type_id, a.quantity, a.is_singleton)
            for a in assets]
    return _rollup_stock(rows)


def _rollup_stock_from_cache(raw: list[dict]) -> dict[int, dict[int, int]]:
    rows = [(a["item_id"], a["location_id"], a.get("location_flag", ""),
             a["type_id"], a["quantity"], a.get("is_singleton", False))
            for a in raw]
    return _rollup_stock(rows)


async def _build_stock_station_options(
    conn: sqlite3.Connection,
    plan_char_id: int | None,
    token: str | None,
    *,
    selected_ids: set[int],
    default_station: int,
    explicit: bool,
) -> list[dict]:
    """Stations where the planning character has non-singleton items — options for
    the stock-source picker. Names are resolved via ESI (resolve_station_names_bulk)
    so bare IDs aren't shown. `selected` = the user's explicit choice,
    otherwise default to the manufacturing station.
    """
    if not plan_char_id:
        return []
    with _connect() as _ac:
        raw = _load_assets_from_cache(_ac, plan_char_id)
    # Roll up container contents onto their station and skip ship cargo/fittings.
    station_types = _rollup_stock_from_cache(raw)
    if not station_types:
        return []
    seen_types = {sid: set(types.keys()) for sid, types in station_types.items()}
    loc_ids = list(seen_types.keys())

    def _is_real(n: str | None, lid: int) -> bool:
        return bool(n) and not n.startswith("[") and n != str(lid)

    # The DB cache holds real names accumulated earlier (Assets resolves them
    # per-owner with a token and stores them here) — placeholders are never
    # stored in the DB. We use it as the primary source WITHOUT an ESI call.
    with _connect() as _lc:
        db_names = load_location_names_from_db(_lc)

    # Resolve via ESI only for stations that don't have a real name yet — and only
    # with the planning character's token. Resolving all ~79 structures with the
    # tokens of ALL characters (as v0.6.1/0.6.2 did) generates a flood of 403
    # responses and ESI error-limits us (HTTP 420), which then also breaks product
    # resolution. resolve_station_name also remembers 403s and 420s so they don't repeat.
    resolved: dict[int, str] = {}
    unresolved = [lid for lid in loc_ids if not _is_real(db_names.get(lid), lid)]
    if unresolved:
        try:
            with _connect() as _lc:
                r = await resolve_station_names_bulk(
                    unresolved, token=token, conn=_lc)
            resolved = {lid: n for lid, n in r.items() if _is_real(n, lid)}
        except Exception:
            pass

    def _best_name(lid: int) -> str:
        if _is_real(db_names.get(lid), lid):
            return db_names[lid]
        if _is_real(resolved.get(lid), lid):
            return resolved[lid]
        return f"Private structure · {lid}"

    options = [
        {
            "location_id": lid,
            "name": _best_name(lid),
            "count": len(types),
            "selected": (lid in selected_ids) if explicit else (lid == default_station),
        }
        for lid, types in seen_types.items()
    ]
    options.sort(key=lambda o: (-o["count"], o["name"]))
    return options


def _derive_job_splits(
    conn: Connection,
    root,
    *,
    max_days: float,
    te: int,
    industry_level: int,
    adv_industry_level: int,
    mfg_facility,
    rxn_facility,
    char_skills: dict,
) -> dict[int, int]:
    """Runs-per-job per product, so no single job runs longer than `max_days`.

    A day limit produces a different run count for every product — a 2-hour
    reaction and a 3-day capital part hit the same ceiling at wildly different
    run counts — which is why this returns a map rather than one number.

    The per-run time is computed exactly as the job-duration loop below computes
    it (same TE rule, same skills, same per-product facility multiplier). It has
    to be: the split decides the material totals, so if this disagreed with the
    displayed job durations the Materials tab and the Jobs list would drift
    apart, which is the whole failure this two-pass exists to prevent.
    """
    from app.manufacturing.schedule import max_runs_per_job

    if not max_days or max_days <= 0:
        return {}

    seen: dict[int, tuple[int, str]] = {}       # type_id → (blueprint_id, activity)

    def walk(node):
        if node.is_leaf or not node.blueprint_type_id:
            return
        seen.setdefault(node.type_id,
                        (node.blueprint_type_id, node.activity or "manufacturing"))
        for child in node.children:
            walk(child)

    walk(root)
    if not seen:
        return {}

    bp_ids = {bp for bp, _ in seen.values()}
    with _connect() as _tc:
        times = {r[0]: (r[1], r[2]) for r in _tc.execute(
            text("SELECT blueprint_type_id, manufacturing_time, reaction_time"
                 " FROM sde_blueprints WHERE blueprint_type_id IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": list(bp_ids)})}

    splits: dict[int, int] = {}
    # One connection for the whole loop: `get_product_te_multiplier` is called
    # once per product, and this function is only reached when a day limit is
    # set, so opening per iteration would be a fresh sqlite3 handle per product.
    with _connect() as _ic:
        for type_id, (bp_id, activity) in seen.items():
            row = times.get(bp_id)
            if not row:
                continue
            is_rxn = activity == "reaction"
            base_time = row[1] if is_rxn else row[0]
            if not base_time:
                continue
            sci_mult, _details = _science_skill_mult(conn, bp_id, activity, char_skills)
            per_run = calc_job_time(
                base_time=base_time,
                runs=1,
                te=0 if is_rxn else te,
                industry_level=industry_level,
                adv_industry_level=adv_industry_level,
                facility_te_multiplier=get_product_te_multiplier(
                    _ic, rxn_facility if is_rxn else mfg_facility, type_id),
                is_reaction=is_rxn,
                science_skill_mult=sci_mult,
            )
            limit = max_runs_per_job(per_run, max_days)
            if limit:
                splits[type_id] = limit
    return splits


def _split_step_jobs(steps: list[dict], splits: dict[int, int]) -> None:
    """Expands each aggregated job into the jobs you would actually install.

    `_build_manufacturing_steps` produces one entry per product carrying the
    whole run count. Once a day limit applies, that single 260-day row is a
    fiction — the materials were costed as ten separate jobs, so the job list
    has to show ten. Mutates `steps` in place, preserving order.

    Per-job input quantities are scaled by run share. They are indicative: ME
    rounds per job, so the true per-job draw varies by a unit here and there.
    The Materials tab is the authoritative total, and it is computed from the
    same splits.
    """
    for step in steps:
        expanded: list[dict] = []
        for job in step["jobs"]:
            per_job = splits.get(job["type_id"])
            total_runs = job.get("runs") or 0
            pieces = _schedule_split_runs(total_runs, per_job)
            if len(pieces) <= 1:
                expanded.append(job)
                continue
            for runs in pieces:
                share = runs / total_runs
                clone = dict(job)
                clone["runs"] = runs
                clone["quantity"] = round(job.get("quantity", 0) * share)
                if job.get("total_price"):
                    clone["total_price"] = job["total_price"] * share
                if job.get("input_cost"):
                    clone["input_cost"] = job["input_cost"] * share
                clone["inputs"] = [
                    {**inp,
                     "quantity": round(inp.get("quantity", 0) * share),
                     "total_price": (inp["total_price"] * share)
                                    if inp.get("total_price") else inp.get("total_price")}
                    for inp in job.get("inputs", [])
                ]
                clone["split_of"] = len(pieces)
                expanded.append(clone)
        step["jobs"] = expanded


def _schedule_split_runs(total_runs: int, per_job: int | None) -> list[int]:
    from app.manufacturing.schedule import split_runs
    return split_runs(total_runs, per_job)


def _plan_group_ids(conn: Connection, steps: list[dict]) -> dict[int, int]:
    """type_id → SDE group_id for everything in the plan.

    Classification is by group, never by product name — a rename in a patch
    must not silently reclassify a job.
    """
    ids = {job["type_id"] for step in steps for job in step["jobs"]}
    if not ids:
        return {}
    with _connect() as _gc:
        return {r[0]: r[1] for r in _gc.execute(
            text("SELECT type_id, group_id FROM sde_types WHERE type_id IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": list(ids)})}


def _capital_group_lookup(conn: sqlite3.Connection, steps: list[dict]) -> set[int]:
    """type_ids in the plan that are capital components."""
    from app.manufacturing.schedule import CAPITAL_COMPONENT_GROUPS

    return {tid for tid, gid in _plan_group_ids(conn, steps).items()
            if gid in CAPITAL_COMPONENT_GROUPS}


def _schedule_step(step: dict, limits, capital_ids: set[int]) -> tuple[int, int]:
    """(manufacturing seconds, reaction seconds) for one level, across slots.

    The two pools run concurrently, so the caller adds each to its own running
    total exactly as it did when the figure was "longest job in the level".
    """
    from app.manufacturing.schedule import Job, _pack, _pack_manufacturing

    reactions, manufacturing = [], []
    for job in step["jobs"]:
        secs = job.get("job_duration_seconds")
        if not secs:
            continue
        entry = Job(
            type_id=job["type_id"], name=job.get("name", ""),
            activity=job.get("activity", "manufacturing"),
            runs=job.get("runs", 1) or 1, seconds=secs,
            is_capital=job["type_id"] in capital_ids,
        )
        (reactions if entry.activity == "reaction" else manufacturing).append(entry)
    return _pack_manufacturing(manufacturing, limits), _pack(reactions, limits.reaction)


def _build_manufacturing_steps(root, prices: dict, available: dict,
                               input_basis: str = "sell") -> list[dict]:
    """
    Manufacturing steps: level 1 = manufactured first (everything from RAW), level N = last.
    Deduplicates the same type_id across branches, aggregates quantities.
    """
    from collections import defaultdict

    price_idx = 1 if input_basis == "buy" else 0
    level_memo: dict[int, int] = {}

    def manufacture_level(node) -> int:
        if node.is_leaf:
            return 0
        if node.type_id in level_memo:
            return level_memo[node.type_id]
        child_levels = [manufacture_level(c) for c in node.children]
        non_zero = [l for l in child_levels if l > 0]
        result = 1 + max(non_zero) if non_zero else 1
        level_memo[node.type_id] = result
        return result

    aggregated: dict[int, dict] = {}
    inputs_agg: dict[int, dict[int, dict]] = {}

    def collect(node):
        if node.is_leaf:
            return
        for child in node.children:
            collect(child)

        tid   = node.type_id
        level = manufacture_level(node)
        sell_p = prices.get(tid, (None, None))[price_idx]

        if tid not in aggregated:
            aggregated[tid] = {
                "type_id":           tid,
                "name":              node.name,
                "quantity":          node.quantity,
                "runs":              node.runs,
                "per_run":           getattr(node, "product_qty_per_run", 1),
                "blueprint_type_id": node.blueprint_type_id,
                "level":             level,
                "activity":          node.activity,
                "me":                node.me,
                "unit_price":        sell_p,
                "total_price":       sell_p * node.quantity if sell_p else None,
                "available":         available.get(tid, 0),
            }
            inputs_agg[tid] = {}
        else:
            aggregated[tid]["quantity"] += node.quantity
            # Recompute runs from aggregated quantity instead of summing per-branch
            # runs. Per-branch ceil() rounds up locally; summed it over-states the
            # total. Example: Helium Fuel Block (40/run) needed 5 in Carbon Polymers
            # branch + 5 in Dysporite branch — each rounded to 1 run → sum 2 runs
            # shown to user, but in reality 10 / 40 = 1 run suffices.
            from math import ceil
            per_run = aggregated[tid].get("per_run") or 1
            aggregated[tid]["runs"] = ceil(aggregated[tid]["quantity"] / per_run)
            if sell_p:
                aggregated[tid]["total_price"] = sell_p * aggregated[tid]["quantity"]

        for c in node.children:
            c_sell = prices.get(c.type_id, (None, None))[price_idx]
            if c.type_id not in inputs_agg[tid]:
                inputs_agg[tid][c.type_id] = {
                    "type_id":    c.type_id,
                    "name":       c.name,
                    "quantity":   c.quantity,
                    "is_leaf":    c.is_leaf,
                    "activity":   c.activity,
                    "unit_price": c_sell,
                    "total_price": c_sell * c.quantity if c_sell else None,
                    "available":  available.get(c.type_id, 0),
                }
            else:
                inputs_agg[tid][c.type_id]["quantity"] += c.quantity
                if c_sell:
                    inputs_agg[tid][c.type_id]["total_price"] = (
                        c_sell * inputs_agg[tid][c.type_id]["quantity"]
                    )

    collect(root)

    for tid, job in aggregated.items():
        job["inputs"] = sorted(inputs_agg[tid].values(), key=lambda x: x["name"])
        job["input_cost"] = sum(i["total_price"] for i in job["inputs"] if i["total_price"]) or None

    by_level: defaultdict[int, list] = defaultdict(list)
    for job in aggregated.values():
        by_level[job["level"]].append(job)

    max_level = max(by_level.keys()) if by_level else 1
    steps = []
    for level in sorted(by_level.keys()):
        jobs = sorted(by_level[level], key=lambda x: x["name"])
        steps.append({
            "step":       level,
            "jobs":       jobs,
            "total_cost": sum(j["total_price"] for j in jobs if j["total_price"]) or None,
            "is_final":   level == max_level,
        })
    return steps


def _plan_to_dict(plan, prices, type_name: str, conn: Connection | None = None,
                  input_basis: str = "sell") -> dict:
    price_idx = 1 if input_basis == "buy" else 0
    bp = plan.blueprint
    bp_info = None
    if bp:
        bp_info = {
            "kind": "BPO" if bp.is_original else "BPC",
            "me": plan.me,
            "te": plan.te,
            "runs": "∞" if bp.runs == -1 else bp.runs,
        }

    # Bulk-fetch group names for the "Type" column so the materials table
    # can be sorted by category.
    group_names: dict[int, str] = {}
    if conn is not None and plan.materials:
        ids = list({m.type_id for m in plan.materials})
        if ids:
            with _connect() as _gn:
                rows = _gn.execute(
                    text("""SELECT t.type_id, g.name
                    FROM sde_types t LEFT JOIN sde_groups g
                      ON g.group_id = t.group_id
                    WHERE t.type_id IN :ids""")
                    .bindparams(bindparam("ids", expanding=True)),
                    {"ids": ids},
                ).fetchall()
            group_names = {r[0]: (r[1] or "—") for r in rows}

    materials = []
    for m in sorted(plan.materials, key=lambda x: (x.ok, x.coverage_pct)):
        in_p = prices.get(m.type_id, (None, None))[price_idx]
        materials.append({
            "type_id": m.type_id,
            "name": m.name,
            "group_name": group_names.get(m.type_id, "—"),
            "required": m.required,
            "available": m.available,
            "missing": m.missing,
            "ok": m.ok,
            "coverage_pct": m.coverage_pct,
            "unit_price": in_p,
            "total_price": in_p * m.required if in_p else None,
            "buy_price": in_p * m.missing if (in_p and m.missing > 0) else None,
        })

    total_buy = sum(m["buy_price"] for m in materials if m["buy_price"])
    # Revenue always uses the product's sell price (what you receive when
    # selling) — the input_basis toggle governs only what you PAY for inputs.
    sell_p, _ = prices.get(plan.product_type_id, (None, None))
    revenue = sell_p * plan.quantity if sell_p else None
    profit = (revenue - total_buy) if (revenue and total_buy) else None

    return {
        "product_name": type_name,
        "product_type_id": plan.product_type_id,
        "quantity": plan.quantity,
        "mode": plan.mode,
        "blueprint": bp_info,
        "location_id": plan.location_id,
        "can_manufacture": plan.can_manufacture,
        "total_missing_types": plan.total_missing_types,
        "materials": materials,
        "opt_total_cost": plan.opt_total_cost,
        "opt_naive_cost": plan.opt_naive_cost,
        "total_buy": total_buy,
        "sell_price": sell_p,
        "revenue": revenue,
        "profit": profit,
        # Expected cost of the invented blueprints in this tree. Deliberately NOT
        # folded into total_buy or profit: total_buy is the shopping list (what
        # you still have to acquire) and `profit` above already excludes job fees
        # for the same reason. Folding invention in alone would make that line
        # inconsistent in a new way instead of fixing it — see the note in §8 of
        # the design doc.
        "invention_cost": plan.invention_cost,
        "invention_unpriced": plan.invention_unpriced,
    }
