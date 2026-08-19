# Authentication (EVE SSO / OAuth 2.0)

Many ESI endpoints are public and need no token; others require a scope
granted via EVE's SSO. The API Explorer lists each route's required scope(s).

## Core concepts

- **Client ID / Secret**: obtained by registering your app on the EVE
  Developers Portal. Client ID is public; secret must stay server-side only.
- **Scopes**: space-separated permissions the user must explicitly consent to
  (e.g. character location, skill queue, wallet). Your app can only request
  scopes it registered for, and can only use scopes the user actually granted.
- **Access token**: short-lived JWT used to call ESI. Valid only for the
  character + scopes consented to.
- **Refresh token**: long-lived, used to mint new access tokens without
  re-prompting the user. Treat it like a credential — store it securely.
- **State parameter**: random string you generate, sent in the auth request,
  and must verify matches on the callback — CSRF protection. Never skip this
  check.
- **Redirect URI**: must exactly match one pre-registered with your app.
- **Endpoints**: don't hardcode them beyond the metadata URL — fetch actual
  endpoint URLs from the well-known metadata document (safe and recommended
  to cache for a while, they rarely change but aren't guaranteed static):
  `https://login.eveonline.com/.well-known/oauth-authorization-server`
  Known-stable token endpoint: `https://login.eveonline.com/v2/oauth/token`

## Which flow to use

- **Authorization Code flow**: server-side apps that can securely hold a
  client secret.
- **Authorization Code + PKCE**: mobile/desktop/SPA apps that can't protect a
  secret. No client secret in the token exchange; a code_verifier/challenge
  pair replaces it.

## Authorization Code flow

1. Build the auth URL: append URL-encoded query params to
   `authorization_endpoint`:
   - `response_type=code`
   - `client_id=<your_client_id>`
   - `redirect_uri=<your_redirect_uri>` (must match registered value)
   - `scope=<space-separated scopes>`
   - `state=<random string you generate>`
2. Redirect the user there; they log in, pick a character, and consent.
3. SSO redirects back to your `redirect_uri` with `code` and `state`.
   **Verify `state` matches before doing anything else.**
4. POST to `token_endpoint` with Basic auth (`client_id:client_secret`,
   base64) and form body `grant_type=authorization_code&code=<code>`.
5. Response contains `access_token` and `refresh_token`.

```python
import base64, random, string, urllib, requests

client_id = "your_client_id"
client_secret = "your_client_secret"

def redirect_to_sso(scopes, redirect_uri):
    state = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
    })
    return f"https://login.eveonline.com/v2/oauth/authorize?{query}", state

def request_token(authorization_code):
    basic_auth = base64.urlsafe_b64encode(f"{client_id}:{client_secret}".encode()).decode()
    resp = requests.post(
        "https://login.eveonline.com/v2/oauth/token",
        headers={
            "Authorization": f"Basic {basic_auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "authorization_code", "code": authorization_code},
    )
    resp.raise_for_status()
    return resp.json()
```

## Authorization Code + PKCE flow

Same shape, plus a code verifier/challenge and no client secret:

1. Generate a `code_verifier`: 32 random bytes, base64url-encoded.
2. Generate a `code_challenge`: SHA-256 hash of the verifier, base64url-encoded,
   **no padding** (strip trailing `=` per RFC 4648 §5).
3. Auth URL adds `code_challenge=<challenge>` and
   `code_challenge_method=S256` to the params listed above.
4. Token request body: `grant_type=authorization_code&code=<code>
   &client_id=<client_id>&code_verifier=<verifier>` — no Basic auth, no
   secret.

```python
import base64, hashlib, secrets, random, string, urllib, requests

client_id = "your_client_id"  # no secret needed

def generate_code_challenge():
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32))
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier).digest()
    ).decode().rstrip("=")
    return code_verifier, code_challenge

def redirect_to_sso(scopes, redirect_uri, challenge):
    state = "".join(random.choices(string.ascii_letters + string.digits, k=16))
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return f"https://login.eveonline.com/v2/oauth/authorize?{query}", state

def request_token(authorization_code, code_verifier):
    resp = requests.post(
        "https://login.eveonline.com/v2/oauth/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "authorization_code",
            "code": authorization_code,
            "client_id": client_id,
            "code_verifier": code_verifier,
        },
    )
    resp.raise_for_status()
    return resp.json()
```

## Refreshing tokens

When the access token expires, exchange the refresh token for a new access
token (endpoint/shape not shown in the local docs snippet set — use
`grant_type=refresh_token&refresh_token=<token>` against the same token
endpoint, with the same client auth style as the flow you used to obtain it).
Refresh tokens are valid indefinitely until the user revokes access — store
them securely, they're as sensitive as a password.

## Validating the JWT access token

Access tokens are JWTs. Validate three things before trusting one:

1. **Signature**: fetch the JWKS URI from the metadata endpoint, find the key
   matching the token header's `kid`/`alg`, verify the signature.
2. **Issuer** (`iss` claim): must be `https://login.eveonline.com/` — also
   accept the bare `logineveonline.com` form some tokens use.
3. **Audience** (`aud` claim): must contain **both** your `client_id` and the
   literal string `"EVE Online"`.
4. **Expiration** (`exp` claim): reject if expired — most JWT libraries do
   this automatically.

```python
import time, requests
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTError

METADATA_URL = "https://login.eveonline.com/.well-known/oauth-authorization-server"
METADATA_CACHE_TIME = 300
ACCEPTED_ISSUERS = ("logineveonline.com", "https://login.eveonline.com")
EXPECTED_AUDIENCE = "EVE Online"
client_id = "your_client_id"

_jwks_cache, _jwks_ttl = None, 0

def fetch_jwks_metadata():
    global _jwks_cache, _jwks_ttl
    if _jwks_cache is None or _jwks_ttl < time.time():
        metadata = requests.get(METADATA_URL).json()
        _jwks_cache = requests.get(metadata["jwks_uri"]).json()
        _jwks_ttl = time.time() + METADATA_CACHE_TIME
    return _jwks_cache

def validate_jwt_token(token):
    keys = fetch_jwks_metadata()["keys"]
    header = jwt.get_unverified_header(token)
    key = next(k for k in keys if k["kid"] == header["kid"] and k["alg"] == header["alg"])
    return jwt.decode(
        token, key=key, algorithms=header["alg"],
        issuer=ACCEPTED_ISSUERS, audience=EXPECTED_AUDIENCE,
    )

def is_token_valid(token):
    try:
        claims = validate_jwt_token(token)
        return client_id in claims["aud"]
    except (ExpiredSignatureError, JWTError, Exception):
        return False
```

## Token claims

- `sub`: `CHARACTER:EVE:<character-id>`
- `name`: character name
- `scp`: array of granted scopes

## Login button assets

If adding a "Log in with EVE Online" button, use CCP's official assets for
consistency rather than a custom button:
- https://web.ccpgamescdn.com/eveonlineassets/developers/eve-sso-login-white-large.png
- https://web.ccpgamescdn.com/eveonlineassets/developers/eve-sso-login-black-large.png
- https://web.ccpgamescdn.com/eveonlineassets/developers/eve-sso-login-white-small.png
- https://web.ccpgamescdn.com/eveonlineassets/developers/eve-sso-login-black-small.png
