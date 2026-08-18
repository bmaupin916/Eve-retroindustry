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
