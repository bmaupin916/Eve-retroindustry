"""
Loading market prices from ESI.

Two modes:
  adjusted  – global adjusted/average prices, single API call
  jita      – live Jita sell/buy prices, N parallel calls, 30 min cache
"""
import asyncio
import json
import time
import sqlite3
import httpx
from app.esi.client import esi_client
from sqlalchemy import text
from sqlalchemy.engine import Connection

from app.db.conn import dbapi
from app.db.schema import ensure_schema as ensure_db_schema

ESI_BASE = "https://esi.evetech.net/latest"
JITA_REGION = 10000002   # The Forge
JITA_STATION = 60003760  # Jita 4-4 CNAP
PRICE_CACHE_TTL = 60 * 60 * 12  # 12 hours

# Secondary trade hubs — region_id → {name, station}. Jita stays the app-wide
# reference (market_price_cache); these are fetched on demand per hub into
# hub_price_cache and shown as comparison columns on the Prices page. Sell/buy are
# region-wide best (as with Jita/The Forge); `station` is the hub's main station,
# used to sum "available" units (sell-order volume) there.
TRADE_HUBS: dict[int, dict] = {
    10000043: {"name": "Amarr",   "station": 60008494},  # Domain / Amarr VIII (Oris)
    10000032: {"name": "Dodixie", "station": 60011866},  # Sinq Laison / Dodixie IX-M20
    10000030: {"name": "Rens",    "station": 60004588},  # Heimatar / Rens VI-M8
    10000042: {"name": "Hek",     "station": 60005686},  # Metropolis / Hek VIII-M12
}
# Used ONLY for the UI freshness indicator (green/red badge on /prices,
# `fresh` flag in the API). For price calculations (`get_prices_for_ids`) the
# cache does NOT expire — the last fetched Jita / The Forge sell value is always
# used, regardless of age. A full refresh via `/markets/{region}/orders/` takes
# ~3 s, and the user usually refreshes once a day.

_JITA_SEM = asyncio.Semaphore(10)
# The 7-day history is fetched per-type (no bulk endpoint), so it is the dominant
# part of a refresh (~19k calls). Concurrency 30 = ~2.5x faster than 10 (515 vs
# 204 req/s measured), while staying safely under the ESI rate limit — from ~45
# concurrent, ESI starts returning HTTP 420 (error-limit), which is slower AND
# damages the shared error budget of the whole app. 30 keeps zero 420s with margin.
_HIST_SEM = asyncio.Semaphore(30)


# ---------------------------------------------------------------------------
# DB schema
# ---------------------------------------------------------------------------

def ensure_price_table(conn) -> None:
    """Schema shim. The table lives in app/db/schema.py; this only guarantees it exists.

    Dialect-guarded like its sibling `ensure_hist_etag_table`: `ensure_schema`
    memoises by asking `PRAGMA database_list` which file it is looking at, which
    is a syntax error on Postgres. Converted alongside the rest of this module
    so every caller hands these functions the same kind of connection — a shim
    left on the old contract is how a caller ends up passing two different
    connection types into one module.
    """
    if conn.engine.dialect.name != "sqlite":
        return
    ensure_db_schema(dbapi(conn))


# ---------------------------------------------------------------------------
# Adjusted prices (global, 1 call)
# ---------------------------------------------------------------------------

async def fetch_adjusted_prices(client: httpx.AsyncClient) -> dict[int, dict]:
    """
    Returns {type_id: {adjusted_price, average_price}} for all types.
    A single API call — suitable for a quick estimate.

    Best-effort: this is only a fallback price estimate. NEVER raises —
    on a 420 (ESI error-limit), timeout, or any other error it returns {}, so
    an ESI failure never takes down the dashboard / plan. The caller handles an empty dict.
    """
    try:
        r = await client.get(
            f"{ESI_BASE}/markets/prices/",
            params={"datasource": "tranquility"},
            timeout=20,
        )
        if r.status_code != 200:
            return {}
        return {d["type_id"]: d for d in r.json()}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Jita live prices (per type, cached)
# ---------------------------------------------------------------------------

def _get_cached_price(conn: Connection, type_id: int) -> tuple[float | None, float | None]:
    row = conn.execute(
        text("SELECT sell_price, buy_price, cached_at FROM market_price_cache"
             " WHERE type_id=:tid"),
        {"tid": type_id},
    ).fetchone()
    if row and (time.time() - (row[2] or 0)) < PRICE_CACHE_TTL:
        return row[0], row[1]
    return None, None


def _save_cached_price(
    conn: Connection,
    type_id: int,
    sell: float | None,
    buy: float | None,
    volume: int | None = None,
    jita_available: int | None = None,
):
    conn.execute(
        text("INSERT INTO market_price_cache"
             " (type_id, sell_price, buy_price, volume, jita_available, cached_at)"
             " VALUES (:type_id, :sell, :buy, :volume, :jita_available, :cached_at)"
             " ON CONFLICT (type_id) DO UPDATE SET"
             " sell_price=excluded.sell_price, buy_price=excluded.buy_price,"
             " volume=excluded.volume, jita_available=excluded.jita_available,"
             " cached_at=excluded.cached_at"),
        {"type_id": type_id, "sell": sell, "buy": buy, "volume": volume,
         "jita_available": jita_available, "cached_at": time.time()},
    )
    conn.commit()


async def fetch_region_history(client: httpx.AsyncClient, region_id: int, type_id: int) -> list[dict] | None:
    """Full daily market history (~1 year) for a type in a region. Returns a list
    of {d, avg, low, high, vol} oldest→newest, or None on error. Same ESI endpoint
    the 7-day volume already uses — we just keep the whole series."""
    async with _HIST_SEM:
        try:
            r = await client.get(
                f"{ESI_BASE}/markets/{region_id}/history/",
                params={"type_id": type_id, "datasource": "tranquility"},
                timeout=20,
            )
            if r.status_code != 200:
                return None
            hist = r.json()
            if not isinstance(hist, list):
                return None
            return [
                {"d": e.get("date"), "avg": e.get("average"),
                 "low": e.get("lowest"), "high": e.get("highest"),
                 "vol": e.get("volume", 0),
                 # ESI returns this in the same record and it was being
                 # thrown away. Keeping it is what makes §9.4's Competition
                 # KPI possible without re-fetching a year of history per
                 # type — which is why it lands before the bulk fill, not
                 # after it.
                 "orders": e.get("order_count", 0)}
                for e in hist
            ]
        except Exception:
            return None


# ── Market-history ETag cache ────────────────────────────────────────────────
# A market-history response is ~42 KB (≈408 daily records) and the volume phase
# asks for ~19k types, i.e. ~800 MB of JSON to download and parse on every
# refresh. ESI serves these with an ETag and honours If-None-Match, answering
# 304 with an EMPTY body when nothing changed (history is rebuilt once a day).
# Measured on a 100-type sample: 3.93 MB -> 0 bytes, 37% less wall-clock.
#
# 304 alone isn't enough to be correct, though: "last 7 CALENDAR days" is a
# moving window, so an unchanged history can still yield a different number
# tomorrow. We therefore keep the last _ETAG_KEEP_DAYS daily volumes next to the
# ETag (a handful of ints) and recompute the window locally on a 304 — exact,
# and still zero bytes over the wire.
_ETAG_KEEP_DAYS = 12          # > 7, so the window can always be recomputed
_HIST_WINDOW_DAYS = 7

# (region_id, type_id) -> (etag, {date: volume}, expires_at epoch)
_hist_etags: dict[tuple[int, int], tuple[str, dict[str, int], float]] = {}
_hist_etags_dirty: set[tuple[int, int]] = set()


def ensure_hist_etag_table(conn) -> None:
    """Schema shim. The table lives in app/db/schema.py; this only guarantees it exists.

    Guarded on the dialect, for the same reason as the other shims:
    `ensure_schema` memoises by asking `PRAGMA database_list` which file it has,
    and that is a syntax error on Postgres. There the table arrives from the
    migration history, so there is nothing to shim.
    """
    if conn.engine.dialect.name != "sqlite":
        return
    ensure_db_schema(dbapi(conn))


def load_hist_etags(conn: Connection, region_id: int) -> int:
    """Load a region's stored ETags into memory before a volume phase."""
    ensure_hist_etag_table(conn)
    n = 0
    for tid, etag, days_json, expires_at in conn.execute(
        text("SELECT type_id, etag, days_json, expires_at FROM market_hist_etag"
             " WHERE region_id=:rid"),
        {"rid": region_id},
    ):
        if not etag:
            continue
        try:
            days = json.loads(days_json) if days_json else {}
        except Exception:
            days = {}
        _hist_etags[(region_id, tid)] = (etag, days, expires_at or 0.0)
        n += 1
    return n


def flush_hist_etags(conn: Connection) -> int:
    """Persist ETags collected during a volume phase (bulk, one transaction)."""
    if not _hist_etags_dirty:
        return 0
    ensure_hist_etag_table(conn)
    now = time.time()
    rows = []
    for key in list(_hist_etags_dirty):
        entry = _hist_etags.get(key)
        if not entry:
            continue
        rows.append({"region_id": key[0], "type_id": key[1], "etag": entry[0],
                     "days_json": json.dumps(entry[1]), "cached_at": now,
                     "expires_at": entry[2]})
    if not rows:
        # Every dirty key had already been evicted from `_hist_etags`. Under
        # `sqlite3` an empty `executemany` was a no-op; SQLAlchemy raises
        # `StatementError` on an empty parameter list, so this guard is
        # load-bearing after the conversion rather than tidiness.
        _hist_etags_dirty.clear()
        return 0
    conn.execute(
        text("INSERT INTO market_hist_etag"
             " (region_id, type_id, etag, days_json, cached_at, expires_at)"
             " VALUES (:region_id, :type_id, :etag, :days_json, :cached_at,"
             "  :expires_at)"
             " ON CONFLICT (region_id, type_id) DO UPDATE SET"
             " etag=excluded.etag, days_json=excluded.days_json,"
             " cached_at=excluded.cached_at, expires_at=excluded.expires_at"),
        rows,
    )
    conn.commit()
    _hist_etags_dirty.clear()
    # Drop the in-memory copy: it is now durable in SQLite, and holding every
    # region's map (Jita + 4 hubs + custom stations, ~19k types each) would cost
    # tens of MB in a desktop app. Each phase reloads just the region it needs.
    _hist_etags.clear()
    return len(rows)


def _window_sum(days: dict[str, int]) -> int:
    """Sum the daily volumes that fall inside the current 7-calendar-day window."""
    import datetime
    cutoff = (datetime.date.today() - datetime.timedelta(days=_HIST_WINDOW_DAYS)).isoformat()
    return sum(v for d, v in days.items() if d >= cutoff)


def _recent_days(history: list[dict]) -> dict[str, int]:
    """Keep only the newest _ETAG_KEEP_DAYS entries — enough to recompute the
    window later without storing a year of data per type."""
    tail = history[-_ETAG_KEEP_DAYS:] if len(history) > _ETAG_KEEP_DAYS else history
    return {e["date"]: e.get("volume", 0) for e in tail if e.get("date")}


def _parse_http_date(v: str | None) -> float:
    """RFC-1123 date -> epoch seconds (0 if absent/unparseable)."""
    if not v:
        return 0.0
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(v).timestamp()
    except Exception:
        return 0.0


async def _fetch_region_volume(client: httpx.AsyncClient, region_id: int, type_id: int) -> int | None:
    """Total units traded over the last 7 CALENDAR days from ESI history.

    ESI omits days with no trades, so summing the last 7 *entries* over-counts
    for illiquid items (e.g. a SKIN that trades once a week would sum ~2 months
    of days). We sum only entries dated within the last 7 days — 0 if it hasn't
    traded recently, which is the truthful answer.

    Sends If-None-Match when we already have an ETag: a 304 costs no body at all
    and the window is recomputed from the stored daily volumes.
    """
    key = (region_id, type_id)
    cached = _hist_etags.get(key)
    # ESI rebuilds market history once a day and tells us when the current copy
    # stops being authoritative (Expires). While that hasn't passed, a refetch is
    # guaranteed to return the same bytes — so skip the round trip entirely and
    # recompute the moving 7-day window from the stored daily volumes. This is
    # plain HTTP caching (no invented TTL, no staler data), and it turns a repeat
    # refresh on the same day from ~19k requests into zero.
    if cached and cached[2] and time.time() < cached[2]:
        return _window_sum(cached[1])
    req_headers = {"If-None-Match": cached[0]} if cached else {}

    async with _HIST_SEM:
        # Retry on transient failures. Loading a custom station fires this for
        # ~19k types at once; without retries a large fraction hit the ESI error
        # limit (420) or time out and came back None, leaving the "region vol/7d"
        # column blank for ~half the items. A 200 with an empty history list means
        # the type has simply never traded → 0, which is a real answer (not a
        # failure), so we don't retry that.
        for attempt in range(2):   # one quick retry — enough for transient blips
            try:
                r = await client.get(
                    f"{ESI_BASE}/markets/{region_id}/history/",
                    params={"type_id": type_id, "datasource": "tranquility"},
                    headers=req_headers,
                    timeout=12,
                )
            except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError):
                if attempt == 0:
                    await asyncio.sleep(0.3)
                    continue
                return None
            if r.status_code == 304 and cached:
                exp = _parse_http_date(r.headers.get("expires"))
                if exp:                             # 304s carry a fresh Expires
                    _hist_etags[key] = (cached[0], cached[1], exp)
                    _hist_etags_dirty.add(key)
                return _window_sum(cached[1])       # unchanged upstream → local recompute
            if r.status_code == 200:
                history = r.json()
                if not isinstance(history, list) or not history:
                    return 0
                etag = r.headers.get("etag")
                days = _recent_days(history)
                if etag:
                    _hist_etags[key] = (etag, days, _parse_http_date(r.headers.get("expires")))
                    _hist_etags_dirty.add(key)
                return _window_sum(days)
            if (r.status_code == 420 or r.status_code >= 500) and attempt == 0:
                await asyncio.sleep(0.6)
                continue
            return None   # 400/404/… → no usable data
    return None


async def _fetch_jita_volume(client: httpx.AsyncClient, type_id: int) -> int | None:
    return await _fetch_region_volume(client, JITA_REGION, type_id)


async def fetch_jita_price(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    type_id: int,
    force: bool = False,
) -> tuple[float | None, float | None]:
    """
    Returns (best_sell, best_buy) for the given type in Jita.
    Uses the cache — valid for 30 minutes. force=True skips the cache and always fetches fresh data.
    """
    if not force:
        sell_c, buy_c = _get_cached_price(conn, type_id)
        if sell_c is not None or buy_c is not None:
            return sell_c, buy_c

    orders_resp = None
    for attempt in range(3):
        try:
            async with _JITA_SEM:
                orders_resp = await client.get(
                    f"{ESI_BASE}/markets/{JITA_REGION}/orders/",
                    params={"type_id": type_id, "order_type": "all", "datasource": "tranquility"},
                    timeout=15,
                )
        except (httpx.TimeoutException, httpx.ConnectError):
            if attempt < 2:
                await asyncio.sleep(2 ** attempt * 3)
                continue
            return None, None

        if orders_resp.status_code in (420, 429):
            retry_after = int(orders_resp.headers.get("Retry-After", 60))
            await asyncio.sleep(min(retry_after, 120))
            continue
        if orders_resp.status_code == 404:
            _save_cached_price(conn, type_id, None, None, None, None)
            return None, None
        if orders_resp.status_code != 200:
            if attempt < 2:
                await asyncio.sleep(5)
                continue
            return None, None
        break
    else:
        return None, None

    volume = await _fetch_jita_volume(client, type_id)

    orders = orders_resp.json()
    sell_orders = [o for o in orders if not o["is_buy_order"]]
    buy_orders  = [o for o in orders if o["is_buy_order"]]

    best_sell = min((o["price"] for o in sell_orders), default=None)
    best_buy  = max((o["price"] for o in buy_orders),  default=None)

    jita_available = sum(
        o.get("volume_remain", 0) for o in sell_orders
        if o.get("location_id") == JITA_STATION
    )

    _save_cached_price(conn, type_id, best_sell, best_buy, volume, jita_available)
    return best_sell, best_buy


async def fetch_jita_prices_bulk(
    client: httpx.AsyncClient,
    conn: sqlite3.Connection,
    type_ids: list[int],
    force: bool = False,
) -> dict[int, tuple[float | None, float | None]]:
    """Fetches Jita prices for a list of types in parallel."""
    tasks = [fetch_jita_price(client, conn, tid, force=force) for tid in type_ids]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return {
        tid: res if isinstance(res, tuple) else (None, None)
        for tid, res in zip(type_ids, results)
    }


# ---------------------------------------------------------------------------
# Bulk Jita orders — fetch all active orders in the region at once (paginated)
# ---------------------------------------------------------------------------

async def _fetch_orders_page(
    client: httpx.AsyncClient,
    region_id: int,
    page: int,
) -> tuple[list[dict], int]:
    """Fetches a single page of orders and returns (orders, x_pages)."""
    async with _JITA_SEM:
        for attempt in range(3):
            try:
                r = await client.get(
                    f"{ESI_BASE}/markets/{region_id}/orders/",
                    params={"order_type": "all", "datasource": "tranquility", "page": page},
                    timeout=30,
                )
            except (httpx.TimeoutException, httpx.ConnectError):
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt * 3)
                    continue
                return [], 0
            if r.status_code in (420, 429):
                retry_after = int(r.headers.get("Retry-After", 60))
                await asyncio.sleep(min(retry_after, 120))
                continue
            if r.status_code != 200:
                if attempt < 2:
                    await asyncio.sleep(5)
                    continue
                return [], 0
            return r.json(), int(r.headers.get("x-pages", 1))
        return [], 0


async def _fetch_all_region_orders(
    client: httpx.AsyncClient,
    region_id: int,
    progress_cb=None,
) -> list[list[dict]]:
    """Fetches ALL pages of the region's orders, paginated (in parallel, _JITA_SEM).
    Returns a list of pages (each = list of orders). progress_cb(done, total) after each
    page. Shared between region-bulk and station-bulk aggregation."""
    first, total_pages = await _fetch_orders_page(client, region_id, 1)
    if not first and total_pages == 0:
        return []

    pages_data: list[list[dict]] = [first]
    if progress_cb:
        await _maybe_call(progress_cb, 1, total_pages)

    remaining = list(range(2, total_pages + 1))
    completed = [1]
    lock = asyncio.Lock()

    async def _one(p: int):
        page_data, _ = await _fetch_orders_page(client, region_id, p)
        async with lock:
            pages_data.append(page_data)
            completed[0] += 1
            if progress_cb:
                await _maybe_call(progress_cb, completed[0], total_pages)

    await asyncio.gather(*[_one(p) for p in remaining], return_exceptions=True)
    return pages_data


async def fetch_station_orders_bulk(
    client: httpx.AsyncClient,
    region_id: int,
    location_id: int,
    progress_cb=None,
) -> dict[int, tuple[int, float]]:
    """Bulk variant for a specific station: fetches regional orders once
    (~pages, not ~19k per-type calls) and returns {type_id: (sell_volume_sum,
    best_sell)} aggregated ONLY for sell orders at the given location_id.

    Orders of magnitude faster than per-type `_fetch_orders_for_type` for large type_ids."""
    pages_data = await _fetch_all_region_orders(client, region_id, progress_cb)
    agg: dict[int, tuple[int, float]] = {}
    for page_orders in pages_data:
        for o in page_orders:
            if o.get("is_buy_order"):
                continue
            if o.get("location_id") != location_id:
                continue
            tid = o.get("type_id")
            price = o.get("price")
            if tid is None or price is None:
                continue
            vol = int(o.get("volume_remain", 0))
            cur = agg.get(tid)
            if cur is None:
                agg[tid] = (vol, price)
            else:
                agg[tid] = (cur[0] + vol, min(cur[1], price))
    return agg


async def fetch_region_orders_bulk(
    client: httpx.AsyncClient,
    region_id: int = JITA_REGION,
    progress_cb=None,
    station_id: int = JITA_STATION,
) -> dict[int, dict]:
    """Fetches ALL active orders for the region, paginated, and aggregates
    per type_id: {type_id: {sell, buy, available}}. `available` = units in sell
    orders at `station_id` (the hub's main station).

    This is orders of magnitude more efficient than a per-type call: ~500 pages vs. 19k calls.
    progress_cb(page, total_pages) is called after each page (if provided).
    """
    pages_data = await _fetch_all_region_orders(client, region_id, progress_cb)
    if not pages_data:
        return {}

    # Aggregate per type_id
    agg: dict[int, dict] = {}
    for page_orders in pages_data:
        for o in page_orders:
            tid = o.get("type_id")
            price = o.get("price")
            if tid is None or price is None:
                continue
            entry = agg.setdefault(tid, {"sell": None, "buy": None, "available": 0})
            if o.get("is_buy_order"):
                if entry["buy"] is None or price > entry["buy"]:
                    entry["buy"] = price
            else:
                if entry["sell"] is None or price < entry["sell"]:
                    entry["sell"] = price
                if o.get("location_id") == station_id:
                    entry["available"] += int(o.get("volume_remain", 0))
    return agg


async def _maybe_call(cb, *args):
    """Helper — the callback can be sync or async."""
    if asyncio.iscoroutinefunction(cb):
        await cb(*args)
    else:
        cb(*args)


# Per-type orders at a custom station (phase A in fetch_station_volumes). Runs
# sequentially before the history phase (_HIST_SEM), so concurrency does not add up — 30 is
# safe under the ESI rate limit (same as _HIST_SEM).
_STATION_SEM = asyncio.Semaphore(30)
STATION_VOLUME_TTL = 60 * 30
# From this many type_ids onward, bulk (a single region download) pays off in
# fetch_station_volumes instead of per-type calls. Below the threshold, per-type is lighter and faster.
_BULK_ORDERS_THRESHOLD = 1000
_region_cache: dict[int, int] = {}  # structure_id → region_id (in-memory)


async def get_region_for_structure(structure_id: int) -> int | None:
    """Resolves the region_id for a structure via ESI (system→constellation→region). Cached in memory."""
    if structure_id in _region_cache:
        return _region_cache[structure_id]
    try:
        async with esi_client() as client:
            # NPC station: /universe/stations/{id}/ → system_id
            if structure_id < 1_000_000_000_000:
                r = await client.get(f"{ESI_BASE}/universe/stations/{structure_id}/",
                                     params={"datasource": "tranquility"}, timeout=8)
                sys_id = r.json().get("system_id") if r.status_code == 200 else None
            else:
                # Player structure — we have no token here, try via DB location_name_cache
                return None

            if not sys_id:
                return None

            sys_r = await client.get(f"{ESI_BASE}/universe/systems/{sys_id}/",
                                     params={"datasource": "tranquility"}, timeout=8)
            if sys_r.status_code != 200:
                return None
            con_id = sys_r.json().get("constellation_id")

            con_r = await client.get(f"{ESI_BASE}/universe/constellations/{con_id}/",
                                     params={"datasource": "tranquility"}, timeout=8)
            if con_r.status_code != 200:
                return None
            region_id = con_r.json().get("region_id")

        if region_id:
            _region_cache[structure_id] = region_id
        return region_id
    except Exception:
        return None


def _cached_region_volume(conn: Connection, region_id: int | None) -> dict[int, int] | None:
    """Reuse an already-fetched 7-day *region* volume map so a custom station in a
    known region needn't re-fetch ~19k histories (the slow part of a station load).
    The Jita refresh stores The Forge volume in market_price_cache; hub refreshes
    store theirs in hub_price_cache. A custom station's "region vol/7d" is exactly
    that region-wide number, so when the region is one we've already loaded we can
    reuse it verbatim — the same data the Jita/hub columns show. Returns
    {type_id: volume} or None if that region isn't cached yet.

    **Only while ESI still calls that copy current.** The stored `volume` is a
    precomputed 7-day *sum*, and a sum cannot be re-windowed: once ESI rebuilds
    its history the window has moved, the total is wrong, and nothing about the
    value says so. Reusing it verbatim for ever therefore quietly under- or
    over-states the region volume on every custom station in the region.

    The freshness test is ESI's own `Expires`, which `market_hist_etag` already
    records per type — the same header `_region_history_volume` uses a few lines
    up to skip a refetch entirely. No invented TTL: this file's stance is that a
    number is current while the server says it is and not one second longer.
    `MIN(expires_at)` is the conservative summary of a region — if the earliest
    entry has expired, the window has moved for all of them.

    A region with no recorded expiry returns None and takes the slow path. That
    is the right direction: a sum whose freshness cannot be established is not
    one to serve as current.
    """
    if not region_id:
        return None

    fresh_until = conn.execute(
        text("SELECT MIN(expires_at) FROM market_hist_etag WHERE region_id = :rid"),
        {"rid": region_id}).scalar()
    if not fresh_until or time.time() >= fresh_until:
        return None

    if region_id == JITA_REGION:
        rows = conn.execute(text(
            "SELECT type_id, volume FROM market_price_cache"
            " WHERE volume IS NOT NULL")).fetchall()
    else:
        rows = conn.execute(
            text("SELECT type_id, volume FROM hub_price_cache"
                 " WHERE region_id=:rid AND volume IS NOT NULL"),
            {"rid": region_id},
        ).fetchall()
    return {r[0]: r[1] for r in rows} if rows else None


async def fetch_structure_market(
    conn: Connection,
    structure_id: int,
    token: str,
    our_type_ids: set[int],
    region_id: int | None = None,
    progress_cb=None,
) -> dict[int, tuple[int | None, float | None, int | None]]:
    """
    Fetches all sell orders from a player structure via the authorized endpoint.
    Returns {type_id: (volume, best_sell)} only for type_ids from our cache.
    Requires the esi-markets.structure_markets.v1 scope.
    """
    ensure_price_table(conn)
    aggregated: dict[int, dict] = {}
    page = 1
    got_ok = False   # did we successfully read at least one page?

    async with esi_client() as client:
        while True:
            r = None
            for attempt in range(3):   # retry transient failures — a timed-out
                try:                    # page must NOT silently cache blank prices
                    r = await client.get(
                        f"{ESI_BASE}/markets/structures/{structure_id}/",
                        params={"datasource": "tranquility", "page": page},
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=20,
                    )
                except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError):
                    await asyncio.sleep(0.6 * (attempt + 1)); r = None; continue
                if r.status_code == 403:
                    raise PermissionError("Insufficient permissions to access the structure market (403).")
                if r.status_code == 200:
                    break
                if r.status_code == 420 or r.status_code >= 500:
                    await asyncio.sleep(1.0 * (attempt + 1)); r = None; continue
                r = None; break   # 400/404/… → give up this page

            if r is None or r.status_code != 200:
                # Couldn't read this page after retries. If we never read ANY page,
                # fail loudly so the caller surfaces it and we don't cache blank
                # sell/available over good data (the bug: only 7d vol showed).
                if not got_ok:
                    raise RuntimeError("Could not read the structure market (ESI timeout/error). Try again.")
                break

            got_ok = True
            orders = r.json()
            if not orders:
                break

            for o in orders:
                if o.get("is_buy_order"):
                    continue
                tid = o.get("type_id")
                if tid not in our_type_ids:
                    continue
                if tid not in aggregated:
                    aggregated[tid] = {"volume": 0, "best_sell": None}
                aggregated[tid]["volume"] += o.get("volume_remain", 0)
                price = o.get("price")
                if price and (aggregated[tid]["best_sell"] is None or price < aggregated[tid]["best_sell"]):
                    aggregated[tid]["best_sell"] = price

            total_pages = int(r.headers.get("X-Pages", 1))
            if page >= total_pages:
                break
            page += 1

    # The 7-day "volume" is REGIONAL history (ESI does not publish trade history
    # for player structures). Fetch it for ALL requested types — even those
    # that currently have no offer in the structure, otherwise "sold in the last
    # 7 days" would be missing for them even though they are traded in the region.
    if region_id is None:
        # Try location_name_cache first (populated by location resolver in web layer)
        try:
            row = conn.execute(
                text("SELECT region_id FROM location_name_cache"
                     " WHERE location_id=:lid"),
                {"lid": structure_id},
            ).fetchone()
            if row and row[0]:
                region_id = row[0]
        except Exception:
            pass
    if region_id is None:
        region_id = await get_region_for_structure(structure_id)

    history_map: dict[int, int | None] = {}
    if region_id and our_type_ids:
        tids = list(our_type_ids)
        reuse = _cached_region_volume(conn, region_id)
        if reuse is not None:
            history_map = {tid: reuse.get(tid) for tid in tids}
            if progress_cb:
                try:
                    progress_cb(len(tids), len(tids))
                except Exception:
                    pass
        else:
            total = len(tids)
            done = 0
            _BATCH = 300
            async with esi_client() as client:
                for start in range(0, total, _BATCH):
                    batch = tids[start:start + _BATCH]
                    res = await asyncio.gather(
                        *[_fetch_region_volume(client, region_id, t) for t in batch],
                        return_exceptions=True,
                    )
                    for tid, r in zip(batch, res):
                        history_map[tid] = r if isinstance(r, int) else None
                    done += len(batch)
                    if progress_cb:
                        try:
                            progress_cb(done, total)
                        except Exception:
                            pass

    now = time.time()
    result: dict[int, tuple[int | None, float | None, int | None]] = {}
    rows = []
    for tid in our_type_ids:
        entry = aggregated.get(tid)
        vol = entry["volume"] if entry else 0
        sell = entry["best_sell"] if entry else None
        traded = history_map.get(tid)
        result[tid] = (vol, sell, traded)
        rows.append({"location_id": structure_id, "type_id": tid,
                     "volume": vol, "best_sell": sell,
                     "traded_volume": traded, "cached_at": now})

    if rows:
        conn.execute(
            text("INSERT INTO station_volume_cache"
                 " (location_id, type_id, volume, best_sell, traded_volume,"
                 "  cached_at)"
                 " VALUES (:location_id, :type_id, :volume, :best_sell,"
                 "  :traded_volume, :cached_at)"
                 " ON CONFLICT (location_id, type_id) DO UPDATE SET"
                 " volume=excluded.volume, best_sell=excluded.best_sell,"
                 " traded_volume=excluded.traded_volume,"
                 " cached_at=excluded.cached_at"),
            rows,
        )
    conn.commit()
    return result


async def _fetch_orders_for_type(
    client: httpx.AsyncClient,
    region_id: int,
    location_id: int,
    type_id: int,
) -> tuple[int | None, float | None]:
    """Returns (volume_sum, best_sell) for the given type at a specific station."""
    async with _STATION_SEM:
        try:
            r = await client.get(
                f"{ESI_BASE}/markets/{region_id}/orders/",
                params={"type_id": type_id, "order_type": "sell", "datasource": "tranquility"},
                timeout=15,
            )
            if r.status_code != 200:
                return None, None
        except Exception:
            return None, None

    orders = [o for o in r.json() if o.get("location_id") == location_id]
    if not orders:
        return 0, None
    volume = sum(o.get("volume_remain", 0) for o in orders)
    best_sell = min(o["price"] for o in orders)
    return volume, best_sell


async def fetch_station_volumes(
    conn: Connection,
    location_id: int,
    region_id: int,
    type_ids: list[int],
    progress_cb=None,
) -> dict[int, tuple[int | None, float | None, int | None]]:
    """Fetches and stores volumes+prices+history for all type_ids at the given NPC station."""
    ensure_price_table(conn)
    # Reuse stored history ETags: whatever this region already answered before
    # comes back as a bodyless 304 instead of ~42 KB of JSON per type.
    load_hist_etags(conn, region_id)

    # Phase A (prices): two strategies depending on the number of types.
    #  - few types → per-type calls (light, no 94MB region download); suitable
    #    for plan sell price (1 type).
    #  - many types → bulk regional orders once + station filter (~2 s
    #    instead of ~37 s); crossover ~1000 types (bulk has a fixed ~2 s + 94MB overhead).
    order_map: dict[int, tuple] = {}
    if len(type_ids) >= _BULK_ORDERS_THRESHOLD:
        async with esi_client() as client:
            station_orders = await fetch_station_orders_bulk(client, region_id, location_id)
        for tid in type_ids:
            vs = station_orders.get(tid)
            # type with no sell order at the station → (0, None), consistent with per-type
            order_map[tid] = vs if vs is not None else (0, None)
    else:
        async with esi_client() as client:
            order_tasks = [_fetch_orders_for_type(client, region_id, location_id, tid) for tid in type_ids]
            order_results = await asyncio.gather(*order_tasks, return_exceptions=True)
        for tid, res in zip(type_ids, order_results):
            order_map[tid] = res if isinstance(res, tuple) else (None, None)

    # 7-day regional volume for ALL types — even those that currently have no
    # order at the station (otherwise "sold in the last 7 days" would be missing for them).
    history_map: dict[int, int | None] = {}
    if type_ids:
        reuse = _cached_region_volume(conn, region_id)
        if reuse is not None:
            # Region already loaded (Jita/Forge or a hub) — reuse its 7-day volume
            # instead of re-fetching ~19k histories. Turns a ~3-minute load into
            # seconds (only the station-specific orders phase remains).
            history_map = {tid: reuse.get(tid) for tid in type_ids}
            if progress_cb:
                try:
                    progress_cb(len(type_ids), len(type_ids))
                except Exception:
                    pass
        else:
            total = len(type_ids)
            done = 0
            _BATCH = 300   # report progress every 300 types (this phase is the slow one)
            async with esi_client() as client:
                for start in range(0, total, _BATCH):
                    batch = type_ids[start:start + _BATCH]
                    res = await asyncio.gather(
                        *[_fetch_region_volume(client, region_id, t) for t in batch],
                        return_exceptions=True,
                    )
                    for tid, r in zip(batch, res):
                        history_map[tid] = r if isinstance(r, int) else None
                    done += len(batch)
                    if progress_cb:
                        try:
                            progress_cb(done, total)
                        except Exception:
                            pass

    now = time.time()
    rows = []
    result_map: dict[int, tuple[int | None, float | None, int | None]] = {}
    for tid in type_ids:
        vol, sell = order_map.get(tid, (None, None))
        traded = history_map.get(tid)
        rows.append({"location_id": location_id, "type_id": tid,
                     "volume": vol, "best_sell": sell,
                     "traded_volume": traded, "cached_at": now})
        result_map[tid] = (vol, sell, traded)

    if rows:
        conn.execute(
            text("INSERT INTO station_volume_cache"
                 " (location_id, type_id, volume, best_sell, traded_volume,"
                 "  cached_at)"
                 " VALUES (:location_id, :type_id, :volume, :best_sell,"
                 "  :traded_volume, :cached_at)"
                 " ON CONFLICT (location_id, type_id) DO UPDATE SET"
                 " volume=excluded.volume, best_sell=excluded.best_sell,"
                 " traded_volume=excluded.traded_volume,"
                 " cached_at=excluded.cached_at"),
            rows,
        )
    conn.commit()
    flush_hist_etags(conn)
    return result_map


def get_cached_station_volumes(
    conn: Connection,
    location_id: int,
) -> dict[int, tuple[int | None, float | None, int | None]] | None:
    """Returns cached data if it is fresh, otherwise None."""
    rows = conn.execute(
        text("SELECT type_id, volume, best_sell, traded_volume, cached_at"
             " FROM station_volume_cache WHERE location_id=:lid"),
        {"lid": location_id}).fetchall()
    if not rows:
        return None
    now = time.time()
    if any((now - (r[4] or 0)) > STATION_VOLUME_TTL for r in rows):
        return None
    # If there are records with volume>0 but all traded_volume are NULL,
    # the cache is incomplete (the region was unknown at save time) — force a refetch.
    has_stock = any(r[1] and r[1] > 0 for r in rows)
    all_traded_null = all(r[3] is None for r in rows)
    if has_stock and all_traded_null:
        return None
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def get_station_volumes_any_age(
    conn: Connection,
    location_id: int,
) -> tuple[dict[int, tuple[int | None, float | None, int | None]], float] | None:
    """Cached station volumes regardless of age — for restoring a previously
    loaded custom station on page load (like the never-expiring Jita cache).
    Returns (data, newest_cached_at) or None if nothing is cached."""
    rows = conn.execute(
        text("SELECT type_id, volume, best_sell, traded_volume, cached_at"
             " FROM station_volume_cache WHERE location_id=:lid"),
        {"lid": location_id}).fetchall()
    if not rows:
        return None
    data = {r[0]: (r[1], r[2], r[3]) for r in rows}
    cached_at = max((r[4] or 0) for r in rows)
    return data, cached_at
