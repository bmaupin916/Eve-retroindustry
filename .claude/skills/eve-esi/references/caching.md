# Caching

ESI is both the HTTP handler for game data and a cache manager sitting in front
of it. Respecting the cache headers is mandatory, not an optimization — hitting
the same resource before its cache expires can be treated as circumventing
ESI's caching and is a bannable offense.

## The three headers

- **`expires`**: when ESI's cached copy of this resource expires and updated
  data becomes available. **Do not request again before this time.** Best
  case if you do: you just get the same cached data back, wasting a request on
  both ends. Worst case: you get fresh data anyway, which counts as
  circumventing the cache.
- **`last-modified`**: when the cached data was last updated. Useful for
  detecting whether anything actually changed since your last fetch.
- **`ETag`**: a content hash. Send it back as `If-None-Match` on the next
  request to that resource; if the content hasn't changed, ESI returns `304
  Not Modified` instead of `200` — cheaper for both sides, and (per the rate
  limiting rules) a 3XX only costs 1 token vs 2 for a 2XX.

## Conditional request pattern

```python
import requests

etag = None

def fetch(url, headers):
    global etag
    req_headers = dict(headers)
    if etag:
        req_headers["If-None-Match"] = etag

    resp = requests.get(url, headers=req_headers)
    if resp.status_code == 304:
        return None  # unchanged — not an error, don't retry/log as failure
    resp.raise_for_status()
    etag = resp.headers.get("ETag")
    return resp.json()
```

## Gotchas

- **Paginated resources**: `last-modified` should be identical across all
  pages of the same resource fetched in one pass. If it differs between pages,
  the data changed mid-fetch (e.g. via a cache refresh) — treat the pull as
  inconsistent and re-fetch, don't merge mismatched pages.
- **POST endpoints** typically don't return cache headers even though ESI
  still caches internally — don't assume "no headers" means "not cached."
- **Static data** (types, planets, moons, etc.) shares consistent caching
  headers across the whole category.
- Never build a scheduler that treats `expires` as a suggestion. If you need
  fresher data than `expires` allows, that's a sign you're polling the wrong
  way for your use case, not a reason to bypass the header.
