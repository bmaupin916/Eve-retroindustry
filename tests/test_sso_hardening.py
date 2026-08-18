"""Step 2, items 2-5: SSO endpoint discovery, token verification, state, bind.

Findings 3, 4, 8 and 9 of `docs/design-hosted-v2.md` §4, plus the "JWT signature
verification becomes mandatory" obligation. Nothing here touches the network:
discovery is stubbed and the JWKS is generated locally, so the suite stays
hermetic.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.request

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

def test_matching_state_passes():
    assert esi_oauth._check_state("abc123", "abc123") is True


def test_mismatched_state_is_rejected():
    assert esi_oauth._check_state("abc123", "different") is False


def test_missing_state_is_rejected():
    assert esi_oauth._check_state("abc123", None) is False
    assert esi_oauth._check_state("abc123", "") is False


# --- Finding 3: the callback listens on loopback only ------------------------

@pytest.fixture
def callback_port(monkeypatch):
    """Bind the callback group on a free port instead of the real 5173."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setattr(esi_oauth, "CALLBACK_PORT", port)
    return port


def test_callback_binds_loopback_only(callback_port):
    group = esi_oauth._make_callback_server()
    try:
        hosts = {addr[0] for addr in group.addresses}
        assert hosts, "no callback socket bound at all"
        assert hosts <= {"127.0.0.1", "::1"}, f"callback is listening on {hosts}"
        # The regression this guards: ("::", port) with IPV6_V6ONLY cleared put
        # the callback on every interface, public IP included.
        assert "::" not in hosts and "0.0.0.0" not in hosts
    finally:
        group.server_close()


def test_callback_is_reachable_over_both_loopback_families(callback_port):
    """The reason two sockets exist: "localhost" resolves to either family.

    A single socket bound to ``::1`` refuses IPv4 even with IPV6_V6ONLY off, so
    this is the test that would have caught that mistake.
    """
    families = {addr[0] for addr in esi_oauth._make_callback_server().addresses}
    if families != {"127.0.0.1", "::1"}:
        pytest.skip(f"only {families} available on this machine")

    for host in ("127.0.0.1", "[::1]"):
        esi_oauth._CallbackHandler.code = None
        esi_oauth._CallbackHandler.state = None
        group = esi_oauth._make_callback_server()
        esi_oauth._active_server = group
        result: list = []

        def _serve():
            try:
                group.serve_forever(poll_interval=0.05)
            finally:
                result.append((esi_oauth._CallbackHandler.code,
                               esi_oauth._CallbackHandler.state))

        t = threading.Thread(target=_serve, daemon=True)
        t.start()
        time.sleep(0.2)
        try:
            urllib.request.urlopen(
                f"http://{host}:{callback_port}/callback?code=CODE-{host}&state=STATE",
                timeout=5,
            ).read()
            t.join(timeout=5)
            assert result and result[0] == (f"CODE-{host}", "STATE"), \
                f"callback over {host} did not reach the handler"
        finally:
            group.shutdown()
            group.server_close()
            esi_oauth._active_server = None


def test_one_callback_shuts_down_every_listener(callback_port):
    """The sibling socket must not keep holding the login open."""
    families = {addr[0] for addr in esi_oauth._make_callback_server().addresses}
    if families != {"127.0.0.1", "::1"}:
        pytest.skip(f"only {families} available on this machine")

    esi_oauth._CallbackHandler.code = None
    group = esi_oauth._make_callback_server()
    esi_oauth._active_server = group
    t = threading.Thread(target=lambda: group.serve_forever(poll_interval=0.05), daemon=True)
    t.start()
    time.sleep(0.2)
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{callback_port}/callback?code=C&state=S", timeout=5
        ).read()
        t.join(timeout=5)
        assert not t.is_alive(), "a listener stayed up after the callback arrived"
    finally:
        group.shutdown()
        group.server_close()
        esi_oauth._active_server = None
