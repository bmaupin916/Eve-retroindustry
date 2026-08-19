# Pagination

ESI uses **three different** pagination styles depending on the route. Check
the route's spec to know which applies — they are not interchangeable, and
using the wrong client loop for a given style will silently miss or
duplicate data.

## 1. Cursor-based (newer routes, being rolled out gradually)

Tokens are opaque — **never decode or construct them yourself**, just pass
back exactly what the server gave you.

**Request params**: `limit` (max records, you may get fewer), `before`
(records just before this position), `after` (records just after this
position). Omit both to get the most recent records.

**Response**: includes a `cursor: {before, after}` object alongside the data.

**Ordering**: always by last-modified/created, oldest first, newest last.
`before` walks backward (older), `after` walks forward (newer / detects
changes).

**Two-phase pattern**:

1. **Backfill**: start with no params, then repeatedly request with the
   `before` token from the previous response until you get an empty list —
   you now have the full historical dataset. Remember the very first
   response's `after` token for phase 2.
2. **Monitor**: repeatedly request with the current `after` token until you
   get an empty list — those are new/changed records since last check. Save
   the latest `after` token and repeat this phase indefinitely (e.g. every
   30s), even coming back a day later — tokens stay valid a long time (24h+).

**Duplicates are normal**, not a bug:
- Seen while using `before` → your stored copy is newer, keep it, discard the
  new one.
- Seen while using `after` → the new copy is newer, replace your stored one.

```python
import requests, time

def fetch_records(url, headers, limit=100, before=None, after=None):
    params = {"limit": limit}
    if before: params["before"] = before
    if after: params["after"] = after
    resp = requests.get(url, params=params, headers=headers)
    resp.raise_for_status()
    return resp.json()

def collect_current_data(url, headers):
    all_records = {}
    after_token = None
    before_token = None
    while True:
        data = fetch_records(url, headers, before=before_token)
        records = data.get("records", [])
        cursor = data.get("cursor", {})
        if not records:
            break
        for record in records:
            if record["id"] in all_records:
                continue  # keep existing (newer) copy
            all_records[record["id"]] = record
        before_token = cursor["before"]
        if after_token is None:
            after_token = cursor["after"]  # remember first response's after-token
    return all_records, after_token

def monitor_new_data(url, headers, after_token):
    new_records = {}
    current_after = after_token
    while True:
        data = fetch_records(url, headers, after=current_after)
        records = data.get("records", [])
        cursor = data.get("cursor", {})
        if not records:
            break
        for record in records:
            new_records[record["id"]] = record  # newer always overwrites
        current_after = cursor["after"]
    return new_records
```

## 2. From-id (historical/transactional data, backward-only)

**Request param**: `from_id` — returns that record plus older ones. Omit for
the most recent records.

**Ordering**: newest first. `from_id` always walks backward in time.

**Stop condition**: the response always includes the `from_id` record itself.
If the response contains *only* that one record, you've reached the end.

```python
def collect_current_data(url, headers):
    all_records = []
    from_id = None
    while True:
        records = fetch_records(url, headers, from_id=from_id)
        if not records:
            break
        if from_id is not None and len(records) == 1:
            break  # only the from_id record itself came back — done
        all_records.extend(records)
        from_id = records[-1]["transaction_id"]
        time.sleep(0.1)  # throttle
    return all_records
```

## 3. X-Pages (older routes, traditional page numbers)

**Request param**: `page` (1-indexed). **Response header**: `X-Pages` — total
page count, read it from page 1's response.

**Caching caveat**: if the cache expires between fetching page 1 and page 2,
page 1's data may shift when re-generated, causing duplicate items across
your fetched pages. Mitigation: if page 1 is within a few seconds of its
`expires` time, wait for the cache to refresh *before* starting the full
page-fetch pass, rather than fetching straight through.

```python
def collect_current_data(url, headers):
    all_records = []
    records, page1_headers = fetch_page(url, headers, page=1)
    total_pages = int(page1_headers.get("X-Pages", 1))
    all_records.extend(records)
    for page in range(2, total_pages + 1):
        records, _ = fetch_page(url, headers, page=page)
        all_records.extend(records)
        time.sleep(0.1)  # throttle
    return all_records
```
