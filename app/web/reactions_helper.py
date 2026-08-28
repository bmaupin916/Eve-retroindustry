"""View model for the Reactions board (`/reactions`).

Where `/margins` tracks a watchlist you curate, this prices the **entire**
reaction space on every load. That completeness is the whole point: a curated
list can only rank things you already suspected were worth running, so it
structurally cannot answer "what should I *not* make" — you would have to think
to add the loser first. The board carries the unprofitable rows explicitly.

Affordable because the space is small and fixed. 119 published reaction products
priced end to end measured ~1.7 s, so there is no snapshot table and no
background job here: the page recomputes, and what it shows is current as of the
market cache behind it.

**Why 119 and not the 111 in the design doc.** §9.1 counts Intermediate Materials
41, Composite 17, Biochemical 32, Hybrid Polymers 9 and Molecular-Forged 12. That
misses the eighth group — 8 **Unrefined Mineral** products, the alchemy branch.
They are included here and flagged, because their economics differ in a way a
profit column cannot express on its own: the output is an intermediate you are
expected to *reprocess* (§9.2's flat 0.55 factor), so the sell price priced here
is what the unrefined item fetches as-is, which is not why anyone runs them.
Flagging beats excluding — dropping eight rows silently would recreate exactly
the blind spot the board exists to remove.

**Slot-hours use this reaction's own job time, not the tree's.** `MarginRow`
carries `build_seconds` for the whole tree resolved to raw, which answers "how
long to build this from scratch" — a different question from "what does an hour
of one reaction slot earn", and much longer for composites whose inputs are
themselves reactions. A slot metric built on the tree total would rank every
composite far too low. The per-job time is recomputed here from `reaction_time`.

The jobs-per-month figure is **floored**, following the spreadsheet model in
§9.2: a 25-day job fits once into 30 days, and a pure rate credits it with 1.2.
Both views ship — `isk_per_slot_hour` is the rate, `monthly_profit` is the
floored period — because they disagree precisely for the long jobs where the
distinction decides whether a slot is worth committing.
"""
from __future__ import annotations

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from app.db.conn import (NO_SUCH_TABLE as _NO_SUCH_TABLE,
                         recover_from_missing_table as _recover)

from app.bom.resolver import BOMResolver
from app.manufacturing.margins import _station_context
from app.manufacturing.planner import calc_job_time
from app.market.taxes import BuyingCosts, buying_costs, selling_costs
from app.web.app_defaults import get_defaults, is_configured
from app.web.industry_helper import get_adjusted_prices_cached

# 7 days, single job — "7d 1j". The spreadsheet used 30d; a week is the more
# useful planning window and the arithmetic is identical, only floored over a
# shorter period. The sheet's own footnote makes the point that you do not
# actually start 30-day jobs, you cycle short ones.
PERIOD_SECONDS = 7 * 24 * 3600              # 604_800
PERIOD_LABEL = "7d 1j"

# Kept for anything still reasoning in months.
SECONDS_PER_MONTH = 30 * 24 * 3600

# Alchemy. Output is meant to be reprocessed, not sold as-is — see the module
# docstring. Matched on the SDE group name, which is stable.
ALCHEMY_GROUP = "Unrefined Mineral"

# A sell price this many times the buy price means nobody is actually trading the
# item: it is one optimistic order on a thin market, not a price you could
# realise. Observed live at 144x — Pure Strong X-Instinct Booster listed at 5.0M
# sell against a 34.7k buy, which alone put it top of the board with a 92.9%
# margin and 24 billion ISK a month of imaginary profit. Liquid items sit near
# 1.0-1.5x; 5x is loose enough not to catch a merely wide spread.
THIN_MARKET_RATIO = 5.0

# Every column is sortable, so the sort keys are defined once here and the
# template renders headers from this list — a header with no entry could
# otherwise link to a sort that silently falls back, which looks like the click
# did nothing.
COLUMNS: list[tuple[str, str, str]] = [
    # (key, heading, alignment)
    ("name",      "Reaction",      "start"),
    ("sell",      "Sell",          "end"),
    ("inputs",    "Inputs",        "end"),
    ("fee",       "Fee",           "end"),
    ("profit",    "Profit / job",  "end"),
    ("margin",    "Margin",        "end"),
    ("slot_hour", "ISK / slot-hr", "end"),
    ("jobs",      "Jobs / mo",     "end"),
    ("monthly",   "Profit / mo",   "end"),
]

_SORT_KEYS = {
    "sell":      lambda r: r["sell_price"],
    "inputs":    lambda r: r["material_cost"],
    "fee":       lambda r: r["job_fee"],
    "profit":    lambda r: r["per_job_profit"],
    "margin":    lambda r: r["margin_pct"],
    "slot_hour": lambda r: r["isk_per_slot_hour"],
    "jobs":      lambda r: r["jobs_per_month"],
    "monthly":   lambda r: r["monthly_profit"],
    # wide layout
    "gross":      lambda r: r["gross"],
    "cost_int":   lambda r: r["cost_int"],
    "cost_raw":   lambda r: r["cost_raw"],
    "export":     lambda r: r["export"],
    "import_int": lambda r: r["import_int"],
    "net_int":    lambda r: r["net_int"],
    "net_raw":    lambda r: r["net_raw"],
    "sh_int":     lambda r: r["slot_hour_int"],
    "sh_raw":     lambda r: r["slot_hour_raw"],
    "margin_int": lambda r: r["margin_int"],
    "margin_raw": lambda r: r["margin_raw"],
    "build_adv":  lambda r: r["build_advantage"],
    "sell_adv":   lambda r: r["sell_advantage"],
}

# The Composite group gets the spreadsheet's two-model layout: every money
# column exists twice, once for buying the intermediates and once for building
# them from raw, because the difference between those two is the decision.
WIDE_COLUMNS: list[tuple[str, str, str]] = [
    ("name",       "Complex Composite", "start"),
    ("gross",      "Gross",             "end"),
    ("cost_int",   "Job cost · int",    "end"),
    ("cost_raw",   "Job cost · raw",    "end"),
    ("import_int", "Import · int",      "end"),
    ("export",     "Export",            "end"),
    ("net_int",    "Net · int",         "end"),
    ("net_raw",    "Net · raw",         "end"),
    ("sh_int",     "ISK/slot-hr · int", "end"),
    ("sh_raw",     "ISK/slot-hr · raw", "end"),
    ("margin_int", "Margin · int",      "end"),
    ("margin_raw", "Margin · raw",      "end"),
    ("build_adv",  "Build adv.",        "end"),
    ("sell_adv",   "Sell adv.",         "end"),
]

# Groups rendered with WIDE_COLUMNS. Only Composite for now — an Intermediate
# Material has no intermediate inputs, so its int/raw pair would be the same
# number twice and the layout would imply a choice that does not exist.
WIDE_GROUPS = {"Composite"}

DEFAULT_SORT = "slot_hour"
DEFAULT_DIR = "desc"


def list_reaction_products(conn: Connection) -> list[dict]:
    """Every published product with a reaction blueprint, with its group.

    Published only: the SDE carries unpublished test and legacy entries that no
    character can build, and they would sit in the ranking as noise.
    """
    rows = conn.execute(text("""
        SELECT DISTINCT p.product_type_id, t.name, COALESCE(g.name, '—')
        FROM sde_blueprint_products p
        JOIN sde_types t  ON t.type_id  = p.product_type_id
        LEFT JOIN sde_groups g ON g.group_id = t.group_id
        WHERE p.activity = 'reaction' AND t.published = 1
        ORDER BY g.name, t.name
    """)).fetchall()
    # Indexed, not keyed. SQLAlchemy's Row supports attribute access and
    # `._mapping`, not string subscripting, and the two backends label the
    # `COALESCE` column differently — position is the one thing that is the
    # same everywhere.
    return [{"type_id": r[0], "name": r[1], "group_name": r[2]} for r in rows]


def _input_cost(resolver: BOMResolver, conn: Connection, type_id: int,
                blueprint, prices: dict[int, float | None], ctx: dict,
                volumes: dict[int, float] | None = None,
                ) -> tuple[float, float, list[str], float]:
    """Cost of ONE run priced on the reaction's **direct inputs**, at market.

    Returns (material cost, install fee, names of unpriced inputs, hauled m³).

    Every direct input under this model is bought, so all of it is hauled —
    which is the whole difference from the build-from-raw side, where only the
    leaves are. Charging freight on one model and not the other would bias
    Build Advantage, and Build Advantage is the delta between them.

    This — not a recursive resolve to raw — is what a reaction costs to run.
    You buy the moon goo and the intermediates and you react them; nobody makes
    Tungsten Carbide by first manufacturing fuel blocks. Pricing the full tree
    instead answers a different question and answers it badly at this scale: a
    Tungsten Carbide run needs 5 Nitrogen Fuel Blocks, a fuel block job yields
    40, and `resolve()` correctly refuses to run 1/8 of a job — so the tree
    charges a whole 40-block run, three times over (once directly, once inside
    Rolled Tungsten Alloy, once inside Sulfuric Acid). 120 blocks of inputs to
    consume 15, and the row reads -247% when buying the inputs yields +23%.

    That rounding is right for a build plan and wrong for a rate, which is why
    the build-from-raw figure is kept as a *comparison* column (see
    `build_advantage`) rather than as the headline.

    Material quantities still go through the resolver's own ME path, so the
    structure and rig bonuses that apply to reactions — and EVE's floor of one
    unit per run — behave exactly as they do everywhere else.
    """
    materials = resolver.get_materials(blueprint["blueprint_type_id"], "reaction")
    mult = resolver._product_facility_multiplier(type_id, ctx["rxn_facility"])
    adjusted = resolver._adj_prices or {}

    vols = volumes or {}
    cost = 0.0
    eiv = 0.0
    hauled = 0.0
    unpriced: list[str] = []
    for mat in materials:
        mat_id = mat["material_type_id"]
        qty = resolver._apply_me(mat["quantity"], 1, 0.0, mult, runs_per_job=None)
        hauled += qty * vols.get(mat_id, 0.0)
        unit = prices.get(mat_id)
        if unit is None:
            unpriced.append(mat["name"])
        else:
            cost += unit * qty
        # The install fee is EIV × rate, and EIV uses BASE (pre-ME) quantities
        # with CCP's adjusted prices — not the market price and not the reduced
        # quantity. Same formula the resolver uses for every other job.
        eiv += (adjusted.get(mat_id, 0.0) or 0.0) * mat["quantity"]
    return cost, eiv * ctx["rate_rxn"], unpriced, hauled


def raw_unit_cost(resolver: BOMResolver, type_id: int, prices: dict[int, float | None],
                  ctx: dict, memo: dict, unpriced: set[str],
                  _visiting: frozenset[int] = frozenset()) -> float | None:
    """Cost of ONE unit — the cost half of `raw_unit_cost_and_volume`.

    A wrapper rather than a second recursion. Two walks of the same tree would
    have to agree about ME, EVE's one-unit floor and the cycle rule, and this
    codebase has twice been bitten by exactly that kind of pair drifting apart.
    """
    pair = raw_unit_cost_and_volume(resolver, type_id, prices, ctx, memo,
                                    unpriced, _visiting=_visiting)
    return None if pair is None else pair[0]


def raw_unit_cost_and_volume(
    resolver: BOMResolver, type_id: int, prices: dict[int, float | None],
    ctx: dict, memo: dict, unpriced: set[str],
    volumes: dict[int, float] | None = None,
    _visiting: frozenset[int] = frozenset(),
) -> tuple[float, float] | None:
    """(cost, imported m³) for ONE unit, building everything that can be built.

    **The volume is what you haul in, not what the tree contains.** A material
    you *buy* contributes its own packaged m³; one you *build* contributes its
    inputs' m³ instead, because you never haul the intermediate — you make it
    where you are. Freight is charged at the boundary of the operation, never
    between its own runs.

    `volumes` may be omitted when only the cost is wanted, in which case the
    volume half is zero throughout and costs nothing to carry.

    Cost of ONE unit, building everything that can be built, down to raw.

    The counterpart to `_input_cost`: that one buys the intermediates, this one
    makes them. The difference between the two margins is the Build Advantage,
    and it only means anything if both sides are costed honestly.

    **Whole units of an intermediate, amortised sub-runs.** If a job needs 4.8
    fuel blocks it needs 5 — quantities are whole, and `_apply_me` already
    ceilings them. But needing 5 does not mean building 40: the per-unit cost of
    a fuel block is one run's materials divided by its yield, and you are charged
    for 5 of those. Charging a whole 40-block job — which is what
    `BOMResolver.resolve()` does, correctly, when planning an actual build — read
    -247% on Tungsten Carbide against +14% for buying the same inputs, because
    a single run needs 5 blocks in three separate places and paid for 120.

    Install fees are amortised the same way, per sub-job, so the Raw column's
    job cost is the whole chain's fees rather than only the final reaction's.

    Returns None when anything underneath has no price: a partially-priced raw
    cost is lower than the truth, and a Build Advantage computed from it would
    favour building for a reason that is purely missing data.
    """
    vols = volumes or {}

    def bought(tid: int) -> tuple[float, float] | None:
        """What a purchased unit costs and what hauling it costs volume-wise."""
        price = prices.get(tid)
        return None if price is None else (price, vols.get(tid, 0.0))

    if type_id in memo:
        return memo[type_id]
    # A blueprint whose inputs eventually include its own product would recurse
    # forever. Treat the repeat as something you buy, which is what you would
    # actually do, rather than unwinding the loop. Bought means hauled, so it
    # carries its own volume.
    if type_id in _visiting:
        return bought(type_id)

    blueprint = resolver.find_blueprint(type_id)
    if blueprint is None:
        pair = bought(type_id)
        if pair is None:
            unpriced.add(resolver.get_type_name(type_id))
        memo[type_id] = pair
        return pair

    activity = blueprint["activity"]
    facility = ctx["rxn_facility"] if activity == "reaction" else ctx["mfg_facility"]
    rate = ctx["rate_rxn"] if activity == "reaction" else ctx["rate_mfg"]
    mult = resolver._product_facility_multiplier(type_id, facility)
    adjusted = resolver._adj_prices or {}
    per_run = int(blueprint["product_qty"] or 1)

    total = 0.0
    eiv = 0.0
    hauled = 0.0
    for mat in resolver.get_materials(blueprint["blueprint_type_id"], activity):
        qty = resolver._apply_me(mat["quantity"], 1, 0.0, mult, runs_per_job=None)
        pair = raw_unit_cost_and_volume(
            resolver, mat["material_type_id"], prices, ctx, memo, unpriced,
            volumes, _visiting | {type_id})
        if pair is None:
            memo[type_id] = None
            return None
        unit, unit_vol = pair
        total += qty * unit
        # This node is built, so it contributes what its inputs weigh rather
        # than what it weighs — nothing hauls an intermediate you make.
        hauled += qty * unit_vol
        eiv += (adjusted.get(mat["material_type_id"], 0.0) or 0.0) * mat["quantity"]

    runs = max(1, per_run)
    memo[type_id] = ((total + eiv * rate) / runs, hauled / runs)
    return memo[type_id]


def _prices(conn: Connection, type_ids: set[int], basis: str,
            costs: BuyingCosts | None = None) -> dict[int, float | None]:
    """{type_id: unit price} on the given basis, custom overrides winning.

    Deliberately the same rule the margin tracker follows — an override is a
    deliberate "this is what it actually costs me" statement, so it beats the
    market on both sides. A second, subtly different price path would make this
    page disagree with `/margins` about the same material.

    `costs` adds the acquisition broker fee for an input basis. It is applied
    to the *market* price only and before overrides land, for the same reason
    overrides win at all: a stated real cost already includes whatever it
    really cost, and marking it up would contradict the statement. Pass None
    for the output basis — that is the selling side, and its costs come from
    `selling_costs`.
    """
    if not type_ids:
        return {}
    ids = list(type_ids)
    out: dict[int, float | None] = {}
    for tid, sell, buy in conn.execute(
        text("SELECT type_id, sell_price, buy_price FROM market_price_cache"
             " WHERE type_id IN :ids")
        .bindparams(bindparam("ids", expanding=True)),
        {"ids": ids},
    ):
        chosen = buy if basis == "buy" else sell
        chosen = chosen or None
        out[tid] = costs.paid(chosen) if costs else chosen
    try:
        for tid, override in conn.execute(
            text("SELECT type_id, price FROM custom_price_override"
                 " WHERE type_id IN :ids")
            .bindparams(bindparam("ids", expanding=True)),
            {"ids": ids},
        ):
            out[tid] = override or None
    except _NO_SUCH_TABLE:
        _recover(conn)            # overrides table not created yet
    return out


def _volumes(conn: Connection) -> dict[int, float]:
    """{type_id: packaged m³}. Packaged, because that is what a hauler carries."""
    return {r[0]: r[1] for r in conn.execute(text(
        "SELECT type_id, packaged_volume FROM sde_types"
        " WHERE packaged_volume IS NOT NULL"))}


def _sell_venue(conn: Connection, defaults: dict) -> dict:
    """Where output is sold, and the cached prices there.

    Jita is the baseline — it is what `market_price_cache` holds and what every
    other venue is measured against, so choosing it means there is no advantage
    to compute and the column says so rather than showing a misleading zero.

    Any other hub is priced from `hub_price_cache`, which is filled on demand
    from /prices. **It is empty until someone fetches that hub**, and an empty
    cache must read as "not fetched" rather than "no advantage" — the two look
    identical in a number and mean opposite things.
    """
    from app.market.prices import TRADE_HUBS

    region_id = int(defaults.get("sell_hub_region_id") or 0)
    structure_id = int(defaults.get("sell_structure_id") or 0)

    if structure_id:
        return {"kind": "structure", "name": f"structure {structure_id}",
                "prices": {}, "fetched": False}
    if not region_id or region_id not in TRADE_HUBS:
        return {"kind": "jita", "name": "Jita", "prices": {}, "fetched": True}

    prices = {r[0]: r[1] for r in conn.execute(
        text("SELECT type_id, sell_price FROM hub_price_cache"
             " WHERE region_id = :rid"), {"rid": region_id})}
    return {
        "kind": "hub",
        "name": TRADE_HUBS[region_id]["name"],
        "prices": prices,
        "fetched": bool(prices),
    }


def _sell_advantage(type_id: int, venue: dict, jita_sell: float | None,
                    volume: float, export_rate: float) -> float | None:
    """How much better selling locally is than hauling to Jita, as a fraction.

        (local − (jita − freight_per_unit)) / local

    The freight term is the whole point: a worse local price still wins once you
    stop paying to move the goods. Jita itself has nothing to compare against, so
    it returns None rather than 0 — "no advantage" and "you are already there"
    are different statements, and a 0.0 in the column would assert the first.
    """
    if venue["kind"] == "jita" or not venue.get("fetched"):
        return None
    local = venue["prices"].get(type_id)
    if not local or not jita_sell:
        return None
    return (local - (jita_sell - volume * export_rate)) / local


def _job_seconds(base_time: int, defaults: dict, ctx: dict) -> int:
    """Seconds for ONE run of this reaction — the slot occupancy.

    `base_time` comes from the same `find_blueprint()` row the pricing used.
    That is deliberate: the SDE carries two reaction blueprints for Tungsten
    Carbide — the real formula (yield 10,000, 10,800 s) and a leftover "Test
    Reaction Blueprint" (yield 20, 360 s), 500× apart. Selecting the blueprint
    independently here would let the board time a job with one recipe while
    costing it with the other, and the row would look catastrophically
    unprofitable for a reason nothing on the page could explain.

    Reactions take no Industry / Advanced Industry bonus and have no TE
    research, so the only modifier is the facility's. `calc_job_time` is given
    the skill levels anyway and ignores them for reactions, rather than this
    module deciding separately what applies and drifting from it.
    """
    if not base_time:
        return 0
    return calc_job_time(
        base_time,
        runs=1,
        te=0,
        industry_level=int(defaults.get("industry_skill") or 0),
        adv_industry_level=int(defaults.get("adv_industry_skill") or 0),
        is_reaction=True,
        facility_te_multiplier=ctx["rxn_te_mult"],
    )


def build_board(conn: Connection, db_path: str,
                sort: str = DEFAULT_SORT, direction: str = DEFAULT_DIR,
                group: str = "") -> dict:
    """Price every reaction and rank it.

    `sort`, `direction` and `group` only shape presentation — every row is
    priced regardless, so the counts in the summary describe the whole space
    rather than the current filter. All three are sanitised here rather than
    trusted: they arrive from a query string a user can hand-edit.
    """
    defaults = get_defaults(conn)
    view: dict = {
        "configured": is_configured(defaults),
        "defaults": defaults,
        "rows": [],
        "groups": [],
        "sort": sort if (sort in _SORT_KEYS or sort == "name") else DEFAULT_SORT,
        "dir": "asc" if direction == "asc" else "desc",
        "columns": COLUMNS,
        "wide": False,
        "period_label": PERIOD_LABEL,
        "group": group,
        "selling": selling_costs(defaults),
        "alchemy_group": ALCHEMY_GROUP,
        "sci_cached": True,
        "counts": {"total": 0, "profitable": 0, "unprofitable": 0,
                   "unpriced": 0, "unreliable": 0, "thin": 0},
    }
    if not view["configured"]:
        # Without a station there is no system cost index, so every profit
        # figure would be fiction. The page says so instead of showing numbers.
        return view

    products = list_reaction_products(conn)
    ctx = _station_context(conn, defaults)
    view["sci_cached"] = ctx["sci_cached"]
    # One resolver for the whole board. It resolves product → blueprint by the
    # same rules the pricing uses (excluding the SDE's test/QA/tournament
    # blueprints), and its ME path is reused so reaction rig and structure
    # bonuses are applied here exactly as they are everywhere else. Its lookups
    # are cached, so this costs one query per product rather than one per call.
    resolver = BOMResolver(
        conn, blueprints=[], runs_per_job=None,
        adjusted_prices=get_adjusted_prices_cached(conn),
        rate_mfg=ctx["rate_mfg"], rate_rxn=ctx["rate_rxn"],
    )
    raw_basis = str(defaults.get("raw_input_basis")
                    or defaults.get("input_basis") or "buy")
    int_basis = str(defaults.get("intermediate_input_basis")
                    or defaults.get("input_basis") or "sell")
    # Dumping into buy orders pays the BUY price. `sales_method` used to drop the
    # broker fee while still valuing the output at the sell price, which
    # overstated profit twice over for anyone selling that way.
    out_basis = "buy" if str(defaults.get("sales_method")) == "immediate" else "sell"
    export_rate = float(defaults.get("freight_export_isk_m3") or 0.0)
    import_rate = float(defaults.get("freight_import_isk_m3") or 0.0)
    costs = selling_costs(defaults)
    local = _sell_venue(conn, defaults)
    venue_info = local

    all_ids = {r[0] for r in conn.execute(
        text("SELECT type_id FROM market_price_cache"))}
    # Inputs carry the acquisition broker fee; the output does not — that side
    # is priced by `selling_costs`, and charging both would double-count.
    raw_prices = _prices(conn, all_ids, raw_basis, buying_costs(raw_basis, defaults))
    int_prices = _prices(conn, all_ids, int_basis, buying_costs(int_basis, defaults))
    out_prices = _prices(conn, all_ids, out_basis)
    volumes = _volumes(conn)
    # (cost, hauled m3) per unit, keyed by type. Pairs rather than a bare
    # cost so one walk of the tree answers both, and the two can never
    # disagree about ME or the one-unit floor.
    raw_memo: dict = {}

    rows: list[dict] = []
    try:
        for product in products:
            type_id = product["type_id"]
            blueprint = resolver.find_blueprint(type_id)
            if blueprint is None:
                continue
            per_run = int(blueprint["product_qty"] or 1)

            material_cost, job_fee, unpriced, in_vol_run = _input_cost(
                resolver, conn, type_id, blueprint, int_prices, ctx, volumes)

            raw_unpriced: set[str] = set()
            raw_pair = raw_unit_cost_and_volume(
                resolver, type_id, raw_prices, ctx, raw_memo, raw_unpriced,
                volumes)
            raw_per_unit = raw_pair[0] if raw_pair else None
            raw_vol_unit = raw_pair[1] if raw_pair else None

            sell = out_prices.get(type_id)
            buy = _prices(conn, {type_id}, "buy").get(type_id)
            ref_sell = _prices(conn, {type_id}, "sell").get(type_id)
            thin = bool(ref_sell and buy and ref_sell > buy * THIN_MARKET_RATIO)

            seconds = _job_seconds(blueprint["reaction_time"], defaults, ctx)
            jobs = PERIOD_SECONDS // seconds if seconds else 0
            hours = (seconds / 3600) if seconds else 0.0

            units = per_run * jobs
            gross = (sell or 0.0) * units
            selling = costs.on(gross)
            # Export is charged on what leaves: output volume over the period.
            # Zero when no freight rate is configured, which is right for anyone
            # building and selling in the same station.
            export = units * volumes.get(type_id, 0.0) * export_rate

            # Import is the mirror of export and charged the same flat way:
            # what you haul in, times the rate. The two models haul different
            # things — buying the intermediates means hauling intermediates,
            # building from raw means hauling the moon goo — so each is charged
            # on its own purchased set. Nothing is charged between runs: an
            # intermediate you make never moves.
            import_int = in_vol_run * jobs * import_rate
            import_raw = ((raw_vol_unit * units * import_rate)
                          if raw_vol_unit is not None else None)

            cost_int = (material_cost + job_fee) * jobs + import_int
            cost_raw = ((raw_per_unit * units + import_raw)
                        if raw_per_unit is not None and import_raw is not None
                        else None)

            net_int = gross - cost_int - selling - export
            net_raw = (gross - cost_raw - selling - export) if cost_raw is not None else None

            per_job_profit = (net_int / jobs) if jobs else None
            margin_int = (net_int / gross * 100) if gross else None
            margin_raw = (net_raw / gross * 100) if (gross and net_raw is not None) else None
            # Positive means building the intermediates yourself beats buying
            # them. The optimiser already decides this per node; the delta is
            # what says how close the call was, which is what matters for a
            # standing decision.
            build_adv = (margin_raw - margin_int) if (
                margin_raw is not None and margin_int is not None) else None

            rows.append({
                "type_id": type_id,
                "name": product["name"],
                "group_name": product["group_name"],
                "is_alchemy": product["group_name"] == ALCHEMY_GROUP,
                "per_run": per_run,
                "sell_price": sell,
                "buy_price": buy,
                "thin_market": thin,
                "unpriced": unpriced,
                "raw_unpriced": sorted(raw_unpriced),
                "reliable": bool(sell) and not unpriced and not thin,
                "material_cost": material_cost,
                "job_fee": job_fee,
                "selling_cost": costs.on((sell or 0.0) * per_run),
                "job_seconds": seconds,
                "jobs_per_month": jobs,
                "per_job_profit": per_job_profit,
                "margin_pct": margin_int,
                "isk_per_slot_hour": (net_int / (hours * jobs)) if (hours and jobs) else None,
                "monthly_profit": net_int if jobs else None,
                # ── the wide (Composite) layout ──────────────────────────
                "gross": gross if jobs else None,
                "cost_int": cost_int if jobs else None,
                "cost_raw": cost_raw,
                "export": export if jobs else None,
                "import_int": import_int if jobs else None,
                "import_raw": import_raw if jobs else None,
                "net_int": net_int if jobs else None,
                "net_raw": net_raw,
                "margin_int": margin_int,
                "margin_raw": margin_raw,
                "slot_hour_int": (net_int / (hours * jobs)) if (hours and jobs) else None,
                "slot_hour_raw": (net_raw / (hours * jobs)) if (hours and jobs and net_raw is not None) else None,
                "build_advantage": build_adv,
                "sell_advantage": _sell_advantage(
                    type_id, local, ref_sell, volumes.get(type_id, 0.0), export_rate),
            })
    finally:
        pass

    counts = view["counts"]
    counts["total"] = len(rows)
    counts["unpriced"] = sum(1 for r in rows if r["unpriced"] or r["sell_price"] is None)
    counts["unreliable"] = sum(1 for r in rows if not r["reliable"])
    counts["thin"] = sum(1 for r in rows if r["thin_market"])
    counts["profitable"] = sum(1 for r in rows if (r["per_job_profit"] or 0) > 0)
    counts["unprofitable"] = counts["total"] - counts["profitable"]

    view["groups"] = sorted({r["group_name"] for r in rows})
    # The filter picks the LAYOUT as well as the rows: the wide two-model view
    # only makes sense for one group at a time, and only for groups whose
    # products actually have intermediates to buy or build.
    if group:
        rows = [r for r in rows if r["group_name"] == group]
        if group in WIDE_GROUPS:
            view["wide"] = True
            view["columns"] = WIDE_COLUMNS
            if view["sort"] not in {k for k, _h, _a in WIDE_COLUMNS}:
                view["sort"] = "net_int"
    view["venue"] = venue_info

    view["rows"] = _sorted(rows, view["sort"], view["dir"])
    return view


def _sorted(rows: list[dict], sort: str, direction: str) -> list[dict]:
    """Order by one column, with unreliable rows below reliable ones either way.

    Reliability is applied as a separate, final pass rather than folded into the
    sort key, because it must NOT invert when the user asks for ascending. A row
    is demoted when its cost is incomplete (an unpriced input is costed at zero,
    which only ever flatters) or when its output has no real bid (see
    `THIN_MARKET_RATIO`). Both were live at once on the original top row, which
    recommended reacting something that could not be sold — reversing the
    direction must not hand that row the top of the board back.

    Unknown values go last in both directions too. `None` is not the smallest
    value, it is the absence of one, and floating it to the top of an ascending
    sort would be the same failure in a different costume.

    Demoted, not hidden — the numbers stay visible with the reason attached,
    since "this looks great but nobody trades it" is itself worth knowing.
    """
    reverse = direction != "asc"
    if sort == "name":
        ordered = sorted(rows, key=lambda r: r["name"].lower(), reverse=reverse)
    else:
        key = _SORT_KEYS[sort]
        known = [r for r in rows if key(r) is not None]
        unknown = [r for r in rows if key(r) is None]
        known.sort(key=key, reverse=reverse)
        ordered = known + unknown

    # Stable, so the ordering just established survives inside each tier.
    return sorted(ordered, key=lambda r: r["reliable"], reverse=True)
