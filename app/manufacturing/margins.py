"""Per-item build margin — sell price vs. cost of building one unit.

Powers the margin tracker: a watchlist of products, each priced against your
configured build station, so you can see at a glance which ones are still worth
building today.

**Cache-only.** Nothing here fetches. Prices come from `market_price_cache`,
job-cost inputs from `adjusted_price_cache` and `sci_cache` — all filled by the
normal market/industry refreshes. A watchlist prices dozens of items at once and
would otherwise hammer ESI on every page load. The cost of that choice is that a
price we've never cached is *missing*, not zero: a missing material price would
silently understate cost and overstate profit, so every row reports what it
could not price and the UI marks it rather than showing a confident wrong number.

The model is a full build: buy the raw leaves at market, run every job in the
tree yourself, manufacturing jobs at the build station and reactions at the
reaction station, each with its own system cost index and tax — and then *sell*
it, which is not free. Sales tax and broker's fee come off the top via
`app.market.taxes`; between 4.4% and 10.5% of the sale price never reaches the
wallet, which on a thin margin is the entire margin.

A T2 product is also charged for the blueprint it had to be invented from —
datacores divided by the success chance and spread over the runs on the
resulting BPC, via `app.manufacturing.invention`. Without that, T2 rows were
flattered by a free blueprint while T1 rows were penalised by the conservative
single-run ME rounding below, so the ranking was biased between the two classes
for reasons that were not real.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import sqlite3

from app.bom.resolver import BOMResolver, StationFacility
from app.manufacturing.planner import calc_job_time
from app.market.taxes import selling_costs
from app.manufacturing import invention as invention_mod

# Sales tax component of the official job-cost formula:
#   fee = EIV × (SCI × (1 − structure bonus) + facility tax + SCC)
# Kept in sync with industry_helper._SCC.
SCC = 0.04

JITA_REGION = 10000002


@dataclass
class MarginRow:
    type_id: int
    name: str
    group_name: str
    me: int
    te: int

    units_per_run: int = 1
    sell_price: float | None = None       # per unit
    material_cost: float = 0.0            # per unit
    job_fee: float = 0.0                  # per unit
    # What selling costs, per unit: sales tax always, broker's fee only when
    # listing an order. Held apart from `profit` so the UI can show why the
    # number is lower than the naive sell-minus-cost figure.
    selling_cost: float = 0.0             # per unit
    selling_cost_pct: float = 0.0         # % of sell price
    # Expected invention cost per unit for a T2 product: datacores and any
    # decryptor, divided by the success chance and spread over the runs on the
    # resulting BPC. Zero for anything not invented.
    invention_cost: float = 0.0           # per unit
    invention_chance: float | None = None  # success chance, for the tooltip
    invention_runs: int = 0               # runs on the invented BPC
    profit: float | None = None           # per unit, net of everything
    margin_pct: float | None = None
    volume_m3: float | None = None        # packaged volume of one unit
    profit_per_m3: float | None = None
    day_volume: float | None = None       # avg units traded per day
    build_seconds: int = 0                # for one run, whole tree
    profit_per_hour: float | None = None

    # Everything the row could not price, by name. A non-empty list means
    # `profit` is optimistic and the UI must say so.
    unpriced: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def priced(self) -> bool:
        return self.error is None and not self.unpriced and self.sell_price is not None


def _cached_prices(conn: sqlite3.Connection, type_ids: set[int]) -> dict[int, tuple]:
    """{type_id: (sell, buy)} from the Jita cache, custom overrides winning.

    An override is a deliberate "this is what it really costs me" statement, so
    it beats the market on both sides.
    """
    if not type_ids:
        return {}
    placeholders = ",".join("?" * len(type_ids))
    ids = list(type_ids)
    prices = {r[0]: (r[1], r[2]) for r in conn.execute(
        f"SELECT type_id, sell_price, buy_price FROM market_price_cache "
        f"WHERE type_id IN ({placeholders})", ids)}
    try:
        for type_id, override in conn.execute(
            f"SELECT type_id, price FROM custom_price_override WHERE type_id IN ({placeholders})", ids
        ):
            prices[type_id] = (override, override)
    except sqlite3.OperationalError:
        pass                      # overrides table not created yet
    return prices


def _pick(price: tuple | None, basis: str) -> float | None:
    """Sell = instant-buy off sell orders; buy = patiently place buy orders."""
    if not price:
        return None
    sell, buy = price
    chosen = buy if basis == "buy" else sell
    return chosen if chosen else None


def _avg_day_volume(conn: sqlite3.Connection, type_id: int, days: int = 30) -> float | None:
    """Average units/day traded, from the cached daily history.

    Falls back to the 7-day regional figure on `market_price_cache` when no
    history has been pulled for this type.
    """
    row = conn.execute(
        "SELECT data_json FROM price_history_cache WHERE region_id=? AND type_id=?",
        (JITA_REGION, type_id),
    ).fetchone()
    if row and row[0]:
        import json
        try:
            series = json.loads(row[0])
        except (ValueError, TypeError):
            series = []
        recent = [d.get("volume") or 0 for d in series[-days:] if isinstance(d, dict)]
        if recent:
            return sum(recent) / len(recent)
    fallback = conn.execute(
        "SELECT volume FROM market_price_cache WHERE type_id=?", (type_id,)
    ).fetchone()
    if fallback and fallback[0]:
        return fallback[0] / 7.0          # the cached figure is a 7-day total
    return None


def _station_context(conn: sqlite3.Connection, defaults: dict) -> dict:
    """Cost-index and bonus inputs for both activities, read from cache only."""
    from app.web.industry_helper import (
        get_station_cost_bonus, get_station_facility, get_station_te_multiplier,
    )

    build = int(defaults.get("build_station_id") or 0)
    reaction = int(defaults.get("reaction_station_id") or 0) or build

    def _system(location_id: int) -> int | None:
        if not location_id:
            return None
        row = conn.execute(
            "SELECT solar_system_id FROM location_name_cache WHERE location_id=?",
            (location_id,),
        ).fetchone()
        return row[0] if row and row[0] else None

    def _sci(system_id: int | None, activity: str) -> tuple[float, bool]:
        """(index, cached). Never fetches — an uncached system yields 0.0 and
        `False`, which the caller surfaces rather than quietly under-charging."""
        if not system_id:
            return 0.0, False
        try:
            row = conn.execute(
                "SELECT cost_index FROM sci_cache WHERE solar_system_id=? AND activity=?",
                (system_id, activity),
            ).fetchone()
        except sqlite3.OperationalError:
            return 0.0, False
        return (row[0], True) if row and row[0] is not None else (0.0, False)

    mfg_sci, mfg_cached = _sci(_system(build), "manufacturing")
    rxn_sci, rxn_cached = _sci(_system(reaction), "reaction")
    mfg_tax = float(defaults.get("facility_tax") or 0) / 100
    rxn_tax = float(defaults.get("reaction_facility_tax") or 0) / 100
    mfg_bonus = get_station_cost_bonus(conn, build) if build else 0.0
    rxn_bonus = get_station_cost_bonus(conn, reaction) if reaction else mfg_bonus

    return {
        "build_station_id": build,
        "reaction_station_id": reaction,
        "rate_mfg": mfg_sci * (1.0 - mfg_bonus) + mfg_tax + SCC,
        "rate_rxn": rxn_sci * (1.0 - rxn_bonus) + rxn_tax + SCC,
        "sci_cached": mfg_cached and rxn_cached,
        "mfg_facility": get_station_facility(conn, build) if build else StationFacility(),
        "rxn_facility": get_station_facility(conn, reaction) if reaction else StationFacility(),
        "mfg_te_mult": get_station_te_multiplier(conn, build) if build else 1.0,
        "rxn_te_mult": get_station_te_multiplier(conn, reaction) if reaction else 1.0,
    }


def _build_time(node, te: int, defaults: dict, ctx: dict,
                times: dict[int, tuple]) -> int:
    """Total seconds to run every job in the tree once, sequentially.

    `te` applies to this node only; children recurse with 0. The tracked TE
    belongs to the product's own blueprint — an intermediate component is built
    from whatever blueprint you happen to have, and the tracker does not model
    per-component research.
    """
    if node.is_leaf or not node.blueprint_type_id:
        return 0
    row = times.get(node.blueprint_type_id)
    is_reaction = node.activity == "reaction"
    base = (row[1] if is_reaction else row[0]) if row else 0
    total = 0
    if base:
        total += calc_job_time(
            base,
            runs=node.runs,
            te=te,
            industry_level=int(defaults.get("industry_skill") or 0),
            adv_industry_level=int(defaults.get("adv_industry_skill") or 0),
            is_reaction=is_reaction,
            facility_te_multiplier=ctx["rxn_te_mult"] if is_reaction else ctx["mfg_te_mult"],
        )
    for child in node.children:
        total += _build_time(child, 0, defaults, ctx, times)
    return total


def compute_margin(
    conn: sqlite3.Connection,
    db_path: str,
    type_id: int,
    me: int,
    te: int,
    defaults: dict,
    blueprints: list | None = None,
    ctx: dict | None = None,
) -> MarginRow:
    """Prices one product. `ctx` is `_station_context` output — pass it in when
    pricing a whole watchlist so the station lookups happen once."""
    name_row = conn.execute(
        "SELECT t.name, g.name FROM sde_types t "
        "LEFT JOIN sde_groups g ON g.group_id = t.group_id WHERE t.type_id=?",
        (type_id,),
    ).fetchone()
    row = MarginRow(
        type_id=type_id,
        name=name_row[0] if name_row else f"#{type_id}",
        group_name=(name_row[1] if name_row and name_row[1] else "—"),
        me=me, te=te,
    )
    row.volume_m3 = _packaged_volume(conn, type_id)

    ctx = ctx or _station_context(conn, defaults)
    basis = str(defaults.get("input_basis") or "sell")

    from app.web.industry_helper import get_adjusted_prices_cached
    adjusted = get_adjusted_prices_cached(conn)

    resolver = BOMResolver(
        db_path,
        blueprints=blueprints or [],
        runs_per_job=None,                # one batched job — this is a rate, not a job queue
        adjusted_prices=adjusted,
        rate_mfg=ctx["rate_mfg"],
        rate_rxn=ctx["rate_rxn"],
    )
    try:
        blueprint = resolver.find_blueprint(type_id)
        if blueprint is None:
            row.error = "No blueprint — this product cannot be manufactured."
            return row
        row.units_per_run = int(blueprint["product_qty"] or 1)

        node = resolver.resolve(
            type_id, row.units_per_run, me=me,
            mfg_facility=ctx["mfg_facility"], rxn_facility=ctx["rxn_facility"],
        )

        leaves = node.aggregate_leaves()
        wanted = set(leaves) | {type_id}
        prices = _cached_prices(conn, wanted)

        cost = 0.0
        for leaf_id, (leaf_name, qty) in leaves.items():
            unit = _pick(prices.get(leaf_id), basis)
            if unit is None:
                row.unpriced.append(leaf_name)
                continue
            cost += unit * qty
        row.material_cost = cost / row.units_per_run

        fee = _total_job_fee(node)
        row.job_fee = fee / row.units_per_run

        _apply_invention(conn, row, defaults)

        # The product always sells off sell orders — you are the one listing it.
        sell = _pick(prices.get(type_id), "sell")
        row.sell_price = sell
        if sell is None:
            row.unpriced.append(row.name)
        else:
            costs = selling_costs(defaults)
            row.selling_cost = costs.on(sell)
            row.selling_cost_pct = costs.pct
            row.profit = (sell - row.material_cost - row.job_fee
                          - row.selling_cost - row.invention_cost)
            row.margin_pct = (row.profit / sell * 100) if sell else None
            if row.volume_m3:
                row.profit_per_m3 = row.profit / row.volume_m3

        times = _blueprint_times(conn, node)
        row.build_seconds = _build_time(node, te, defaults, ctx, times)
        if row.profit is not None and row.build_seconds:
            hours = row.build_seconds / 3600
            row.profit_per_hour = (row.profit * row.units_per_run) / hours

        row.day_volume = _avg_day_volume(conn, type_id)
        return row
    finally:
        resolver.close()


def _apply_invention(conn: sqlite3.Connection, row: MarginRow, defaults: dict) -> None:
    """Charge a T2 product for the blueprint it had to be invented from.

    Silently does nothing for anything not invented, which is the common case —
    `find_recipe` returning None is how "not a T2 item" is distinguished from
    "T2 item that happens to cost nothing".

    An unpriced datacore is reported on the row rather than counted as free,
    the same rule the materials follow: understating cost overstates profit,
    which is the one error this page exists to avoid.
    """
    if not int(defaults.get("invent_t2", 1) or 0):
        return
    recipe = invention_mod.find_recipe(conn, row.type_id)
    if recipe is None:
        return

    decryptor = invention_mod.load_decryptor(
        conn, int(defaults.get("decryptor_type_id") or 0))

    science = int(defaults.get("science_skill") or 0)
    levels = {sid: science for sid in recipe.science_skill_ids}
    if recipe.encryption_skill_id:
        levels[recipe.encryption_skill_id] = int(defaults.get("encryption_skill") or 0)

    wanted = {tid for tid, _n, _q in recipe.datacores}
    if decryptor:
        wanted.add(decryptor.type_id)
    prices = _cached_prices(conn, wanted)
    unit_prices = {tid: _pick(prices.get(tid), "sell") for tid in wanted}

    decryptor_price = 0.0
    if decryptor:
        decryptor_price = unit_prices.get(decryptor.type_id) or 0.0
        if unit_prices.get(decryptor.type_id) is None:
            row.unpriced.append(decryptor.name)

    cost = invention_mod.compute_cost(
        recipe,
        prices={k: v for k, v in unit_prices.items() if v is not None},
        skill_levels=levels,
        decryptor=decryptor,
        decryptor_price=decryptor_price,
    )
    row.invention_cost = cost.per_unit
    row.invention_chance = cost.success_chance
    row.invention_runs = cost.runs_per_bpc
    row.unpriced.extend(cost.unpriced)


def _packaged_volume(conn: sqlite3.Connection, type_id: int) -> float | None:
    """Packaged m³, or None when the SDE predates the column.

    None is deliberate: profit-per-m³ is meaningless without it, and a made-up
    volume would rank the whole watchlist wrongly.

    Packaged, not assembled. An assembled Nidhoggur is 11,250,000 m³ and a
    packaged one is 1,300,000 — and it is the packaged figure that decides what
    a hauler carries. 829 types differ, all ships and containers, which is
    exactly the set where profit-per-m³ gets used.
    """
    try:
        row = conn.execute(
            "SELECT packaged_volume FROM sde_types WHERE type_id=?", (type_id,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row and row[0] else None


def _total_job_fee(node) -> float:
    total = node.job_fee or 0.0
    for child in node.children:
        total += _total_job_fee(child)
    return total


def _blueprint_times(conn: sqlite3.Connection, node) -> dict[int, tuple]:
    """{blueprint_type_id: (manufacturing_time, reaction_time)} for the tree."""
    ids: set[int] = set()

    def walk(n):
        if n.blueprint_type_id:
            ids.add(n.blueprint_type_id)
        for child in n.children:
            walk(child)

    walk(node)
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    return {r[0]: (r[1], r[2]) for r in conn.execute(
        f"SELECT blueprint_type_id, manufacturing_time, reaction_time FROM sde_blueprints "
        f"WHERE blueprint_type_id IN ({placeholders})", list(ids))}
