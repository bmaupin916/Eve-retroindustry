"""EVE Online ESI OAuth2 PKCE flow, served by the app itself.

The redirect target is a route on this application — ``GET /callback`` — rather
than a throwaway HTTP server on port 5173. That local-server dance existed
because a desktop app has nowhere else to receive a redirect; a hosted app does,
and keeping it would have meant running a second listener beside the real one for
no reason.

What it removes: the two loopback sockets, the 15-minute watchdog, the cancel
endpoint that shut the listener down, the waiting page, and the status endpoint
the waiting page polled. The browser now goes to EVE and comes straight back to
us, which is simpler and is the only shape that works behind TLS on a domain.

**The redirect URI must match the one registered with CCP exactly**, scheme and
port included — they compare it as a string. It is read from EVE_CALLBACK_URL so
that moving from a laptop to a domain is a configuration change rather than an
edit here. An application has one registered URI, so switching is a cutover:
change the registration at https://developers.eveonline.com/ at the same time.
If that ever locks you out, ``python -m app.web.bootstrap`` mints a session
without going through SSO at all.
"""
import base64
import hashlib
import os
import secrets
import sqlite3
import threading
import time
import urllib.parse

import httpx
from rich.console import Console

from app.auth import sso_metadata
from app.auth.jwt_verify import (
    verify_access_token, character_from_claims, TokenVerificationError,
)
from app.auth.token_store import (
    save_tokens, save_client_id, get_client_id, ensure_characters_table,
)
from app.version import USER_AGENT

console = Console()

DEFAULT_CALLBACK_URL = "http://localhost:8000/callback"


def callback_url() -> str:
    """The redirect URI. Must match the application registration at CCP."""
    return os.environ.get("EVE_CALLBACK_URL", "").strip() or DEFAULT_CALLBACK_URL


def _open_conn() -> sqlite3.Connection:
    """Open a fresh SQLite connection to the app DB."""
    app_dir = os.environ.get("EVE_APP_DIR") or os.path.join(
        os.path.dirname(__file__), "..", ".."
    )
    conn = sqlite3.connect(os.path.join(app_dir, "eve_cache.db"), timeout=30.0)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    return conn


SCOPES = [
    # --- Blueprints and manufacturing ---
    "esi-characters.read_blueprints.v1",       # character blueprints (ME/TE, BPO/BPC)
    "esi-corporations.read_blueprints.v1",     # corporation blueprints
    "esi-industry.read_character_jobs.v1",     # active character industry jobs
    "esi-industry.read_corporation_jobs.v1",   # active corporation industry jobs
    "esi-industry.read_character_mining.v1",   # character mining ledger

    # --- Assets and inventory ---
    "esi-assets.read_assets.v1",               # character assets (materials at stations)
    "esi-assets.read_corporation_assets.v1",   # corporation assets

    # --- Space structures ---
    "esi-universe.read_structures.v1",         # player structure names (citadels)
    "esi-search.search_structures.v1",         # search structures by name

    # --- Market and finance ---
    "esi-wallet.read_character_wallet.v1",     # character ISK balance
    "esi-wallet.read_corporation_wallets.v1",  # corporation ISK balance
    "esi-markets.read_character_orders.v1",    # own market orders
    "esi-markets.read_corporation_orders.v1",  # corporation market orders
    "esi-markets.structure_markets.v1",        # markets in player structures (citadels)
    "esi-contracts.read_character_contracts.v1",   # character contracts
    "esi-contracts.read_corporation_contracts.v1", # corporation contracts

    # --- Skills ---
    "esi-skills.read_skills.v1",               # trained skills (affect manufacturing)
    "esi-skills.read_skillqueue.v1",           # skill queue

    # --- Location ---
    "esi-location.read_location.v1",           # current character location
    "esi-location.read_ship_type.v1",          # current ship

    # --- Planetary interaction (PI materials) ---
    "esi-planets.manage_planets.v1",           # PI colonies and extraction

    # --- Corporation ---
    "esi-corporations.read_facilities.v1",     # corporation industry facilities
    "esi-characters.read_corporation_roles.v1", # corporation roles
]


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------

def _pkce_pair() -> tuple[str, str]:
    verifier  = secrets.token_urlsafe(43)
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ---------------------------------------------------------------------------
# Logins in flight
# ---------------------------------------------------------------------------
#
# A login in flight is a (verifier, state) pair waiting for the browser to come
# back. Keyed by state, so an abandoned login no longer blocks a fresh one: the
# old code held one global lock for up to fifteen minutes, and a user who closed
# the SSO tab had to wait it out or find the cancel button.
#
# Bounded and time-limited, because /auth/login is reachable without a session
# and this is server memory that it allocates.

_PENDING_TTL = 600.0
_PENDING_MAX = 16

_pending: dict[str, tuple[str, float]] = {}
_pending_lock = threading.Lock()


def _remember_pending(state: str, verifier: str) -> None:
    now = time.monotonic()
    with _pending_lock:
        for key in [k for k, (_, born) in _pending.items() if now - born > _PENDING_TTL]:
            del _pending[key]
        while len(_pending) >= _PENDING_MAX:
            del _pending[min(_pending, key=lambda k: _pending[k][1])]
        _pending[state] = (verifier, now)


def _take_pending(state: str | None) -> str | None:
    """Redeem the verifier for this state, once. None if there is no live match.

    Returning None *is* the state check. A callback carrying a state we did not
    issue, or one already consumed, has no verifier to exchange with and cannot
    proceed. CCP's documentation requires that the application verify state
    matches the one it sent, so this is a compliance requirement and not only
    defence in depth.
    """
    if not state:
        return None
    with _pending_lock:
        entry = _pending.pop(state, None)
    if entry is None:
        return None
    verifier, born = entry
    if time.monotonic() - born > _PENDING_TTL:
        return None
    return verifier


def pending_count() -> int:
    with _pending_lock:
        return len(_pending)


def reset_pending() -> None:
    """Drop every login in flight. For tests."""
    with _pending_lock:
        _pending.clear()


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------

class LoginError(Exception):
    """The callback could not be turned into a stored character."""


def begin_login() -> str | None:
    """Return the EVE SSO URL to send the browser to, or None with no client ID."""
    client_id = get_client_id()
    if not client_id:
        return None

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)
    _remember_pending(state, verifier)

    params = {
        "response_type":         "code",
        "redirect_uri":          callback_url(),
        "client_id":             client_id,
        "scope":                 " ".join(SCOPES),
        "state":                 state,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
    }
    return f"{sso_metadata.authorization_endpoint()}?{urllib.parse.urlencode(params)}"


def complete_login(code: str | None, state: str | None) -> tuple[int, str]:
    """Exchange the callback for tokens and store the character.

    Returns (character_id, character_name). Raises LoginError carrying a message
    safe to show a user — never the code, the token or the verifier.
    """
    if not code:
        raise LoginError("EVE did not return an authorization code.")

    verifier = _take_pending(state)
    if verifier is None:
        raise LoginError("This login has expired, or was not started here.")

    client_id = get_client_id()
    if not client_id:
        raise LoginError("No client ID is configured.")

    try:
        r = httpx.post(
            sso_metadata.token_endpoint(),
            data={
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  callback_url(),
                "client_id":     client_id,
                "code_verifier": verifier,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "User-Agent": USER_AGENT},
            timeout=15,
        )
    except Exception as exc:
        print(f"[auth] token exchange failed: request error {exc!r}", flush=True)
        raise LoginError("Could not reach EVE's token endpoint.") from exc

    if r.status_code != 200:
        # A redirect_uri that does not match the registration fails exactly here,
        # and after a move it is far and away the likeliest cause, so name it.
        print(f"[auth] token exchange failed: HTTP {r.status_code} {r.text[:300]}",
              flush=True)
        raise LoginError(
            "EVE rejected the token exchange. If this instance was just moved, check "
            f"that the callback URL registered with CCP is exactly {callback_url()}"
        )

    try:
        data = r.json()
        payload = verify_access_token(data["access_token"], client_id)
        character_id, character_name = character_from_claims(payload)
    except TokenVerificationError as exc:
        print(f"[auth] access token failed verification: {exc}", flush=True)
        raise LoginError("The access token from EVE failed verification.") from exc
    except (KeyError, ValueError) as exc:
        raise LoginError("EVE's response did not contain the expected tokens.") from exc

    # `token_store` is on the portable query layer, so this takes an engine
    # connection rather than the module's own sqlite3 one. `_open_conn` still
    # exists for the rest of this module.
    from app.db.conn import connect as _connect
    with _connect() as conn:
        ensure_characters_table(conn)
        save_tokens(
            conn,
            data["access_token"], data["refresh_token"],
            data.get("expires_in", 1200), character_id, character_name,
        )

    print(f"[auth] login OK: {character_name} (ID {character_id})", flush=True)
    return character_id, character_name


def set_client_id(client_id: str) -> None:
    save_client_id(client_id)
