"""Verification of EVE SSO access tokens.

Both login flows used to call ``jwt.decode(token, options={"verify_signature":
False})`` and read the character out of the result. That was defensible while the
token arrived over TLS straight from the token endpoint and was only used to
display one's own character name. It stops being defensible the moment that JWT
decides *who you are and what you can see* — which is what hosting makes it.
Baseline finding 7 (new obligations).

Three things here are worth knowing before changing anything:

**The algorithm list is pinned, not discovered.** CCP's metadata document
advertises ``id_token_signing_alg_values_supported: ["HS256"]``, which is simply
wrong — HS256 is symmetric and could never appear in a JWKS. The live JWKS serves
RS256 and ES256. Trusting the advertised value would be worse than useless: HS256
is the classic algorithm-confusion attack, where an attacker signs a forged token
using the RSA *public* key as the HMAC secret and a naive verifier accepts it. So
we pin ``_ALGORITHMS`` to the asymmetric algorithms the JWKS actually contains,
and HS256 can never be selected regardless of what the token header claims.

**Both issuer spellings are accepted.** The discovery document says
``https://login.eveonline.com``; CCP's tokens have historically carried the bare
host ``login.eveonline.com``. Accepting only one form would reject every valid
token, so both are allowed — and nothing else is.

**Failure is fatal, deliberately.** If the JWKS cannot be fetched, verification
raises and login fails. A signature check that falls back to not checking the
signature provides no security at all, and this is the one place in the codebase
where degrading gracefully would be the wrong instinct.
"""
from __future__ import annotations

import threading

import jwt

from app.auth import sso_metadata
from app.version import USER_AGENT

# Pinned to what the JWKS actually serves. Never HS256 — see the module docstring.
_ALGORITHMS = ("RS256", "ES256")

# CCP has used both spellings. Anything else is rejected.
_ISSUERS = ("https://login.eveonline.com", "login.eveonline.com")

# Every EVE access token names the character in `sub` as "CHARACTER:EVE:<id>".
_SUB_PREFIX = "CHARACTER:EVE:"

_lock = threading.Lock()
_client: jwt.PyJWKClient | None = None
_client_uri: str | None = None


class TokenVerificationError(Exception):
    """The access token could not be verified. Never contains the token itself."""


def _jwk_client() -> jwt.PyJWKClient:
    """The JWKS client, rebuilt if discovery starts pointing somewhere else.

    PyJWKClient caches signing keys and refetches automatically when a token
    arrives with an unknown `kid`, which is what makes CCP's key rotation a
    non-event. `headers` carries our User-Agent so this fetch is identified like
    every other call we make to CCP (baseline finding 7).
    """
    global _client, _client_uri

    uri = sso_metadata.jwks_uri()
    with _lock:
        if _client is None or _client_uri != uri:
            _client = jwt.PyJWKClient(
                uri,
                cache_keys=True,
                headers={"User-Agent": USER_AGENT},
                timeout=10,
            )
            _client_uri = uri
        return _client


def verify_access_token(token: str, client_id: str) -> dict:
    """Verify an EVE SSO access token and return its claims.

    Checks the signature against CCP's JWKS, plus `iss`, `aud` and `exp`.
    `client_id` is required: EVE puts both the client ID and the literal
    "EVE Online" in `aud`, and without it we would be accepting tokens minted
    for somebody else's application.

    Raises TokenVerificationError on any failure.
    """
    if not client_id:
        raise TokenVerificationError("no client_id available to check the audience against")

    try:
        signing_key = _jwk_client().get_signing_key_from_jwt(token)
    except Exception as exc:
        raise TokenVerificationError(f"could not fetch the SSO signing key: {exc!r}") from exc

    try:
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=list(_ALGORITHMS),
            audience=client_id,
            issuer=_ISSUERS,
            options={"require": ["exp", "iss", "sub"]},
        )
    except jwt.PyJWTError as exc:
        # The exception type is the useful part; the token is a live credential
        # and must not reach a log line.
        raise TokenVerificationError(f"{type(exc).__name__}: {exc}") from exc


def character_from_claims(payload: dict) -> tuple[int, str]:
    """Pull (character_id, character_name) out of verified claims.

    Both flows used to do this inline with `int(sub.split(":")[-1])`, which turns
    an unexpected `sub` shape into a ValueError at a call site that reports it as
    "failed to decode the JWT". Parsing it once, strictly, means a malformed or
    non-character subject is named as such.
    """
    sub = payload.get("sub") or ""
    if not sub.startswith(_SUB_PREFIX):
        raise TokenVerificationError(f"unexpected token subject {sub[:32]!r}")
    try:
        character_id = int(sub[len(_SUB_PREFIX):])
    except ValueError as exc:
        raise TokenVerificationError(f"non-numeric character id in subject {sub[:32]!r}") from exc
    return character_id, payload.get("name") or "Unknown"


def reset_cache() -> None:
    """Drop the cached JWKS client. For tests, and after a discovery refresh."""
    global _client, _client_uri
    with _lock:
        _client, _client_uri = None, None
