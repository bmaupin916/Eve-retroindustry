"""Wallet and market orders: the two per-character ESI ledgers.

Moved out of `main.py` unchanged (W6). Both pages share the same shape — fetch
personal or corporate rows, resolve the ids in them to names, decorate, cap the
list — so the decorators and the name resolvers move with them.

`/portrait` went to `routers/media.py` rather than here, even though it is a
character thing, because it shares the whole on-disk image cache with
`/icon`.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.auth.token_store import get_character_row, list_characters
from app.character import orders as orders_api
from app.character import wallet as wallet_api
from app.market.prices import JITA_REGION, TRADE_HUBS
from app.db.conn import connect as _connect
from app.web.location_resolver import (
    get_region_for_location,
    load_location_names_from_db,
    resolve_station_names_bulk,
)
from app.web.deps import (
    _resolve_party_names,
    _valid_token_async,
    _tr,
    get_active_character_id,
    get_conn,
)

router = APIRouter()


# ── Wallet ───────────────────────────────────────────────────────────────────

_CORP_DIVISION_NAMES = {
    1: "Master Wallet", 2: "2nd Wallet", 3: "3rd Wallet", 4: "4th Wallet",
    5: "5th Wallet", 6: "6th Wallet", 7: "7th Wallet",
}


@router.get("/wallet", response_class=HTMLResponse)
async def wallet_page(request: Request, char: str = "", scope: str = "personal",
                      division: int = 1):
    conn = get_conn()
    # Which character drives the page (?char= overrides the active cookie)
    plan_char_id: int | None = None
    if char.isdigit() and get_character_row(conn, int(char)):
        plan_char_id = int(char)
    if plan_char_id is None:
        plan_char_id = get_active_character_id(request, conn)

    ctx: dict = {
        "scope": scope, "division": division,
        "wallet_char_id": plan_char_id,
        "balance": None, "journal": [], "transactions": [],
        "corp_wallets": None, "corp_error": None, "corp_name": None,
        "error": None, "division_names": _CORP_DIVISION_NAMES,
        "row_cap": _WALLET_ROW_CAP, "cached_at": 0.0,
    }

    if not plan_char_id:
        ctx["error"] = "You are not signed in."
        conn.close()
        return _tr("wallet.html", request, ctx)

    token = await _valid_token_async(plan_char_id)
    row = get_character_row(conn, plan_char_id)
    if not token or not row:
        ctx["error"] = "The character token expired — sign in again."
        conn.close()
        return _tr("wallet.html", request, ctx)

    division = max(1, min(7, division))
    ctx["division"] = division

    # Type names from the local SDE (transactions have a type_id)
    def _type_names(type_ids: set[int]) -> dict[int, str]:
        type_ids = {t for t in type_ids if t}
        if not type_ids:
            return {}
        ph = ",".join("?" * len(type_ids))
        return {r[0]: r[1] for r in conn.execute(
            f"SELECT type_id, name FROM sde_types WHERE type_id IN ({ph})", list(type_ids)
        ).fetchall()}

    try:
        if scope == "corp":
            corp_id = row.get("corporation_id")
            if not corp_id:
                # Recorded by the sync worker. Absent means this character has
                # not been synced, not that it has no corporation — the page
                # used to ask ESI here on every single load, for a value that
                # changes about once a year.
                ctx["corp_error"] = ("This character has not been synced yet, so "
                                     "its corporation is not known.")
            else:
                cn = await _resolve_party_names({corp_id})
                ctx["corp_name"] = cn.get(corp_id, str(corp_id))
                wallets, _at = wallet_api.load_cached_ledger(
                    conn, corp_id, wallet_api.BALANCES, wallet_api.CORPORATION)
                ctx["corp_wallets"] = wallets
                if wallets is None:
                    # Not "you lack the role": the worker may simply not have
                    # reached this corporation, and telling somebody their
                    # permissions are wrong when they are not sends them to the
                    # wrong place entirely.
                    ctx["corp_error"] = _NOT_SYNCED_WALLET
                else:
                    journal, j_at = wallet_api.load_cached_ledger(
                        conn, corp_id, wallet_api.JOURNAL,
                        wallet_api.CORPORATION, division)
                    txns, _t_at = wallet_api.load_cached_ledger(
                        conn, corp_id, wallet_api.TRANSACTIONS,
                        wallet_api.CORPORATION, division)
                    if journal is None and txns is None:
                        ctx["corp_error"] = _NOT_SYNCED_DIVISION.format(division)
                    ctx["balance"] = next(
                        (w["balance"] for w in wallets
                         if w.get("division") == division), None)
                    ctx["cached_at"] = j_at
                    journal, txns = journal or [], txns or []
                    names = await _wallet_names(conn, journal, txns, token)
                    ctx["journal"], ctx["transactions"] = _decorate(
                        conn, journal, txns, _type_names, names)
        else:  # personal
            balance, b_at = wallet_api.load_cached_balance(conn, plan_char_id)
            journal, j_at = wallet_api.load_cached_ledger(
                conn, plan_char_id, wallet_api.JOURNAL)
            txns, _t_at = wallet_api.load_cached_ledger(
                conn, plan_char_id, wallet_api.TRANSACTIONS)
            if journal is None and txns is None:
                ctx["error"] = _NOT_SYNCED_WALLET
            ctx["balance"] = balance
            ctx["cached_at"] = j_at or b_at
            journal, txns = journal or [], txns or []
            names = await _wallet_names(conn, journal, txns, token)
            ctx["journal"], ctx["transactions"] = _decorate(
                conn, journal, txns, _type_names, names)
    except Exception as exc:
        ctx["error"] = f"Error loading wallet: {exc}"

    conn.close()
    return _tr("wallet.html", request, ctx)


def _party_ids(journal: list[dict], txns: list[dict]) -> set[int]:
    ids: set[int] = set()
    for j in journal:
        for k in ("first_party_id", "second_party_id"):
            if j.get(k):
                ids.add(j[k])
        # context system/station (e.g. the system where the bounty was earned) —
        # /universe/names/ can handle them (both <1e12)
        if j.get("context_id_type") in ("system_id", "station_id") and j.get("context_id"):
            ids.add(j["context_id"])
    for t in txns:
        if t.get("client_id"):
            ids.add(t["client_id"])
    return ids


def _context_structure_ids(journal: list[dict]) -> list[int]:
    """Player-structure IDs from the journal context (resolved via the auth endpoint)."""
    return list({
        j["context_id"] for j in journal
        if j.get("context_id_type") == "structure_id" and j.get("context_id")
    })


async def _wallet_names(conn, journal: list[dict], txns: list[dict], token: str
                        ) -> dict[int, str]:
    """Party names + context locations (system/station via /universe/names/,
    player structures via the authorized resolve_station_names_bulk)."""
    names = await _resolve_party_names(_party_ids(journal, txns))
    struct_ids = _context_structure_ids(journal)
    if struct_ids:
        try:
            with _connect() as _lc:
                names.update(await resolve_station_names_bulk(
                    struct_ids, token=token, conn=_lc))
        except Exception:
            pass
    return names


# How many journal / transaction rows reach the page. Measured before choosing:
# 500 rows per tab renders in ~290 ms, 2500 in ~590 ms, 5000 in ~1.1 s (6.5 MB of
# HTML). 2500 gives five times the history for twice the render cost and still
# costs a third of what the Prices page does, so that is the trade taken. Raising
# it further wants virtualised rows, not a bigger number.
_WALLET_ROW_CAP = 2500


def _decorate(conn, journal: list[dict], txns: list[dict],
              type_names_fn, party_names: dict[int, str]
              ) -> tuple[list[dict], list[dict]]:
    """Augment the journal with a humanized ref_type + party names; transactions
    with the item name, party names and total price. Return (journal, transactions)
    sorted newest first."""
    import re as _re
    # Bounty/agent payouts have a machine-readable breakdown of NPC kills in
    # `reason` ("24067: 2,24068: 3,…") — not shown in-game. We discard a reason
    # that is only digits/colons/commas (no readable text).
    _numeric_reason = _re.compile(r"^[\d\s:,]*$")
    dj = []
    for j in journal[:_WALLET_ROW_CAP]:
        reason = (j.get("reason") or "").strip()
        if _numeric_reason.match(reason):
            reason = ""
        # ESI sometimes prefixes a player-donation reason with "DESC: "
        if reason.startswith("DESC:"):
            reason = reason[5:].strip()
        # Location from the context (system where the bounty was earned, station/structure…)
        location = ""
        if j.get("context_id_type") in ("system_id", "station_id", "structure_id"):
            location = party_names.get(j.get("context_id"), "")
        dj.append({
            "date": j.get("date", ""),
            "ref_type": wallet_api.humanize_ref_type(j.get("ref_type", "")),
            "amount": j.get("amount"),
            "balance": j.get("balance"),
            "description": j.get("description", ""),
            "reason": reason,
            "location": location,
            "first_party": party_names.get(j.get("first_party_id"), ""),
            "second_party": party_names.get(j.get("second_party_id"), ""),
        })
    type_ids = {t.get("type_id") for t in txns}
    tnames = type_names_fn(type_ids)
    dt = []
    for t in txns[:_WALLET_ROW_CAP]:
        qty = t.get("quantity", 0)
        up = t.get("unit_price", 0.0)
        dt.append({
            "date": t.get("date", ""),
            "type_id": t.get("type_id"),
            "item": tnames.get(t.get("type_id"), f"#{t.get('type_id')}"),
            "quantity": qty,
            "unit_price": up,
            "total": qty * up,
            "is_buy": t.get("is_buy", False),
            "client": party_names.get(t.get("client_id"), ""),
        })
    dj.sort(key=lambda x: x["date"], reverse=True)
    dt.sort(key=lambda x: x["date"], reverse=True)
    return dj, dt


# ── Market Orders ─────────────────────────────────────────────────────────────

def _market_hubs_list() -> list[dict]:
    """Markets offered in the Orders item popup: Jita + the trade hubs."""
    return [{"id": JITA_REGION, "name": "Jita"}] + [
        {"id": rid, "name": info["name"]} for rid, info in TRADE_HUBS.items()]


def _decorate_orders(orders: list[dict], type_names: dict[int, str],
                     loc_names: dict[int, str],
                     loc_regions: dict[int, int] | None = None) -> list[dict]:
    """Augment orders with the item name, location, fill % and status. Sort newest
    by issue date (issued) first."""
    loc_regions = loc_regions or {}
    import datetime as _dt
    out = []
    for o in orders:
        total = o.get("volume_total", 0) or 0
        remain = o.get("volume_remain", 0) or 0
        filled = total - remain
        issued = o.get("issued", "")
        expiry = ""
        expiry_iso = ""
        try:
            if issued and o.get("duration"):
                base = _dt.datetime.fromisoformat(issued.replace("Z", "+00:00"))
                exp_dt = base + _dt.timedelta(days=o["duration"])
                expiry = exp_dt.strftime("%Y-%m-%d")
                expiry_iso = exp_dt.isoformat()   # exact — for the live d/h countdown
        except Exception:
            pass
        price = o.get("price", 0.0) or 0.0
        # ESI history has state only "expired"/"cancelled" — a fully filled order
        # closes as "expired" with volume_remain==0. So distinguish the real state:
        # completed = sold/bought with no remainder; expired = duration ran out with
        # a remainder; cancelled = cancelled by the user.
        raw_state = o.get("state", "")
        if remain == 0 and total:
            status_label = "completed"
        elif raw_state == "cancelled":
            status_label = "cancelled"
        else:
            status_label = raw_state or "expired"
        out.append({
            "type_id": o.get("type_id"),
            "item": type_names.get(o.get("type_id"), f"#{o.get('type_id')}"),
            "is_buy": o.get("is_buy_order", False),
            "price": price,
            "order_total": price * total,      # price for all units of the order
            "remain_total": price * remain,    # value of the still-unfilled part
            "volume_total": total,
            "volume_remain": remain,
            "filled": filled,
            "filled_pct": int(round(100 * filled / total)) if total else 0,
            "location": loc_names.get(o.get("location_id"), str(o.get("location_id", ""))),
            "location_id": o.get("location_id"),
            "region_id": loc_regions.get(o.get("location_id")),   # for the market-book popup
            "issued": issued,
            "expiry": expiry,
            "expiry_iso": expiry_iso,
            "state": o.get("state", ""),   # history only: expired / cancelled
            "status_label": status_label,  # completed / expired / cancelled
        })
    out.sort(key=lambda x: x["issued"], reverse=True)
    return out


@router.get("/orders", response_class=HTMLResponse)
async def orders_page(request: Request, char: str = "", scope: str = "personal",
                      state: str = "active"):
    conn = get_conn()
    all_chars = (char == "all")

    def _type_names(type_ids: set[int]) -> dict[int, str]:
        type_ids = {t for t in type_ids if t}
        if not type_ids:
            return {}
        ph = ",".join("?" * len(type_ids))
        return {r[0]: r[1] for r in conn.execute(
            f"SELECT type_id, name FROM sde_types WHERE type_id IN ({ph})", list(type_ids)
        ).fetchall()}

    # ── All characters: orders across all characters, tagged with "party" ──
    #   personal → one set per character (party = character)
    #   corp     → one set per UNIQUE corporation (party = corporation), so shared
    #              corp orders aren't duplicated when several characters are in one corp
    if all_chars:
        is_corp = (scope == "corp")
        ctx: dict = {
            "scope": "corp" if is_corp else "personal", "state": state,
            "orders_char_id": None, "all_chars": True, "orders": [],
            "error": None, "corp_error": None, "corp_name": None,
            "cached_at": 0.0, "unsynced": [],
            "market_hubs": _market_hubs_list(),
        }
        chars = list_characters(conn)
        if not chars:
            ctx["error"] = "You are not signed in."
            conn.close()
            return _tr("orders.html", request, ctx)

        # Names the cache has nothing for. Collected rather than counted so the
        # page can say *who* is missing — "3 characters not synced" sends you
        # looking, and the answer is usually one character whose token lapsed.
        unsynced: list[str] = []
        # The oldest reading in the merged set, because that is the age of the
        # weakest part of the answer — reporting the newest would describe the
        # one character that happened to sync last.
        oldest: list[float] = []

        async def _char_orders(cid: int, cname: str) -> list[dict]:
            tok = await _valid_token_async(cid)
            cached, at = orders_api.load_cached_orders(
                conn, cid, orders_api.CHARACTER, _cache_state(state))
            if cached is None:
                unsynced.append(cname or str(cid))
                return []
            oldest.append(at)
            decorated = await _finalize_orders(conn, cached, _type_names, tok,
                                               resolve_regions=(state != "history"))
            for o in decorated:
                o["party_id"], o["party_name"], o["party_kind"] = cid, cname, "char"
            return decorated

        async def _corp_orders(corp_id: int, corp_name: str, tok: str) -> list[dict]:
            cached, at = orders_api.load_cached_orders(
                conn, corp_id, orders_api.CORPORATION, _cache_state(state))
            if cached is None:
                unsynced.append(corp_name or str(corp_id))
                return []
            oldest.append(at)
            decorated = await _finalize_orders(conn, cached, _type_names, tok,
                                               resolve_regions=(state != "history"))
            for o in decorated:
                o["party_id"], o["party_name"], o["party_kind"] = corp_id, corp_name, "corp"
            return decorated

        try:
            if is_corp:
                # unique corp → token of a character in it. `corporation_id` is
                # written by the sync worker; a character it has not reached
                # has no corp here and is reported as unsynced rather than
                # looked up, which is what keeps this page off ESI.
                corp_token: dict[int, str] = {}
                for cid, cn in chars:
                    tok = await _valid_token_async(cid)
                    if not tok:
                        continue
                    crow = get_character_row(conn, cid) or {}
                    corp_id = crow.get("corporation_id")
                    if not corp_id:
                        unsynced.append(cn or str(cid))
                        continue
                    if corp_id not in corp_token:
                        corp_token[corp_id] = tok
                corp_names = await _resolve_party_names(set(corp_token)) if corp_token else {}
                results = await asyncio.gather(*[
                    _corp_orders(corp_id, corp_names.get(corp_id, str(corp_id)), tok)
                    for corp_id, tok in corp_token.items()
                ])
            else:
                results = await asyncio.gather(*[_char_orders(cid, cn) for cid, cn in chars])
            merged = [o for r in results for o in r]
            merged.sort(key=lambda x: x.get("issued", ""), reverse=True)
            ctx["orders"] = merged
            ctx["unsynced"] = unsynced
            ctx["cached_at"] = min(oldest) if oldest else 0.0
        except Exception as exc:
            ctx["error"] = f"Error loading orders: {exc}"
        conn.close()
        return _tr("orders.html", request, ctx)

    plan_char_id: int | None = None
    if char.isdigit() and get_character_row(conn, int(char)):
        plan_char_id = int(char)
    if plan_char_id is None:
        plan_char_id = get_active_character_id(request, conn)

    ctx: dict = {
        "scope": scope, "state": state, "orders_char_id": plan_char_id,
        "all_chars": False,
        "orders": [], "error": None, "corp_error": None, "corp_name": None,
        "cached_at": 0.0, "unsynced": [],
        "market_hubs": _market_hubs_list(),
    }
    if not plan_char_id:
        ctx["error"] = "You are not signed in."
        conn.close()
        return _tr("orders.html", request, ctx)
    token = await _valid_token_async(plan_char_id)
    row = get_character_row(conn, plan_char_id)
    if not token or not row:
        ctx["error"] = "The character token expired — sign in again."
        conn.close()
        return _tr("orders.html", request, ctx)

    try:
        if scope == "corp":
            corp_id = row.get("corporation_id")
            if not corp_id:
                # Written by the sync worker. Absent means this character has
                # not been synced yet, not that it has no corporation — the
                # page used to ask ESI here, which put a round trip on every
                # view of a page whose answer changes about once a year.
                ctx["corp_error"] = ("This character has not been synced yet, so "
                                     "its corporation is not known.")
            else:
                cn = await _resolve_party_names({corp_id})
                ctx["corp_name"] = cn.get(corp_id, str(corp_id))
                raw_orders, cached_at = orders_api.load_cached_orders(
                    conn, corp_id, orders_api.CORPORATION, _cache_state(state))
                if raw_orders is None:
                    ctx["corp_error"] = _NOT_SYNCED
                    raw_orders = []
                ctx["cached_at"] = cached_at
                ctx["orders"] = await _finalize_orders(conn, raw_orders, _type_names, token,
                                                       resolve_regions=(state != "history"))
        else:
            raw_orders, cached_at = orders_api.load_cached_orders(
                conn, plan_char_id, orders_api.CHARACTER, _cache_state(state))
            if raw_orders is None:
                ctx["error"] = _NOT_SYNCED
                raw_orders = []
            ctx["cached_at"] = cached_at
            ctx["orders"] = await _finalize_orders(conn, raw_orders, _type_names, token,
                                                   resolve_regions=(state != "history"))
    except Exception as exc:
        ctx["error"] = f"Error loading orders: {exc}"

    conn.close()
    return _tr("orders.html", request, ctx)


#: Nothing cached for this owner at all.
_NOT_SYNCED_WALLET = ("Not synced yet — the background worker fills this within "
                      "a few minutes of signing in.")

#: The corporation is known and its balances are cached, but this particular
#: division is not. Separate from the message above because the fix is
#: different: waiting will not help if the division has never held anything,
#: since the worker only walks divisions ESI reports.
_NOT_SYNCED_DIVISION = ("Division {} has not been synced. The worker only walks "
                        "divisions the corporation actually reports.")

#: Shown when the cache has nothing for an owner. Deliberately not "no orders":
#: the page has not looked yet, and saying otherwise is the bug this whole
#: cache-only conversion exists to avoid.
_NOT_SYNCED = ("Not synced yet — the background worker fills this within a few "
               "minutes of signing in.")


def _cache_state(state: str) -> str:
    """The page's `?state=` mapped onto the cache's. Anything unrecognised is
    active, matching what the rest of the handler already assumes."""
    from app.character import orders as _orders
    return _orders.HISTORY if state == "history" else _orders.ACTIVE


async def _finalize_orders(conn, raw_orders: list[dict], type_names_fn, token: str,
                           resolve_regions: bool = True) -> list[dict]:
    type_names = type_names_fn({o.get("type_id") for o in raw_orders})
    loc_ids = list({o.get("location_id") for o in raw_orders if o.get("location_id")})
    loc_names: dict[int, str] = {}
    if loc_ids:
        with _connect() as _lc:
            try:
                loc_names = await resolve_station_names_bulk(
                    loc_ids, token=token, conn=_lc)
            except Exception:
                loc_names = load_location_names_from_db(_lc)
    # Region per order location — so clicking an order can open the region-wide
    # order book it competes in (cached; failures → None, popup falls back to Jita).
    # Skipped for history (those items aren't clickable) to avoid wasted lookups.
    loc_regions: dict[int, int] = {}
    if loc_ids and resolve_regions:
        with _connect() as _lc:
            _regs = await asyncio.gather(
                *[get_region_for_location(_lc, lid, token) for lid in loc_ids],
                return_exceptions=True,
            )
        for lid, reg in zip(loc_ids, _regs):
            if isinstance(reg, int):
                loc_regions[lid] = reg
    return _decorate_orders(raw_orders, type_names, loc_names, loc_regions)
