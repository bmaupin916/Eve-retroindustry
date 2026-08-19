---
name: eve-esi
description: Use when building, reviewing, or debugging code that calls EVE Online's ESI API (esi.evetech.net) — covers SSO/OAuth2 authentication, User-Agent requirements, rate limiting, ETag caching, and pagination. Triggers on "ESI", "EVE Swagger Interface", "EVE Online API", "esi.evetech.net", "EVE SSO", or third-party EVE Online tooling (killboards, market tools, corp/alliance auth, fleet tools).
---

# EVE Online ESI Integration

ESI (EVE Swagger Interface, `esi.evetech.net`) is CCP's official REST API for EVE
Online. It is a **shared resource** — non-compliant clients get banned, sometimes
across all of a developer's applications. Every request this skill helps write
should follow the rules below; they are not optional style points.

Source of truth for everything here: CCP's official docs at
https://developers.eveonline.com/docs/services/esi/overview/ (and the `sso`
section alongside it), mirrored in the `esi-docs` repository at
https://github.com/esi/esi-docs. There is **no local copy in this project** —
an earlier version of this file pointed at one, which was another machine's.

For per-route facts — required scope, rate-limit group, pagination style — the
live spec beats prose, including this skill's: see
[openapi-spec.md](references/openapi-spec.md).

## Non-negotiables for every request

1. **Send a User-Agent.** Every single request needs one. See
   [references/user-agent.md](references/user-agent.md) for the header rules and
   format — don't skip this even for quick prototypes, since ESI uses it to
   identify and contact you if something's wrong with your client.
2. **Send `X-Compatibility-Date`.** ISO `YYYY-MM-DD`. Omitting it silently pins you
   to the oldest available API behavior. Use today's UTC date, adjusted: the API
   flips at 11:00 UTC, so compute it as `now_utc - 11h`, not a naive `today()`.
3. **Never poll faster than `expires`.** Read the caching rules in
   [references/caching.md](references/caching.md) before writing any polling loop
   or scheduled job. Requesting before `expires` can count as circumventing the
   cache — a bannable offense, not just wasteful.
4. **Respect rate limit headers.** Don't write a client that ignores
   `X-Ratelimit-Remaining` / `X-ESI-Error-Limit-Remain`. See
   [references/rate-limiting.md](references/rate-limiting.md).
5. **Never hand-parse pagination tokens.** Cursor tokens are opaque — pass them
   back verbatim. See [references/pagination.md](references/pagination.md) for
   which of the three pagination styles a given route uses and the correct loop
   for each.

## Live spec is the ground truth

ESI serves its own OpenAPI spec at `https://esi.evetech.net/meta/openapi.json`
— for any question about a *specific* route (does it need auth, which scope,
which rate-limit group, which pagination style), fetch and read that instead
of guessing or relying on this skill's prose. It requires
`X-Compatibility-Date` like everything else — get a valid one from
`https://esi.evetech.net/meta/compatibility-dates` first, don't omit it (the
dateless response is missing routes added since 2020-01-01).
→ [references/openapi-spec.md](references/openapi-spec.md)

## Decision guide

- **Does the route need auth, and with which scope?** Public endpoints
  (market data, universe info, killmails by hash, etc.) need no token.
  Anything character/corp/alliance specific needs an SSO access token with
  the right scope. Don't guess — look up the route's `security` field in the
  live OpenAPI spec. → [references/openapi-spec.md](references/openapi-spec.md),
  [references/authentication.md](references/authentication.md)
- **Building the auth flow?** Server-side app that can hold a client secret →
  Authorization Code flow. Mobile/desktop/SPA that can't → Authorization Code +
  PKCE. → [references/authentication.md](references/authentication.md)
- **Writing a polling/sync loop?** Read caching + rate limiting first; both
  change what "correct" polling looks like.
  → [references/caching.md](references/caching.md),
  [references/rate-limiting.md](references/rate-limiting.md)
- **Route returns a list and might be large?** Check whether it uses cursor,
  `from_id`, or `X-Pages` pagination — they require different client loops and
  aren't interchangeable. The spec's `parameters` list for that route tells
  you which (look for `before`/`after`, `from_id`, or `page`).
  → [references/openapi-spec.md](references/openapi-spec.md),
  [references/pagination.md](references/pagination.md)
- **Getting 420s or 429s?** 420 = legacy error-rate limit (>100 non-2xx/3xx
  responses/minute); 429 = new bucket rate limit, has a `Retry-After` header. Fix
  the underlying error rate or backoff — don't just retry blindly.
  → [references/rate-limiting.md](references/rate-limiting.md)

## Quick facts

- Base URL: `https://esi.evetech.net`
- Live spec: `https://esi.evetech.net/meta/openapi.json` (needs
  `X-Compatibility-Date`)
- Valid compatibility dates: `https://esi.evetech.net/meta/compatibility-dates`
- SSO metadata (always fetch endpoints from here, don't hardcode): 
  `https://login.eveonline.com/.well-known/oauth-authorization-server`
- Token endpoint: `https://login.eveonline.com/v2/oauth/token`
- Report ESI bugs/feature requests: https://github.com/esi/esi-issues
- A 304 response to a conditional (`If-None-Match`) request means "unchanged" —
  treat it as success, not an error to retry.
