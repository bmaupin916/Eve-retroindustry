# Rate Limiting & Error Limiting

ESI has **two separate, mutually exclusive** limiting systems depending on the
route. Know which one applies — their headers and remedies differ.

## 1. Bucket rate limit (new system, floating window)

Not active on all routes yet — check the route's OpenAPI spec or response
headers to see if it applies.

**How it works**: each `(rate limit group, userID)` pair gets its own token
bucket.

- `rate limit group`: assigned per-route, shown in both the spec and response
  headers.
- `userID`: `<applicationID>:<characterID>` for authenticated requests, or
  `<sourceIP>` (`<sourceIP>:<applicationID>` if a token is supplied without
  the route requiring it) for unauthenticated ones.

It's a **floating/sliding window**: tokens consumed by a request are released
back after `window-size` has elapsed from that specific request — not a hard
reset at fixed intervals. E.g. with a 15-minute window, 2 tokens spent at
10:00 are returned around 10:15; 1 token spent at 10:05 returns around 10:20.

**Token cost by response status**:

| Status | Cost | Why |
|--------|------|-----|
| 2XX | 2 | standard |
| 3XX | 1 | rewards using `If-None-Match`/`If-Modified-Since` |
| 4XX | 5 | discourages client-side mistakes (429s excluded) |
| 5XX | 0 | not your fault |

**Response headers**:

- `X-Ratelimit-Group` — which group this route belongs to
- `X-Ratelimit-Limit` — e.g. `150/15m` (tokens per window; `m`=minutes, `h`=hours)
- `X-Ratelimit-Remaining` — tokens left
- `X-Ratelimit-Used` — tokens this request cost
- `Retry-After` — only present on `429`, seconds until you'll have tokens again

**OpenAPI spec** exposes this per-route via the `x-rate-limit` extension:
`group`, `window-size`, `max-tokens`.

A `429` means you exceeded your bucket — back off for `Retry-After` seconds,
don't just retry immediately.

## 2. Error limit (legacy system, fixed window)

Applies to routes that don't yet have the new bucket limiting. Allows **at
most 100 non-2xx/3xx responses per minute** across all ESI routes for you;
exceeding it returns `420` on *everything*, including routes that do have the
new rate limiting.

**Headers**:

- `X-ESI-Error-Limit-Remain` — errors left this window
- `X-ESI-Error-Limit-Reset` — seconds until window resets to full

Unlike the bucket system this is a **fixed** window: once you hit the limit,
every request is discarded until the window resets — there's no partial
recovery mid-window.

## Also: undocumented server-side limiters

Some routes have a rate limiter deep in EVE's server code that can also
return `429` without any of the headers above explaining why. CCP is working
on removing these, but a client should tolerate a `429` with no rate-limit
headers gracefully (generic backoff) rather than assuming the headers will
always be present.

## Client best practices

- Don't design to run at the limit — treat the limit as a ceiling CCP is
  showing you, not a target.
- Start slowing down once `X-Ratelimit-Remaining` (or
  `X-ESI-Error-Limit-Remain`) approaches zero, don't wait for the 429/420.
- Spread requests over time instead of bursting every window.
- Bursting occasionally is fine; bursting *every* window is not.
- For periodic jobs, stagger instead of synchronizing — e.g. "5 minutes after
  the last job finished" rather than a `*/5` cron that every instance of your
  app hits simultaneously.
- Respect cache `expires` (see [caching.md](caching.md)) — it's the single
  biggest lever for staying well under any limit.
- Non-compliance risks an application ban of varying duration, so treat these
  as hard constraints when writing retry/backoff logic, not tunables.
