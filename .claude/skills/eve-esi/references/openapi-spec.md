# The OpenAPI Spec (`/meta/openapi.json`)

The full, authoritative route catalog is served live by ESI itself — treat it
as ground truth over any prose docs (including this skill) for per-route
details: required scope, rate-limit group, parameters, response shape.

```
GET https://esi.evetech.net/meta/openapi.json
```

## You must pass a compatibility date

Like every other ESI route, this one obeys `X-Compatibility-Date`. Omitting
it silently serves the oldest schema (`info.version: "2020-01-01"`), which is
missing routes added since — confirmed empirically: the dateless response had
182 paths vs. 210 with a current date passed. Don't fetch this endpoint
without the header, or you'll be reading a stale/incomplete map of the API.

## Getting a valid, current date

Don't hand-roll the `now_utc - 11h` arithmetic for this specific call — ask
ESI directly which dates it currently accepts:

```
GET https://esi.evetech.net/meta/compatibility-dates
```

```json
{"compatibility_dates": ["2026-08-04", "2026-07-21", "2026-07-17", "..."]}
```

The list is sorted newest-first, but don't rely on ordering — treat the
strings as ISO dates and take the max (`YYYY-MM-DD` sorts correctly as a
plain string). This is the same value ESI itself will echo back in the
response's `X-Compatibility-Date` header, so it's a good way to sanity-check
you sent what you think you sent.

## Fetching it correctly

```python
import requests

HEADERS = {
    "User-Agent": "MyTool/1.0.0 (foo@example.com; +https://github.com/me/my-tool)",
}

def latest_compatibility_date():
    resp = requests.get("https://esi.evetech.net/meta/compatibility-dates", headers=HEADERS)
    resp.raise_for_status()
    return max(resp.json()["compatibility_dates"])

def fetch_openapi_spec():
    date = latest_compatibility_date()
    resp = requests.get(
        "https://esi.evetech.net/meta/openapi.json",
        headers={**HEADERS, "X-Compatibility-Date": date},
    )
    resp.raise_for_status()
    return resp.json()
```

## Caching it locally

Response is `Cache-Control: public, max-age=600` (10 min) and lives under the
`meta` rate-limit group (`150/15m` at time of writing — always confirm from
the live `X-Ratelimit-Limit` header, don't hardcode it as gospel). It's also
~500-600KB. Don't re-fetch it per-route-lookup in a loop — fetch once, cache
to disk/memory for the session, and only refresh when you bump your app's
compatibility date or periodically (e.g. daily).

## What to look up in it, and how

The spec is standard OpenAPI 3. For any route you're about to call, look up
`paths["<path>"]["<method>"]` and check:

- **`security`**: absent/`null` → public, no token needed. Present, e.g.
  `[{"OAuth2": ["esi-assets.read_assets.v1"]}]` → needs an access token with
  that scope. This is the authoritative way to determine required scopes —
  more reliable than reading the API Explorer by eye.
- **`x-rate-limit`**: e.g. `{"group": "char-asset", "max-tokens": 1800,
  "window-size": "15m"}` — tells you the bucket this route shares with other
  routes in the same group *before* you've made a single request and seen
  the response headers. Absent → route isn't on the new bucket limiter yet
  (still subject to the legacy error limit, see
  [rate-limiting.md](rate-limiting.md)).
- **`parameters`**: look for `page` (X-Pages pagination), or `before`/`after`
  (cursor pagination), or `from_id` (from-id pagination) to determine which
  pagination style a listing route uses — see
  [pagination.md](pagination.md) for the matching client loop. Also check for
  `$ref: '#/components/parameters/IfNoneMatch'` /
  `IfModifiedSince` to confirm conditional-request support.
- **`components.securitySchemes.OAuth2.flows.authorizationCode.scopes`**:
  the full list of every scope ESI knows about, with descriptions — useful
  for building a scope picker or validating a requested scope string exists
  before sending a user through the SSO flow.

## Example: does this route need a token, and what scope?

```python
def route_requirements(spec, path, method="get"):
    op = spec["paths"][path][method]
    security = op.get("security")
    if not security:
        return None  # public route
    return security[0]["OAuth2"]  # list of required scopes

spec = fetch_openapi_spec()
route_requirements(spec, "/characters/{character_id}/assets")
# -> ['esi-assets.read_assets.v1']
route_requirements(spec, "/characters/{character_id}")
# -> None (public)
```
