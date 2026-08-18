"""Step 2, items 6-8: session authentication, CSRF and Host validation.

Baseline findings 1 and 2. The `client` fixture holds a real session, so the rest
of the suite exercises the authenticated path; this file is the other half —
proof that the gate refuses when it should, rather than being a formality
everything happens to pass.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.web import security
from tests.conftest import OWNER_CHARACTER_ID

OTHER_CHARACTER_ID = 900000002


@pytest.fixture
def conn(app_module):
    c = app_module.get_conn()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def scratch_margin(app_module):
    """Undo watchlist rows added by the CSRF form tests.

    The app DB is session-scoped, and "already tracked" is keyed on
    (type_id, ME, TE) — so a row left behind here surfaces as a confusing
    failure over in test_margins.py rather than in this file.
    """
    from app.web import margins_helper

    def _rows():
        conn = app_module.get_conn()
        try:
            margins_helper.ensure_margin_tables(conn)
            return {tuple(r) for r in conn.execute(
                "SELECT type_id, me, te FROM margin_watchlist")}
        finally:
            conn.close()

    before = _rows()
    yield
    added = _rows() - before
    if not added:
        return
    conn = app_module.get_conn()
    try:
        for type_id, me, te in added:
            conn.execute("DELETE FROM margin_watchlist WHERE type_id=? AND me=? AND te=?",
                         (type_id, me, te))
        conn.commit()
    finally:
        conn.close()


# --- Finding 1: no authentication of any kind --------------------------------

def test_api_without_a_session_is_401(anon_client):
    r = anon_client.get("/api/sync-status")
    assert r.status_code == 401


def test_a_page_without_a_session_redirects_to_login(anon_client):
    r = anon_client.get("/margins", headers={"accept": "text/html"},
                        follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/login"


def test_an_unknown_session_id_is_refused(app_module):
    c = TestClient(app_module.app)
    c.cookies.set(security.SESSION_COOKIE, "not-a-real-session-id")
    assert c.get("/api/sync-status").status_code == 401


def test_the_active_char_cookie_is_not_identity(app_module):
    """It was a display preference being mistaken for auth; it must stay one."""
    c = TestClient(app_module.app)
    c.cookies.set(app_module.ACTIVE_COOKIE, str(OWNER_CHARACTER_ID))
    assert c.get("/api/sync-status").status_code == 401


def test_public_paths_work_without_a_session(anon_client):
    """Only the three the login flow cannot work without."""
    r = anon_client.get("/callback", follow_redirects=False)
    assert r.status_code == 200, "the callback must be reachable without a session"
    assert security.is_public_path("/auth/login")
    assert security.is_public_path("/auth/bootstrap")
    assert not security.is_public_path("/api/sync-status")


def test_a_valid_session_gets_through(client):
    assert client.get("/api/sync-status").status_code == 200


def test_an_expired_session_is_refused_and_deleted(app_module, conn):
    session_id, _ = security.create_session(conn, OWNER_CHARACTER_ID)
    conn.execute(
        "UPDATE app_sessions SET last_seen_at = ? WHERE session_id = ?",
        (time.time() - security.SESSION_MAX_AGE - 1, session_id),
    )
    conn.commit()

    c = TestClient(app_module.app)
    c.cookies.set(security.SESSION_COOKIE, session_id)
    assert c.get("/api/sync-status").status_code == 401
    assert security.load_session(conn, session_id) is None


def test_logout_drops_the_session(app_module, conn):
    session_id, csrf = security.create_session(conn, OWNER_CHARACTER_ID)
    c = TestClient(app_module.app)
    c.cookies.set(security.SESSION_COOKIE, session_id)

    assert c.post("/auth/logout", headers={security.CSRF_HEADER: csrf}).status_code == 200
    assert security.load_session(conn, session_id) is None


# --- Only the owner may hold a session ---------------------------------------

def test_a_second_character_cannot_sign_in(conn):
    """Adding a character stores its tokens; it must not also grant a login."""
    assert security.get_owner_id(conn) == OWNER_CHARACTER_ID
    assert security.may_sign_in(conn, OWNER_CHARACTER_ID) is True
    assert security.may_sign_in(conn, OTHER_CHARACTER_ID) is False


def test_an_env_pin_overrides_the_stored_claim(conn, monkeypatch):
    monkeypatch.setenv("EVE_OWNER_CHARACTER_ID", str(OTHER_CHARACTER_ID))
    assert security.get_owner_id(conn) == OTHER_CHARACTER_ID
    assert security.may_sign_in(conn, OWNER_CHARACTER_ID) is False


def test_a_nonsense_env_pin_falls_back_to_the_claim(conn, monkeypatch):
    monkeypatch.setenv("EVE_OWNER_CHARACTER_ID", "not-a-number")
    assert security.get_owner_id(conn) == OWNER_CHARACTER_ID


# --- Finding 2: CSRF ---------------------------------------------------------

def test_a_post_without_a_csrf_token_is_403(app_module, conn):
    session_id, _ = security.create_session(conn, OWNER_CHARACTER_ID)
    c = TestClient(app_module.app)
    c.cookies.set(security.SESSION_COOKIE, session_id)

    r = c.post("/api/sync/start")
    assert r.status_code == 403


def test_a_post_with_the_wrong_csrf_token_is_403(app_module, conn):
    session_id, _ = security.create_session(conn, OWNER_CHARACTER_ID)
    c = TestClient(app_module.app)
    c.cookies.set(security.SESSION_COOKIE, session_id)

    r = c.post("/api/sync/start", headers={security.CSRF_HEADER: "wrong"})
    assert r.status_code == 403


def test_another_sessions_csrf_token_does_not_work(app_module, conn):
    """The token is bound to the session, not merely to a valid-looking string."""
    session_a, _ = security.create_session(conn, OWNER_CHARACTER_ID)
    _, csrf_b = security.create_session(conn, OWNER_CHARACTER_ID)

    c = TestClient(app_module.app)
    c.cookies.set(security.SESSION_COOKIE, session_a)
    assert c.post("/api/sync/start", headers={security.CSRF_HEADER: csrf_b}).status_code == 403


def test_a_post_with_the_right_csrf_header_passes(client):
    assert client.post("/api/sync/start").status_code == 200


def test_a_form_post_carries_the_token_in_a_field(app_module, conn, scratch_margin):
    """The four server-rendered <form method=post> cannot set a header."""
    session_id, csrf = security.create_session(conn, OWNER_CHARACTER_ID)
    c = TestClient(app_module.app)
    c.cookies.set(security.SESSION_COOKIE, session_id)

    r = c.post("/margins/add",
               data={"product": "Crane", "me": "3", "te": "6", security.CSRF_FIELD: csrf},
               follow_redirects=False)
    assert r.status_code == 303


def test_a_form_post_without_the_field_is_403(app_module, conn):
    session_id, _ = security.create_session(conn, OWNER_CHARACTER_ID)
    c = TestClient(app_module.app)
    c.cookies.set(security.SESSION_COOKIE, session_id)

    r = c.post("/margins/add", data={"product": "Crane", "me": "5", "te": "20"},
               follow_redirects=False)
    assert r.status_code == 403


def test_the_form_body_survives_the_csrf_check(client, scratch_margin):
    """Reading the body in middleware once broke every form POST with a 422.

    The route must still see its fields, so this asserts the request was parsed
    rather than merely not rejected.
    """
    r = client.post("/margins/add",
                    data={"product": "Crane", "me": "7", "te": "14"},
                    follow_redirects=False)
    assert r.status_code == 303, "the route did not parse its form body"


def test_a_get_needs_no_csrf_token(app_module, conn):
    session_id, _ = security.create_session(conn, OWNER_CHARACTER_ID)
    c = TestClient(app_module.app)
    c.cookies.set(security.SESSION_COOKIE, session_id)
    assert c.get("/api/sync-status").status_code == 200


def test_only_state_changing_methods_are_checked():
    assert security._UNSAFE_METHODS == {"POST", "PUT", "PATCH", "DELETE"}
    for safe in ("GET", "HEAD", "OPTIONS", "TRACE"):
        assert safe not in security._UNSAFE_METHODS


# --- Finding 2: Host validation (DNS rebinding) ------------------------------

def test_an_unexpected_host_is_rejected(client):
    """What actually stops DNS rebinding: the attacker's name is not ours.

    Binding loopback keeps other machines out but does nothing about the browser
    the user is already running, which a hostile page can aim at us via a name
    that resolves to 127.0.0.1.
    """
    r = client.get("/api/sync-status", headers={"host": "evil.example.com"})
    assert r.status_code == 400


def test_the_configured_host_is_accepted(client):
    assert client.get("/api/sync-status", headers={"host": "testserver"}).status_code == 200


def test_host_check_ignores_the_port():
    assert security.host_is_allowed("localhost:8000")
    assert security.host_is_allowed("127.0.0.1:8000")
    assert not security.host_is_allowed("evil.test:8000")


def test_host_check_handles_ipv6_literals(monkeypatch):
    monkeypatch.delenv("EVE_ALLOWED_HOSTS", raising=False)  # fall back to the loopback defaults
    assert security.host_is_allowed("[::1]:8000")
    assert security.host_is_allowed("[::1]")
    assert security.host_is_allowed("localhost:8000")


def test_a_missing_host_header_is_rejected():
    assert not security.host_is_allowed(None)
    assert not security.host_is_allowed("")


def test_allowed_hosts_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("EVE_ALLOWED_HOSTS", "industry.example.com, Other.Example.COM")
    assert security.host_is_allowed("industry.example.com:443")
    assert security.host_is_allowed("other.example.com")
    assert not security.host_is_allowed("localhost")


def test_cookie_secure_is_off_locally_and_on_when_configured(monkeypatch):
    monkeypatch.delenv("EVE_COOKIE_SECURE", raising=False)
    assert security.cookie_secure() is False
    monkeypatch.setenv("EVE_COOKIE_SECURE", "1")
    assert security.cookie_secure() is True


# --- The login handoff -------------------------------------------------------
#
# There is no ticket any more. SSO used to complete on a background thread with
# no response to attach a cookie to; /callback is a request, so it sets the
# cookie itself.

def test_the_callback_issues_a_session(app_module, monkeypatch):
    from app.auth import esi_oauth

    monkeypatch.setattr(esi_oauth, "complete_login",
                        lambda code, state: (OWNER_CHARACTER_ID, "Test Pilot Alpha"))
    monkeypatch.setattr(app_module, "complete_login",
                        lambda code, state: (OWNER_CHARACTER_ID, "Test Pilot Alpha"))

    c = TestClient(app_module.app)
    r = c.get("/callback?code=x&state=y", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/sync"
    assert r.cookies.get(security.SESSION_COOKIE), "no session cookie was set"


def test_a_non_owner_gets_no_session_from_the_callback(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "complete_login",
                        lambda code, state: (OTHER_CHARACTER_ID, "Test Pilot Beta"))

    c = TestClient(app_module.app)
    r = c.get("/callback?code=x&state=y", follow_redirects=False)
    assert r.status_code == 200          # renders the failure page
    assert security.SESSION_COOKIE not in r.cookies


def test_a_failed_login_sets_no_session(app_module, monkeypatch):
    from app.auth.esi_oauth import LoginError

    def _fail(code, state):
        raise LoginError("This login has expired, or was not started here.")

    monkeypatch.setattr(app_module, "complete_login", _fail)

    c = TestClient(app_module.app)
    r = c.get("/callback?code=x&state=stale", follow_redirects=False)
    assert r.status_code == 200
    assert security.SESSION_COOKIE not in r.cookies
    assert "expired" in r.text


# --- The bootstrap escape hatch ---------------------------------------------

def test_a_bootstrap_token_is_single_use(app_module, conn):
    from app.web.bootstrap import issue_token, redeem_token

    token = issue_token(conn, OWNER_CHARACTER_ID)
    assert redeem_token(conn, token) == OWNER_CHARACTER_ID
    assert redeem_token(conn, token) is None


def test_an_unknown_bootstrap_token_is_refused(conn):
    from app.web.bootstrap import redeem_token

    assert redeem_token(conn, "never-issued") is None
    assert redeem_token(conn, None) is None


def test_a_stale_bootstrap_token_is_refused(app_module, conn, monkeypatch):
    from app.web import bootstrap

    token = bootstrap.issue_token(conn, OWNER_CHARACTER_ID)
    conn.execute("UPDATE app_bootstrap SET created_at = ? WHERE token = ?",
                 (time.time() - bootstrap.TOKEN_TTL - 1, token))
    conn.commit()
    assert bootstrap.redeem_token(conn, token) is None


def test_the_bootstrap_route_signs_you_in(app_module, conn):
    from app.web.bootstrap import issue_token

    token = issue_token(conn, OWNER_CHARACTER_ID)
    c = TestClient(app_module.app)
    r = c.get(f"/auth/bootstrap?token={token}", follow_redirects=False)
    assert r.status_code == 303
    assert r.cookies.get(security.SESSION_COOKIE)


def test_the_bootstrap_route_refuses_a_spent_token(app_module, conn):
    from app.web.bootstrap import issue_token

    token = issue_token(conn, OWNER_CHARACTER_ID)
    c = TestClient(app_module.app)
    assert c.get(f"/auth/bootstrap?token={token}", follow_redirects=False).status_code == 303

    c2 = TestClient(app_module.app)
    r = c2.get(f"/auth/bootstrap?token={token}", follow_redirects=False)
    assert r.status_code == 200
    assert security.SESSION_COOKIE not in r.cookies


# ── first-run client ID entry (v0.9.29) ────────────────────────────────────
# Sessionless by necessity: the client ID is what makes a login possible, so
# demanding a session to set it is circular — that circle is exactly what the
# v0.9.28 callback move turned into a lockout. The route is fenced twice
# instead: loopback Host only, and only while nothing is configured.

@pytest.fixture
def unconfigured(app_module, monkeypatch, tmp_path):
    """No client ID from either source, without disturbing the real config."""
    from app.auth import token_store as ts

    monkeypatch.delenv("EVE_CLIENT_ID", raising=False)
    monkeypatch.setattr(ts, "CONFIG_PATH", str(tmp_path / ".eve_config.json"))
    return ts


def _local(app_module):
    """TestClient whose Host is loopback rather than the default 'testserver'."""
    return TestClient(app_module.app, base_url="http://localhost:8000")


def test_setup_page_is_served_on_loopback_when_unconfigured(app_module, unconfigured):
    r = _local(app_module).get("/setup/client-id", headers={"Accept": "text/html"})
    assert r.status_code == 200
    # The callback URL is the one thing that must be copied exactly, so the page
    # is useless if it does not state it.
    assert "http://localhost:8000/callback" in r.text


def test_setup_page_is_not_served_off_loopback(app_module, unconfigured):
    """An unauthenticated endpoint that writes configuration must not exist on a
    public host. 'testserver' is an allowed Host here but is not loopback, so
    this isolates the loopback fence from the Host check."""
    r = TestClient(app_module.app).get("/setup/client-id", headers={"Accept": "text/html"})
    assert r.status_code == 404
    r = TestClient(app_module.app).post("/setup/client-id", data={"client_id": "a" * 32})
    assert r.status_code == 404


def test_setup_page_closes_once_a_client_id_exists(app_module, unconfigured):
    unconfigured.save_client_id("1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d")
    r = _local(app_module).get("/setup/client-id", headers={"Accept": "text/html"})
    assert r.status_code == 404


def test_saving_a_client_id_persists_it_and_starts_the_login(app_module, unconfigured):
    r = _local(app_module).post(
        "/setup/client-id",
        data={"client_id": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/login"
    assert unconfigured.get_client_id() == "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"


def test_a_cross_site_post_is_rejected(app_module, unconfigured):
    """Public paths return before the gate's CSRF check, so Origin is the only
    thing standing between this form and a page on another site submitting it."""
    r = _local(app_module).post(
        "/setup/client-id",
        data={"client_id": "1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d"},
        headers={"Origin": "http://evil.example"},
    )
    assert r.status_code == 400
    assert unconfigured.get_client_id() is None      # nothing was written


def test_a_malformed_client_id_is_refused_before_it_reaches_ccp(app_module, unconfigured):
    """A pasted URL or secret key otherwise fails much later, at CCP, with a
    message that never mentions this form."""
    r = _local(app_module).post(
        "/setup/client-id",
        data={"client_id": "https://developers.eveonline.com/applications/12345"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/setup/client-id?error=")
    assert unconfigured.get_client_id() is None


def test_login_sends_an_unconfigured_localhost_install_to_setup(app_module, unconfigured):
    r = _local(app_module).get(
        "/auth/login", headers={"Accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/setup/client-id"


# ── adding a character is not a failed login (v0.9.29) ─────────────────────
# "Add Character" and "Log In" are the same link, so /callback cannot read
# intent from the request. complete_login() has already stored the tokens by
# the time the owner check runs, so a non-owner arriving there means the
# character was added and only the session is refused. Whoever already holds a
# session is the owner doing exactly that.

def test_the_owner_can_add_a_second_character(app_module, monkeypatch, client):
    """The alt is stored and the owner is returned to the app, not shown a
    'Login failed. Nothing was changed.' page that is wrong on both counts."""
    monkeypatch.setattr(app_module, "complete_login",
                        lambda code, state: (OTHER_CHARACTER_ID, "Test Pilot Beta"))

    r = client.get("/callback?code=x&state=y", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/sync"
    # The owner's own session must survive: adding an alt does not re-seat who
    # is signed in, and must not hand the alt one either.
    assert security.SESSION_COOKIE not in r.cookies


def test_adding_a_character_does_not_transfer_ownership(app_module, monkeypatch, client, conn):
    monkeypatch.setattr(app_module, "complete_login",
                        lambda code, state: (OTHER_CHARACTER_ID, "Test Pilot Beta"))

    client.get("/callback?code=x&state=y", follow_redirects=False)
    assert security.get_owner_id(conn) == OWNER_CHARACTER_ID


def test_a_stranger_without_a_session_still_gets_the_refusal(app_module, monkeypatch):
    """The heuristic must not become 'anyone completing SSO is welcome'."""
    monkeypatch.setattr(app_module, "complete_login",
                        lambda code, state: (OTHER_CHARACTER_ID, "Test Pilot Beta"))

    c = TestClient(app_module.app)          # no session cookie
    r = c.get("/callback?code=x&state=y", follow_redirects=False)
    assert r.status_code == 200
    assert "is not the owner" in r.text
    assert security.SESSION_COOKIE not in r.cookies


# ── a refused login leaves no token behind (v0.9.29, open question 9) ───────

def test_an_uninvited_character_is_not_left_stored(app_module, monkeypatch, conn):
    """complete_login() stores tokens before the owner check can refuse, so a
    stranger who merely found the URL was having their ESI refresh token written
    into someone else's database. Custody without consent — §13 R4."""
    from app.auth.token_store import delete_character, get_character_row

    stranger_id = 900000777
    delete_character(conn, stranger_id)          # start clean

    def _stores_then_returns(code, state):
        # Stand in for the real complete_login: it saves, then returns.
        conn2 = app_module.get_conn()
        try:
            from app.auth.token_store import save_tokens
            save_tokens(conn2, "access-tok", "refresh-tok", 1200,
                        stranger_id, "Passing Stranger")
        finally:
            conn2.close()
        return stranger_id, "Passing Stranger"

    monkeypatch.setattr(app_module, "complete_login", _stores_then_returns)

    c = TestClient(app_module.app)               # no session
    r = c.get("/callback?code=x&state=y", follow_redirects=False)
    assert r.status_code == 200
    assert "is not the owner" in r.text
    assert get_character_row(conn, stranger_id) is None, \
        "the refused character's refresh token is still stored"


def test_a_known_character_survives_a_refused_reauth(app_module, monkeypatch, conn):
    """The owner's session can expire during the round trip to EVE. That must
    refuse the session without destroying a character already added — the
    difference between 'uninvited' and 'known' is the whole safety margin."""
    from app.auth.token_store import save_tokens, get_character_row

    save_tokens(conn, "access-tok", "refresh-tok", 1200,
                OTHER_CHARACTER_ID, "Test Pilot Beta")

    monkeypatch.setattr(app_module, "complete_login",
                        lambda code, state: (OTHER_CHARACTER_ID, "Test Pilot Beta"))

    c = TestClient(app_module.app)               # session expired -> none sent
    r = c.get("/callback?code=x&state=y", follow_redirects=False)
    assert r.status_code == 200
    assert "is not the owner" in r.text
    assert get_character_row(conn, OTHER_CHARACTER_ID) is not None, \
        "a previously-added character was deleted by a refused re-auth"
