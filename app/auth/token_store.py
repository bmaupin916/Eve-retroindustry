"""
Multi-character token storage.

Tokens (refresh_token, access_token) are stored in the DB table `characters`.
client_id stays in .eve_config.json (the only application-level value, not per-char).

Public API:
- ensure_characters_table(conn)
- list_characters(conn)                                         → [(id, name), ...]
- has_any_character(conn)                                       → bool
- get_character_row(conn, character_id)                         → dict | None
- save_tokens(conn, access, refresh, expires_in, char_id, name) → upsert
- get_valid_token(conn, character_id)                           → str | None  (auto-refresh)
- delete_character(conn, character_id)
- update_corporation_id(conn, character_id, corp_id)
- get_client_id() / save_client_id(...)                         → JSON file
"""
from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import urlparse

import httpx

from app.version import USER_AGENT
from app.db.location import app_dir
from sqlalchemy import text
from sqlalchemy.engine import Connection
from app.db.conn import NO_SUCH_TABLE, recover_from_missing_table
from app.db.schema import ensure_schema as ensure_db_schema

def config_path() -> str:
    """Where the client ID is stored, resolved per call.

    This was a module constant computed from EVE_APP_DIR at import. Anything
    importing this module before the variable was set kept the wrong answer —
    the same defect that had the test suite writing to a real database. Tests
    had already worked around it by patching the constant *and* setting the
    variable; setting the variable is now enough.
    """
    return os.path.join(app_dir(), ".eve_config.json")

TOKEN_ENDPOINT = "https://login.eveonline.com/v2/oauth/token"
# The reference deployment's EVE application. Usable for local development only
# — see get_client_id().
#
# An EVE application has exactly one registered callback URL, and CCP compares
# redirect_uri against it as an exact string. So the client ID *owns* the
# callback URL. A second deployment falling back to this one would send its users
# to a consent screen naming somebody else's application and then fail the token
# exchange against a callback it does not control. Anyone deploying a copy
# registers their own application and sets EVE_CLIENT_ID.
#
# Retired in v0.9.29 — see get_client_id(). Kept only so the test that pinned the
# old fallback can assert it is no longer handed out.
_DEV_CLIENT_ID = "50cc73daf13d4109a06821c143cb5ca4"

# Per-character refresh locks. EVE rotates the refresh token on every use, so two
# concurrent refreshes with the same token invalidate each other (one gets
# invalid_grant → None). Serializing refreshes per character — whoever wins the
# lock refreshes and stores the new token; everyone else re-reads it — removes
# that race (e.g. "Sync All" running while the dashboard fetches live data).
_refresh_locks: dict[int, threading.Lock] = {}
_refresh_locks_guard = threading.Lock()


def _refresh_lock_for(character_id: int) -> threading.Lock:
    with _refresh_locks_guard:
        lk = _refresh_locks.get(character_id)
        if lk is None:
            lk = threading.Lock()
            _refresh_locks[character_id] = lk
        return lk


# Characters whose refresh token EVE has rejected (invalid_grant) — the token is
# dead and only a re-login fixes it. Tracked in-process so the UI can prompt the
# user; self-corrects (set on a 400 invalid_grant, cleared once a valid token is
# obtained again, e.g. after re-login). Not persisted: a restart just re-detects.
_invalid_refresh: set[int] = set()


def is_refresh_invalid(character_id: int) -> bool:
    """True if this character's refresh token was rejected and needs a re-login."""
    return int(character_id) in _invalid_refresh


def clear_refresh_invalid(character_id: int) -> None:
    _invalid_refresh.discard(int(character_id))


# ---------------------------------------------------------------------------
# JSON config (client_id only)
# ---------------------------------------------------------------------------

def _load_json() -> dict:
    if not os.path.exists(config_path()):
        return {}
    try:
        with open(config_path()) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_json(data: dict) -> None:
    with open(config_path(), "w") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(config_path(), 0o600)
    except Exception:
        pass


def get_client_id() -> str | None:
    """The EVE application this deployment authenticates as.

    Precedence: EVE_CLIENT_ID, then whatever was saved through the settings page,
    then the development fallback — and that last one only when the callback is
    on localhost. A real deployment must bring its own application, because the
    callback URL is a property of the application registration rather than of
    this code. Returning None makes /auth/login say so instead of starting a
    flow that could only ever fail.
    """
    from_env = os.environ.get("EVE_CLIENT_ID", "").strip()
    if from_env:
        return from_env

    saved = _load_json().get("client_id")
    if saved:
        return saved

    # There used to be a localhost fallback to _DEV_CLIENT_ID here. v0.9.28 moved
    # the callback from the throwaway :5173 listener onto this app at
    # :8000/callback, and an EVE application has exactly ONE registered redirect
    # URI — so the bundled application, whose URI is the old one and which nobody
    # deploying a copy can edit, has been unable to complete a login ever since.
    # Keeping it turned a missing client ID into CCP's "The redirect URL does not
    # match any of the configured values for this client", which names neither the
    # cause nor the fix. Returning None instead reaches the message in
    # /auth/login, which quotes the exact callback URL to register.
    return None


def save_client_id(client_id: str) -> None:
    data = _load_json()
    data["client_id"] = client_id
    _save_json(data)


# ---------------------------------------------------------------------------
# DB schema + migration from legacy .eve_config.json
# ---------------------------------------------------------------------------

def ensure_characters_table(conn: Connection) -> None:
    """Schema shim. The table lives in app/db/schema.py; this only guarantees
    it exists, and only on SQLite.

    The lazy create predates migrations and is SQLite-only by construction:
    `app/db/schema.py` memoises by asking `PRAGMA database_list`, which is a
    syntax error on Postgres. There the schema arrives through Alembic.
    """
    if conn.engine.dialect.name != "sqlite":
        return
    ensure_db_schema(conn.connection.driver_connection)


def _migrate_legacy_json(conn: Connection) -> None:
    """One-time migration of single-character .eve_config.json to characters table."""
    data = _load_json()
    char_id = data.get("character_id")
    refresh = data.get("refresh_token")
    if not (char_id and refresh):
        return  # nothing to migrate

    # Migrate only if this char isn't already in DB
    existing = conn.execute(
        text("SELECT 1 FROM characters WHERE character_id=:cid"),
        {"cid": int(char_id)},
    ).fetchone()
    if existing:
        _strip_token_fields(data)
        return

    conn.execute(
        text("""INSERT INTO characters
           (character_id, character_name, refresh_token, access_token,
            token_expires_at, added_at)
           VALUES (:cid, :name, :refresh, :access, :expires, :added)"""),
        {
            "cid": int(char_id),
            "name": data.get("character_name", "Unknown"),
            "refresh": refresh,
            "access": data.get("access_token"),
            "expires": data.get("token_expires_at"),
            "added": time.time(),
        },
    )
    conn.commit()
    _strip_token_fields(data)


def _strip_token_fields(data: dict) -> None:
    """Remove migrated per-char fields from .eve_config.json (keep client_id)."""
    changed = False
    for k in ("access_token", "refresh_token", "token_expires_at",
              "character_id", "character_name"):
        if k in data:
            del data[k]
            changed = True
    if changed:
        _save_json(data)


# ---------------------------------------------------------------------------
# Character CRUD
# ---------------------------------------------------------------------------

def list_characters(conn: Connection) -> list[tuple[int, str]]:
    rows = conn.execute(
        text("SELECT character_id, character_name FROM characters"
             " ORDER BY added_at ASC")).fetchall()
    return [(int(r[0]), r[1]) for r in rows]


def has_any_character(conn: Connection) -> bool:
    row = conn.execute(text("SELECT 1 FROM characters LIMIT 1")).fetchone()
    return row is not None


def get_character_row(conn: Connection, character_id: int) -> dict | None:
    row = conn.execute(
        text("""SELECT character_id, character_name, refresh_token, access_token,
                  token_expires_at, corporation_id, last_sync_at, added_at
           FROM characters WHERE character_id=:cid"""),
        {"cid": int(character_id)},
    ).fetchone()
    if not row:
        return None
    return {
        "character_id":     int(row[0]),
        "character_name":   row[1],
        "refresh_token":    row[2],
        "access_token":     row[3],
        "token_expires_at": row[4],
        "corporation_id":   row[5],
        "last_sync_at":     row[6],
        "added_at":         row[7],
    }


def save_tokens(
    conn: Connection,
    access_token: str,
    refresh_token: str,
    expires_in: int,
    character_id: int,
    character_name: str,
) -> None:
    """Upsert character + tokens."""
    expires_at = time.time() + expires_in - 60
    conn.execute(
        text("""INSERT INTO characters
           (character_id, character_name, refresh_token, access_token,
            token_expires_at, added_at)
           VALUES (:cid, :name, :refresh, :access, :expires, :added)
           ON CONFLICT(character_id) DO UPDATE SET
             character_name   = excluded.character_name,
             refresh_token    = excluded.refresh_token,
             access_token     = excluded.access_token,
             token_expires_at = excluded.token_expires_at"""),
        {
            "cid": int(character_id), "name": character_name,
            "refresh": refresh_token, "access": access_token,
            "expires": expires_at, "added": time.time(),
        },
    )
    conn.commit()


def delete_character(conn: Connection, character_id: int) -> None:
    conn.execute(text("DELETE FROM characters WHERE character_id=:cid"),
                 {"cid": int(character_id)})
    # Cascade-clean per-char cache rows. A cache table can legitimately be
    # absent on an older database, and swallowing that is only safe with the
    # rollback: Postgres aborts the whole transaction on any failed statement
    # and refuses every later one — including the two remaining DELETEs and the
    # commit — until it is rolled back. On SQLite the bare `except` worked and
    # the damage would have appeared here first.
    for tbl, col in (
        ("char_blueprints_cache", "character_id"),
        ("char_assets_cache",     "character_id"),
        ("char_skills_cache",     "character_id"),
    ):
        try:
            conn.execute(text(f"DELETE FROM {tbl} WHERE {col}=:cid"),
                         {"cid": int(character_id)})
        except NO_SUCH_TABLE:
            recover_from_missing_table(conn)
            conn.execute(text("DELETE FROM characters WHERE character_id=:cid"),
                         {"cid": int(character_id)})
    conn.commit()


def update_corporation_id(
    conn: Connection, character_id: int, corp_id: int
) -> None:
    conn.execute(
        text("UPDATE characters SET corporation_id=:corp"
             " WHERE character_id=:cid"),
        {"corp": int(corp_id), "cid": int(character_id)},
    )
    conn.commit()


def update_last_sync(conn: Connection, character_id: int) -> None:
    conn.execute(
        text("UPDATE characters SET last_sync_at=:now WHERE character_id=:cid"),
        {"now": time.time(), "cid": int(character_id)},
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Token retrieval / refresh
# ---------------------------------------------------------------------------

def get_valid_token(conn: Connection, character_id: int) -> str | None:
    """Return a valid access_token for the given char — auto-refresh on expiry.

    Refreshes are serialized per character (see _refresh_locks) so concurrent
    callers — e.g. "Sync All" and the dashboard live fetch — can't invalidate
    each other's rotating refresh token.
    """
    row = get_character_row(conn, character_id)
    if not row:
        return None

    access = row["access_token"]
    expires = row["token_expires_at"] or 0
    if access and time.time() < expires:
        clear_refresh_invalid(character_id)   # a valid token means it's not dead
        return access

    client_id = get_client_id()
    if not client_id:
        return None

    lock = _refresh_lock_for(int(character_id))
    with lock:
        # Re-read under the lock: another caller may have just refreshed and
        # stored a fresh token, in which case we must NOT refresh again (that
        # would use an already-rotated refresh token and fail).
        row = get_character_row(conn, character_id)
        if not row:
            return None
        access = row["access_token"]
        refresh = row["refresh_token"]
        expires = row["token_expires_at"] or 0
        if access and time.time() < expires:
            return access
        if not refresh:
            return None

        try:
            r = httpx.post(
                TOKEN_ENDPOINT,
                data={
                    "grant_type":    "refresh_token",
                    "refresh_token": refresh,
                    "client_id":     client_id,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "User-Agent": USER_AGENT},
                timeout=15,
            )
        except Exception as exc:
            print(f"[token] refresh request failed for {character_id}: {exc!r}", flush=True)
            return None
        if r.status_code != 200:
            print(f"[token] refresh rejected for {character_id}: HTTP {r.status_code} "
                  f"{r.text[:200]}", flush=True)
            # 400 invalid_grant = the refresh token is dead → only a re-login
            # fixes it. Flag it so the UI can prompt. (5xx / timeouts are
            # transient — leave the flag alone so we don't nag on a blip.)
            if r.status_code == 400 and "invalid_grant" in r.text.lower():
                _invalid_refresh.add(int(character_id))
            return None

        resp = r.json()
        new_access = resp["access_token"]
        new_refresh = resp.get("refresh_token", refresh)
        new_expires_at = time.time() + resp.get("expires_in", 1200) - 60

        # Persisting the rotated refresh token is critical: EVE has already
        # invalidated the old one server-side, so if we don't store the new one
        # the character is locked out until re-login. Retry briefly on a lock.
        for _attempt in range(3):
            try:
                conn.execute(
                    text("""UPDATE characters
                       SET access_token=:access, refresh_token=:refresh,
                           token_expires_at=:expires
                       WHERE character_id=:cid"""),
                    {"access": new_access, "refresh": new_refresh,
                     "expires": new_expires_at, "cid": int(character_id)},
                )
                conn.commit()
                break
            except NO_SUCH_TABLE as exc:
                # "database is locked" is SQLite's, and it arrives wrapped in
                # SQLAlchemy's OperationalError now rather than the driver's.
                # The rollback matters on the other backend: without it a
                # retry would hit InFailedSqlTransaction instead of the real
                # error.
                try:
                    conn.rollback()
                except Exception:
                    pass
                if "locked" in str(exc).lower() and _attempt < 2:
                    time.sleep(0.5)
                    continue
                print(f"[token] failed to persist refreshed token for "
                      f"{character_id}: {exc!r}", flush=True)
                break
        clear_refresh_invalid(character_id)   # refresh succeeded → token is alive
        return new_access
