"""Step 2, items 2-5: SSO endpoint discovery, token verification, state, bind.

Findings 3, 4, 8 and 9 of `docs/design-hosted-v2.md` §4, plus the "JWT signature
verification becomes mandatory" obligation. Nothing here touches the network:
discovery is stubbed and the JWKS is generated locally, so the suite stays
hermetic.
"""
from __future__ import annotations

import json
import time
import urllib.parse

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.auth import esi_oauth, jwt_verify, sso_metadata

CLIENT_ID = "50cc73daf13d4109a06821c143cb5ca4"


# --- Finding 9: endpoints come from the discovery document -------------------

@pytest.fixture(autouse=True)
def clean_caches():
    sso_metadata.reset_cache()
    jwt_verify.reset_cache()
    yield
    sso_metadata.reset_cache()
    jwt_verify.reset_cache()


def test_discovery_supplies_the_endpoints(monkeypatch):
    doc = {
        "issuer": "https://login.example.test",
        "authorization_endpoint": "https://login.example.test/authorize",
        "token_endpoint": "https://login.example.test/token",
        "jwks_uri": "https://login.example.test/jwks",
    }
    monkeypatch.setattr(sso_metadata, "_fetch", lambda: doc)

    assert sso_metadata.authorization_endpoint() == doc["authorization_endpoint"]
    assert sso_metadata.token_endpoint() == doc["token_endpoint"]
    assert sso_metadata.jwks_uri() == doc["jwks_uri"]


def test_discovery_is_cached(monkeypatch):
    calls = []

    def _fetch():
        calls.append(1)
        return {
            "authorization_endpoint": "https://a.test/authorize",
            "token_endpoint": "https://a.test/token",
            "jwks_uri": "https://a.test/jwks",
        }

    monkeypatch.setattr(sso_metadata, "_fetch", _fetch)
    for _ in range(5):
        sso_metadata.token_endpoint()
    assert len(calls) == 1, "the document should be fetched once per TTL, not per call"


def test_discovery_failure_falls_back_to_the_pinned_endpoints(monkeypatch):
    """A CCP hiccup must not make login impossible while the old URLs still work."""
    def _boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(sso_metadata, "_fetch", _boom)

    assert sso_metadata.token_endpoint() == "https://login.eveonline.com/v2/oauth/token"
    assert sso_metadata.authorization_endpoint() == \
        "https://login.eveonline.com/v2/oauth/authorize"
    assert sso_metadata.jwks_uri() == "https://login.eveonline.com/oauth/jwks"


def test_a_half_written_document_is_rejected_not_merged(monkeypatch):
    """Missing fields would otherwise hand None to the token exchange."""
    import httpx

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"issuer": "https://login.eveonline.com"}  # no endpoints

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    sso_metadata.reset_cache()

    # Falls back rather than returning a document with holes in it.
    assert sso_metadata.token_endpoint() == "https://login.eveonline.com/v2/oauth/token"


def test_a_previously_good_document_survives_a_later_failure(monkeypatch):
    good = {
        "authorization_endpoint": "https://a.test/authorize",
        "token_endpoint": "https://a.test/token",
        "jwks_uri": "https://a.test/jwks",
    }
    monkeypatch.setattr(sso_metadata, "_fetch", lambda: good)
    assert sso_metadata.token_endpoint() == "https://a.test/token"

    def _boom():
        raise RuntimeError("network down")

    monkeypatch.setattr(sso_metadata, "_fetch", _boom)
    assert sso_metadata.get_metadata(force=True)["token_endpoint"] == "https://a.test/token"


def test_esi_oauth_no_longer_pins_the_endpoints():
    assert not hasattr(esi_oauth, "AUTH_URL")
    assert not hasattr(esi_oauth, "TOKEN_URL")


# --- JWT verification --------------------------------------------------------

@pytest.fixture(scope="module")
def signing():
    """An RSA keypair plus the JWKS a verifier would fetch for it."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    pub.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
    return {"private": key, "jwks": {"keys": [pub]}}


@pytest.fixture
def verifier(signing, monkeypatch):
    """Point jwt_verify at the local JWKS instead of CCP's."""
    class _FakeJWKClient:
        def __init__(self, *a, **k):
            pass

        def get_signing_key_from_jwt(self, token):
            return jwt.PyJWK(signing["jwks"]["keys"][0], algorithm="RS256")

    monkeypatch.setattr(jwt, "PyJWKClient", _FakeJWKClient)
    jwt_verify.reset_cache()
    yield
    jwt_verify.reset_cache()


def _claims(**over):
    now = int(time.time())
    base = {
        "sub": "CHARACTER:EVE:95465499",
        "name": "Test Pilot",
        "iss": "login.eveonline.com",
        "aud": [CLIENT_ID, "EVE Online"],
        "exp": now + 1200,
        "iat": now,
    }
    base.update(over)
    return base


def test_a_valid_token_verifies(signing, verifier):
    token = jwt.encode(_claims(), signing["private"], algorithm="RS256",
                       headers={"kid": "test-key"})
    payload = jwt_verify.verify_access_token(token, CLIENT_ID)
    assert jwt_verify.character_from_claims(payload) == (95465499, "Test Pilot")


def test_both_issuer_spellings_are_accepted(signing, verifier):
    """CCP's document says https://login.eveonline.com; tokens have used the bare host."""
    for iss in ("login.eveonline.com", "https://login.eveonline.com"):
        token = jwt.encode(_claims(iss=iss), signing["private"], algorithm="RS256",
                           headers={"kid": "test-key"})
        assert jwt_verify.verify_access_token(token, CLIENT_ID)["iss"] == iss


def test_an_hs256_token_signed_with_the_public_key_is_rejected(signing, verifier):
    """Algorithm confusion — the attack that makes a permissive alg list fatal.

    CCP's metadata document actually advertises HS256, so this is not
    hypothetical: an implementation that trusted the advertised algorithm list
    would accept a token an attacker forged by HMAC-ing with the RSA *public*
    key, which is public knowledge.
    """
    import base64
    import hmac
    import hashlib

    from cryptography.hazmat.primitives import serialization

    public_pem = signing["private"].public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )

    # Assembled by hand: PyJWT refuses to *encode* HS256 with a PEM key, but an
    # attacker is under no such constraint, so building it manually is the only
    # way to test what we would actually be sent.
    def _seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=")

    header = _seg({"alg": "HS256", "typ": "JWT", "kid": "test-key"})
    signing_input = header + b"." + _seg(_claims(name="Attacker"))
    sig = base64.urlsafe_b64encode(
        hmac.new(public_pem, signing_input, hashlib.sha256).digest()
    ).rstrip(b"=")
    forged = (signing_input + b"." + sig).decode()

    with pytest.raises(jwt_verify.TokenVerificationError):
        jwt_verify.verify_access_token(forged, CLIENT_ID)


def test_an_unsigned_token_is_rejected(signing, verifier):
    forged = jwt.encode(_claims(name="Attacker"), key="", algorithm="none",
                        headers={"kid": "test-key"})
    with pytest.raises(jwt_verify.TokenVerificationError):
        jwt_verify.verify_access_token(forged, CLIENT_ID)


def test_a_token_signed_by_someone_else_is_rejected(verifier):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(_claims(), other, algorithm="RS256", headers={"kid": "test-key"})
    with pytest.raises(jwt_verify.TokenVerificationError):
        jwt_verify.verify_access_token(token, CLIENT_ID)


def test_an_expired_token_is_rejected(signing, verifier):
    token = jwt.encode(_claims(exp=int(time.time()) - 60), signing["private"],
                       algorithm="RS256", headers={"kid": "test-key"})
    with pytest.raises(jwt_verify.TokenVerificationError):
        jwt_verify.verify_access_token(token, CLIENT_ID)


def test_a_token_for_another_application_is_rejected(signing, verifier):
    token = jwt.encode(_claims(aud=["someone-elses-client-id", "EVE Online"]),
                       signing["private"], algorithm="RS256", headers={"kid": "test-key"})
    with pytest.raises(jwt_verify.TokenVerificationError):
        jwt_verify.verify_access_token(token, CLIENT_ID)


def test_a_foreign_issuer_is_rejected(signing, verifier):
    token = jwt.encode(_claims(iss="https://login.evil.test"), signing["private"],
                       algorithm="RS256", headers={"kid": "test-key"})
    with pytest.raises(jwt_verify.TokenVerificationError):
        jwt_verify.verify_access_token(token, CLIENT_ID)


def test_verification_without_a_client_id_refuses_rather_than_skipping_aud(signing, verifier):
    token = jwt.encode(_claims(), signing["private"], algorithm="RS256",
                       headers={"kid": "test-key"})
    with pytest.raises(jwt_verify.TokenVerificationError):
        jwt_verify.verify_access_token(token, "")


@pytest.mark.parametrize("sub", ["", "CORPORATION:EVE:98000001", "CHARACTER:EVE:notanumber"])
def test_an_unexpected_subject_is_named_as_such(sub):
    with pytest.raises(jwt_verify.TokenVerificationError):
        jwt_verify.character_from_claims({"sub": sub})


# --- Findings 4 and 8: the state parameter is checked ------------------------
#
# The check is no longer a comparison of two strings. A login in flight is stored
# as state -> PKCE verifier, and the callback redeems it: a state we did not
# issue has no verifier behind it, so the exchange cannot even be attempted.
# Same requirement, expressed as a lookup rather than an `if`.

@pytest.fixture(autouse=True)
def clean_pending():
    esi_oauth.reset_pending()
    yield
    esi_oauth.reset_pending()


def test_a_state_we_issued_redeems_once():
    esi_oauth._remember_pending("state-a", "verifier-a")
    assert esi_oauth._take_pending("state-a") == "verifier-a"
    assert esi_oauth._take_pending("state-a") is None, "a state must not be reusable"


def test_a_state_we_never_issued_has_nothing_behind_it():
    assert esi_oauth._take_pending("made-up") is None


def test_a_missing_state_is_rejected():
    assert esi_oauth._take_pending(None) is None
    assert esi_oauth._take_pending("") is None


def test_a_stale_state_is_rejected(monkeypatch):
    esi_oauth._remember_pending("state-b", "verifier-b")
    real_monotonic = time.monotonic
    monkeypatch.setattr(esi_oauth.time, "monotonic",
                        lambda: real_monotonic() + esi_oauth._PENDING_TTL + 1)
    assert esi_oauth._take_pending("state-b") is None


def test_completing_a_login_with_a_foreign_state_never_reaches_the_token_endpoint(monkeypatch):
    """The state check must happen before we talk to CCP, not after."""
    import httpx

    def _boom(*a, **k):
        raise AssertionError("token endpoint contacted despite an unknown state")

    monkeypatch.setattr(httpx, "post", _boom)
    with pytest.raises(esi_oauth.LoginError):
        esi_oauth.complete_login("some-code", "a-state-we-did-not-issue")


def test_concurrent_logins_do_not_invalidate_each_other():
    """The old flow held one global lock, so a second login had to wait it out."""
    esi_oauth._remember_pending("s1", "v1")
    esi_oauth._remember_pending("s2", "v2")
    assert esi_oauth._take_pending("s2") == "v2"
    assert esi_oauth._take_pending("s1") == "v1"


def test_pending_logins_are_bounded():
    """/auth/login is reachable without a session, so this is attacker-allocated."""
    for i in range(esi_oauth._PENDING_MAX * 3):
        esi_oauth._remember_pending(f"state-{i}", f"verifier-{i}")
    assert esi_oauth.pending_count() <= esi_oauth._PENDING_MAX


# --- Finding 3: the callback is a route, not a second listener ---------------

def test_no_local_callback_server_remains():
    """The loopback sockets are gone; EVE redirects to /callback on this app.

    Finding 3 was that the callback bound every interface. The fix that survived
    is structural: there is no separate listener left to bind anything.
    """
    for gone in ("_make_callback_server", "_CallbackHandler", "_CallbackServerGroup",
                 "_LoopbackServer", "_wait_for_callback", "cancel_web_login"):
        assert not hasattr(esi_oauth, gone), f"{gone} should have been removed"


def test_the_callback_url_is_configuration(monkeypatch):
    monkeypatch.delenv("EVE_CALLBACK_URL", raising=False)
    assert esi_oauth.callback_url() == esi_oauth.DEFAULT_CALLBACK_URL
    monkeypatch.setenv("EVE_CALLBACK_URL", "https://industry.example.com/callback")
    assert esi_oauth.callback_url() == "https://industry.example.com/callback"


def test_the_auth_url_carries_the_configured_callback(monkeypatch):
    monkeypatch.setenv("EVE_CALLBACK_URL", "https://industry.example.com/callback")
    # A non-localhost callback now requires the deployment to name its own EVE
    # application, so supply one.
    monkeypatch.setenv("EVE_CLIENT_ID", "a-deployments-own-client-id")
    monkeypatch.setattr(sso_metadata, "_fetch", lambda: {
        "authorization_endpoint": "https://login.test/authorize",
        "token_endpoint": "https://login.test/token",
        "jwks_uri": "https://login.test/jwks",
    })
    sso_metadata.reset_cache()

    url = esi_oauth.begin_login()
    assert url is not None
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["redirect_uri"] == ["https://industry.example.com/callback"]
    assert q["code_challenge_method"] == ["S256"]
    assert q["state"] and len(q["state"][0]) >= 20


# --- Deployability: the client ID owns the callback, so it is per-deployment --

def test_a_real_deployment_must_bring_its_own_application(monkeypatch, tmp_path):
    """The shipped client ID must not be usable by a second deployment.

    An EVE application has one registered callback URL, so falling back to
    somebody else's client ID means sending users to a consent screen naming
    their application and then failing the exchange against a callback this
    deployment does not control. Better to refuse up front.
    """
    from app.auth import token_store

    monkeypatch.setenv("EVE_APP_DIR", str(tmp_path))
    monkeypatch.delenv("EVE_CLIENT_ID", raising=False)
    monkeypatch.setattr(token_store, "CONFIG_PATH", str(tmp_path / ".eve_config.json"))

    monkeypatch.setenv("EVE_CALLBACK_URL", "https://industry.example.com/callback")
    assert token_store.get_client_id() is None

    monkeypatch.setenv("EVE_CLIENT_ID", "the-deployers-own-id")
    assert token_store.get_client_id() == "the-deployers-own-id"


def test_local_development_still_works_without_configuration(monkeypatch, tmp_path):
    from app.auth import token_store

    monkeypatch.setenv("EVE_APP_DIR", str(tmp_path))
    monkeypatch.delenv("EVE_CLIENT_ID", raising=False)
    monkeypatch.delenv("EVE_CALLBACK_URL", raising=False)
    monkeypatch.setattr(token_store, "CONFIG_PATH", str(tmp_path / ".eve_config.json"))

    assert token_store.get_client_id() == token_store._DEV_CLIENT_ID


def test_an_unconfigured_deployment_cannot_start_a_login(monkeypatch, tmp_path):
    from app.auth import token_store

    monkeypatch.setenv("EVE_APP_DIR", str(tmp_path))
    monkeypatch.delenv("EVE_CLIENT_ID", raising=False)
    monkeypatch.setenv("EVE_CALLBACK_URL", "https://industry.example.com/callback")
    monkeypatch.setattr(token_store, "CONFIG_PATH", str(tmp_path / ".eve_config.json"))

    assert esi_oauth.begin_login() is None


def test_nothing_in_the_app_hardcodes_a_deployment_domain():
    """Every deployment-specific value must be configuration, not a literal."""
    import pathlib
    import re

    allowed = re.compile(
        r"eveonline\.com|evetech\.net|fuzzwork\.co\.uk|github\.com|ko-fi\.com|"
        r"localhost|127\.0\.0\.1|example\.(com|test)|w3\.org|"
        r"schemas\.|json-schema\.org"
    )
    offenders = []
    for path in pathlib.Path("app").rglob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for url in re.findall(r"https?://[A-Za-z0-9.-]+", line):
                if not allowed.search(url):
                    offenders.append(f"{path}:{i}: {url}")
    assert not offenders, "hardcoded host(s): " + "; ".join(offenders)
