"""Contracts: the personal/corporate contracts page and the public-contract
region index and search.

Moved out of `main.py` unchanged (W6). `_resolve_party_names` went to
`deps.py` on the way, because the wallet and orders pages resolve the same
character/corp ids and will land in different routers.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from app.auth.token_store import (
    get_character_row,
    list_characters,
    update_corporation_id,
)
from app.character import contracts as contracts_api
from app.esi.client import esi_client
from app.web import contracts_helper
from app.web.deps import (
    _resolve_party_names,
    _valid_token_async,
    _tr,
    get_active_character_id,
    get_conn,
)
from app.web.location_resolver import (
    load_location_names_from_db,
    resolve_station_names_bulk,
)

router = APIRouter()


# ── Contracts (personal / corporate) ───────────────────────────────────────────

def _decorate_contracts(raw: list[dict], party_names: dict[int, str],
                        loc_names: dict[int, str]) -> list[dict]:
    out = []
    for c in raw:
        ctype = c.get("type", "")
        iid, aid, acc = c.get("issuer_id"), c.get("assignee_id"), c.get("acceptor_id")
        start_id, end_id = c.get("start_location_id"), c.get("end_location_id")
        out.append({
            "contract_id":  c.get("contract_id"),
            "type":         contracts_api.type_label(ctype),
            "type_raw":     ctype,
            "status":       contracts_api.status_label(c.get("status", "")),
            "status_raw":   c.get("status", ""),
            "title":        c.get("title") or "",
            "price":        c.get("price") or 0.0,
            "reward":       c.get("reward") or 0.0,
            "collateral":   c.get("collateral") or 0.0,
            "volume":       c.get("volume") or 0.0,
            "issuer":       party_names.get(iid, str(iid) if iid else ""),
            "assignee":     party_names.get(aid, "") if aid else "",
            "acceptor":     party_names.get(acc, "") if acc else "",
            "start":        loc_names.get(start_id, "") if start_id else "",
            "end":          loc_names.get(end_id, "") if end_id else "",
            "courier":      ctype == "courier",
            "date_issued":  c.get("date_issued", ""),
            "date_expired": c.get("date_expired", ""),
            "for_corp":     c.get("for_corporation", False),
            "char_id":      c.get("_char_id") or 0,
            "corp_id":      c.get("_corp_id") or 0,
            "party_label":  c.get("_party_label") or "",
        })
    out.sort(key=lambda x: x["date_issued"], reverse=True)
    return out


async def _finalize_contracts(conn, raw: list[dict], token: str | None) -> list[dict]:
    party_ids: set[int] = set()
    loc_ids: set[int] = set()
    for c in raw:
        for k in ("issuer_id", "assignee_id", "acceptor_id"):
            if c.get(k):
                party_ids.add(c[k])
        for k in ("start_location_id", "end_location_id"):
            if c.get(k):
                loc_ids.add(c[k])
    party_names = await _resolve_party_names(party_ids) if party_ids else {}
    loc_names: dict[int, str] = {}
    if loc_ids:
        try:
            loc_names = await resolve_station_names_bulk(list(loc_ids), token=token, conn=conn)
        except Exception:
            loc_names = load_location_names_from_db(conn)
    return _decorate_contracts(raw, party_names, loc_names)


@router.get("/contracts", response_class=HTMLResponse)
async def contracts_page(request: Request, char: str = "", scope: str = "personal"):
    conn = get_conn()
    all_chars = (char == "all")
    ctx: dict = {
        "scope": scope, "contracts_char_id": None, "all_chars": all_chars,
        "contracts": [], "error": None, "corp_error": None,
    }

    chars = list_characters(conn)
    if not chars:
        ctx["error"] = "You are not signed in."
        conn.close()
        return _tr("contracts.html", request, ctx)

    try:
        if all_chars:
            raw: list[dict] = []
            if scope == "corp":
                corp_token: dict[int, str] = {}
                for cid, _cn in chars:
                    tok = await _valid_token_async(cid)
                    if not tok:
                        continue
                    corp_id = (get_character_row(conn, cid) or {}).get("corporation_id")
                    if corp_id and corp_id not in corp_token:
                        corp_token[corp_id] = tok
                corp_names = await _resolve_party_names(set(corp_token)) if corp_token else {}
                async with esi_client() as client:
                    for corp_id, tok in corp_token.items():
                        lst, _err = await contracts_api.fetch_corp_contracts(client, corp_id, tok)
                        for c in (lst or []):
                            c["_corp_id"] = corp_id
                            c["_party_label"] = corp_names.get(corp_id, str(corp_id))
                            raw.append(c)
            else:
                async with esi_client() as client:
                    for cid, cname in chars:
                        tok = await _valid_token_async(cid)
                        if not tok:
                            continue
                        for c in await contracts_api.fetch_character_contracts(client, cid, tok):
                            c["_char_id"] = cid
                            c["_party_label"] = cname
                            raw.append(c)
            # dedup by contract_id (several characters may see the same contract)
            seen: set[int] = set()
            raw = [c for c in raw if not (c.get("contract_id") in seen or seen.add(c.get("contract_id")))]
            # One refresh per character, concurrently. The generator called
            # the blocking version twice each — condition and value.
            tokens = await asyncio.gather(*[_valid_token_async(c) for c, _ in chars])
            any_tok = next((t for t in tokens if t), None)
            ctx["contracts"] = await _finalize_contracts(conn, raw, any_tok)
            conn.close()
            return _tr("contracts.html", request, ctx)

        # single character
        plan_char_id = int(char) if char.isdigit() and get_character_row(conn, int(char)) else None
        if plan_char_id is None:
            plan_char_id = get_active_character_id(request, conn)
        ctx["contracts_char_id"] = plan_char_id
        token = await _valid_token_async(plan_char_id) if plan_char_id else None
        row = get_character_row(conn, plan_char_id) if plan_char_id else None
        if not token or not row:
            ctx["error"] = "The character token expired — sign in again."
            conn.close()
            return _tr("contracts.html", request, ctx)

        async with esi_client() as client:
            if scope == "corp":
                corp_id = row.get("corporation_id")
                if not corp_id:
                    cr = await client.get(
                        f"https://esi.evetech.net/latest/characters/{plan_char_id}/", timeout=10)
                    if cr.status_code == 200:
                        corp_id = cr.json().get("corporation_id")
                        if corp_id:
                            update_corporation_id(conn, plan_char_id, corp_id)
                if not corp_id:
                    ctx["corp_error"] = "Could not determine the character's corporation."
                    raw = []
                else:
                    lst, err = await contracts_api.fetch_corp_contracts(client, corp_id, token)
                    ctx["corp_error"] = err
                    raw = lst or []
                    for c in raw:
                        c["_corp_id"] = corp_id
            else:
                raw = await contracts_api.fetch_character_contracts(client, plan_char_id, token)
                for c in raw:
                    c["_char_id"] = plan_char_id
        ctx["contracts"] = await _finalize_contracts(conn, raw, token)
    except Exception as exc:
        ctx["error"] = f"Error loading contracts: {exc}"

    conn.close()
    return _tr("contracts.html", request, ctx)


@router.get("/api/contracts/items")
async def api_contract_items(request: Request, contract_id: int,
                             char_id: int = 0, corp_id: int = 0):
    """Lazy fetch of a contract's items (on expand). Returns resolved names from the SDE."""
    conn = get_conn()
    try:
        items: list[dict] = []
        async with esi_client() as client:
            if corp_id:
                tok = None
                for cid, _ in list_characters(conn):
                    if (get_character_row(conn, cid) or {}).get("corporation_id") == corp_id:
                        tok = await _valid_token_async(cid)
                        if tok:
                            break
                if tok:
                    items = await contracts_api.fetch_corp_contract_items(client, corp_id, contract_id, tok)
            elif char_id:
                tok = await _valid_token_async(char_id)
                if tok:
                    items = await contracts_api.fetch_character_contract_items(client, char_id, contract_id, tok)
        tids = {it.get("type_id") for it in items if it.get("type_id")}
        names: dict[int, str] = {}
        if tids:
            ph = ",".join("?" * len(tids))
            names = {r[0]: r[1] for r in conn.execute(
                f"SELECT type_id, name FROM sde_types WHERE type_id IN ({ph})", list(tids)
            ).fetchall()}
        out = [{
            "type_id":  it.get("type_id"),
            "name":     names.get(it.get("type_id"), f"#{it.get('type_id')}"),
            "quantity": it.get("quantity", 0),
            "included": it.get("is_included", True),
        } for it in items]
        return {"items": out}
    finally:
        conn.close()


# ── Public contracts (per-region index + local search) ─────────────────────

_REGIONS_CACHE: list[tuple[int, str]] | None = None


async def _get_all_regions() -> list[tuple[int, str]]:
    """All regions (id, name) from ESI, sorted by name. Cached per process
    (regions practically never change). Skips wormhole/abyssal (>= 11000000) —
    there are no public contracts there."""
    global _REGIONS_CACHE
    if _REGIONS_CACHE is not None:
        return _REGIONS_CACHE
    try:
        async with esi_client(timeout=15) as client:
            r = await client.get("https://esi.evetech.net/latest/universe/regions/")
            ids = [i for i in (r.json() if r.status_code == 200 else []) if i < 11000000]
            names: dict[int, str] = {}
            for i in range(0, len(ids), 1000):
                rr = await client.post("https://esi.evetech.net/latest/universe/names/",
                                      json=ids[i:i + 1000], headers={"Accept": "application/json"})
                if rr.status_code == 200:
                    for it in rr.json():
                        names[it["id"]] = it["name"]
        regs = sorted(((rid, names.get(rid, str(rid))) for rid in ids), key=lambda x: x[1])
        if regs:
            _REGIONS_CACHE = regs
        return regs
    except Exception:
        return _REGIONS_CACHE or []


async def _resolve_region_id(name_or_id: str) -> tuple[int | None, str]:
    """Return (region_id, region_name) from a name or ID. (None,'') if not found."""
    s = name_or_id.strip()
    if not s:
        return None, ""
    if s.isdigit():
        return int(s), s
    try:
        async with esi_client(timeout=10) as client:
            r = await client.post("https://esi.evetech.net/latest/universe/ids/",
                                  json=[s], headers={"Accept": "application/json"})
            if r.status_code == 200:
                regs = r.json().get("regions") or []
                if regs:
                    return regs[0]["id"], regs[0]["name"]
    except Exception:
        pass
    return None, s


@router.get("/contracts/public", response_class=HTMLResponse)
async def public_contracts_page(request: Request, region: str = "", item: str = "",
                                ctype: str = "", max_price: str = ""):
    conn = get_conn()
    ctx: dict = {
        "region_name": region, "region_id": None, "status": None, "results": [],
        "item": item, "ctype": ctype, "max_price": max_price, "error": None,
        "regions": await _get_all_regions(),
    }
    region_id, region_name = await _resolve_region_id(region)
    ctx["region_id"] = region_id
    ctx["region_name"] = region_name
    if region and region_id is None:
        ctx["error"] = f'Region "{region}" not found.'
    if region_id:
        ctx["status"] = contracts_helper.get_index_status(conn, region_id)
        if ctx["status"]:
            mp = None
            try:
                mp = float(max_price) if max_price.strip() else None
            except ValueError:
                mp = None
            results = contracts_helper.search_public_contracts(
                conn, region_id, item=item, ctype=ctype, max_price=mp)
            party_ids = {c["issuer_id"] for c in results if c["issuer_id"]}
            loc_ids = {lid for c in results for lid in (c["start_location_id"], c["end_location_id"]) if lid}
            party_names = await _resolve_party_names(party_ids) if party_ids else {}
            loc_names: dict[int, str] = {}
            if loc_ids:
                tokens = await asyncio.gather(
                    *[_valid_token_async(cid) for cid, _ in list_characters(conn)])
                any_tok = next((t for t in tokens if t), None)
                try:
                    loc_names = await resolve_station_names_bulk(list(loc_ids), token=any_tok, conn=conn)
                except Exception:
                    loc_names = load_location_names_from_db(conn)
            for c in results:
                c["type_label"] = contracts_api.type_label(c["type"])
                c["issuer_name"] = party_names.get(c["issuer_id"], str(c["issuer_id"] or ""))
                c["start_name"] = loc_names.get(c["start_location_id"], "")
                c["end_name"] = loc_names.get(c["end_location_id"], "")
                c["courier"] = c["type"] == "courier"
            ctx["results"] = results
    conn.close()
    return _tr("contracts_public.html", request, ctx)


@router.get("/api/contracts/public/index")
async def api_public_index(request: Request, region_id: int):
    """SSE stream: indexes a region (listing + items) into the cache."""
    async def gen():
        conn = get_conn()
        try:
            async for chunk in contracts_helper.stream_public_index(conn, region_id):
                yield chunk
        finally:
            conn.close()
    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/contracts/public/items")
async def api_public_contract_items(request: Request, contract_id: int):
    conn = get_conn()
    try:
        return {"items": contracts_helper.get_contract_items(conn, contract_id)}
    finally:
        conn.close()
