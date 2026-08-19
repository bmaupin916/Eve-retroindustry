#!/usr/bin/env python3
"""Fetch reproducible public ESI order-book and history snapshots.

This script is intentionally read-only. It never authenticates and never places or
modifies EVE Online orders.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ESI_BASE = "https://esi.evetech.net/latest"
REVIEWED_COMPATIBILITY_DATE = "2026-08-17"
USER_AGENT = "eve-market-trading-skill/1.0 (read-only public ESI research)"

HUBS: dict[str, dict[str, Any]] = {
    "jita": {"region_id": 10000002, "location_id": 60003760, "name": "Jita IV - Moon 4"},
    "amarr": {"region_id": 10000043, "location_id": 60008494, "name": "Amarr VIII (Oris)"},
    "dodixie": {"region_id": 10000032, "location_id": 60011866, "name": "Dodixie IX - Moon 20"},
    "rens": {"region_id": 10000030, "location_id": 60004588, "name": "Rens VI - Moon 8"},
    "hek": {"region_id": 10000042, "location_id": 60005686, "name": "Hek VIII - Moon 12"},
    "plex-global": {"region_id": 19000001, "location_id": None, "name": "Global PLEX market"},
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_json(url: str, timeout: float, compatibility_date: str, attempts: int = 4) -> tuple[Any, dict[str, str]]:
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "X-Compatibility-Date": compatibility_date,
    }
    last_error: Exception | None = None
    for attempt in range(attempts):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_headers = {k.lower(): v for k, v in response.headers.items()}
                payload = json.loads(response.read().decode("utf-8"))
                return payload, response_headers
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {420, 429, 502, 503, 504} or attempt == attempts - 1:
                raise
            retry_after = exc.headers.get("Retry-After")
            delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
            time.sleep(min(delay, 15.0))
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == attempts - 1:
                raise
            time.sleep(min(2**attempt, 10.0))
    raise RuntimeError(f"ESI request failed: {last_error}")


def make_url(path: str, **query: Any) -> str:
    values = {key: value for key, value in query.items() if value is not None}
    return f"{ESI_BASE}{path}?{urllib.parse.urlencode(values)}"


def fetch_orders(region_id: int, type_id: int, timeout: float, compatibility_date: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    first_url = make_url(f"/markets/{region_id}/orders/", datasource="tranquility", order_type="all", type_id=type_id, page=1)
    first, headers = fetch_json(first_url, timeout, compatibility_date)
    pages = int(headers.get("x-pages", "1"))
    orders = list(first)
    for page in range(2, pages + 1):
        url = make_url(f"/markets/{region_id}/orders/", datasource="tranquility", order_type="all", type_id=type_id, page=page)
        payload, page_headers = fetch_json(url, timeout, compatibility_date)
        orders.extend(payload)
        remain = page_headers.get("x-esi-error-limit-remain")
        reset = page_headers.get("x-esi-error-limit-reset")
        if remain is not None and int(remain) < 10:
            time.sleep(min(float(reset or 1), 10.0))
    meta = {
        "url": first_url,
        "pages": pages,
        "expires": headers.get("expires"),
        "etag": headers.get("etag"),
    }
    return orders, meta


def fetch_history(region_id: int, type_id: int, timeout: float, compatibility_date: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    url = make_url(f"/markets/{region_id}/history/", datasource="tranquility", type_id=type_id)
    payload, headers = fetch_json(url, timeout, compatibility_date)
    return list(payload), {"url": url, "expires": headers.get("expires"), "etag": headers.get("etag")}


def fetch_type_name(type_id: int, timeout: float, compatibility_date: str) -> str:
    url = make_url(f"/universe/types/{type_id}/", datasource="tranquility", language="en")
    payload, _ = fetch_json(url, timeout, compatibility_date)
    return str(payload.get("name") or type_id)


def side_summary(orders: list[dict[str, Any]], is_buy: bool, now: datetime) -> dict[str, Any]:
    side = [order for order in orders if bool(order.get("is_buy_order")) is is_buy]
    side.sort(key=lambda order: float(order["price"]), reverse=is_buy)
    if not side:
        return {
            "best_price": None,
            "order_count": 0,
            "total_units": 0,
            "depth": {},
            "top_order": None,
        }

    best = float(side[0]["price"])
    total_units = sum(int(order.get("volume_remain", 0)) for order in side)
    depths: dict[str, Any] = {}
    for pct in (0.01, 0.02, 0.05):
        if is_buy:
            selected = [order for order in side if float(order["price"]) >= best * (1 - pct)]
        else:
            selected = [order for order in side if float(order["price"]) <= best * (1 + pct)]
        units = sum(int(order.get("volume_remain", 0)) for order in selected)
        isk = sum(float(order["price"]) * int(order.get("volume_remain", 0)) for order in selected)
        top_units = int(side[0].get("volume_remain", 0))
        depths[f"{int(pct * 100)}pct"] = {
            "units": units,
            "isk": isk,
            "orders": len(selected),
            "top_order_unit_share": (top_units / units) if units else None,
        }

    issued = parse_datetime(str(side[0]["issued"]))
    top = {
        "price": best,
        "units": int(side[0].get("volume_remain", 0)),
        "issued": str(side[0].get("issued")),
        "age_hours": max(0.0, (now - issued).total_seconds() / 3600),
        "range": side[0].get("range"),
        "location_id": side[0].get("location_id"),
    }
    return {
        "best_price": best,
        "order_count": len(side),
        "total_units": total_units,
        "depth": depths,
        "top_order": top,
    }


def history_summary(history: list[dict[str, Any]], days: int) -> dict[str, Any]:
    if not history:
        return {
            "calendar_days": days,
            "active_days": 0,
            "total_volume": 0,
            "avg_daily_volume": 0.0,
            "vwap": None,
            "price_stddev": None,
            "price_cv": None,
            "latest_date": None,
        }

    parsed = [(date.fromisoformat(str(row["date"])), row) for row in history]
    end = max(day for day, _ in parsed)
    start = end - timedelta(days=days - 1)
    window = [row for day, row in parsed if start <= day <= end]
    total_volume = sum(int(row.get("volume", 0)) for row in window)
    weighted_price_sum = sum(float(row.get("average", 0.0)) * int(row.get("volume", 0)) for row in window)
    vwap = weighted_price_sum / total_volume if total_volume else None
    prices = [float(row["average"]) for row in window if int(row.get("volume", 0)) > 0]
    price_stddev = statistics.pstdev(prices) if len(prices) > 1 else 0.0 if prices else None
    price_mean = statistics.fmean(prices) if prices else None
    price_cv = price_stddev / price_mean if price_stddev is not None and price_mean else None
    return {
        "calendar_days": days,
        "active_days": len(window),
        "total_volume": total_volume,
        "avg_daily_volume": total_volume / days,
        "vwap": vwap,
        "price_stddev": price_stddev,
        "price_cv": price_cv,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "latest_date": end.isoformat(),
        "period_high": max((float(row["highest"]) for row in window), default=None),
        "period_low": min((float(row["lowest"]) for row in window), default=None),
    }


def build_snapshot(type_id: int, region_id: int, location_id: int | None, location_name: str, timeout: float, compatibility_date: str) -> dict[str, Any]:
    observed = utc_now()
    name = fetch_type_name(type_id, timeout, compatibility_date)
    orders, order_meta = fetch_orders(region_id, type_id, timeout, compatibility_date)
    history, history_meta = fetch_history(region_id, type_id, timeout, compatibility_date)

    if location_id is not None:
        scoped_orders = [order for order in orders if int(order.get("location_id", -1)) == location_id]
        scope_note = "Strict order-origin filter for the selected NPC station; remote-range buy orders and structures are excluded."
    else:
        scoped_orders = orders
        scope_note = "All orders returned for the selected market region are included."

    buy = side_summary(scoped_orders, True, observed)
    sell = side_summary(scoped_orders, False, observed)
    best_bid = buy["best_price"]
    best_ask = sell["best_price"]
    gross_spread = (best_ask - best_bid) if best_bid is not None and best_ask is not None else None
    gross_spread_pct = (gross_spread / best_ask) if gross_spread is not None and best_ask else None

    return {
        "type_id": type_id,
        "name": name,
        "datasource": "tranquility",
        "observed_at_utc": iso_utc(observed),
        "compatibility_date": compatibility_date,
        "region_id": region_id,
        "location_id": location_id,
        "location_name": location_name,
        "scope_note": scope_note,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "gross_spread": gross_spread,
        "gross_spread_pct": gross_spread_pct,
        "order_book": {"buy": buy, "sell": sell},
        "history_7d": history_summary(history, 7),
        "history_30d": history_summary(history, 30),
        "history_90d": history_summary(history, 90),
        "source": {"orders": order_meta, "history": history_meta},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--type-id", type=int, action="append", help="EVE type ID; repeat for multiple items")
    parser.add_argument("--hub", choices=sorted(HUBS), default="jita", help="Known market hub")
    parser.add_argument("--region-id", type=int, help="Override hub region ID")
    parser.add_argument("--location-id", type=int, help="Override hub location ID; use 0 to include the full region")
    parser.add_argument("--compatibility-date", default=REVIEWED_COMPATIBILITY_DATE)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path, help="Write JSON to this path; stdout if omitted")
    parser.add_argument("--list-hubs", action="store_true")
    args = parser.parse_args()
    if not args.list_hubs and not args.type_id:
        parser.error("at least one --type-id is required")
    return args


def main() -> int:
    args = parse_args()
    if args.list_hubs:
        print(json.dumps(HUBS, indent=2))
        return 0

    hub = HUBS[args.hub]
    region_id = args.region_id if args.region_id is not None else int(hub["region_id"])
    if args.location_id is None:
        location_id = hub["location_id"]
    else:
        location_id = None if args.location_id == 0 else args.location_id
    location_name = str(hub["name"]) if args.region_id is None and args.location_id is None else "custom market scope"

    items = []
    failures = []
    for type_id in list(dict.fromkeys(args.type_id)):
        try:
            items.append(build_snapshot(type_id, region_id, location_id, location_name, args.timeout, args.compatibility_date))
        except Exception as exc:  # Keep a batch auditable even if one item fails.
            failures.append({"type_id": type_id, "error": f"{type(exc).__name__}: {exc}"})

    document = {
        "schema_version": 1,
        "generated_at_utc": iso_utc(utc_now()),
        "reviewed_compatibility_date": REVIEWED_COMPATIBILITY_DATE,
        "items": items,
        "failures": failures,
    }
    rendered = json.dumps(document, indent=2, sort_keys=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0 if items else 2


if __name__ == "__main__":
    raise SystemExit(main())
