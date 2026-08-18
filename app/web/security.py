"""Session authentication, CSRF and Host validation.

Closes baseline findings 1, 2 and 3's HTTP half: before this, the app had no
authentication of any kind, the only middleware was the SDE setup gate, and the
`active_char` cookie was a display preference rather than identity.

Scope is deliberately Step 2, not Step 5: **one account**. Identity is an EVE
character, sessions are rows in the app DB, and there is no account table yet.
Multi-tenancy replaces the owner check here with a real account scope.

Everything that differs between running locally and running hosted is read from
the environment, so Step 3 changes deployment settings rather than this code:

    EVE_ALLOWED_HOSTS        comma-separated hostnames the app answers on
                             (default: the loopback names, any port)
    EVE_COOKIE_SECURE=1      mark cookies Secure — set once TLS terminates
    EVE_OWNER_CHARACTER_ID   pin the account owner instead of claim-on-first-login

**Owner resolution.** With no pin set, the first character to complete SSO claims
the instance and is persisted. That is fine on a laptop and is a real, if narrow,
window on a public host — between deploy and first login, whoever finds the URL
claims it. Step 3 should set EVE_OWNER_CHARACTER_ID as part of deploying, and the
log says so loudly when the claim happens.
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import time

# Cookie carrying the session id. Not `active_char`, which stays what it always
# was: which character the UI is currently showing.
SESSION_COOKIE = "eve_session"

# Sliding expiry. Long, because this is one person's own tool and being logged
# out weekly would be pure friction; every use extends it.
SESSION_MAX_AGE = 30 * 24 * 3600

# CSRF token travels as a header (fetch) or a form field (the four HTML forms).
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "_csrf"

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Paths reachable without a session. Deliberately tiny:
#   /auth/login   starts SSO — must work before you have a session
#   /callback     where EVE sends the browser back; it issues the session, so it
#                 cannot require one. The state check in complete_login() is what
#                 keeps it from being an open door.
#   /static       CSS and JS, no data
#   /favicon.ico  requested before anything else
_PUBLIC_PREFIXES = ("/static/",)
_PUBLIC_EXACT = frozenset({
    "/auth/login", "/callback", "/favicon.ico",
    # Redeems a token that can only be minted with filesystem access to the DB.
    "/auth/bootstrap",
})

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "[::1]"})


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def allowed_hosts() -> frozenset[str]:
    """Hostnames this app will answer on, lowercased and without port.

    Host validation is what actually stops DNS rebinding (finding 2): binding
    127.0.0.1 keeps remote machines out but does nothing about the browser the
    user is already running, which an attacker's page can point at us via a name
    that resolves to loopback. Checking the Host header rejects that, because the
    attacker's name is not in this set.
    """
    raw = os.environ.get("EVE_ALLOWED_HOSTS", "")
    if not raw.strip():
        return _LOOPBACK_HOSTS
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def cookie_secure() -> bool:
    return bool(os.environ.get("EVE_COOKIE_SECURE"))


def host_is_allowed(host_header: str | None) -> bool:
    if not host_header:
        return False  # HTTP/1.1 requires Host; absent means we cannot check it
    host = host_header.strip().lower()
    # Strip the port. Bracketed IPv6 literals keep their brackets so they match
    # the "[::1]" spelling in the allowlist.
    if host.startswith("["):
        host = host[: host.index("]") + 1] if "]" in host else host
    elif ":" in host:
        host = host.rsplit(":", 1)[0]
    return host in allowed_hosts()


def is_public_path(path: str) -> bool:
    return path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIXES)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def ensure_sessions_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_sessions (
            session_id   TEXT PRIMARY KEY,
            character_id INTEGER NOT NULL,
            csrf_token   TEXT NOT NULL,
            created_at   REAL NOT NULL,
            last_seen_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_owner (
            id           INTEGER PRIMARY KEY CHECK (id = 1),
            character_id INTEGER NOT NULL,
            claimed_at   REAL NOT NULL
        )
        """
    )


def get_owner_id(conn: sqlite3.Connection) -> int | None:
    """The character that owns this instance, or None if unclaimed.

    An explicit EVE_OWNER_CHARACTER_ID always wins over the stored claim, so a
    deployment can assert ownership regardless of what the DB says.
    """
    pinned = os.environ.get("EVE_OWNER_CHARACTER_ID", "").strip()
    if pinned:
        try:
            return int(pinned)
        except ValueError:
            print(f"[auth] EVE_OWNER_CHARACTER_ID={pinned!r} is not a number; ignoring",
                  flush=True)
    row = conn.execute("SELECT character_id FROM app_owner WHERE id = 1").fetchone()
    return int(row[0]) if row else None


def claim_owner(conn: sqlite3.Connection, character_id: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO app_owner (id, character_id, claimed_at) VALUES (1, ?, ?)",
        (character_id, time.time()),
    )
    conn.commit()
    print(f"[auth] character {character_id} claimed this instance. Set "
          f"EVE_OWNER_CHARACTER_ID={character_id} to pin it.", flush=True)


def may_sign_in(conn: sqlite3.Connection, character_id: int) -> bool:
    """Whether this character is allowed to hold a session.

    Only the owner. Other characters can still be *added* — that is what the
    multi-character UI does, and their tokens are stored — but adding a character
    does not grant a way to log in as one.
    """
    owner = get_owner_id(conn)
    if owner is None:
        claim_owner(conn, character_id)
        return True
    return owner == character_id


def create_session(conn: sqlite3.Connection, character_id: int) -> tuple[str, str]:
    """Mint a session. Returns (session_id, csrf_token)."""
    session_id = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    now = time.time()
    conn.execute(
        "INSERT INTO app_sessions (session_id, character_id, csrf_token, created_at, "
        "last_seen_at) VALUES (?,?,?,?,?)",
        (session_id, character_id, csrf_token, now, now),
    )
    conn.commit()
    return session_id, csrf_token


def load_session(conn: sqlite3.Connection, session_id: str | None) -> dict | None:
    """Return the live session for this id, or None. Expired rows are deleted."""
    if not session_id:
        return None
    row = conn.execute(
        "SELECT session_id, character_id, csrf_token, created_at, last_seen_at "
        "FROM app_sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if not row:
        return None

    now = time.time()
    if now - float(row[4]) > SESSION_MAX_AGE:
        delete_session(conn, session_id)
        return None

    # Slide the expiry, but not on every single request — this runs for every
    # page and asset, and a write per request would be pointless contention on
    # the same DB the sync worker is using.
    if now - float(row[4]) > 3600:
        conn.execute("UPDATE app_sessions SET last_seen_at = ? WHERE session_id = ?",
                     (now, session_id))
        conn.commit()

    return {
        "session_id": row[0],
        "character_id": int(row[1]),
        "csrf_token": row[2],
        "created_at": float(row[3]),
        "last_seen_at": float(row[4]),
    }


def delete_session(conn: sqlite3.Connection, session_id: str | None) -> None:
    if session_id:
        conn.execute("DELETE FROM app_sessions WHERE session_id = ?", (session_id,))
        conn.commit()


def purge_expired(conn: sqlite3.Connection) -> int:
    cutoff = time.time() - SESSION_MAX_AGE
    cur = conn.execute("DELETE FROM app_sessions WHERE last_seen_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount or 0


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------

def csrf_ok(expected: str, header_value: str | None, form_value: str | None) -> bool:
    """Constant-time check of a submitted CSRF token against the session's.

    Accepts either the header (everything the JS does, via the fetch wrapper in
    base.html) or the form field (the four server-rendered <form method=post>).
    """
    for candidate in (header_value, form_value):
        if candidate and secrets.compare_digest(expected, candidate):
            return True
    return False


def set_session_cookie(response, session_id: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        session_id,
        max_age=SESSION_MAX_AGE,
        httponly=True,          # never readable by JS; the CSRF token is the JS-visible half
        samesite="lax",         # SSO returns via a top-level GET, which lax permits
        secure=cookie_secure(),
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
