# Step 4 — working checklist

Delete this file when Step 4 closes. It is a worklist, not design: the design
lives in [design-hosted-v2.md](design-hosted-v2.md) §11.

## Where this session ended

* Branch `docs/hosted-v2-design`, v0.9.52. **723 tests green, 1 skipped** — and
  the one skip is POSIX file modes on Windows, not a backend. Postgres 17 has
  now been run: the schema builds, all three migrations reach head on it, and
  the eight tests that had skipped through every previous commit all pass. The
  first run did fail once, correctly: `test_only_our_own_ids_are_generated`
  whitelists the tables allowed to mint their own ids, and `sync_events` —
  added after that whitelist was written — belongs on it, because its id *is*
  the cursor a consumer resumes from. Its sibling `char_jobs_cache` correctly
  did **not** appear, being keyed on a CCP character id. The assertion did its
  job on a table it had never seen.
* **W11 is done.** SQLite holds up under the sync worker, and that is now
  asserted rather than assumed — see `tests/test_sqlite_under_the_worker.py`.
* **`/sync-health` exists**, and the four observability surfaces finally have a
  caller that is not a test. It shows the worker's state, per-character cache
  freshness, the ETag cache, anything the transport is holding back, and the
  newest 60 events with their failure reasons. Read-only on purpose: a health
  view that can act is one you cannot trust about the thing it acts on.
* **W6 is done.** `main.py` 7,112 → 835 lines; eleven routers under
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

Order — `projects`, `industry` and `app_defaults` are **done**; `industry` is
the better worked example, because it hit the cases `projects` did not.
Remaining: `industry_helper`, `app/character/*`, `location_resolver`, then
`media`, `contracts`, `characters`, `planets`, `locations`, `prices`, `assets`,
`plan`, and finally `deps.py`, `main.py` and `app/db/schema.py` itself.

**`industry_helper` is next**, on the same "convert what is underneath" rule
that put `app_defaults` before the routers: three of the four remaining
`dbapi()` boundaries point at it (`get_adjusted_prices_cached` twice and the
station-context block in `margins.py`). The fourth points at `app/character/*`
plus `location_resolver`, which go together.

**"Smallest first" was the wrong heuristic and is abandoned.** It ignored
shared dependencies: `BOMResolver` sat underneath four of the ten and had to be
converted first (v0.9.43). Convert what is *underneath* next, not what is
smallest.

**Boundary crossings are marked.** A converted module calling an unconverted one
passes `dbapi(conn)` — the driver connection from underneath. `grep -rn "dbapi("
app/` is the list of boundaries still standing, and it should shrink to nothing.
It stood at 6 before v0.9.48 and stands at **4** after it.

**A converted module called from an unconverted one is the other direction**, and
it turns up as soon as something low in the stack is converted. `app_defaults`
is called from `auth.py` and `plan.py`, neither of which is converted. Those
call sites open their own connection from the engine — `with connect() as c` —
rather than the whole router being dragged along; `auth.py` was already doing
exactly that for `invention.list_decryptors`. Two rules learned doing it:

* **Read into a plain dict and close.** `plan_result` wants the defaults in
  three places. Holding the connection open across all three leaks one per plan
  run on any path that raises; the defaults are data, so one `with` block at
  the top and a dict afterwards is both shorter and safe.
* **Use `connect()`, not the connection that happens to be nearby.**
  `plan_result` already had a SQLAlchemy connection in scope — `sde_conn` from
  `connect_to_path(database_path())`. Reusing it would have worked today and
  read the defaults out of the SQLite *file* rather than the configured
  database, which is wrong the moment `EVE_DATABASE_URL` is set.
Passing the SQLAlchemy connection instead fails with
`'str' object has no attribute '_execute_on_connection'` from deep inside
SQLAlchemy, which reads like a query bug rather than a boundary crossing.

**Lazy `ensure_*_tables()` is SQLite-only.** It predates migrations, and
`app/db/schema.py` memoises by asking `PRAGMA database_list`, which is a syntax
error on Postgres. A converted shim returns early on any other dialect: there
the schema comes from Alembic.

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
* **`ON CONFLICT ... DO UPDATE SET x = x + excluded.x`** — qualify it:
  `SET x = tbl.x + excluded.x`. **Corrected 2026-08-19 by running it:** this
  used to say Postgres resolves the bare `x` against the proposed row and
  silently double-counts. It does not — the name is visible on both the target
  table and `excluded`, and Postgres refuses to guess, raising
  `AmbiguousColumn: column reference "x" is ambiguous`. SQLite resolves it to
  the stored row and runs. So it is a loud failure on first contact rather than
  a quiet miscount, which is worth knowing before hunting for one.
* **Two LEFT JOINs off one row is a cartesian product**, so `SUM(CASE ...)`
  over either of them counts each match once per row of the other. Use
  `COUNT(DISTINCT CASE WHEN ... THEN id END)`. This was live in
  `list_projects`.

**Verify:** a cross-backend test file per module — `test_projects_on_postgres.py`
and `test_industry_on_postgres.py` are the pattern. Drive the helper directly,
parameterised over both backends, so the same assertion runs twice. Then mutate
the portability fix and check the failure *shape*: reverting
`PRAGMA table_info` fails Postgres and passes SQLite, which is what proves the
test can see a backend difference at all.

~~**Open gap found doing `industry`: the SDE tables are not created on
Postgres.**~~ **Closed in v0.9.47.** `create_sde_schema(bind)` builds them on
whatever dialect is underneath, `import_sde.py` writes through SQLAlchemy so it
can fill them, and startup creates them before it counts `sde_types`.
`tests/test_sde_on_postgres.py` runs the whole importer on both backends.

**Two things learned closing it, both of which contradicted the guess going
in.** They are worth carrying into the modules still to convert:

* **The SDE DDL was already portable.** All fourteen tables compile to
  byte-identical DDL on both dialects, and every SQLite-compiled statement runs
  on Postgres unchanged — checked, not assumed. The reason is that every SDE
  primary key is a natural key CCP assigned, so nothing there is a `SERIAL`,
  where six of the *app* tables are. `test_no_sde_table_mints_its_own_id` pins
  that, because the day it stops being true is the day the two backends quietly
  disagree about the SDE's shape. The defect was the duller one: a function
  nothing on Postgres could call.
* **The importer's default target was wrong on any real deployment.** It wrote
  to a fixed `eve_cache.db` beside the script rather than to
  `EVE_APP_DIR`/`EVE_DATABASE_URL`. Those coincide in the documented VPS layout
  and diverge the moment the data directory is not the checkout — the import
  then reported success into a database nothing opened. Found by converting the
  target resolution, not by anything failing.

**The importer's cross-backend result, measured on the real 99 MB archive:** all
fourteen tables match row-for-row between the two backends, the Nidhoggur bill
of materials is identical, and so are the invention probabilities. Postgres
takes 10.9s to SQLite's 3.0s, which is fine for a thing run on deploy.

**Scope, stated honestly: this does not make the app run on Postgres.**
`deps.get_conn()` is still `sqlite3.connect(database_path())`, and every
unconverted module goes through it. What is closed is that the static data can
now be *created and filled* there, which was the item blocking everything
else — the remaining modules can now be converted and tested against a Postgres
database that has an SDE in it.

## 2. Remaining Step 4 items

Ordered by dependency, not by size.

* ~~**W9 — async token refresh.**~~ **Done.** 22 call sites across six routers.
  The net is `tests/test_async_token_refresh.py`, which scans for the blocking
  call inside any coroutine — keep it green rather than re-auditing by hand.
* ~~**Background sync worker.**~~ **Done.** `app/sync/worker.py`, started from
  the app's startup handler, stopped on shutdown. `EVE_SYNC_WORKER=0` turns it
  off; `tests/conftest.py` sets that, because otherwise every
  `with TestClient(app)` would start a loop against live ESI.

  It syncs blueprints, assets, skills, industry jobs and corp assets. Planets,
  contracts, orders and wallet are still fetched on the request path — moving
  those is the cache-only item below, and the worker is where they go.

  **It has to be told when a character is added.** `wake()` cuts the current
  sleep short, and the login path calls it. Without that, a character added to
  a running instance waits out the interval — and an instance that started with
  `characters` empty found nobody on its first tick and slept the full fifteen
  minutes, so `/jobs` said "not synced yet" for a quarter of an hour after a
  successful login. Found by running the app, not by reading it; the old
  `test_no_characters_means_no_work` had asserted the full-interval sleep as
  correct.

* **Cache-only routes** — no route fetches from ESI on the request path.
  **Done: `/jobs`, `/orders`, `/wallet`, `/contracts`, `/assets` and
  `/blueprints`. `/orders` is the better example**, because it came with tests. The jobs cache has none: it is covered only by the AST
  scan, which proves a handler contains no `fetch_` call and nothing at all
  about whether the cached data is right.

  **`/jobs` is the original pattern**: a `char_jobs_cache` table filled by
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

~~Run `projects` against Postgres~~, ~~sign in~~ and ~~the sync-health page~~ are
all **done**. What that left is below.

1. **Pick up the conversion.** `industry_helper` next — three of the four
   remaining `dbapi()` boundaries point at it. `grep -rn "dbapi(" app/` is the
   live list. `tests/test_app_defaults_on_postgres.py` is the pattern to copy:
   drive the module directly, parameterised over both backends, then mutate the
   portability fix and check the *shape* of the failure. Removing the dialect
   guard from that module's schema shim failed **12 Postgres tests and 0 SQLite
   ones**; that asymmetry is what proves the test can see a backend difference
   at all rather than merely running twice.

   Also worth copying from it: **one assertion that opens a second
   connection.** A converted writer that loses its `commit()` passes every
   same-connection check and drops the write when the request ends — dropping
   the commit from `save_defaults` fails exactly one test, and it is that one.
2. **Cache-only routes**, eight pages left. `/jobs` is the worked example.
3. **Give the health page a second consumer, or don't.** `/sync-health` reads
   the log; nothing subscribes to it with a cursor. That is fine — `since()`
   exists for when something does, and building a subscriber before there is
   anything to subscribe *for* is how the first four surfaces ended up with no
   callers. Wait until a feature needs it.

## How to work on this without wasting an afternoon

Measured from the session that built most of Step 4, by timing every tool call
in the transcript rather than counting them: **4.7 hours were spent inside tool
calls, and 3.5 of those hours were 71 whole-suite pytest runs — 74% of all
working time spent waiting for tests.** The 209 targeted runs in the same
session cost 25 minutes between them. Most of the 71 told us nothing.

* **Run the targeted file while working; run the full suite once, before the
  commit.** Not after every edit. `pytest tests/test_x.py` is two seconds.
* **Mutation-test against the targeted file too.** A mutation batch is 5–8
  runs; at three minutes each that is half an hour to learn something a
  two-second run tells you.
* **Never build Python source in a bash heredoc.** The tool collapses
  backslashes, so a two-character escape sequence arrives in the file as a real
  newline and the source no longer parses. **This bullet was itself written
  through a heredoc and corrupted by the bug it describes** — it has said
  "a string containing `" followed by a line break ever since, which is the
  most direct demonstration available. Use the Write or Edit tool for anything
  containing escapes, regexes, or nested quotes. Four separate repair cycles in
  one session before the lesson stuck.
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

## Two ways a test can guard nothing

Both found by mutation while doing `/orders` (v0.9.49), both of which had left a
test green while the thing it named was broken. Neither is specific to that
page.

* **Patch the module that *calls*, not the module that *defines*.** A router
  doing `from app.esi.client import esi_client` binds the function at import.
  `monkeypatch.setattr(app.esi.client, "esi_client", stub)` rebinds the source
  and leaves every importer pointing at the original, so the guard passes no
  matter what the page does. This is the W6 lesson about `app_module.X` in a
  second costume. `tests/test_sync_health.py` had it too and has been fixed.
* **A stub that raises proves nothing inside a handler that swallows.** These
  route handlers wrap their bodies in `except Exception` to turn a failure into
  an error banner, which catches the stub's `AssertionError` along with
  everything else — 200 returned, test green, page calling ESI on every
  request. Have the stub append to a list and assert the list is empty; that
  survives being caught.

The general shape: **a guard that cannot fail looks exactly like a guard that
passes.** The only way to tell them apart is to break the thing on purpose and
watch — which is the same rule as "mutate the fix and check the failure shape",
applied to tests rather than to code.

## A converted page breaks the tests that stubbed its fetchers

`/wallet` (v0.9.50) took `tests/test_wallet_filter.py` down with it, and the
failure is worth keeping in mind before each remaining page rather than
discovered each time. That file injected journal and transaction rows by
replacing `wallet_api.fetch_journal` and friends. The page stopped calling
them, so the stubs were ignored and it rendered empty — two assertions about
row attributes failed with `0 == 2`.

**The repair is to seed the cache, not to re-stub.** A fixture that writes
`wallet_ledger_cache` exercises the path a real request takes; one that stubs a
fetcher exercises a path that now exists only under test. The same will be true
of `/assets`, `/blueprints`, `/contracts` and `/plan` — check what stubs each
one's fetchers before converting it, because the failure arrives as an
assertion about rendered HTML and reads like a template bug.

It is also, in its way, the best confirmation available that the conversion
took: a page that still fetched would have passed.

## Not every fetching handler should be converted

`api_contract_items` came off the TODO list in v0.9.51 without being converted,
and the reasoning generalises to the pages still on it.

Expanding a contract row is a button press, like `api_market_orders` above it.
Prefetching would mean the worker fetching items for every contract every
character holds — fifty calls a tick to cache what nobody opens. So it keeps
its exemption, but it now *caches* what it fetches, permanently: a contract's
contents are fixed when it is created, so the first read is correct forever and
the second expand is instant.

That is a third category the list did not have a name for. Not "renders from
cache" and not "the answer is the fetch", but **fetched once, on demand, then
never again.** When the remaining pages are looked at, ask which of the three
each piece of their data is before reaching for the worker.

One consequence worth stating: this cache has no TTL and nothing refreshes it,
so caching a *failure* would be permanent. `fetch_character_contract_items`
returning `[]` on an error would show that contract as empty for the life of
the database. It returns None instead, and
`test_a_failed_item_fetch_is_not_cached_as_an_empty_contract` is what keeps it
that way.

## The third time the guard did not cover the page

`test_no_converted_page_calls_a_fetcher` patched every `fetch_*` on the API
modules and then visited only `/orders` and `/wallet`. Putting a fetch back
into `/contracts` failed the AST scan and left it green — the same shape as the
two failures above it, for a third reason: the stub was right, the target was
right, and the **URL list** was short.

The pattern across all three: a guard is only as wide as the thing it actually
exercises, and nothing about a passing test says how wide that is. Mutation is
the only way to find out, and it costs one run per claim.

## A cache that exists is not a cache that is read

`/assets` and `/blueprints` (v0.9.52) started from a different place than the
three pages before them: the caches already existed and the worker already
filled them. What made the pages fetch was the **TTL**. `_load_cache` returns
None once the row is older than `CACHE_TTL`, so an aged cache was
indistinguishable from an empty one and the page went to ESI.

The TTL answers "is another round trip worth it", which is a question for a
fetcher. A page that must not make round trips has no use for it. So both
modules gained a reader that ignores the TTL and returns the age instead —
`load_cached_assets`, `load_cached_blueprints` — and the page says how old its
answer is rather than silently refreshing it.

**The same rule cuts the other way in the worker**, and that turned out to be a
live defect. `sync_character` called `fetch_blueprints` and `fetch_assets`
*without* `force_refresh`, so each consulted the same TTL it was about to
write. Blueprints' TTL is fifteen minutes against a fifteen-minute tick:
whether the worker refreshed them at all came down to scheduling jitter. Assets
at ten minutes always lost the race and so always refreshed, which is why this
never showed up as a stale asset list — only as blueprints that sometimes did
not move. `fetch_industry_jobs` had always passed `force_refresh=True`; the
other three now do too.

Worth checking for on the remaining pages: **a fetcher's freshness rule and a
page's freshness rule are different rules**, and sharing one function for both
hides which is being applied.
