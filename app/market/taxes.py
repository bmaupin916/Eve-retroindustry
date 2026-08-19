"""What it costs to sell — market sales tax and broker's fee.

Every profit figure in this app used to be `sell - materials - job_fee`, which
is what you would earn if selling were free. It is not: between 5% and 11% of
the sale price goes to the SCC and to the station or structure owner, depending
on skills, standings and where you list. On a 5% margin that is the whole
margin.

Two costs, and which apply depends on *how* you sell:

* **Sales tax** is paid on every sale, always, to the SCC. It shows up in the
  wallet journal as "transaction tax".
* **Broker's fee** is paid only when you *place an order*. Selling into someone
  else's buy order costs you no broker fee.

So "dump it to buy orders" and "list it and wait" are different numbers, and a
tool that reports one while you do the other is wrong in the direction that
flatters it. `SellingCosts` carries the two components separately so the UI can
show which is which.

Rates come from the Version 22.02 patch note (2025-03-12, "Sales Tax has been
increased from 4% to 7.5%") and the EVE University *Tax* page, read 2026-08-18.

**Not yet confirmed in game.** EVE Uni's *Trading* page carries a worked total
("between 5.1% and 11%") that contradicts the sales-tax formula printed beside
it — those figures only resolve at an 8% base — so secondary sources have
already proved unreliable for exactly these numbers. The cross-check that gives
confidence is the broker floor: the wiki gives 1% minimum at Broker Relations V
with maximum standings, and 3 − 1.5 − 0.3 − 0.2 = 1.0 exactly, which confirms
the per-level and per-standing coefficients independently of the base rate.
The conclusive test is still to sell one item and read the wallet journal.
Tracked as open question 7 in docs/design-hosted-v2.md.

Not modelled yet: the **relist fee** for adjusting an order that has not sold,
and **Margin Trading** (which affects the escrow on *buy* orders, not what a
seller pays). Relisting is a real cost for anyone competing on price:

    relist = (100% - (50% + 6% x AdvBrokerRelations)) x broker% x new_value
           + broker% x max(new_value - old_value, 0)
"""
from __future__ import annotations

from dataclasses import dataclass

# Skill type IDs, so a caller holding a character's skill map can fill these in
# rather than asking — the same ids `/plan` already uses for Industry (3380)
# and Advanced Industry (3388).
SKILL_ACCOUNTING = 16622
SKILL_BROKER_RELATIONS = 3446
SKILL_ADVANCED_BROKER_RELATIONS = 16597      # relist fee only, unused for now

SALES_TAX_BASE = 0.075          # 7.5% since Version 22.02 (2025-03-12), was 4%
SALES_TAX_PER_LEVEL = 0.11      # Accounting cuts the base by 11% per level

BROKER_BASE = 0.03              # 3% in NPC stations
BROKER_PER_RELATIONS = 0.003    # -0.3% per Broker Relations level
# **Unmodified standings only.** The broker's fee reads raw standing and ignores
# the standing skills entirely — Connections, Diplomacy and the rest do not
# apply, even though the character sheet shows their effect by default. Feeding
# in the modified figure inflates the discount and understates the fee, which
# overstates profit: the one direction of error this module exists to prevent.
# The settings fields are labelled "(unmodified)" for this reason.
BROKER_PER_FACTION_STANDING = 0.0003    # -0.03% per point
BROKER_PER_CORP_STANDING = 0.0002       # -0.02% per point

# In an Upwell structure the fee is a flat SCC surcharge plus whatever the owner
# sets, and **skills and standings do not apply at all**. A tool that quietly
# applied Broker Relations here would understate the cost of trading in nullsec.
UPWELL_SCC_SURCHARGE = 0.005    # 0.5%

# Every broker fee and relist fee has a 100 ISK floor. Deliberately not applied
# per unit: you list a stack in one order, so the floor bites once on the order,
# not once per item. Applying it to a per-unit margin would make cheap items
# look catastrophically unprofitable. `order_broker_fee` applies it where a real
# order value is known.
MIN_BROKER_FEE = 100.0


def _lvl(raw) -> int:
    try:
        return max(0, min(5, int(raw or 0)))
    except (TypeError, ValueError):
        return 0


def _standing(raw) -> float:
    """Standings run -10..+10. Negative standings genuinely raise the fee."""
    try:
        return max(-10.0, min(10.0, float(raw or 0.0)))
    except (TypeError, ValueError):
        return 0.0


def sales_tax_rate(accounting: int = 0) -> float:
    """Fraction of sale price paid as sales tax. 7.5% at Accounting 0,
    3.375% at Accounting V (7.5 × 0.45). Published tables give this variously as
    3.4%, 3.37% or 3.3% — all of them are that same number, rounded or
    truncated, not a different coefficient."""
    return SALES_TAX_BASE * (1.0 - SALES_TAX_PER_LEVEL * _lvl(accounting))


def broker_fee_rate(
    venue: str = "npc",
    broker_relations: int = 0,
    faction_standing: float = 0.0,
    corp_standing: float = 0.0,
    structure_fee_pct: float = 0.0,
) -> float:
    """Fraction of order value paid to place a sell order.

    `structure_fee_pct` is the owner-set percentage on an Upwell structure, as a
    percentage (1.0 means 1%), and is ignored for NPC stations.
    """
    if str(venue or "npc").lower() == "upwell":
        try:
            owner = max(0.0, float(structure_fee_pct or 0.0)) / 100.0
        except (TypeError, ValueError):
            owner = 0.0
        return UPWELL_SCC_SURCHARGE + owner

    rate = (BROKER_BASE
            - BROKER_PER_RELATIONS * _lvl(broker_relations)
            - BROKER_PER_FACTION_STANDING * _standing(faction_standing)
            - BROKER_PER_CORP_STANDING * _standing(corp_standing))
    return max(0.0, rate)


@dataclass(frozen=True)
class SellingCosts:
    """The two components kept apart, so a UI can explain the number."""
    sales_tax: float = 0.0      # fraction of sale price
    broker_fee: float = 0.0     # fraction of sale price, 0 when selling to buy orders
    method: str = "orders"

    @property
    def total(self) -> float:
        return self.sales_tax + self.broker_fee

    @property
    def pct(self) -> float:
        return self.total * 100.0

    def on(self, gross: float | None) -> float:
        """ISK lost on a gross sale of `gross`."""
        return (gross or 0.0) * self.total

    def net(self, gross: float | None) -> float | None:
        """What actually reaches the wallet."""
        return None if gross is None else gross - self.on(gross)


def selling_costs(defaults: dict | None = None, **overrides) -> SellingCosts:
    """Build `SellingCosts` from the app defaults, with keyword overrides.

    `sales_method` picks which costs apply:
      * ``"orders"``    — list a sell order: broker fee **and** sales tax
      * ``"immediate"`` — sell into existing buy orders: sales tax only
    """
    cfg = dict(defaults or {})
    cfg.update({k: v for k, v in overrides.items() if v is not None})

    method = str(cfg.get("sales_method") or "orders").lower()
    if method not in ("orders", "immediate"):
        method = "orders"

    tax = sales_tax_rate(cfg.get("accounting_skill"))
    if method == "immediate":
        return SellingCosts(sales_tax=tax, broker_fee=0.0, method=method)

    fee = broker_fee_rate(
        venue=cfg.get("sell_venue", "npc"),
        broker_relations=cfg.get("broker_relations_skill"),
        faction_standing=cfg.get("faction_standing"),
        corp_standing=cfg.get("corp_standing"),
        structure_fee_pct=cfg.get("structure_broker_pct"),
    )
    return SellingCosts(sales_tax=tax, broker_fee=fee, method=method)


def order_broker_fee(order_value: float, rate: float) -> float:
    """Broker fee on one real order, including the 100 ISK floor.

    Use this when a concrete order value exists. Per-unit margin maths should
    use `SellingCosts` instead — see `MIN_BROKER_FEE`.
    """
    if rate <= 0 or order_value <= 0:
        return 0.0
    return max(MIN_BROKER_FEE, order_value * rate)


# ── Buying ───────────────────────────────────────────────────────────────────
#
# The other half of the trade, and it was missing entirely. This module opened
# with "What it costs to sell", and every profit figure in the app was built on
# the assumption that acquiring materials is free. It is, but only one way:
#
#   * buying off an existing **sell** order is instant and costs no broker fee
#   * placing your own **buy** order and waiting costs a broker fee, same rate
#     and same standings maths as listing a sell order
#
# The input basis setting chooses between exactly those two. So a basis of
# "buy" — which is the reactions board's default for raw inputs — understated
# material cost by the full broker rate, and understatement of cost is
# overstatement of profit.


@dataclass(frozen=True)
class BuyingCosts:
    """What acquiring a unit costs on top of its quoted price."""
    broker_fee: float = 0.0     # fraction added to the quoted price
    basis: str = "sell"

    @property
    def pct(self) -> float:
        return self.broker_fee * 100.0

    def on(self, gross: float | None) -> float:
        """ISK of fee on a purchase of `gross`."""
        return (gross or 0.0) * self.broker_fee

    def paid(self, unit: float | None) -> float | None:
        """What one unit really costs, fee included. None stays None, because
        "not priced" must not silently become "free"."""
        return None if unit is None else unit * (1.0 + self.broker_fee)


def buying_costs(basis: str | None = None, defaults: dict | None = None,
                 **overrides) -> BuyingCosts:
    """Build `BuyingCosts` for an input basis, from the app defaults.

    `basis` picks which cost applies, mirroring `sales_method` on the sell
    side:
      * ``"sell"`` — buy instantly off someone's sell order: no broker fee
      * ``"buy"``  — place a buy order and wait: broker fee, same rate as a
        sell order at the same venue and standings

    Anything unrecognised is treated as ``"sell"``, which is the no-fee answer
    — an unknown basis must not invent a charge.
    """
    cfg = dict(defaults or {})
    cfg.update({k: v for k, v in overrides.items() if v is not None})

    chosen = str(basis or cfg.get("input_basis") or "sell").lower()
    if chosen != "buy":
        return BuyingCosts(broker_fee=0.0, basis="sell")

    # Buy orders are placed at the venue you are buying *in*. There is no
    # separate "buy venue" setting, so the sell venue stands in for it: both
    # describe the station this character trades at.
    fee = broker_fee_rate(
        venue=cfg.get("sell_venue", "npc"),
        broker_relations=cfg.get("broker_relations_skill"),
        faction_standing=cfg.get("faction_standing"),
        corp_standing=cfg.get("corp_standing"),
        structure_fee_pct=cfg.get("structure_broker_pct"),
    )
    return BuyingCosts(broker_fee=fee, basis="buy")
