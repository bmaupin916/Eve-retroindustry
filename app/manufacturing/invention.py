"""What a T2 blueprint actually costs to invent.

Until this existed, every T2 figure in the app was optimistic: the resolver
found a T2 item's materials and treated the blueprint as free. It is not. A T2
BPC comes out of an invention job that consumes datacores, a T1 blueprint copy
and optionally a decryptor — and **fails most of the time**. At max skills with
no decryptor a T2 module invents at roughly 50%, so the real cost per successful
BPC is about twice one attempt.

The shape of the calculation:

    success   = base x (1 + (sci1 + sci2)/30 + encryption/40) x decryptor_mult
    per BPC   = (datacores + decryptor + copy) / success
    per unit  = per BPC / (runs_per_BPC x units_per_run)

Three things it is easy to get wrong, all of which the SDE answers directly:

**Runs per BPC is data, not a rule of thumb.** Guides say "10 runs, or 1 for
ships and rigs". The T2 blueprint's `max_production_limit` gives the real
number, and across the 1,209 invented blueprints it is also 5, 20, 200 and 300
for about 145 of them. Since the whole invention cost is divided by this, the
rule of thumb misprices those by up to 300x.

**A T2 ship carries its entire invention cost on one hull.** Ships are 1 run in
121 cases out of 121; a T2 module spreads the same kind of cost over 10. Any
model that ignores this misranks T2 ships against T2 modules by about an order
of magnitude — which matters most for a tool whose job is ranking candidates.

**Only the datacore skills raise the chance.** An invention activity lists one
encryption skill, the science skills belonging to its datacores, and sometimes a
gating prerequisite that contributes nothing (Capital Ship Construction shows up
on 95 of them). The science skills are found through each datacore's
`requiredSkill1` attribute, not by name: the datacore is "Datacore - Gallentean
Starship Engineering" while the skill is "Gallente Starship Engineering", and
Amarrian/Amarr disagree the same way. Matching on names drops one of the two
science skills for every Amarr and Gallente T2 ship.

Base probabilities come from `sde_blueprint_products.probability` and are never
hardcoded; the values in the SDE match EVE University's published table exactly
(0.34 modules, 0.30 frigates, 0.26 cruisers, 0.22 battleships, 0.18 freighters,
0.14 wrecked relics), which is a reassuring cross-check but not the source.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

# Per-level contributions to the success chance. 1/30 is 3.333% per level of
# each science skill; 1/40 is 2.5% per level of the encryption skill.
SCIENCE_PER_LEVEL = 1.0 / 30.0
ENCRYPTION_PER_LEVEL = 1.0 / 40.0

# Skills whose name ends with this raise the chance at the encryption rate.
# Name-matching is safe here in a way it is not for datacores: every encryption
# skill in the game is "<Faction> Encryption Methods", and 1,109 of the 1,116
# invention activities carry exactly one. The other seven carry none, which is a
# real answer rather than a lookup failure.
_ENCRYPTION_SUFFIX = "Encryption Methods"


@dataclass(frozen=True)
class Decryptor:
    """A decryptor's effect. Loaded from `sde_decryptors`, never hardcoded."""
    type_id: int = 0
    name: str = ""
    probability_mult: float = 1.0
    me_modifier: float = 0.0
    te_modifier: float = 0.0
    run_modifier: float = 0.0


@dataclass
class InventionRecipe:
    """Everything the SDE knows about inventing one T2 product."""
    product_type_id: int
    t1_blueprint_id: int
    t2_blueprint_id: int
    base_probability: float
    base_runs: int                      # the T2 blueprint's max_production_limit
    units_per_run: int = 1
    # (type_id, name, quantity) per attempt
    datacores: list[tuple[int, str, int]] = field(default_factory=list)
    encryption_skill_id: int | None = None
    science_skill_ids: list[int] = field(default_factory=list)


@dataclass
class InventionCost:
    """The result. `per_unit` is what a caller adds to a T2 item's build cost."""
    success_chance: float = 0.0
    expected_attempts: float = 0.0
    runs_per_bpc: int = 0
    datacore_cost: float = 0.0          # one attempt
    decryptor_cost: float = 0.0         # one attempt
    copy_cost: float = 0.0              # one attempt
    per_attempt: float = 0.0
    per_bpc: float = 0.0                # amortised over the failures
    per_unit: float = 0.0
    # Datacores with no cached price. Non-empty means `per_unit` is understated
    # and the caller must say so rather than showing a confident number.
    unpriced: list[str] = field(default_factory=list)


def success_chance(base_probability: float, encryption_level: int = 0,
                   science_levels: tuple[int, ...] = (),
                   decryptor: Decryptor | None = None) -> float:
    """Chance that one invention attempt succeeds.

    Capped at 1.0: `Sleeper Technical Schematics` multiplies by 5, which would
    otherwise report a 170% chance on an already-likely job.
    """
    skill_term = (
        1.0
        + sum(_lvl(v) for v in science_levels) * SCIENCE_PER_LEVEL
        + _lvl(encryption_level) * ENCRYPTION_PER_LEVEL
    )
    mult = decryptor.probability_mult if decryptor else 1.0
    return max(0.0, min(1.0, base_probability * skill_term * mult))


def runs_per_bpc(base_runs: int, decryptor: Decryptor | None = None) -> int:
    """Runs on the invented BPC. Never below 1 — a 0-run BPC cannot exist."""
    runs = (base_runs or 1) + (decryptor.run_modifier if decryptor else 0.0)
    return max(1, int(runs))


def _lvl(raw) -> int:
    try:
        return max(0, min(5, int(raw or 0)))
    except (TypeError, ValueError):
        return 0


def find_recipe(conn: sqlite3.Connection, product_type_id: int) -> InventionRecipe | None:
    """The invention recipe for a T2 product, or None if it is not invented.

    Walks product -> T2 blueprint -> the T1 blueprint that invents it. T1 items,
    reaction outputs and anything bought rather than invented return None, which
    is how a caller tells "no invention cost" from "invention cost of zero".
    """
    row = conn.execute(
        "SELECT blueprint_type_id, quantity FROM sde_blueprint_products "
        "WHERE product_type_id=? AND activity='manufacturing'", (product_type_id,)
    ).fetchone()
    if not row:
        return None
    t2_bp, units_per_run = int(row[0]), int(row[1] or 1)

    row = conn.execute(
        "SELECT blueprint_type_id, probability FROM sde_blueprint_products "
        "WHERE product_type_id=? AND activity='invention'", (t2_bp,)
    ).fetchone()
    if not row:
        return None                     # manufactured directly, not invented
    t1_bp, probability = int(row[0]), float(row[1] or 0.0)
    if probability <= 0:
        return None

    base_runs = conn.execute(
        "SELECT max_production_limit FROM sde_blueprints WHERE blueprint_type_id=?",
        (t2_bp,)).fetchone()

    datacores = [(int(r[0]), r[1], int(r[2])) for r in conn.execute(
        "SELECT m.material_type_id, t.name, m.quantity "
        "FROM sde_blueprint_materials m JOIN sde_types t ON t.type_id=m.material_type_id "
        "WHERE m.blueprint_type_id=? AND m.activity='invention'", (t1_bp,))]

    skills = [(int(r[0]), r[1]) for r in conn.execute(
        "SELECT s.skill_type_id, t.name "
        "FROM sde_blueprint_skills s JOIN sde_types t ON t.type_id=s.skill_type_id "
        "WHERE s.blueprint_type_id=? AND s.activity='invention'", (t1_bp,))]

    encryption_id, science_ids = _classify_skills(conn, skills, datacores)

    return InventionRecipe(
        product_type_id=product_type_id,
        t1_blueprint_id=t1_bp,
        t2_blueprint_id=t2_bp,
        base_probability=probability,
        base_runs=int(base_runs[0]) if base_runs and base_runs[0] else 1,
        units_per_run=units_per_run,
        datacores=datacores,
        encryption_skill_id=encryption_id,
        science_skill_ids=science_ids,
    )


def _classify_skills(conn: sqlite3.Connection,
                     skills: list[tuple[int, str]],
                     datacores: list[tuple[int, str, int]]
                     ) -> tuple[int | None, list[int]]:
    """Split an invention activity's skills into encryption and science.

    The science skills are the ones each consumed datacore declares through its
    `requiredSkill1` attribute — read from the datacores, not from the
    blueprint's skill list. The blueprint's list also carries gating
    prerequisites that do not affect the odds (`Capital Ship Construction` on 95
    of them), and it does not always agree with the datacores: Multispectrum
    Coating II consumes Molecular Engineering datacores while its blueprint
    lists Nanite Engineering.

    **Not by name.** The obvious rule — strip "Datacore - " and match the skill
    — silently fails for two of the five racial lines, because the datacore says
    "Gallentean Starship Engineering" while the skill says "Gallente Starship
    Engineering" (and Amarrian/Amarr likewise). That drops one of two science
    skills for every Amarr and Gallente T2 ship, understating the success chance
    by about 4 points and overstating cost accordingly.

    Seven legacy blueprints (Perpetual Motion Unit, the Exotic Plasma ammo)
    carry no encryption skill at all, so None is a real answer.
    """
    encryption_id = next(
        (sid for sid, name in skills if name and name.endswith(_ENCRYPTION_SUFFIX)), None)

    ids = [tid for tid, _n, _q in datacores]
    science_ids: list[int] = []
    if ids:
        try:
            placeholders = ",".join("?" * len(ids))
            science_ids = [r[0] for r in conn.execute(
                f"SELECT skill_type_id FROM sde_datacore_skills "
                f"WHERE type_id IN ({placeholders})", ids)]
        except sqlite3.OperationalError:
            science_ids = []             # SDE predates the link table

    # Deliberately NOT intersected with the blueprint's own skill list. Those
    # two disagree: Multispectrum Coating II consumes Molecular Engineering
    # datacores while its blueprint lists Nanite Engineering, and it is the
    # datacore skill that moves the odds. Intersecting drops one of the two.
    return encryption_id, [sid for sid in science_ids if sid != encryption_id]


def compute_cost(
    recipe: InventionRecipe,
    prices: dict[int, float],
    skill_levels: dict[int, int] | None = None,
    decryptor: Decryptor | None = None,
    decryptor_price: float = 0.0,
    copy_cost: float = 0.0,
) -> InventionCost:
    """Expected invention cost per unit of the T2 product.

    `prices` maps type_id to unit price; a datacore missing from it is reported
    in `unpriced` rather than counted as free, because a silently-free datacore
    understates cost and overstates profit — the same failure the margin tracker
    exists to avoid.

    `copy_cost` is what one T1 BPC run costs, consumed whether the attempt
    succeeds or fails.
    """
    levels = skill_levels or {}
    cost = InventionCost()

    cost.success_chance = success_chance(
        recipe.base_probability,
        encryption_level=levels.get(recipe.encryption_skill_id or -1, 0),
        science_levels=tuple(levels.get(s, 0) for s in recipe.science_skill_ids),
        decryptor=decryptor,
    )
    if cost.success_chance <= 0:
        return cost

    cost.expected_attempts = 1.0 / cost.success_chance
    cost.runs_per_bpc = runs_per_bpc(recipe.base_runs, decryptor)

    for type_id, name, qty in recipe.datacores:
        unit = prices.get(type_id)
        if unit is None:
            cost.unpriced.append(name)
            continue
        cost.datacore_cost += unit * qty

    cost.decryptor_cost = decryptor_price if decryptor else 0.0
    cost.copy_cost = copy_cost or 0.0
    cost.per_attempt = cost.datacore_cost + cost.decryptor_cost + cost.copy_cost

    # Datacores, decryptor and the copy are consumed whether the job succeeds or
    # fails, so the cost of one success carries all the failures before it.
    cost.per_bpc = cost.per_attempt * cost.expected_attempts
    units = max(1, cost.runs_per_bpc * max(1, recipe.units_per_run))
    cost.per_unit = cost.per_bpc / units
    return cost


def invention_material_ids(conn: sqlite3.Connection) -> set[int]:
    """Every type consumed by an invention job — i.e. every datacore.

    The resolver charges invention at any node in a tree, so it cannot know
    which datacores it will meet before it walks. This is the whole set, and it
    is small enough (a few dozen types) that a caller can price all of it in one
    query and hand over a plain dict, instead of the resolver reaching into the
    market tables itself on every node.
    """
    try:
        return {int(r[0]) for r in conn.execute(
            "SELECT DISTINCT material_type_id FROM sde_blueprint_materials "
            "WHERE activity='invention'")}
    except sqlite3.OperationalError:
        return set()                     # SDE predates the invention activity


def load_decryptor(conn: sqlite3.Connection, type_id: int) -> Decryptor | None:
    if not type_id:
        return None
    try:
        row = conn.execute(
            "SELECT type_id, name, probability_mult, me_modifier, te_modifier, "
            "run_modifier FROM sde_decryptors WHERE type_id=?", (type_id,)).fetchone()
    except sqlite3.OperationalError:
        return None                      # SDE predates the decryptor table
    return Decryptor(*row) if row else None


def list_decryptors(conn: sqlite3.Connection) -> list[Decryptor]:
    """Every decryptor, best probability first. Empty on an older SDE."""
    try:
        rows = conn.execute(
            "SELECT type_id, name, probability_mult, me_modifier, te_modifier, "
            "run_modifier FROM sde_decryptors ORDER BY probability_mult DESC, name"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [Decryptor(*r) for r in rows]
