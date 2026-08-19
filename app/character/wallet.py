"""
Wallet — ISK balance, journal and market transactions for both character and corporation.

ESI endpoints:
  Character:
    GET /characters/{id}/wallet/                 → float (balance)
    GET /characters/{id}/wallet/journal/         → list (paginated, ~2500/page)
    GET /characters/{id}/wallet/transactions/    → list (max 2500, from_id paging)
  Corporation (requires Accountant / Junior Accountant role → otherwise 403):
    GET /corporations/{id}/wallets/              → [{division, balance}]
    GET /corporations/{id}/wallets/{div}/journal/
    GET /corporations/{id}/wallets/{div}/transactions/

Scopes: esi-wallet.read_character_wallet.v1, esi-wallet.read_corporation_wallets.v1
(corporation role: esi-characters? — no, just the wallet scope + in-game role).
"""
from __future__ import annotations

import json
import sqlite3
import time

import httpx

ESI_BASE = "https://esi.evetech.net/latest"

#: Cache-key vocabulary, spelled once. These are part of a primary key, so a
#: typo at one call site writes a row nothing reads — and a miss is
#: indistinguishable from "not synced yet", which is the quietest way to be
#: wrong.
CHARACTER, CORPORATION = "character", "corporation"
JOURNAL, TRANSACTIONS, BALANCES = "journal", "transactions", "balances"

#: A character has no wallet divisions; a corporation has seven. Zero is the
#: character's slot and also where a corporation's whole balance list lives,
#: since ESI returns every division in one response.
NO_DIVISION = 0


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


# ── cache ────────────────────────────────────────────────────────────────────

def load_cached_ledger(conn: sqlite3.Connection, owner_id: int, ledger: str,
                       kind: str = CHARACTER,
                       division: int = NO_DIVISION) -> tuple[list | None, float]:
    """(rows, cached_at), or (None, 0) when this has never been synced.

    `None` and `[]` are different answers and the page renders them
    differently: nobody has looked yet, versus looked and found nothing. A
    wallet that shows an empty journal it never fetched reads as "no activity",
    which is a conclusion rather than a gap.
    """
    row = conn.execute(
        "SELECT data_json, cached_at FROM wallet_ledger_cache"
        " WHERE owner_id=? AND owner_kind=? AND division=? AND ledger=?",
        (owner_id, kind, division, ledger),
    ).fetchone()
    if not row:
        return None, 0.0
    try:
        return json.loads(row[0]), float(row[1] or 0.0)
    except (ValueError, TypeError):
        return None, 0.0


def save_cached_ledger(conn: sqlite3.Connection, owner_id: int, ledger: str,
                       rows: list, kind: str = CHARACTER,
                       division: int = NO_DIVISION) -> None:
    conn.execute(
        "INSERT INTO wallet_ledger_cache"
        " (owner_id, owner_kind, division, ledger, data_json, cached_at)"
        " VALUES (?,?,?,?,?,?)"
        " ON CONFLICT (owner_id, owner_kind, division, ledger) DO UPDATE SET"
        " data_json=excluded.data_json, cached_at=excluded.cached_at",
        (owner_id, kind, division, ledger, json.dumps(rows), time.time()),
    )


def save_cached_balance(conn: sqlite3.Connection, char_id: int, balance: float) -> None:
    """The character's ISK balance, in the table the dashboard already reads.

    Deliberately not the ledger table: `char_wallet_cache` predates it and has
    a second consumer in `app/web/main.py`, which reads it with a five-minute
    TTL and fetches on a miss. Writing it here is what stops that fetch ever
    happening — one number in one place, kept warm by the worker.
    """
    conn.execute(
        "INSERT INTO char_wallet_cache (character_id, balance, cached_at)"
        " VALUES (?,?,?) ON CONFLICT (character_id) DO UPDATE SET"
        " balance=excluded.balance, cached_at=excluded.cached_at",
        (char_id, balance, time.time()),
    )


def load_cached_balance(conn: sqlite3.Connection, char_id: int) -> tuple[float | None, float]:
    row = conn.execute(
        "SELECT balance, cached_at FROM char_wallet_cache WHERE character_id=?",
        (char_id,)).fetchone()
    if not row:
        return None, 0.0
    return row[0], float(row[1] or 0.0)


async def fetch_balance(client: httpx.AsyncClient, char_id: int, token: str,
                        conn: sqlite3.Connection | None = None) -> float | None:
    try:
        r = await client.get(f"{ESI_BASE}/characters/{char_id}/wallet/",
                             headers=_auth(token), timeout=10)
        if r.status_code == 200:
            balance = float(r.json())
            if conn is not None:
                save_cached_balance(conn, char_id, balance)
            return balance
    except Exception:
        pass
    return None


# Pages are only fetched until `limit` entries are in hand, so this bound just
# stops a runaway loop if ESI ever reports a silly x-pages.
_MAX_JOURNAL_PAGES = 12


async def fetch_journal(client: httpx.AsyncClient, char_id: int, token: str,
                        limit: int = 2500,
                        conn: sqlite3.Connection | None = None) -> list[dict] | None:
    """Wallet journal — newest first, up to `limit` entries.

    Keeps pulling pages until it has `limit` entries or ESI runs out, rather than
    assuming a page size: published sources disagree on whether a journal page
    holds 1000 or 2500 rows, and asking for a fixed number of pages would either
    fall short or waste calls depending on which is true. ESI keeps roughly the
    last 30 days either way, so this is bounded in practice.
    """
    out: list[dict] = []
    for page in range(1, _MAX_JOURNAL_PAGES + 1):
        try:
            r = await client.get(
                f"{ESI_BASE}/characters/{char_id}/wallet/journal/",
                params={"page": page}, headers=_auth(token), timeout=15,
            )
        except Exception:
            # A first-page failure is "ESI is unavailable" and must not be
            # written down as "this character has no journal". A later one
            # still returns what arrived — a partial month beats none.
            if page == 1:
                return None
            break
        if r.status_code != 200:
            if page == 1:
                return None
            break
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        total_pages = int(r.headers.get("x-pages", 1))
        if page >= total_pages or len(out) >= limit:
            break
    if conn is not None:
        save_cached_ledger(conn, char_id, JOURNAL, out)
    return out


async def fetch_transactions(client: httpx.AsyncClient, char_id: int, token: str,
                             conn: sqlite3.Connection | None = None
                             ) -> list[dict] | None:
    """The character's market transactions (ESI returns the last ~2500)."""
    try:
        r = await client.get(
            f"{ESI_BASE}/characters/{char_id}/wallet/transactions/",
            headers=_auth(token), timeout=15,
        )
        if r.status_code == 200:
            txns = r.json()
            if conn is not None:
                save_cached_ledger(conn, char_id, TRANSACTIONS, txns)
            return txns
    except Exception:
        pass
    return None


# ── Corporation ─────────────────────────────────────────────────────────────

async def fetch_corp_wallets(client: httpx.AsyncClient, corp_id: int, token: str,
                             conn: sqlite3.Connection | None = None
                             ) -> tuple[list[dict] | None, str | None]:
    """Returns ([{division, balance}], None) or (None, error_message).
    403 = the character lacks the Accountant/Junior Accountant role.
    """
    try:
        r = await client.get(f"{ESI_BASE}/corporations/{corp_id}/wallets/",
                             headers=_auth(token), timeout=12)
        if r.status_code == 200:
            wallets = r.json()
            if conn is not None:
                save_cached_ledger(conn, corp_id, BALANCES, wallets, CORPORATION)
            return wallets, None
        if r.status_code == 403:
            return None, "This character lacks the Accountant / Junior Accountant role required to read the corporation wallet."
        return None, f"ESI returned HTTP {r.status_code}."
    except Exception as exc:
        return None, str(exc)


async def fetch_corp_journal(client: httpx.AsyncClient, corp_id: int, division: int,
                             token: str, limit: int = 2500,
                             conn: sqlite3.Connection | None = None
                             ) -> list[dict] | None:
    """Corp division journal — same paging rules as the character journal."""
    out: list[dict] = []
    for page in range(1, _MAX_JOURNAL_PAGES + 1):
        try:
            r = await client.get(
                f"{ESI_BASE}/corporations/{corp_id}/wallets/{division}/journal/",
                params={"page": page}, headers=_auth(token), timeout=15,
            )
        except Exception:
            if page == 1:
                return None
            break
        if r.status_code != 200:
            if page == 1:
                return None
            break
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if page >= int(r.headers.get("x-pages", 1)) or len(out) >= limit:
            break
    if conn is not None:
        save_cached_ledger(conn, corp_id, JOURNAL, out, CORPORATION, division)
    return out


async def fetch_corp_transactions(client: httpx.AsyncClient, corp_id: int, division: int,
                                  token: str,
                                  conn: sqlite3.Connection | None = None
                                  ) -> list[dict] | None:
    try:
        r = await client.get(
            f"{ESI_BASE}/corporations/{corp_id}/wallets/{division}/transactions/",
            headers=_auth(token), timeout=15,
        )
        if r.status_code == 200:
            txns = r.json()
            if conn is not None:
                save_cached_ledger(conn, corp_id, TRANSACTIONS, txns,
                                   CORPORATION, division)
            return txns
    except Exception:
        pass
    return None


# ── ref_type humanization ─────────────────────────────────────────────────────

_REF_TYPE_LABELS: dict[str, str] = {
    "player_trading": "Player Trading",
    "market_transaction": "Market Transaction",
    "market_escrow": "Market Escrow",
    "transaction_tax": "Transaction Tax",
    "brokers_fee": "Broker's Fee",
    "bounty_prizes": "Bounty Prizes",
    "agent_mission_reward": "Mission Reward",
    "agent_mission_time_bonus_reward": "Mission Time Bonus",
    "corporation_account_withdrawal": "Corp Withdrawal",
    "industry_job_tax": "Industry Job Tax",
    "manufacturing": "Manufacturing",
    "contract_price": "Contract Price",
    "contract_reward": "Contract Reward",
    "contract_collateral": "Contract Collateral",
    "contract_brokers_fee": "Contract Broker's Fee",
    "contract_deposit": "Contract Deposit",
    "insurance": "Insurance",
    "player_donation": "Player Donation",
    "corporate_reward_payout": "Corp Reward Payout",
    "asset_safety_recovery_tax": "Asset Safety Tax",
    "structure_gate_jump": "Structure Gate Jump",
    "reprocessing_tax": "Reprocessing Tax",
    "jump_clone_activation_fee": "Jump Clone Fee",
    "jump_clone_installation_fee": "Jump Clone Install",
    "skill_purchase": "Skill Purchase",
    "war_fee": "War Fee",
    "office_rental_fee": "Office Rental",
    "factory_slot_rental_fee": "Factory Slot Rental",
    "market_provider_tax": "Market Provider Tax",
    "ess_escrow_transfer": "ESS Escrow Transfer",
}


def humanize_ref_type(ref_type: str) -> str:
    if ref_type in _REF_TYPE_LABELS:
        return _REF_TYPE_LABELS[ref_type]
    return ref_type.replace("_", " ").title()
