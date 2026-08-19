# Step 4 — working checklist

Delete this file when Step 4 closes. It is a worklist, not design: the design
lives in [design-hosted-v2.md](design-hosted-v2.md) §11.

## Where this session ended

* Branch `docs/hosted-v2-design`, v0.9.37. **525 tests green**, 8 skip unless a
  Postgres is reachable — and they have skipped through every commit of this
  work, so nothing here is verified against Postgres yet.
* **W6 is done.** `main.py` 7,112 → 822 lines; eleven routers under
  `app/web/routers/`, plus `app/web/deps.py` for what they share.
* Step 4 is **7.5 of 8** items. What is left is the rest of the cache-only
  conversion (`/jobs` done, eight pages to go) and the query conversion
  (`projects` done, ten modules to go).

To bring the Postgres tests back:

```bash
docker run -d --name eve-pg -e POSTGRES_PASSWORD=eve -e POSTGRES_USER=eve -e POSTGRES_DB=eve_retroindustry -p 55432:5432 postgres:17
```

## The thing to read first

`tests/conftest.py`. The suite spent at least one session writing to the real
`eve_cache.db`, because `EVE_APP_DIR` was set inside a fixture and fixtures run
after collection — by which time a test module with a module-level app import
had already bound the path. It cost the three real characters and their refresh
tokens.

Two guards exist and both are mutation-checked: conftest sets the environment
at import, and `pytest_collection_finish` refuses to run if `database_path()`
or `app_dir()` answer anything but the test directory.

**The class is now closed, not just guarded.** Nothing resolves a writable path
at import any more: `app/db/location.py` exposes `app_dir()` and
`database_path()`, and `deps.DB_ABS`, `location.DB_PATH`, `deps._APP_DIR` and
`token_store.CONFIG_PATH` are all gone. `tests/test_web_deps.py` scans the
package for module-level assignments that read `EVE_APP_DIR` — that scan is
what found the `token_store` one, which held the refresh-token config path and
nobody had thought of.

`EVE_BUNDLE_DIR` is still frozen, deliberately: it points at the code, and
getting it wrong renders a 500 rather than deleting a database.

## 1. The query conversion — module by module

**Target:** `app/db/conn.py`, proven on both backends (`tests/test_db_conn.py`,
`tests/test_postgres_schema.py`).

`?` + tuple → `:name` + dict, and take the connection from
`app.db.conn.connect()` instead of `deps.get_conn()`. Roughly 316 statements.

**This was written down as atomic and it is not.** A converted module can use
`connect()` while everything else still uses `get_conn()` — same file, same
process, each sees the other's committed writes
(`test_both_connection_styles_work_on_one_database`). So each module ships on
its own, and what is left at the end is deleting `get_conn()` once nothing
calls it.

Order, smallest first — `projects` is **done** and is the worked example:
`media`, `industry`, `contracts`, `characters`, `planets`, `locations`,
`prices`, `assets`, `plan`, then `deps.py` and `main.py`.

**Convert the router and its `*_helper.py` together.** The helpers take the
connection as an argument, so half a slice does not run.

**Check coverage before converting, not after.** `projects` had eight write
endpoints and no test beyond `/projects` returning 200. Writing the tests found
a live counting bug that had nothing to do with the conversion. Assume the
next module is the same until shown otherwise.

**Traps, each already pinned by a test or found the hard way:**

* **Writes need an explicit `commit()`.** SQLAlchemy opens a transaction on
  first use and rolls it back on close; `sqlite3`'s default isolation mode
  commits some statements for you. A call site moved without its commit loses
  writes *silently*. This is the one that will cost a debugging session.
* **`IN ({ph})`** — several sites build placeholder strings by hand
  (`",".join("?" * len(ids))`). Named binds want SQLAlchemy's
  `bindparam(..., expanding=True)`, not string building.
* **`executemany`** takes a list of dicts, not a list of tuples.
* **`row[0]` still works** — SQLAlchemy `Row` indexes positionally. Verified,
  so readers of results do not have to change.
* **`upsert()` still emits `?`** by design, because its callers pass tuples. It
  has to start emitting named binds in the same commit as its call sites.
* **`cursor.lastrowid`** has no equivalent; `RETURNING id` replaces it and
  needs SQLite >= 3.35 (3.49.1 here, asserted rather than assumed).
* **`ON CONFLICT ... DO UPDATE SET x = x + excluded.x`** — the bare `x` on the
  right means the stored row on SQLite and the *proposed* row on Postgres.
  Qualify it: `SET x = tbl.x + excluded.x`.
* **Two LEFT JOINs off one row is a cartesian product**, so `SUM(CASE ...)`
  over either of them counts each match once per row of the other. Use
  `COUNT(DISTINCT CASE WHEN ... THEN id END)`. This was live in
  `list_projects`.

**Verify:** full suite on SQLite, then the same suite with `EVE_DATABASE_URL`
pointed at the container. Both must pass before the commit lands.

## 2. Remaining Step 4 items

Ordered by dependency, not by size.

* ~~**W9 — async token refresh.**~~ **Done.** 22 call sites across six routers.
  The net is `tests/test_async_token_refresh.py`, which scans for the blocking
  call inside any coroutine — keep it green rather than re-auditing by hand.
* ~~**Background sync worker.**~~ **Done.** `app/sync/worker.py`, started from
  the app's startup handler, stopped on shutdown. `EVE_SYNC_WORKER=0` turns it
  off; `tests/conftest.py` sets that, because otherwise every
  `with TestClient(app)` would start a loop against live ESI.

  It syncs blueprints, assets, skills and corp assets. Jobs, planets,
  contracts, orders and wallet are still fetched on the request path — moving
  those is the cache-only item below, and the worker is where they go.

* **Cache-only routes** — no route fetches from ESI on the request path.
  **`/jobs` is done and is the pattern**: a `char_jobs_cache` table filled by
  the worker, `load_cached_jobs()` read by the page, and the page saying how old
  its answer is. `None` from the cache means "not synced yet" and `[]` means
  "nothing there" — conflating them makes the page claim a character is idle
  when it has simply not been looked at.

  `tests/test_cache_only_routes.py` lists what is left, with a reason each.
  Remove a name from `ALLOWED` when its page stops fetching, never to make the
  test pass. Note the scan does not cross modules, so moving a fetch into a
  helper would pass it — that is a known hole, documented in the file.
* ~~**ETags on every fetch.**~~ **Done.** In the transport alongside the
  quarantine. `etag_stats()` reports hits, misses and bytes held.
* ~~**4XX quarantine per character.**~~ **Done.** In the transport
  (`app/esi/client.py`), keyed on the entity in the URL. `quarantine_state()`
  reports what is currently held, which is what a sync-health view would show.

## What W6 taught that applies to the rest

* **A test that patches `app_module.X` breaks silently when `X` moves.** Nine
  test files needed repointing. The attribute-access form fails at the call
  site; the string form (`monkeypatch.setattr(app_module, "X", ...)`) fails at
  the patch, which is louder. Prefer the string form.
* **pyflakes earns its place.** It found two live bugs on the first run —
  `_bg_fetch_prices` calling `_esi_client()` and `_resolve_corp_container_names`
  posting an undefined `owned_ids`, both swallowed by a broad `except` — and it
  is what makes a router move safe to do quickly.
  `tests/test_no_undefined_names.py` runs it over the package.
* **A green suite is not a working suite.** The route inventory would have
  stopped seeing routes the moment the first one moved into a router, because
  FastAPI 0.141 does not flatten included routers into `app.routes`. Check that
  a net still catches something before trusting it.

## Honest scope note

The design doc estimates 3–5 sessions for all of Step 4, at **low confidence**,
and calls it the item most likely to double. Two long sessions have produced 3
of 8 items plus the Postgres groundwork — roughly on the pessimistic end of
that estimate, which is where it was always likely to land.

The conversion and the worker are each a session. The worker is new design
work, not a move, and the event model has to be decided before it is written.

## Start here next session

In order. The first is ten minutes and is the only one that can invalidate work
already committed.

1. **Start Postgres and run the suite against it.** Eight tests have skipped
   through every commit of this work. The schema, both new migrations and the
   one converted query module are unverified against the backend the whole
   conversion exists for.
2. **Sign in.** `characters` and `app_owner` are empty after the test-data
   cleanup, so the worker has nothing to sync and every page is blank. The
   cached assets, blueprints, wallet and skills for all three real characters
   are still there and come back on login.
3. **Then pick up the conversion** — `industry` is next by size, and it is a
   router plus two helpers (`margins_helper`, `reactions_helper`).

## How to work on this without wasting an afternoon

Measured from the session that built most of Step 4: **1,166 tool calls, of
which 65 were full-suite runs at three minutes each — over three hours of
wall-clock spent re-running tests.** Most of those runs told us nothing.

* **Run the targeted file while working; run the full suite once, before the
  commit.** Not after every edit. `pytest tests/test_x.py` is two seconds.
* **Mutation-test against the targeted file too.** A mutation batch is 5–8
  runs; at three minutes each that is half an hour to learn something a
  two-second run tells you.
* **Never build Python source in a bash heredoc.** The tool collapses
  backslashes, so a string containing `
` arrives as a real newline and the
  file no longer parses. This cost four separate repair cycles before the
  lesson stuck. Use the Write or Edit tool for anything with escapes in it.
* **Never `git add -A`.** It swept unrelated files into a commit twice, each
  needing a reset and a re-commit. Name the paths.
* **When a test is flaky, probe the state — do not re-run the suite.** The
  database-contamination bug took eight full runs to corner and would have
  taken one query: "what is in this table before the failing test?"

## Still unresolved

* **Step 3 is still not deployed.** All of this plumbing is being built ahead of
  the hosted tool it is plumbing for, which inverts the order §11 argues for.
* **Open question 8** — verify sales tax and broker's fee in game against the
  wallet journal. Only you can do this one.
* **The characters were not restored.** `eve_cache.db.bak-before-character-reset`
  still holds Astroasia and Tracy Juan with their refresh tokens if signing in
  again turns out not to be enough. `app_owner` is empty, so the first real
  login claims the instance.
