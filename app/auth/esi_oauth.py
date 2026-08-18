"""
EVE Online ESI OAuth2 PKCE flow for native/CLI applications.
Does not require a client_secret — uses PKCE (code_challenge).

Flow:
  1. Generate code_verifier + code_challenge
  2. Open browser → EVE login
  3. Start a local server on :5173 for the callback
  4. Exchange code + verifier for tokens
  5. Save tokens
"""
import os
import secrets
import hashlib
import base64
import socket
import sqlite3
import webbrowser
import threading
import time
import urllib.parse
import json
import httpx

from app.version import USER_AGENT
from http.server import HTTPServer, BaseHTTPRequestHandler
from rich.console import Console

from app.auth.token_store import (
    save_tokens, save_client_id, get_client_id, ensure_characters_table,
)
from app.auth import sso_metadata
from app.auth.jwt_verify import (
    verify_access_token, character_from_claims, TokenVerificationError,
)


def _open_conn() -> sqlite3.Connection:
    """Open a fresh SQLite connection to the app DB (used from OAuth callback thread)."""
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

_login_lock = threading.Lock()

# The callback listener group for the login currently in progress, or None.
# Defined up here because _CallbackHandler needs it to shut down every socket,
# and /auth/cancel needs it to abandon a login. Assigned in _make_callback_server's
# callers (_wait_for_callback and _run_callback).
_active_server = None

console = Console()

# The authorize/token endpoints now come from CCP's metadata document rather than
# being pinned here; see app/auth/sso_metadata.py, which keeps these exact URLs as
# its offline fallback. Baseline finding 9.
CALLBACK_PORT = 5173
CALLBACK_URL  = f"http://localhost:{CALLBACK_PORT}/callback"

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
# PKCE helpers
# ---------------------------------------------------------------------------

def _pkce_pair() -> tuple[str, str]:
    verifier  = secrets.token_urlsafe(43)
    digest    = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# ---------------------------------------------------------------------------
# Local callback server
# ---------------------------------------------------------------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    code: str | None = None
    state: str | None = None

    redirect_to: str = "http://localhost:8000/auth/sync"

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        _CallbackHandler.code  = params.get("code",  [None])[0]
        _CallbackHandler.state = params.get("state", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        # The browser tab stays on this page — no redirect to
        # localhost:8000/auth/sync, which would open our app in an external
        # browser instead of keeping it in the original window.
        # Meanwhile the webview polls /api/auth/status and redirects itself
        # to /auth/sync as soon as the character is saved.
        body = (
            "<!doctype html>"
            "<meta charset='utf-8'>"
            "<title>EVE Retroindustry — login complete</title>"
            "<style>"
            "  body { font-family: system-ui, sans-serif; background:#0d1117; "
            "         color:#c9d1d9; display:flex; align-items:center; "
            "         justify-content:center; height:100vh; margin:0 }"
            "  .card { background:#161b22; border:1px solid #30363d; "
            "          border-radius:8px; padding:2.5rem 3rem; text-align:center; "
            "          max-width:480px }"
            "  h2 { color:#e3b341; margin:0 0 .75rem }"
            "  p  { margin:.4rem 0; line-height:1.5 }"
            "  .small { color:#8b949e; font-size:.875rem }"
            "</style>"
            "<div class='card'>"
            "<h2>Login complete ✓</h2>"
            "<p>You can close this tab and return to the EVE Retroindustry window.</p>"
            "<p class='small'>The app has already received your authorization "
            "and is loading your character data.</p>"
            "<script>"
            "  // try to auto-close the tab (works only if window was opened "
            "  // by script with window.open) — falls back to staying open."
            "  setTimeout(() => { try { window.close(); } catch (e) {} }, 1500);"
            "</script>"
            "</div>"
        )
        self.wfile.write(body.encode())
        # serve_forever() won't stop the thread on its own — trigger shutdown
        # from another thread, otherwise we'd deadlock (shutdown waits for the
        # serve_forever loop to end, which is waiting on us).
        #
        # Shut down the whole group rather than self.server: the callback arrives
        # on one loopback family, and the sibling socket on the other family would
        # otherwise keep listening and hold the login lock until the watchdog.
        target = _active_server or self.server
        threading.Thread(target=target.shutdown, daemon=True).start()

    def log_message(self, *args):
        pass  # suppress HTTP logs


class _LoopbackServer(HTTPServer):
    """One callback socket, bound to a single loopback address.

    EVE SSO redirects the browser to ``http://localhost:5173/callback``, and
    "localhost" resolves to ``::1`` on some machines and ``127.0.0.1`` on others.
    We therefore listen on both, as two sockets — see _CallbackServerGroup.

    This replaces a dual-stack socket bound to ``::`` with IPV6_V6ONLY cleared.
    That trick does catch both families, but only because it binds *every*
    interface: on a VPS it put the callback on the public IP for the whole login
    window, and `docs/deploy-vps.md` warned about port 8000 while never
    mentioning 5173. Binding ``::1`` alone is not a substitute — measured, an
    IPv6 socket bound to ``::1`` refuses IPv4 connections even with V6ONLY off,
    because IPv4-mapped 127.0.0.1 is ``::ffff:127.0.0.1``, a different address.
    Two sockets is the only way to get both loopbacks without also getting the
    world. Baseline finding 3.
    """

    def __init__(self, address_family: int, host: str, port: int, handler):
        self.address_family = address_family
        super().__init__((host, port), handler)

    def server_bind(self):
        # Keep this socket strictly to its own family, so the IPv6 listener can
        # never quietly pick up IPv4 traffic from outside the machine.
        if self.address_family == socket.AF_INET6:
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            except (AttributeError, OSError):
                pass
        super().server_bind()


class _CallbackServerGroup:
    """The loopback listeners for one login, driven as a single unit.

    Presents the slice of the HTTPServer interface the call sites use, so that
    "the callback server" stays one object to start, shut down and close even
    though it is now two sockets. Shutting down is all-or-nothing: the first
    callback to arrive on either socket ends the login for both.
    """

    def __init__(self, servers: list[HTTPServer]):
        self._servers = servers

    @property
    def addresses(self) -> list:
        return [srv.server_address for srv in self._servers]

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        threads = [
            threading.Thread(
                target=srv.serve_forever, kwargs={"poll_interval": poll_interval}, daemon=True
            )
            for srv in self._servers
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def shutdown(self) -> None:
        for srv in self._servers:
            try:
                srv.shutdown()
            except Exception:
                pass

    def server_close(self) -> None:
        for srv in self._servers:
            try:
                srv.server_close()
            except Exception:
                pass


def _is_addr_in_use(exc: OSError) -> bool:
    import errno as _errno
    return exc.errno in (_errno.EADDRINUSE, getattr(_errno, "WSAEADDRINUSE", 10048))


def _make_callback_server() -> _CallbackServerGroup:
    """Bind the callback port on both loopback addresses.

    Succeeds if at least one family binds — a machine with IPv6 disabled is
    normal and must still be able to log in. Raises OSError only if neither
    works, or if the port is already taken, which the caller reports.
    """
    servers: list[HTTPServer] = []
    in_use: OSError | None = None
    failures: list[str] = []

    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            servers.append(_LoopbackServer(family, host, CALLBACK_PORT, _CallbackHandler))
        except OSError as exc:
            if _is_addr_in_use(exc):
                in_use = exc
            failures.append(f"{host}: {exc!r}")

    if not servers:
        for srv in servers:
            srv.server_close()
        if in_use is not None:
            raise in_use
        raise OSError(f"could not bind {CALLBACK_PORT} on any loopback address "
                      f"({'; '.join(failures)})")

    if failures:
        # One family missing is fine as long as the browser uses the other; say
        # so, because it is the first thing to suspect if the callback never lands.
        print(f"[auth] callback bound on {len(servers)} of 2 loopback addresses "
              f"({'; '.join(failures)})", flush=True)

    return _CallbackServerGroup(servers)


def _wait_for_callback(timeout: int = 120) -> tuple[str | None, str | None]:
    """Block for one callback. Returns (code, state) — the caller must check the
    state against the one it sent; see _check_state().

    Uses serve_forever plus a timer rather than handle_request(), because a
    single blocking accept() cannot wait on two sockets at once.
    """
    global _active_server
    _CallbackHandler.code = None
    _CallbackHandler.state = None
    group = _make_callback_server()
    _active_server = group
    timer = threading.Timer(timeout, group.shutdown)
    timer.start()
    try:
        group.serve_forever()
    finally:
        timer.cancel()
        group.server_close()
        _active_server = None
    return _CallbackHandler.code, _CallbackHandler.state


def _check_state(expected: str, received: str | None) -> bool:
    """Constant-time comparison of the OAuth `state` we sent against the one that
    came back.

    CCP's SSO documentation is explicit that "the application must verify that the
    state parameter matches the one it sent", so this is a compliance requirement
    and not only defence in depth. PKCE already blocks the obvious code-injection
    variant — an injected code was issued against the attacker's challenge and
    fails exchange against our verifier — but without this check the handler
    accepts the first callback from anyone and shuts down, which is a trivial
    login-denial. Baseline findings 4 and 8.
    """
    if not received:
        print("[auth] callback REJECTED: no state parameter returned", flush=True)
        return False
    if not secrets.compare_digest(expected, received):
        print("[auth] callback REJECTED: state mismatch — this callback was not for "
              "the login we started", flush=True)
        return False
    return True


# ---------------------------------------------------------------------------
# Main login function
# ---------------------------------------------------------------------------

def login(client_id: str | None = None) -> bool:
    """
    Runs the OAuth2 PKCE flow.
    Returns True on success.
    """
    if client_id:
        save_client_id(client_id)
    else:
        client_id = get_client_id()

    if not client_id:
        console.print("[red]Missing client_id. Run: python login.py --client-id <ID>[/]")
        return False

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    params = {
        "response_type":         "code",
        "redirect_uri":          CALLBACK_URL,
        "client_id":             client_id,
        "scope":                 " ".join(SCOPES),
        "state":                 state,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{sso_metadata.authorization_endpoint()}?{urllib.parse.urlencode(params)}"

    console.print(f"\n[bold]Opening EVE Online login in the browser...[/]")
    console.print(f"[dim]If the browser doesn't open, go manually to:[/]")
    console.print(f"[cyan]{auth_url}[/]\n")
    webbrowser.open(auth_url)

    console.print("[dim]Waiting for callback (max 120s)...[/]")
    code, got_state = _wait_for_callback()

    if not code:
        console.print("[red]Login timed out or failed.[/]")
        return False

    if not _check_state(state, got_state):
        console.print("[red]Login rejected: the callback did not match this login "
                      "attempt.[/]")
        return False

    # Exchange code for tokens
    r = httpx.post(
        sso_metadata.token_endpoint(),
        data={
            "grant_type":    "authorization_code",
            "code":          code,
            "redirect_uri":  CALLBACK_URL,
            "client_id":     client_id,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": USER_AGENT},
        timeout=15,
    )

    if r.status_code != 200:
        console.print(f"[red]Token exchange selhal: {r.status_code} {r.text}[/]")
        return False

    data = r.json()
    access_token  = data["access_token"]
    refresh_token = data["refresh_token"]
    expires_in    = data.get("expires_in", 1200)

    # Verify the JWT against CCP's JWKS, then read the character out of it.
    try:
        payload = verify_access_token(access_token, client_id)
        character_id, character_name = character_from_claims(payload)
    except TokenVerificationError as exc:
        console.print(f"[red]Access token failed verification: {exc}[/]")
        return False

    conn = _open_conn()
    try:
        ensure_characters_table(conn)
        save_tokens(conn, access_token, refresh_token, expires_in, character_id, character_name)
    finally:
        conn.close()
    console.print(f"[bold green]Logged in as: {character_name} (ID: {character_id})[/]")
    return True


_cancelled: bool = False

# One-shot handoff from the callback thread to /auth/sync.
#
# SSO completes on a background thread, which has no HTTP response to attach a
# session cookie to. Recording the character here lets the next /auth/sync
# request — the browser coming back after a successful login — exchange it for a
# session exactly once. Single-use and time-bounded, so a stale ticket cannot be
# redeemed later by someone else.
_TICKET_TTL = 300.0
_login_ticket: tuple[int, float] | None = None
_ticket_lock = threading.Lock()


def _issue_login_ticket(character_id: int) -> None:
    global _login_ticket
    with _ticket_lock:
        _login_ticket = (character_id, time.monotonic())


def consume_login_ticket() -> int | None:
    """Redeem the pending login, if there is a fresh one. Returns character_id."""
    global _login_ticket
    with _ticket_lock:
        ticket, _login_ticket = _login_ticket, None
    if ticket is None:
        return None
    character_id, issued_at = ticket
    if time.monotonic() - issued_at > _TICKET_TTL:
        return None
    return character_id


def cancel_web_login() -> bool:
    """Cancel an in-progress login flow. Return True if there was anything to cancel.

    Shutting down the local callback HTTP server → the thread in `_run_callback`
    ends, the lock is released, and the user can immediately try logging in again.
    """
    global _active_server, _cancelled
    if _active_server is None:
        return False
    _cancelled = True
    try:
        _active_server.shutdown()
    except Exception:
        pass
    return True


def start_web_login() -> str | None:
    """
    Start the OAuth2 PKCE flow for the web UI.
    Return the auth URL to redirect to, or None if client_id is missing.
    The callback server runs in the background — on success it stores the tokens and redirects to the app.
    """
    global _active_server, _cancelled
    if not _login_lock.acquire(blocking=False):
        return None  # login already in progress

    client_id = get_client_id()
    if not client_id:
        _login_lock.release()
        return None

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(16)

    params = {
        "response_type":         "code",
        "redirect_uri":          CALLBACK_URL,
        "client_id":             client_id,
        "scope":                 " ".join(SCOPES),
        "state":                 state,
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
    }
    auth_url = f"{sso_metadata.authorization_endpoint()}?{urllib.parse.urlencode(params)}"

    # Reset cancellation flag for this run.
    _cancelled = False
    _CallbackHandler.code = None
    _CallbackHandler.state = None

    def _run_callback():
        global _active_server
        try:
            # Dual-stack callback server (accepts ::1 and 127.0.0.1) so the SSO
            # redirect reaches us no matter how the browser resolves "localhost".
            try:
                server = _make_callback_server()
            except OSError as exc:
                if _is_addr_in_use(exc):
                    print(f"[auth] callback FAILED: port {CALLBACK_PORT} is already in use "
                          f"— another program is holding it. Close it and try again. ({exc!r})",
                          flush=True)
                else:
                    print(f"[auth] callback FAILED: could not bind port {CALLBACK_PORT}: {exc!r}",
                          flush=True)
                return
            # serve_forever() instead of handle_request() so it can be interrupted via shutdown()
            # from `cancel_web_login()`. The handler sets the code and, after processing it,
            # shuts down the server.
            _active_server = server
            print(f"[auth] callback server listening on {server.addresses}; "
                  "waiting for SSO redirect", flush=True)
            # Watchdog — if the user doesn't come back within 15 min, shut down and release the lock.
            def _watchdog():
                import time
                time.sleep(15 * 60)
                if _active_server is server:
                    print("[auth] callback watchdog: no redirect within 15 min — giving up",
                          flush=True)
                    try:
                        server.shutdown()
                    except Exception:
                        pass
            threading.Thread(target=_watchdog, daemon=True).start()
            try:
                server.serve_forever(poll_interval=0.5)
            finally:
                try:
                    server.server_close()
                except Exception:
                    pass

            if _cancelled:
                print("[auth] login cancelled by user", flush=True)
                return
            if not _CallbackHandler.code:
                print("[auth] callback FAILED: no authorization code received — the browser "
                      "never reached the callback (IPv6/IPv4, firewall, or closed too early)",
                      flush=True)
                return

            # Reject a callback that is not the one we started. `state` is closed
            # over from start_web_login(), so it is per-login rather than shared
            # class state — two overlapping logins cannot validate each other's.
            if not _check_state(state, _CallbackHandler.state):
                return

            code = _CallbackHandler.code
            try:
                r = httpx.post(
                    sso_metadata.token_endpoint(),
                    data={
                        "grant_type":    "authorization_code",
                        "code":          code,
                        "redirect_uri":  CALLBACK_URL,
                        "client_id":     client_id,
                        "code_verifier": verifier,
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "User-Agent": USER_AGENT},
                    timeout=15,
                )
            except Exception as exc:
                print(f"[auth] token exchange FAILED: request error {exc!r} "
                      "(network down, or a proxy/AV doing TLS interception?)", flush=True)
                return
            if r.status_code != 200:
                print(f"[auth] token exchange FAILED: HTTP {r.status_code} {r.text[:300]}",
                      flush=True)
                return
            try:
                data = r.json()
                payload = verify_access_token(data["access_token"], client_id)
                character_id, character_name = character_from_claims(payload)
                conn = _open_conn()
                try:
                    ensure_characters_table(conn)
                    save_tokens(
                        conn,
                        data["access_token"], data["refresh_token"],
                        data.get("expires_in", 1200), character_id, character_name,
                    )
                finally:
                    conn.close()
            except Exception as exc:
                print(f"[auth] callback FAILED: could not store character after token "
                      f"exchange: {exc!r}", flush=True)
                return
            _issue_login_ticket(character_id)
            print(f"[auth] login OK: {character_name} (ID {character_id})", flush=True)
        finally:
            _active_server = None
            _login_lock.release()

    threading.Thread(target=_run_callback, daemon=True).start()
    return auth_url
