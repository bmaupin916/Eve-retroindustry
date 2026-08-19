#!/usr/bin/env python3
"""Rank EVE market snapshots after fees, liquidity, depth, and sizing limits."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
from pathlib import Path
from typing import Any


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def depth_value(item: dict[str, Any], side: str, band: str, field: str) -> float:
    return finite(item.get("order_book", {}).get(side, {}).get("depth", {}).get(band, {}).get(field))


def score_item(item: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    bid = finite(item.get("best_bid"))
    ask = finite(item.get("best_ask"))
    volume = finite(item.get("history_30d", {}).get("avg_daily_volume"))
    price_cv = finite(item.get("history_30d", {}).get("price_cv"), default=1.0)

    if bid <= 0 or ask <= 0:
        return {
            "type_id": item.get("type_id"),
            "name": item.get("name", item.get("type_id")),
            "eligible": False,
            "reasons": ["missing executable bid or ask"],
            "score": 0.0,
        }

    unit_cost = bid * (1 + args.buy_broker)
    net_proceeds = ask * (1 - args.sell_broker - args.sales_tax - args.relist_reserve) - args.logistics_per_unit
    net_profit = net_proceeds - unit_cost
    net_roi = net_profit / unit_cost if unit_cost else -1.0
    breakeven = (bid * (1 + args.buy_broker) + args.logistics_per_unit) / (
        1 - args.sell_broker - args.sales_tax - args.relist_reserve
    )

    buy_depth = depth_value(item, "buy", "1pct", "units")
    sell_depth = depth_value(item, "sell", "1pct", "units")
    near_depth = min(buy_depth, sell_depth)
    plausible_daily_units = volume * args.capture_rate
    capital_units = (args.capital * args.position_cap) / unit_cost if unit_cost else 0.0
    exit_units = plausible_daily_units * args.target_exit_days
    depth_units = near_depth * args.max_depth_share
    proposed_units = max(0, math.floor(min(capital_units, exit_units, depth_units)))
    proposed_isk = proposed_units * unit_cost
    expected_daily_units = min(plausible_daily_units, proposed_units / max(args.target_exit_days, 1.0))
    expected_daily_profit = max(0.0, net_profit * expected_daily_units)
    expected_exit_days = proposed_units / plausible_daily_units if plausible_daily_units > 0 else math.inf

    reasons: list[str] = []
    if net_roi < args.min_net_roi:
        reasons.append(f"net ROI {net_roi:.2%} is below {args.min_net_roi:.2%}")
    if volume < args.min_daily_volume:
        reasons.append(f"30d daily volume {volume:.2f} is below {args.min_daily_volume:.2f}")
    if near_depth <= 0:
        reasons.append("no two-sided depth within 1%")
    if proposed_units < 1:
        reasons.append("position constraints permit fewer than one unit")
    if price_cv > args.max_price_cv:
        reasons.append(f"30d price CV {price_cv:.2%} exceeds {args.max_price_cv:.2%}")

    eligible = not reasons
    margin_score = 25 * clamp(net_roi / max(args.target_net_roi, 1e-9), 0, 1)
    capacity_target = args.capital * args.target_daily_return
    capacity_score = 20 * clamp(expected_daily_profit / max(capacity_target, 1.0), 0, 1)
    liquidity_score = 15 * clamp(volume / max(args.liquidity_saturation, 1.0), 0, 1)

    buy_orders = finite(item.get("order_book", {}).get("buy", {}).get("order_count"))
    sell_orders = finite(item.get("order_book", {}).get("sell", {}).get("order_count"))
    order_resilience = clamp(min(buy_orders, sell_orders) / 10.0, 0, 1)
    top_share_buy = finite(item.get("order_book", {}).get("buy", {}).get("depth", {}).get("1pct", {}).get("top_order_unit_share"), 1.0)
    top_share_sell = finite(item.get("order_book", {}).get("sell", {}).get("depth", {}).get("1pct", {}).get("top_order_unit_share"), 1.0)
    concentration_resilience = 1 - clamp(max(top_share_buy, top_share_sell), 0, 1)
    depth_score = 15 * (0.5 * order_resilience + 0.5 * concentration_resilience)

    # Order count supplies quote resilience but can also mean competition. This
    # deliberately conservative workload score assumes more visible orders need
    # more attention unless the user provides fill/update evidence.
    competition_load = clamp((buy_orders + sell_orders) / 100.0, 0, 1)
    workload_score = 10 * (1 - competition_load)
    stability_score = 10 * (1 - clamp(price_cv / max(args.max_price_cv, 1e-9), 0, 1))
    freshness_score = 5.0 if item.get("observed_at_utc") else 0.0
    score = margin_score + capacity_score + liquidity_score + depth_score + workload_score + stability_score + freshness_score
    if not eligible:
        score *= 0.35

    return {
        "type_id": item.get("type_id"),
        "name": item.get("name", item.get("type_id")),
        "market": item.get("location_name"),
        "observed_at_utc": item.get("observed_at_utc"),
        "eligible": eligible,
        "reasons": reasons,
        "score": round(score, 2),
        "best_bid": bid,
        "best_ask": ask,
        "unit_cost_after_buy_fee": unit_cost,
        "net_proceeds_per_unit": net_proceeds,
        "net_profit_per_unit": net_profit,
        "net_roi": net_roi,
        "breakeven_sell": breakeven,
        "avg_daily_volume_30d": volume,
        "plausible_daily_units": plausible_daily_units,
        "buy_depth_1pct": buy_depth,
        "sell_depth_1pct": sell_depth,
        "proposed_units": proposed_units,
        "proposed_isk": proposed_isk,
        "expected_daily_profit": expected_daily_profit,
        "expected_exit_days": expected_exit_days,
        "price_cv_30d": price_cv,
        "score_components": {
            "margin": round(margin_score, 2),
            "capacity": round(capacity_score, 2),
            "liquidity": round(liquidity_score, 2),
            "depth": round(depth_score, 2),
            "workload": round(workload_score, 2),
            "stability": round(stability_score, 2),
            "freshness": round(freshness_score, 2),
        },
    }


def fmt_isk(value: Any) -> str:
    number = finite(value)
    if abs(number) >= 1_000_000_000:
        return f"{number / 1_000_000_000:.2f}b"
    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:.2f}m"
    if abs(number) >= 1_000:
        return f"{number / 1_000:.1f}k"
    return f"{number:.2f}"


def render_markdown(rows: list[dict[str, Any]], assumptions: dict[str, Any]) -> str:
    lines = [
        "# EVE Market Candidate Ranking",
        "",
        "Scores organize review; they do not prove profitability.",
        "",
        "## Assumptions",
        "",
    ]
    for key, value in assumptions.items():
        lines.append(f"- {key}: {value}")
    lines.extend(
        [
            "",
            "## Candidates",
            "",
            "| Rank | Item | Score | Gate | Bid | Ask | Net/unit | Net ROI | 30d vol/day | Size | ISK deployed | Est. ISK/day | Exit days |",
            "|---:|---|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for index, row in enumerate(rows, 1):
        gate = "pass" if row.get("eligible") else "fail"
        exit_days = row.get("expected_exit_days")
        exit_text = "inf" if exit_days is None or not math.isfinite(finite(exit_days, math.inf)) else f"{float(exit_days):.1f}"
        lines.append(
            "| {rank} | {name} ({type_id}) | {score:.1f} | {gate} | {bid} | {ask} | {net} | {roi:.2%} | {vol:.1f} | {units} | {deployed} | {daily} | {exit_days} |".format(
                rank=index,
                name=str(row.get("name", "")).replace("|", "\\|"),
                type_id=row.get("type_id"),
                score=finite(row.get("score")),
                gate=gate,
                bid=fmt_isk(row.get("best_bid")),
                ask=fmt_isk(row.get("best_ask")),
                net=fmt_isk(row.get("net_profit_per_unit")),
                roi=finite(row.get("net_roi")),
                vol=finite(row.get("avg_daily_volume_30d")),
                units=int(finite(row.get("proposed_units"))),
                deployed=fmt_isk(row.get("proposed_isk")),
                daily=fmt_isk(row.get("expected_daily_profit")),
                exit_days=exit_text,
            )
        )
    lines.extend(["", "## Failed gates", ""])
    failed = [row for row in rows if row.get("reasons")]
    if failed:
        for row in failed:
            lines.append(f"- **{row.get('name')} ({row.get('type_id')}):** " + "; ".join(row["reasons"]))
    else:
        lines.append("- None.")
    return "\n".join(lines) + "\n"


def render_csv(rows: list[dict[str, Any]]) -> str:
    fields = [
        "type_id", "name", "market", "observed_at_utc", "eligible", "score", "best_bid", "best_ask",
        "net_profit_per_unit", "net_roi", "breakeven_sell", "avg_daily_volume_30d", "buy_depth_1pct",
        "sell_depth_1pct", "proposed_units", "proposed_isk", "expected_daily_profit", "expected_exit_days",
        "price_cv_30d", "reasons",
    ]
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        record = dict(row)
        record["reasons"] = "; ".join(row.get("reasons", []))
        writer.writerow(record)
    return stream.getvalue()


def self_test() -> None:
    args = argparse.Namespace(
        capital=1_000_000,
        sales_tax=0.05,
        buy_broker=0.01,
        sell_broker=0.01,
        relist_reserve=0.0,
        logistics_per_unit=0.0,
        capture_rate=0.10,
        position_cap=0.10,
        target_exit_days=3.0,
        max_depth_share=0.10,
        min_net_roi=0.10,
        min_daily_volume=5.0,
        max_price_cv=0.50,
        target_net_roi=0.20,
        target_daily_return=0.005,
        liquidity_saturation=100.0,
    )
    item = {
        "type_id": 1,
        "name": "Test Item",
        "observed_at_utc": "2026-08-17T00:00:00Z",
        "best_bid": 100,
        "best_ask": 130,
        "history_30d": {"avg_daily_volume": 100, "price_cv": 0.1},
        "order_book": {
            "buy": {"order_count": 12, "depth": {"1pct": {"units": 500, "top_order_unit_share": 0.1}}},
            "sell": {"order_count": 12, "depth": {"1pct": {"units": 500, "top_order_unit_share": 0.1}}},
        },
    }
    result = score_item(item, args)
    assert math.isclose(result["unit_cost_after_buy_fee"], 101.0, rel_tol=1e-12)
    assert math.isclose(result["net_proceeds_per_unit"], 122.2, rel_tol=1e-12)
    assert math.isclose(result["net_profit_per_unit"], 21.2, rel_tol=1e-12)
    assert result["eligible"] is True
    print("self-test passed")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="JSON produced by eve_market_snapshot.py")
    parser.add_argument("--output", type=Path, help="Write output to this path; stdout if omitted")
    parser.add_argument("--format", choices=("markdown", "json", "csv"), default="markdown")
    parser.add_argument("--capital", type=float, default=1_000_000_000)
    parser.add_argument("--sales-tax", type=float, default=0.03375)
    parser.add_argument("--buy-broker", type=float, default=0.01)
    parser.add_argument("--sell-broker", type=float, default=0.01)
    parser.add_argument("--relist-reserve", type=float, default=0.005)
    parser.add_argument("--logistics-per-unit", type=float, default=0.0)
    parser.add_argument("--capture-rate", type=float, default=0.10)
    parser.add_argument("--position-cap", type=float, default=0.05)
    parser.add_argument("--target-exit-days", type=float, default=5.0)
    parser.add_argument("--max-depth-share", type=float, default=0.10)
    parser.add_argument("--min-net-roi", type=float, default=0.10)
    parser.add_argument("--min-daily-volume", type=float, default=5.0)
    parser.add_argument("--max-price-cv", type=float, default=0.50)
    parser.add_argument("--target-net-roi", type=float, default=0.20)
    parser.add_argument("--target-daily-return", type=float, default=0.005)
    parser.add_argument("--liquidity-saturation", type=float, default=100.0)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if not args.self_test and args.input is None:
        parser.error("--input is required unless --self-test is used")
    rates = (args.sales_tax, args.buy_broker, args.sell_broker, args.relist_reserve, args.capture_rate, args.position_cap, args.max_depth_share)
    if any(rate < 0 or rate >= 1 for rate in rates):
        parser.error("rates must be decimal fractions in [0, 1)")
    if args.capital <= 0 or args.target_exit_days <= 0:
        parser.error("capital and target-exit-days must be positive")
    return args


def main() -> int:
    args = parse_args()
    if args.self_test:
        self_test()
        return 0

    document = json.loads(args.input.read_text(encoding="utf-8"))
    items = document.get("items", document if isinstance(document, list) else [])
    rows = [score_item(item, args) for item in items]
    rows.sort(key=lambda row: (bool(row.get("eligible")), finite(row.get("score"))), reverse=True)

    assumptions = {
        "capital": f"{args.capital:,.0f} ISK",
        "sales tax": f"{args.sales_tax:.3%}",
        "buy broker": f"{args.buy_broker:.3%}",
        "sell broker": f"{args.sell_broker:.3%}",
        "relist reserve": f"{args.relist_reserve:.3%} of sell value",
        "capture rate": f"{args.capture_rate:.1%} of 30d volume",
        "position cap": f"{args.position_cap:.1%} of capital",
        "target exit": f"{args.target_exit_days:g} days",
        "depth cap": f"{args.max_depth_share:.1%} of units within 1%",
    }
    payload = {"schema_version": 1, "assumptions": assumptions, "candidates": rows}
    if args.format == "json":
        rendered = json.dumps(payload, indent=2) + "\n"
    elif args.format == "csv":
        rendered = render_csv(rows)
    else:
        rendered = render_markdown(rows, assumptions)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
