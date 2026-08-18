"""Step 2 security baseline — the items that are checkable without a live server.

Each test names the finding it pins from `docs/design-hosted-v2.md` §4, so a
regression points straight back at the reason the rule exists rather than at a
bare assertion.
"""
from __future__ import annotations

import os
import stat

import pytest

from app.version import APP_VERSION, USER_AGENT, user_agent


# --- Finding 7: identify ourselves to CCP on every request -------------------

def test_user_agent_carries_version_and_contact():
    """CCP's best practices want the app, its version and a way to reach a human."""
    assert APP_VERSION in USER_AGENT
    assert "EVE-Retroindustry" in USER_AGENT
    assert "@" in USER_AGENT                      # contact address
    assert "+https://" in USER_AGENT              # project URL, RFC-style '+' prefix


def test_user_agent_component_tag_keeps_version_and_contact():
    tagged = user_agent("import_sde")
    assert "import_sde" in tagged
    assert APP_VERSION in tagged
    assert "@" in tagged


def test_esi_client_sends_user_agent():
    """Every call through esi_client() — the path all ESI traffic takes."""
    from app.esi.client import esi_client

    # Never awaited, so no connection is opened and nothing needs closing.
    client = esi_client()
    assert client.headers["User-Agent"] == USER_AGENT
    # The compatibility-date pin must survive the addition.
    assert client.headers["X-Compatibility-Date"]


def test_esi_client_caller_can_still_override_headers():
    from app.esi.client import esi_client

    client = esi_client(headers={"X-Test": "1"})
    assert client.headers["X-Test"] == "1"
    assert client.headers["User-Agent"] == USER_AGENT


def test_sso_and_sde_calls_are_identified_too():
    """The three httpx.post call sites that bypass esi_client(), plus the SDE feed.

    These hit login.eveonline.com and developers.eveonline.com — CCP services
    that the shared client never sees, so they are easy to leave unidentified.
    """
    import app.auth.esi_oauth as oauth
    import app.auth.token_store as token_store
    import app.sde.feed as feed

    assert oauth.USER_AGENT == USER_AGENT
    assert token_store.USER_AGENT == USER_AGENT
    assert APP_VERSION in feed.USER_AGENT and "import_sde" in feed.USER_AGENT


def test_version_has_exactly_one_definition():
    """main.py used to carry its own copy; a second one silently goes stale."""
    import app.web.main as web_main

    assert web_main.APP_VERSION is APP_VERSION


# --- Finding 6: no tracebacks in the response body ---------------------------

def test_error_response_hides_traceback_by_default(app_module, client):
    """A 500 must not hand the caller filesystem paths, SQL or local variables."""
    import app.web.main as web_main

    assert web_main._DEBUG_ERRORS is False, "EVE_DEBUG_ERRORS must default off"

    @web_main.app.get("/__boom_test")
    async def _boom():
        raise RuntimeError("secret at C:/Users/someone/eve_cache.db")

    # raise_server_exceptions=False so the transport returns the handler's
    # response instead of re-raising — Starlette's ServerErrorMiddleware always
    # re-raises after building it, which is what a real ASGI server swallows.
    from fastapi.testclient import TestClient

    try:
        local = TestClient(web_main.app, raise_server_exceptions=False)
        local.cookies = client.cookies          # the gate runs before the route
        resp = local.get("/__boom_test")
        assert resp.status_code == 500
        body = resp.text
        assert body.strip() == "Internal Server Error"
        assert "Traceback" not in body
        assert "RuntimeError" not in body
        assert "eve_cache.db" not in body
    finally:
        web_main.app.router.routes = [
            r for r in web_main.app.router.routes
            if getattr(r, "path", None) != "/__boom_test"
        ]


# --- Finding 5: the file holding refresh tokens is owner-only ----------------

@pytest.mark.skipif(os.name == "nt", reason="Windows does not enforce POSIX modes")
def test_db_file_is_owner_only(tmp_path, monkeypatch):
    from app.db import database

    db = tmp_path / "eve_cache.db"
    db.write_bytes(b"")
    db.chmod(0o644)
    monkeypatch.setattr(database, "DB_PATH", str(db))

    database.harden_db_permissions()

    mode = stat.S_IMODE(os.stat(db).st_mode)
    assert mode == 0o600, f"group/other can read refresh tokens (mode {mode:o})"


def test_harden_db_permissions_survives_a_missing_file(tmp_path, monkeypatch):
    """Called on import, before the DB necessarily exists — must never raise."""
    from app.db import database

    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "nope.db"))
    database.harden_db_permissions()
