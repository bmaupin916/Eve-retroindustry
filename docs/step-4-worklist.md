# Step 4 — working checklist

Delete this file when Step 4 closes. It is a worklist, not design: the design
lives in [design-hosted-v2.md](design-hosted-v2.md) §11.

## Where this session ended

* Branch `docs/hosted-v2-design`, v0.9.66. **1432 tests green, 2 skipped** — and
  the skip is POSIX file modes on Windows, not a backend. The `sqlite_only`
  marker is gone: `location_resolver` converted, so the test that named it as
  the blocker now runs on both backends.
  Postgres 17 is run routinely now: the schema builds, all ten migrations reach
  head on it, and every cross-backend file runs both halves. The
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
* Step 4 is **8 of 8 items in outline**, with one of them part-finished. The
  cache-only conversion is **done** (v0.9.55). What is left is the query
  conversion. Converted so far: `projects`, `industry`, `app_defaults`,
  `industry_helper` (v0.9.56), `location_resolver` (v0.9.58), and
  `token_store`, `character/jobs`, `character/skills` (v0.9.59).

  The live measure is **0 callers** as of v0.9.59 — there are no boundaries
  left to cross. Count them with

  ```bash
  grep -rn "dbapi(" app/ --include=*.py | grep -v "app/db/conn.py"
  ```

  and not with the bare `grep -rn "dbapi(" app/` written here before: that also
  matches the definition in `app/db/conn.py` and its docstring, so it answers
  **2** on a fully converted tree and reads like a regression. What remains is not
  crossings but modules still holding a raw `get_conn()` handle of their own:
  **`app/character/*` is done as of v0.9.65** — `assets` (v0.9.60),
  `blueprints` (v0.9.61), `contracts` (v0.9.62), `wallet` (v0.9.64),
  `orders` and `planets` (v0.9.65). What is left is the web layer:
  `plan`, `prices`, `locations`, `assets`, `planets`, `media`, the
  helpers beside them, and finally `deps.py`, `main.py` and
  `app/db/schema.py`.

  **The `conn=raw` measure has retired too** — it reached zero in v0.9.65
  and the `raw = conn.connection.driver_connection` line went with it. It
  ran 13 → 11 → 5 → 0 across v0.9.62–65.

  **The measure from here is the count of raw `?`-parameter statements**, and
  it has to be taken with an AST walk rather than a grep — see "Count
  statements with an AST" below. **137** of them at v0.9.66, down from 145 at
  v0.9.65; twelve files still hold a raw `get_conn()` handle.

To bring the Postgres tests back:

```bash
docker start eve-pg
```

If the container is gone entirely:

```bash
docker run -d --name eve-pg -e POSTGRES_PASSWORD=eve -e POSTGRES_USER=eve -e POSTGRES_DB=eve_retroindustry -p 5433:5432 postgres:17
```

**The host port is 5433, and it must stay below 49152.** It was 55432 until
2026-08-25, when `docker start` began failing with *"bind: An attempt was made
to access a socket in a way forbidden by its access permissions"*. That is not
Docker: Windows' TCP dynamic port range is 49152–65535, and WinNAT/Hyper-V
reserves blocks inside it at boot. One boot happened to reserve 55357–55456,
which swallowed 55432. Any port in that range can be taken away on any reboot,
so the fix was to move below the range rather than to restart `winnat`.

To confirm it is really a reservation rather than something listening:

```bash
netsh interface ipv4 show excludedportrange protocol=tcp
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

Order — `projects`, `industry`, `app_defaults`, `industry_helper`,
`location_resolver`, `token_store`, `character/jobs` and `character/skills` are
**done**; `industry` is the better worked example, because it hit the cases
`projects` did not.

**The `dbapi()` count is zero from v0.9.59**, so the metric that drove the order
so far has retired. What is left is the rest of `app/character/*` (`orders`
and `planets` — `assets`, `blueprints`, `contracts` and `wallet` are done), then
`media`, `contracts`, `characters`, `planets`, `locations`, `prices`, `assets`,
`plan`, and finally `deps.py`, `main.py` and `app/db/schema.py` itself. None of them
blocks another, so take them one at a time and let the coverage probe pick the
order — the untested ones are the ones that bite.

~~**`industry_helper` is next**~~ — **done in v0.9.56**, on the same "convert
what is underneath" rule that put `app_defaults` before the routers. Three of
the four boundaries standing before it pointed at it: `get_adjusted_prices_cached`
twice and the station-context block in `margins.py`.

**Checked rather than assumed, because the obvious alternative looks better
than it is.** `location_resolver` sits *underneath* `industry_helper` — three
functions call into it — and it is the smaller file, so both heuristics appear
to point there first. They are wrong here: it has **40 call sites across seven
routers that are not converted**, and converting it first makes every one of
them open its own `connect()`, in code that gets re-touched the moment those
routers convert. `industry_helper` first costs three `dbapi()` markers instead
of forty adaptations.

**The boundary count went 4 → 5, and that was the right outcome.**
The prediction going in was that it would stay flat: converting
`industry_helper` removes three boundaries (`margins.py` twice,
`reactions_helper.py` once) and creates three, because its own calls to
`get_station_security_multiplier` then cross into the unconverted
`location_resolver`. It went up by one more than that because three handlers in
`routers/locations.py` had to move onto `connect()` as well — they were almost
entirely `industry_helper` calls — and one of them also asks `location_resolver`
for a security status, which is a fifth crossing.

The boundary *moved down a level*; it did not disappear, and it will not until
`location_resolver` goes. All five now point at that one module. Worth writing
down because `grep -rn "dbapi(" app/` is the progress metric everywhere else in
this file, and a rising number here means the conversion worked.

**Seven of its nineteen functions had no test at all**, found before converting
anything by wrapping every function in the module and running the whole suite
(the grep for each name in `tests/` said twelve were uncovered, and undercounts
in both directions — it misses everything reached through a route). The seven:
`populate_rig_bonuses`, `get_rig_types`, `save_station_rigs_full`,
`get_station_rigs_full`, `get_station_me_bonus`, `get_station_me_bonus_pct`,
`save_station_me_bonus` — the whole station-rig cluster, including **both
writers**. They were covered first in `tests/test_industry_helper_rigs.py`,
written against the `sqlite3` version deliberately, so the conversion had
assertions to *preserve* rather than assertions invented afterwards to fit
whatever it did — thirteen mutations, each caught by the test that names it.
That file became `tests/test_industry_helper_on_postgres.py` when the module
moved; the assertions are unchanged and only the fixture underneath them is
different, which is the whole point of having written them first.

**The probe is worth repeating on the next module.** It is twenty lines: wrap
every function the module defines, count calls, print the zeroes at the end of
the run. One caveat that decides whether it tells the truth — it has to rebind
the wrappers into any module that already did `from … import X`, or it silently
reports "never called" for exactly the callers that matter. That is the same
from-import trap that has now made four guards in this project guard nothing.

**"Smallest first" was the wrong heuristic and is abandoned.** It ignored
shared dependencies: `BOMResolver` sat underneath four of the ten and had to be
converted first (v0.9.43). Convert what is *underneath* next, not what is
smallest.

**Boundary crossings are marked.** A converted module calling an unconverted one
passes `dbapi(conn)` — the driver connection from underneath. `grep -rn "dbapi("
app/` is the list of boundaries still standing, and it should shrink to nothing.
It stood at 6 before v0.9.48, 4 after it, and **5** after v0.9.56 — see
"The boundary count went 4 → 5" above for why that was the right direction.

**A converted module called from an unconverted one is the other direction**, and
it turns up as soon as something low in the stack is converted. `app_defaults`
is called from `auth.py` and `plan.py`, neither of which is converted. Those
call sites open their own connection from the engine — `with connect() as c` —
rather than the whole router being dragged along; `auth.py` was already doing
exactly that for `invention.list_decryptors`. Three rules learned doing it:

* **Read into a plain dict and close.** `plan_result` wants the defaults in
  three places. Holding the connection open across all three leaks one per plan
  run on any path that raises; the defaults are data, so one `with` block at
  the top and a dict afterwards is both shorter and safe.
* **Use `connect()`, not the connection that happens to be nearby.**
  `plan_result` already had a SQLAlchemy connection in scope — `sde_conn` from
  `connect_to_path(database_path())`. Reusing it would have worked today and
  read the defaults out of the SQLite *file* rather than the configured
  database, which is wrong the moment `EVE_DATABASE_URL` is set.
* **When a leak rule gets written down, grep the same function for every other
  connection.** The first bullet was written in v0.9.48 about the defaults.
  Four lines below where it landed, `sde_conn = connect_to_path(database_path())`
  had the identical flaw and had had it for far longer: opened, used across
  `_derive_job_splits`, `build_invention_params` and two `resolve` calls that
  all raise on bad input, then closed by a bare `sde_conn.close()` at the
  bottom with no `try`/`finally`. `plan_result` held two connections and only
  one of them got the lesson. Fixed in v0.9.54 with a `with` block, which the
  resolver allows: it documents its connection as borrowed rather than owned,
  and rows leave it as plain dicts, so nothing below the block needs it open.
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
* **A leaked connection inside a route handler has no symptom.** `plan_result`
  wraps its body in `except Exception` and renders the message into the page's
  error banner, so a request that leaked a handle is a **200 with a polite red
  box** — nothing in the response, the logs or the tests goes red. And
  `connect_to_path` builds its engine with `NullPool`, so each call is a fresh
  sqlite3 handle rather than one going back to a pool: the leak is a real file
  handle per failed request, held until the process exits. Assume every
  `except Exception` route handler is hiding one until checked.
* **Test connection lifetime by spying, not by inspecting.** Monkeypatch
  `app.db.conn.connect_to_path` to record what it hands out, drive the route,
  then assert every recorded connection has `.closed`. Patching the source
  module is enough because the routers import it *inside* the handler, so the
  import runs per request. `tests/test_plan_connection_lifetime.py` is the
  worked example. Pin the happy path as well as the raising one — the old code
  closed correctly on success, so a raise-only test is also satisfied by
  moving the close into an `except`, which is not the fix.
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

* ~~**Cache-only routes**~~ — **done in v0.9.55.** No route fetches from ESI
  to render. All eleven handlers that were on the TODO list are off it, and
  `ALLOWED` now contains only routes where the fetch *is* the answer: buttons,
  streams, image proxies, and two lookups of something the user just typed.

  `/orders` is the example to copy. The jobs cache — the original pattern — has
  no tests of its own: it is covered only by the AST scan, which proves a
  handler contains no `fetch_` call and nothing at all about whether the cached
  data is right.

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
and calls it the item most likely to double. Three long sessions have produced
7 of 8 items plus the Postgres groundwork, with only the query conversion still
open — roughly on the pessimistic end of that estimate, which is where it was
always likely to land. The conversion is the one item that has consistently
taken longer than its size suggests, because each module needs test coverage
written before it can be moved.

The conversion and the worker are each a session. The worker is new design
work, not a move, and the event model has to be decided before it is written.

## Start here next session

~~Run `projects` against Postgres~~, ~~sign in~~ and ~~the sync-health page~~ are
all **done**. What that left is below.

1. **Pick up the conversion.** No boundary is forcing the order any more —
   `dbapi()` has had no callers since v0.9.59 (count it with the grep at the
   top of this file — the bare one answers 2 on a clean tree). Four modules left in
   `app/character/*`, one at a time: `contracts`, `wallet`, `orders`,
   `planets`. (`assets` converted in v0.9.60, `blueprints` in v0.9.61.)

   **`app/character/*` is finished (v0.9.65). What is left is the web layer**,
   and it converts by *router* rather than by module — the opposite of the rule
   that ordered the six character modules, for a measured reason.

   `deps.py`'s three cache readers (`_load_assets_from_cache`,
   `_load_blueprints_from_cache`, `_load_corp_assets_from_cache`) have **12 call
   sites across five routers that are still on `get_conn()`**. Converting the
   readers first makes every one of those open its own `connect()`, in code that
   gets re-touched the moment its router converts — the same trap that put
   `industry_helper` before `location_resolver` back at v0.9.56. The readers are
   leaves and their callers own the connection, so the callers go first and the
   readers come along free.

   Sized, cheapest first — own raw statements / deps-reader sites / `get_conn()`
   handles:

   | unit | stmts | readers | handles |
   | --- | --- | --- | --- |
   | `routers/contracts.py` | **0** | 0 | 3 |
   | `routers/assets.py` | 8 | 3 | 5 |
   | `routers/locations.py` | 8 | 2 | 9 |
   | `routers/planets.py` | 11 | 0 | 4 |
   | `routers/plan.py` | 18 | 2 | 2 |
   | `routers/prices.py` | 13 | 4 | 13 |

   ~~**`routers/contracts.py` first**~~ — **done in v0.9.66**, together with
   `contracts_helper.py`, which is where its SQL actually lived. `assets` is
   next.

   Then `assets` → `locations` → `planets` → `plan`, with the prices cluster
   (`prices_helper` 15 + `routers/prices` 13 + `market/prices` 11 = 39) last as
   its own project. **`deps.py` and `get_conn()` itself go at the very end** —
   `get_conn()` has 59 call sites, so deleting it is the closing act rather
   than a step along the way.

   **Before switching any handler's connection, list the handler's own
   statements — with an AST walk, not a grep.** A raw `conn.execute` inside a
   handler is what broke `/api/contracts/items` in v0.9.62 (see "A conversion
   broke a page"), and a grep under-reported the same thing by half in
   `tests/test_orders_cache.py` (see "Count statements with an AST").

   **`raw = conn.connection.driver_connection` in `app/sync/worker.py` is the
   thermometer.** It exists only for the modules still on `sqlite3`, and every
   conversion removes call sites from it — 13 before v0.9.62, 11 after, 5 after
   v0.9.64. When `grep -c "conn=raw" app/sync/worker.py` reaches zero, that
   whole area is done and the line goes with it.

   **Run the coverage probe first and let it confirm.** It is twenty lines, and
   it has to rebind wrappers into modules that already did `from … import X` —
   see the note further down. It also has a known blind spot worth remembering:
   it reports a function as never-called when the tests monkeypatch it onto the
   *caller's* module, which is why every `fetch_*` shows as dead.

   **Also worth doing, and measured:** **six** cross-backend test files still use
   a function-scoped `engine` fixture, which rebuilds a Postgres schema — all ten
   migrations — per test: `test_app_defaults_on_postgres`,
   `test_industry_helper_on_postgres`, `test_int64_columns_on_postgres`,
   `test_industry_on_postgres`, `test_projects_on_postgres`,
   `test_sde_on_postgres`. Module scope plus clearing tables is ~18x faster.
   `test_character_assets_on_postgres.py` is the worked example. See "The
   cross-backend fixtures are why the suite got slow"; note the last two need
   their cleared-table lists chosen with care, because one seeds `sde_types` per
   module and the other runs the importer.

   `app/character/*` is also what `token_store` sits behind, so read
   `tests/conftest.py` first: this is the area where a test writing to the real
   database cost three characters and their refresh tokens.

   **Run the coverage probe before writing anything.** Twenty lines — wrap every
   function the module defines, run the whole suite, print the ones with a count
   of zero. It found seven untested functions in `industry_helper` and four in
   `location_resolver`, both times including writers, and both times the grep for
   the same names in `tests/` gave a different and wrong answer. The one caveat
   that decides whether it tells the truth is in the note further down: it has to
   rebind the wrappers into modules that already did `from … import X`.

   **Leave a `sqlite_only` marker if this slice ends with a known edge.** That is
   how v0.9.56 handed `location_resolver` to v0.9.58, and it worked better than
   the paragraph beside it — see "A marker that names the next slice" below. The
   marker is still registered in `pytest.ini` with nothing using it, deliberately.

   **Nothing is blocked any more.** `industry_helper` was deferred through
   v0.9.49–0.9.53 only because it has ~26 call sites inside
   `app/web/routers/plan.py`, which a parallel session was editing. That work
   merged at v0.9.54 and the worktree is gone, so `plan.py` is free. `tests/test_app_defaults_on_postgres.py` is the pattern to copy:
   drive the module directly, parameterised over both backends, then mutate the
   portability fix and check the *shape* of the failure. Removing the dialect
   guard from that module's schema shim failed **12 Postgres tests and 0 SQLite
   ones**; that asymmetry is what proves the test can see a backend difference
   at all rather than merely running twice.

   Also worth copying from it: **one assertion that opens a second
   connection.** A converted writer that loses its `commit()` passes every
   same-connection check and drops the write when the request ends — dropping
   the commit from `save_defaults` fails exactly one test, and it is that one.
2. ~~**Cache-only routes**~~ — done in v0.9.55.
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
* ~~**`app/manufacturing/planner.py` leaks two connections.**~~ **Fixed in
  v0.9.55.** `build_plan` now scopes both with a single `with` — the SQLAlchemy
  one directly, the raw `sqlite3` one through `contextlib.closing`. Keeping the
  lesson, because it is the reason the fix is not obvious: a
  `sqlite3.Connection` used as a context manager commits or rolls back and does
  **not** close, so a bare `with` looks like the fix and leaks exactly as much
  as before. The mutation that swaps `closing(...)` back for a bare `with` is
  what proves the test can tell them apart.
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

## Testing the store is not testing the contract

Two of the five PI mutations (v0.9.53) went unnoticed on the first pass, and
both for one reason: the tests exercised `save_cached_colonies` and
`load_cached_colonies` directly, so they proved the *storage* round-trips and
said nothing about what `fetch_colonies` decides to store.

The two things they missed were exactly the decisions:

* **A 403 is cached.** "Forbidden" means the token predates the PI scope,
  which is durable — re-discovering it costs one call per character every tick,
  forever, and the answer does not change until a re-auth. A test that writes
  `FORBIDDEN` itself and reads it back cannot see whether the fetcher ever
  writes it.
* **A failed detail call stays in its slot as None.** `details` is aligned
  positionally with `colonies`; dropping a failure shifts every colony after it
  onto another planet's pins. That is a page which looks entirely plausible and
  attributes your extractors to the wrong worlds. Again invisible to a test
  that hands `save_*` an already-correct list.

The general form: **a cache has two contracts, and they need separate tests.**
The store's contract is "what goes in comes out"; the fetcher's is "what is
worth storing, and in what shape". Round-tripping the first is easy and proves
nothing about the second — which is where every decision lives.


## One test is timing-based and will fail again

`test_several_characters_refresh_concurrently` asserts that three stubbed
200 ms token refreshes finish in under 0.5 s — the point being that they gather
rather than run in series. It is a wall-clock assertion on a shared machine,
and at 740 tests the suite is heavy enough to trip it: it failed once during
v0.9.55 and passed on the next run with no change in between, and it passes
alone every time.

Left as it is rather than loosened. The margin is 0.5 s against an ideal of
0.2 s, so widening it far enough to never flake would stop it distinguishing
concurrent from serial, which is the only thing it is for. **If it fails, re-run
that file alone before believing it** — and if it starts failing alone, that is
a real regression.

## What the last slice added, briefly

* **The AST scan matches on names, so an alias walks past it.** The first
  mutation batch for `/plan` imported `esi_client as _ec` and called `_ec()`;
  nothing noticed, because the scan looks for the literal name. That is a
  mutation testing the scan's blind spot rather than the conversion, and it
  had to be redone honestly. The lasting fix is the recorder-style guard —
  `test_the_plan_pages_do_not_call_a_collection_fetcher` — which an alias
  cannot dodge.
* **Patch the module that calls, for the third time.** The unsynced-character
  test patched `app.character.assets.load_cached_assets` while `plan.py` had
  already bound the name. Same lesson as the two above it, now three for three.
* **`sqlite3.Connection` as a context manager does not close.** It commits or
  rolls back the transaction and leaves the connection open, so
  `with sqlite3.connect(...)` looks like a leak fix and is not one. It needs
  `contextlib.closing`. `build_plan` now uses it, and the mutation that swaps
  it back for a bare `with` is what proves the test can tell them apart.


## int64 ids: a whole class of column is declared too narrow

**Found by running the `industry_helper` rig tests against Postgres for the
first time — eight failures there, none on SQLite.** Every one was
`NumericValueOutOfRange: integer out of range`, from saving a rig configuration
against a station id of 1035466617946.

`station_rigs.location_id` was declared `Integer`. SQLite's INTEGER is a
variable-width 64-bit type and the declaration is advisory, so it stored the
value happily and had done for the life of the project. A Postgres INTEGER is
exactly 32 bits. An Upwell structure id is around 1.03e12, and Upwell structures
are the only things that can carry rigs at all — so on Postgres that table
accepted NPC stations and rejected every station a player could actually
configure.

Fixed for that one column in migration 0009, and for the other **23 in
v0.9.57** (migration 0010). `tests/test_int64_columns_on_postgres.py` writes a
real oversized value into each and reads it back, on both backends.

**The criterion had to change on contact.** The plan was "widen whatever ESI
declares int64". Asking the live OpenAPI spec what CCP actually declares gives:

    location_id int64   character_id int64   type_id int64
    contract_id int64   corporation_id int64  group_id int64
    order_id    int64   item_id       int64   category_id int64

— which is *everything*, `type_id` and `group_id` included. So the declared type
cannot decide this; taken literally it means widening every column in the
schema, which is the reflex the exercise was supposed to avoid. CCP declaring
int64 means they have reserved the room, not that they are using it.

What decides it is the range the values actually occupy, so that got measured —
static ids from the SDE, live ids sampled from public ESI endpoints:

| id | highest actually seen | against the int32 ceiling |
|---|---|---|
| `type_id` | 371,027 | 0.017 % |
| `solar_system_id` | 30,030,141 | 1.4 % |
| `contract_id` | 234,465,667 | 10.9 % |
| `corporation_id` | 2,042,491,468 | **95.1 %** |
| `character_id` | 2,124,549,094 | **98.9 %** |
| `order_id` | 7,407,646,135 | **3.4x over** |
| `location_id` (structure) | 1,049,982,731,184 | **489x over** |
| `volume`, 7 days of Tritanium | 34,190,149,437 | **15.9x over** |

Two things fell out of measuring that no amount of reading would have given:

* **The market volume columns were the worst of it, and were not on the list at
  all** — the list was about ids. `market_price_cache.volume` is seven days of
  regional trade and `jita_available` is every Jita sell order's remaining units
  summed; both are over the ceiling today for Tritanium, Pyerite and Mexallon,
  which is most of what an industry tool prices.
* **`margin_snapshot.item_id` must not be widened.** It is `margin_watchlist.id`
  — our own autoincrement row number, not an EVE asset id. It is the one column
  whose name would have got it swept in by a name-based pass.
* **`character_id` is the only preventive one.** 2,124,549,094 still *fits*, so
  a test writing a real id passes with or without the fix. The test writes
  2,200,000,000 instead — the id of a character created a little way into the
  future, near enough that ~23 million signups close the gap. That is stated in
  the file rather than glossed, because a test that cannot fail is not evidence.

Left alone, and the reasons are in migration 0010's docstring: `type_id`,
`group_id`, `category_id`, `region_id`, `solar_system_id`, `planet_id`,
`contract_id`, every `id` we mint ourselves, and the count columns. `quantity`
is the one to re-check one day — ESI declares it int64 and an asset stack can
hold billions of units — but nothing was measured over the ceiling.

**The general lesson, which is the reusable part:** SQLite ignores the width in
a column declaration and Postgres enforces it, so *every* integer column in this
schema is unverified until something writes a real-world-sized value to it on
Postgres. Type declarations are not portable claims; they are portable only once
a test has pushed a realistic value through them.

## Mutating the wrong artifact proves nothing, quietly

Reverting `station_rigs.location_id` to `Integer` **in the declaration** failed
nothing on either backend, and the first reading of that was "the test is
decorative". It was not: the Postgres fixture builds its tables with
`upgrade_to_head`, so the column type there comes from the *migration*, and the
declaration is only consulted by `create_all` and by
`test_the_migrations_match_the_declaration`.

Two artifacts, two separate claims, and each needs its own mutation:

* neuter migration 0009 → **8 Postgres failures, 0 SQLite**. That asymmetry is
  the evidence that the cross-backend half tests a backend difference rather
  than running the same assertions twice.
* revert the declaration → **1 failure**, in `test_migrations.py`, from the
  guard that keeps declaration and history in step.

Before concluding a mutation proves an assertion is decorative, check that it
changed the artifact the test actually reads.


## A vendor's declared type is a ceiling, not a measurement

The int64 sweep (v0.9.57) nearly went wrong in a way worth remembering, because
the same shape will come up for every other "make it portable" question.

The plan said: verify each column against ESI's declared type. That sounds like
the rigorous option — go to the source of truth rather than guess. The source of
truth answered "int64" for every integer field it has, `type_id` and `group_id`
included, and following it literally would have widened the entire schema while
feeling principled about it.

A declared type says what the vendor has reserved the right to send. It says
nothing about what they send. The question "does this column need 64 bits" is
answered by the range the data occupies, and that has to be measured: the SDE
for static ids, public ESI endpoints for live ones. Twenty minutes of sampling
turned a schema-wide rewrite into 23 columns with a stated reason each, and
turned up the two things reading could not have: that the worst offenders were
the market volume columns, which were not ids at all, and that one column named
`item_id` holds our own row number and had to be left alone.

**When a specification and a measurement disagree about scope, the
specification is telling you the worst case and the measurement is telling you
the problem.** Design for the worst case where it is free; act on the
measurement where it is not.


## The boundary count came back down: 5 → 1

`location_resolver` landed in v0.9.58 and every crossing it was holding open
went with it — three from inside `industry_helper`, one from
`routers/locations.py`, one from `routers/industry.py`. What is left is a single
`dbapi()` in `routers/industry.py:64`, and it points at `app/character/*`.

The prediction from v0.9.56 held: the number rising then was the boundary moving
*down* a level rather than nothing happening, and a module that collects
boundaries is a module whose conversion pays for several at once.

**The cost landed where it was predicted to.** Nine caller files, ~39 call
sites. `locations.py` got four small helpers rather than inline `with` blocks,
because `suggest_station` touches the cache six times across a 140-line body and
wrapping it would have reindented a handler this change had no other reason to
touch — which is how a mechanical diff becomes an unreviewable one.

## A marker that names the next slice beats a paragraph that describes it

v0.9.56 left exactly one test marked `sqlite_only`, on
`test_the_computed_percentage_stacks_multiplicatively`, because a station with
rigs fitted routed through `get_station_security_multiplier` and that still
spoke `?`. The marker *was* the definition of done for this slice: delete it, and
if it passes on both backends the module is finished.

It worked better than the worklist paragraph next to it. A paragraph goes stale
silently; the marker is executable, sits in the file the next person is already
reading, and fails the moment the claim behind it stops being true. Worth
repeating: when a slice leaves a known edge, mark the test that sits on it rather
than only writing it down here.

The marker itself stays registered in `pytest.ini` with nothing using it. That is
deliberate — the next module will want it.

## Blanket exemptions in a scanner hide the thing the scanner is for

Converting `location_resolver` tripped `test_no_insert_or_replace_survives`, on a
**docstring** that explained why `INSERT OR REPLACE` had been removed. Not a use
of it.

The scan had met this twice before and patched around it twice: once with a
"line starts with `#`" check, once by skipping the whole of `app/db/schema.py`
because it "documents it in a docstring". The second of those is the expensive
kind of fix — it exempted an entire module, so a real offender anywhere in the
schema file would have gone unreported for as long as the exemption stood.

Replaced with the precise version: blank out docstring bodies via the AST
(keeping line numbers), scan everything else, and drop the whole-file skip.
Verified rather than assumed — planting a real `INSERT OR REPLACE` in
`location_resolver.py` **and** in `schema.py` is caught in both, and prose
mentioning it is not.

**The general form:** when a check produces a false positive for the third time,
the fix is to make the check understand the distinction, not to add a third
exemption. And an exemption scoped to a whole file is a hole, not a tuning.


## app/character/* is a third untested, and the probe says which third

Run over all eight `app/character` modules plus `app/auth/token_store.py`:
**35 of 102 functions are never executed by the suite.** Not "lightly covered" —
never called at all. The dead set is weighted towards writers and fetchers:

* `assets`: `fetch_assets`, `fetch_corp_assets`, `_save_cache`,
  `_save_corp_cache`, `_load_corp_cache`, `assets_at_location(s)`, both
  `ensure_*_table`
* `blueprints`: `fetch_blueprints`, `_save_cache`, `ensure_bp_table`
* `skills`: `fetch_skills`, `fetch_skill_queue`, `fetch_location`, `_save_cache`,
  `_load_cache_fresh`, `_parse_blob`, `get_mfg_skill_ids`, `ensure_skills_table`
* `contracts`: `fetch_public_contracts`, `fetch_public_contract_items`,
  `fetch_corp_contract_items`, `_fetch_public_page`, `status_label`, `type_label`
* `wallet`: `fetch_corp_journal`, `fetch_corp_transactions`
* `jobs`: `save_cached_jobs`, `activity_label`
* `token_store`: `ensure_characters_table`, `update_corporation_id`,
  `update_last_sync`, `_migrate_legacy_json`, `_strip_token_fields`

**The probe is not lying about the fetchers, and it is worth understanding why
before dismissing the number.** `tests/test_sync_worker.py` monkeypatches
`fetch_assets`, `fetch_blueprints` and friends *onto the worker module*, so the
worker's orchestration is well tested and the real fetchers never run. That is a
reasonable way to test a worker and it leaves the fetchers uncovered; both things
are true at once.

**So this is several slices, not one.** The first — `token_store`,
`character/jobs`, `character/skills` — is the one the last `dbapi()` boundary
needs, and its tests are in `tests/test_character_caches_on_postgres.py`
(named `test_character_caches.py` until the modules converted). The rest
(`assets`, `blueprints`, `contracts`, `wallet`, `orders`, `planets`) can follow
one at a time; none of them holds a boundary open, so there is no pressure to
bundle them.

**Two things in that first slice are riskier than anything so far:**

* `token_store` owns the `characters` table, which holds refresh tokens — the
  table a test once wrote to for real. `config_path()` resolves per call from
  `EVE_APP_DIR`, and that plus the `pytest_collection_finish` guard is what makes
  the JSON-migration tests safe to write. The tests also save and restore
  `.eve_config.json`, because it lives in the shared test app dir rather than in
  `tmp_path` and `get_client_id()` reads it.
* **`save_cached_jobs` does not commit** — its only caller,
  `fetch_industry_jobs`, commits for it. Pinned by
  `test_the_commit_for_a_job_save_lives_in_the_fetcher`, because both halves are
  easy to get wrong: adding a commit inside the writer moves the transaction
  boundary, and dropping the caller's loses the write with no symptom.

## Mutation scripts: write bytes, or they rewrite line endings

The mutation harness restores its target with `pathlib.write_text`, which uses
`os.linesep` — so on Windows it hands back CRLF whatever it read. For a file
already stored CRLF that is invisible. For `token_store.py` and
`character/jobs.py`, which were LF on disk, it flipped them, and `git status`
showed both as modified while `git diff` showed nothing at all, because git
normalises on the way to the index.

Harmless, but confusing at exactly the wrong moment — it looks like a mutation
that failed to restore. `git checkout --` on the paths clears it. Better: have
the harness read and write bytes, or pass `newline=""`.


## The boundary count reached zero (v0.9.59)

`token_store`, `character/jobs` and `character/skills` converted together,
because the single remaining `dbapi()` fed all three. The last line of it turned
out to be a **dead assignment**: `raw = dbapi(conn)` in `jobs_page`, whose three
uses had already moved. Deleting a variable took the metric to zero, which is a
fittingly undramatic ending for something that has been tracked since v0.9.43.

**Two portability bugs came out of it**, both hidden the same way — a
driver-specific `except sqlite3.OperationalError` around a statement that is
allowed to fail:

* `delete_character` cascades into three per-character cache tables, any of
  which can be absent on an older database. On SQLite swallowing that was
  correct. On Postgres a failed statement aborts the whole transaction, so the
  two remaining DELETEs *and the commit* would fail — leaving the character, and
  its refresh token, in place. It uses `recover_from_missing_table` now, and
  re-issues the character DELETE that the rollback discards.
* `get_mfg_skill_ids` tolerates an SDE that has not been imported yet. Same
  shape, and the damage would have surfaced in whatever query ran next rather
  than here.

Both are pinned, and both mutations fail **Postgres only** — which is the whole
point: the code they replace was correct on the backend anyone was running.

The second of those needed the test strengthening before it could catch
anything. Asserting only the return value passes with the rollback removed,
because the return value is right either way; the test has to make *another*
query on the same connection, which is exactly the failure mode
`recover_from_missing_table` exists to prevent.

**`deps.py` was the leverage point.** Three connection-owning read helpers
there — `all_characters()`, `character_row()`, `any_character()` — turned about
forty router call sites into a mechanical substitution instead of forty hand
edits. The five active-character helpers now always open their own engine
connection and their `conn` parameter is accepted and ignored, documented as
such; removing it is forty more edits in files this change had no reason to
open, and it goes when those routers convert.

**`_valid_token_async` needed its connection opened inside the worker thread.**
The comment already said why for sqlite3 — objects belong to the thread that
made them — and a SQLAlchemy Connection is no more shareable.

## Two tests that were passing for the wrong reason

**`assert all(results)` is vacuously true on an empty list.** In
`test_token_refresh_is_serialized`, six worker threads each died with a
`TypeError`, `results` stayed empty, `all([])` returned True, and the test
reported "expected 1 real refresh, got 0" — which reads as a serialization
problem rather than as six crashes. It asserts `len(results) == len(threads)`
first now. Any test that collects results from threads or a gather needs that
guard; the aggregate assertion cannot distinguish "all fine" from "none ran".

**A fast suite is not the same as a passing suite.** With Docker down, every
Postgres parameterisation *skips*, so the run gets much faster while testing
half of what it claims. A 15-second reading that looked like a fixture
optimisation was really the backend being absent. Look for a block of `s` before
believing a quick run.

## The cross-backend fixtures are why the suite got slow

Each `engine` fixture is parameterised over both backends, and a function-scoped
one drops and rebuilds a Postgres schema — all ten migrations — **for every
test**. The 63 tests added in v0.9.59 nearly doubled that and pushed the suite
past ten minutes.

Module scope, plus emptying the tables between tests, is the same isolation for
far less: measured **0.31 s/test** function-scoped against **0.017 s/test**
module-scoped, an 18x difference. These tests only ever assert on rows they
insert themselves, so clearing is enough — and clearing *before* each test, not
after, so a test that dies half-way cannot feed the next one its rows.

Done for `test_character_caches_on_postgres.py` and
`test_location_resolver_on_postgres.py`. **Still function-scoped, worth about
38 seconds between them:** `test_app_defaults_on_postgres`,
`test_industry_helper_on_postgres`, `test_int64_columns_on_postgres`,
`test_industry_on_postgres`, `test_projects_on_postgres`,
`test_sde_on_postgres`. Note `test_industry_helper` seeds `sde_types` per module
and `test_sde_on_postgres` runs the importer, so those two need their cleared-
table lists chosen with more care than the others.

Full suite as of v0.9.66: **4m30s** with Postgres up and 1,434 tests, against
roughly three minutes before any cross-backend file existed. It peaked over ten
minutes before the two biggest files went module-scoped.


## character/assets converted (v0.9.60)

Nine of its functions had never been executed by the suite — both cache writers,
both fetchers, both `ensure_*_table` shims, both location roll-ups and the corp
cache reader. Tests first, then the conversion, and the assertions came through
unchanged onto the cross-backend fixture.

Three of those nine were carrying behaviour worth naming:

* `_save_cache` is **DELETE then INSERT**, not an upsert. One row per character
  is the invariant; without the delete a second save leaves two and "which one
  wins" becomes a question about row order.
* **Two readers of one table with opposite TTL rules.** `load_cached_assets`
  ignores `CACHE_TTL` on purpose and `_load_cache`, which the fetcher uses,
  enforces it — because applying the TTL on the read path made an aged cache
  indistinguishable from an empty one, so the page fetched.
* `save_cached_container_names` does not commit, and neither does
  `fetch_container_names`. The worker's per-character block does.

## A test can pass because the limit it names is not the limit

`load_cached_container_names` chunks its `IN (...)` at 900, and the comment said
that was because "SQLite's parameter limit is 999 by default". Measured, because
the mutation that removed the chunking failed nothing:

    SQLITE_LIMIT_VARIABLE_NUMBER: 32766
      32766 placeholders: OK
      32767 placeholders: too many SQL variables

999 has been wrong since SQLite 3.32 in 2020. So a test asking for 1,500 ids —
written as "more containers than the parameter limit" — was nowhere near the
limit and passed with the chunking deleted.

The chunking is still right: the cap is a **compile-time setting**, some
distribution builds do ship 999, and Postgres caps at 65,535. What changed is
the test, which now lowers `SQLITE_LIMIT_VARIABLE_NUMBER` to 999 for the
connection. That reproduces the build the chunking exists for at a realistic
number of containers instead of needing 33,000 of them, and it is the one test
in the file marked `sqlite_only` — there is no Postgres equivalent of lowering
a SQLite compile-time limit.

**The general form:** when a test names a threshold, check the threshold is
real. "More than the limit" is an assertion about the environment, and
environments move.

## The mutation harness was filtering on a substring, not a parameter

The batteries split backends with `pytest -k sqlite` and `-k postgres`. That
matches the **test name** as well as the parameter id, and
`test_the_schema_shims_are_a_no_op_off_sqlite[postgres]` contains both words —
so the "sqlite" run included a Postgres case, and two asymmetric mutations
reported as failing on both backends when they only failed on one.

The fix is `-k "[sqlite]"` / `-k "[postgres]"`, matching the bracketed parameter
id. Worth carrying into every future battery, along with the smaller habit of
not putting a backend name in a test name.

It cost nothing this time because the wrong answer was *more* alarming than the
truth. It would cost a great deal in the other direction — a mutation that
fails only Postgres, reported as failing both, looks like a portable bug rather
than the backend difference it is.


## character/blueprints converted (v0.9.61)

Three of its functions had never been executed by the suite — `_save_cache`,
`_load_cache` and `ensure_bp_table`, the cache writer among them. Tests first
(37 of them), then the conversion, and the assertions came through unchanged
onto the cross-backend fixture.

The traps were the same family as `assets`, which is what made it quick:

* `_save_cache` is **DELETE then INSERT**, and here the reason is visible in the
  schema — `char_blueprints_cache` has no primary key and no
  `UNIQUE(character_id)` to upsert against. Without the delete a second save
  leaves two rows and `fetchone()` picks one by row order, which the two
  backends need not agree on.
* **Two readers of one table with opposite TTL rules**, exactly as in `assets`:
  `_load_cache` enforces `CACHE_TTL` for the fetcher, `load_cached_blueprints`
  ignores it for the pages.
* `_save_cache` **commits**; `fetch_blueprints` does not commit separately.

Call sites moved with it: `routers/assets.py` (two), `routers/plan.py`,
`routers/auth.py`, and `sync/worker.py`, where the blueprints fetch stopped
using `raw` and took the engine connection directly.

Conversion mutations: **8/8 in the expected shape**, the dialect guard giving
the asymmetric signature that matters — 2 Postgres failures, 0 SQLite.

## A live crash path in load_cached_blueprints

`load_cached_blueprints` catches `ValueError`/`TypeError`/`KeyError` and returns
`(None, 0.0)`, so a corrupt cache reads as never-synced rather than as an empty
hangar. Writing the test for that found a hole: a payload that **parses** and is
not a list of dicts raises `AttributeError`, which the tuple does not catch. It
escapes to `/assets`, `/blueprints` and `/plan` as a 500.

The reason to believe it is an oversight rather than a decision:
`app/web/margins_helper.py`, the only other caller of `_parse_blueprints`,
already filters `isinstance(entries, list)` and `isinstance(e, dict)` before
calling it. That path was hardened and this one was missed.

Only reachable through a cache written by something other than `_save_cache`,
which is why it has not been seen in the wild. **Pinned, not fixed** —
`test_a_non_dict_entry_escapes_the_handler` asserts the current behaviour, so
widening the tuple has to be a deliberate commit that updates the test rather
than something smuggled into a conversion.

## A test can be weaker than its own name

The first mutation battery came back 26/27. The miss was the test, not the code:
a mutation that changed `item["item_id"]` to `item.get("item_id", 0)` failed
nothing, because the payload written as "an entry missing item_id" was also
missing `location_id` — so it still raised `KeyError`, just from a different
line, and the test still passed.

Now there is one case per required key, each dropped **on its own**, and the
three mutations that give each key a default are each caught by exactly the case
that names it.

**The general form:** a fixture that violates several rules at once only proves
that *at least one* of them is enforced. If a test names one condition, the
input should differ from a valid one in exactly that condition — otherwise the
assertion is anchored to whichever rule happens to fire first, and that is not
the one in the name.

## The ensure_*_table shims have no callers

Traced while converting: `ensure_bp_table`, `ensure_assets_table` and
`ensure_corp_assets_table` are imported once each, in `app/web/main.py`, and
never called. pyflakes has been reporting all three as unused imports.

They are dead, but deleting them is its own change and it is not free —
`ensure_bp_table` is the only thing that creates the table on a bare SQLite file
outside Alembic, so anything relying on that path has to be found first. Left in
place, converted with the dialect guard like the rest. **Worth doing once the
conversion is finished**, together with the other dead imports pyflakes lists in
`main.py`.


## character/contracts converted (v0.9.62)

290 lines but only **four SQL statements** — the public-contract half of the
module talks to ESI and never to a database, and `app/web/contracts_helper.py`
uses only those public fetchers, so it is untouched by this.

The traps, all pinned before the rewrite:

* **Both writers are upserts**, not DELETE-then-INSERT, and `contracts_cache`
  conflicts on the **composite** key `(owner_id, owner_kind)`. A wrong conflict
  target does not raise — it inserts a second row, and one owner quietly has
  two contract lists with nothing to choose between them.
* **Neither writer commits.** The caller owns the boundary.
* **`_get_all_pages` tells "ESI is down" from "no contracts"**: page one failing
  returns `None`, a later page failing returns what already arrived. That is
  what stops a transport blip being cached as an empty contract list.
* **`contract_items_cache` has no expiry and nothing refreshes it**, so an `[]`
  written after a failed expand shows that contract as empty permanently.

Call sites: `routers/contracts.py` in two handlers, and the worker's two
fetches. The router change was one word each — `connect()` returns a
`Connection` that supports `.close()`, so it drops straight into handlers built
around `conn = get_conn()` and their four `conn.close()` paths, with no
reindentation. The three *public* handlers still use `get_conn()`; they go
through `contracts_helper`, which is a separate module on the later list.

**Worth knowing for the handlers that convert next:** in `contracts_page`,
`load_cached_contracts` was the only real consumer of `conn`.
`_finalize_contracts(conn, ...)` never touches its `conn` — it opens its own
`_connect()` — and `get_active_character_id` documents that it accepts and
ignores one. That dead `_finalize_contracts` parameter is worth removing when
`routers/contracts.py` itself converts.

## Splitting a mutation battery by backend with `-k` does not work

Both spellings are wrong, and both produced a wrong answer before being caught:

* `-k sqlite` matches the word anywhere, including in a **test name**, so a
  "sqlite only" run silently includes Postgres cases. (Found at v0.9.60.)
* `-k "[sqlite]"` is a literal substring **including the closing bracket**. It
  matches `test_x[sqlite]` but not `test_x[sqlite-second_param]` — so any test
  parametrized on a second axis drops out of **both** halves and runs in
  neither.

The second one cost a real result here. Eight item-fetcher cases in
`test_character_contracts_on_postgres.py` never ran, and the mutation written
to catch them — "the character item fetcher stops writing the cache" —
reported as caught by nothing. It looked like a missing assertion. The
assertion existed and was being skipped.

The same filter had been used on the `blueprints` battery one commit earlier,
where it selected 33 of 39 tests per backend. That battery's "8/8" was measured
on 66 of 78 cases and has been re-run.

**Run the file once and classify each `FAILED` line** by whether its id
contains `[sqlite` or `[postgres`, with no closing bracket. No blind spot, and
one pytest invocation instead of two. Then assert every failure matched one of
the two — a failure matching neither means the id format has moved and the
classifier is lying rather than reporting zero, which is the same class of
silent-zero this whole note is about.

**The general form, and it is the third instance today:** a filter that selects
nothing looks exactly like a check that finds nothing. `-k`, a grep with an
over-tight anchor, and a `--collect-only` count all fail this way. When a
selector decides what gets measured, measure the selector — count what it
returned and compare it against what you expected to be there.


## A conversion broke a page, and the tests did not notice (v0.9.63)

v0.9.62 moved `api_contract_items` from `get_conn()` to `connect()` and left one
raw statement behind it **in the router**:

```python
ph = ",".join("?" * len(tids))
conn.execute(f"... WHERE type_id IN ({ph})", list(tids))
```

A SQLAlchemy connection rejects that with `ArgumentError: List argument must
consist only of dictionaries`. `/api/contracts/items` raised for every contract
that actually had items — the normal case — and it was pushed.

**Why the suite stayed green.** The test net for that conversion covered
`app/character/contracts.py`: 44 tests, 29/29 mutations. This code is in
`app/web/routers/contracts.py`. The module was thoroughly tested and the handler
calling it had no test at all; nothing in the suite requested
`/api/contracts/items`.

Three things worth carrying forward:

* **Converting a module is not the risky part — converting its callers is.** A
  caller can hold statements of its own that the module's tests never see.
  Before switching a handler's connection, `grep -n "conn.execute" ` the handler
  and convert what is there in the same change. That grep now happens *before*
  the conversion rather than after it.
* **A wrong parameter style fails more quietly than a wrong query.** It raises
  only when the branch that binds parameters is entered. Here that needed a
  contract with items rather than one that merely existed, so the empty-cache
  path passed throughout.
* **A converted handler needs one test that exercises its populated path.**
  `tests/test_contract_items_route.py` is that test for contracts. `/wallet`
  already had one — `tests/test_wallet_filter.py` renders the page over seeded
  journal and transaction rows, which is what made the same class of bug
  impossible to ship in v0.9.64.

## character/wallet converted (v0.9.64)

Four SQL statements, and the widest conflict target in the codebase:
`ON CONFLICT (owner_id, owner_kind, division, ledger)`. Getting any one part
wrong does not raise — it inserts a second row, and the failure surfaces as a
wallet tab showing another division's money, which looks entirely plausible.

The three corporation fetchers had **no test at all**, and they are the only
callers that vary the half of the key the character paths never touch
(`kind="corporation"`, `division` 1–7). The new file has one mutation per key
part, each dropped on its own — a conflict target that breaks several rules at
once only proves that at least one is enforced.

Corporation *balances* break the division rule on purpose: ESI returns every
division in one response, so they are stored once at `division=0` under
`ledger="balances"` rather than split across seven rows that would all have to
be written together to stay consistent. Division 0 is also the character's slot;
`owner_kind` is what keeps those two apart, and that is now pinned.

Call sites: six in the worker, and `wallet_page` in `routers/characters.py` —
which held one raw `sde_types` lookup of its own, found by grepping the handler
*before* converting it rather than after. `conn=raw` in the worker: 11 → **5**.

Checked rather than assumed: `char_wallet_cache.balance` is `double precision`
on Postgres and an 8-byte REAL on SQLite, so a trillion-ISK balance survives to
the cent on both. There is a test that says so, because the failure mode of a
column quietly becoming single precision is a number that still looks like a
balance.

**One departure from the usual discipline, noted rather than hidden:** the test
net and the conversion share this commit. The tests were written and
mutation-verified against the `sqlite3` version first, as always — 31/32 from
the new file alone, and 32/32 once `tests/test_orders_cache.py` is included,
which already covered the balance writer — but unlike `blueprints` and
`contracts` there is no separate tests-first commit to point at.


## character/orders and character/planets converted (v0.9.65)

The last two of the six, and `app/character/*` is now entirely on the portable
query layer. `conn=raw` in the worker reached **zero**, and the
`raw = conn.connection.driver_connection` line went with it — the comment that
replaced it records why the property it protected (an event never announced
before the data it describes) still holds with one connection rather than two
views of one.

`orders`: three-part key `(owner_id, owner_kind, state)`, 23/23 mutations. Its
`orders_page` held one raw `sde_types` lookup, found by grepping the handler
first — the step that was skipped in v0.9.62.

`planets` was the awkward one, and not because of the module. Its **router** is
heavily raw: about a dozen statements spread over `_load_pi_colonies`,
`_pi_cache_age`, `_resolve_planet_names`, `_store_pi_cache_for_chars`,
`_pi_refresh_alerts` and `_pi_alert_summary`. Converting it wholesale would have
been a second change wearing the first one's clothes, so the four `planets_api`
call sites take their own engine connection and the router keeps `get_conn()`
for its own statements. `load_planet_names`' chunked `IN (...)` became an
expanding bindparam, the same shape `assets` uses.

## The test scaffolding came out with it

`tests/test_orders_cache.py` exercises all six of these modules, so with the
last one converted its `conn` fixture is now an **engine connection** and the
raw sqlite3 handle it used to hand out has no consumer. The file no longer
imports `sqlite3` at all.

* `wconn`, the shim added for the wallet tests, is gone — 39 uses renamed back
  to `conn`.
* `_engine_conn` survives as a `nullcontext` so its twelve `with` blocks stay
  as they are. Unwrapping them is a pure reindent with no behaviour in it, and
  doing it inside the conversion commit would bury the part worth reading. It
  is documented as vestigial rather than left looking load-bearing. **Worth
  finishing** the next time that file is opened for another reason.

## Count statements with an AST, not a grep

`grep 'conn.execute("'` found **two** raw statements in
`tests/test_orders_cache.py`. An AST walk found **four** — the two it missed
were spread across lines, which is how most of them are written.

That is the third instance of one failure mode today, after the `-k "[sqlite]"`
filter that silently selected 37 of 45 tests and the bare `grep -rn "dbapi("`
that answered 2 on a clean tree. **A selector that quietly under-reports looks
exactly like a clean result.** The fix is the same each time: measure the
selector, not just its output — count what it returned and compare it against
what you expected to be there.

The walk that gives the real number, and the one the count above uses:

```python
import ast, pathlib
for f in pathlib.Path("app").rglob("*.py"):
    for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("execute", "executemany") and node.args
                and isinstance(node.args[0], (ast.Constant, ast.JoinedStr))):
            print(f, node.lineno)
```

`ast.Constant` catches the plain strings and `ast.JoinedStr` the f-strings —
which is where the `IN ({ph})` placeholder patterns live, and those are the ones
that need an expanding bindparam rather than a mechanical rewrite.

## Where the remaining statements are (145 at v0.9.65, 137 after v0.9.66)

| cluster | statements |
| --- | --- |
| `routers/plan.py` | 18 |
| prices — `prices_helper`, `routers/prices`, `market/prices` | 39 |
| PI — `routers/planets`, `pi_planner_helper` | 18 |
| `main.py`, `deps.py` | 17 |
| `routers/assets`, `routers/locations`, `contracts_helper` | 24 |
| infrastructure — `bootstrap`, `security`, `schema`, `conn` | 19 |
| the rest | 10 |

**Take `deps.py` next, and not because it is biggest.** Three of the four
conversions in this session tripped over *callers* rather than modules: dead
`conn` parameters in `_finalize_contracts`, `_wallet_names`, `_decorate` and
`_finalize_orders`, and the `/api/contracts/items` regression. `deps.py` is
where `get_conn()` itself lives, so converting it forces those dead parameters
out in one place instead of leaving them to be rediscovered router by router.


## contracts_helper and routers/contracts converted (v0.9.66)

The first unit of the web layer, and it confirmed the ordering rule: the router
had **zero** raw statements of its own — its one was converted in the v0.9.63
fix — because the SQL all lives in `app/web/contracts_helper.py` behind it. The
unit is helper + router, not router alone.

**The risk in this file was never SQL dialect. It was parameter order.**
`search_public_contracts` builds its WHERE clauses conditionally and used to
append each value to a positional list as its clause fired, with the `LIKE`
first, then the type, then the price, and the `LIMIT` last. The binding order
was therefore a property of the *branch* order, and a mispairing returns
**plausible rows rather than an error** — the failure mode with no symptom. The
values now go into a dict keyed by name, which makes that class of bug
unrepresentable.

That is also why the test net went in first with one test per filter *and* one
with all three on, over a fixture where exactly one contract legitimately
matches all three. A mispaired parameter widens that result visibly.

The battery found two gaps in the net before the conversion, both mine:

* `get_contract_items` ignoring its `WHERE` was invisible, because every test
  in that section stored exactly **one** contract with items. Two contracts is
  the smallest fixture that can tell "the right items" from "all the items".
* A zero-quantity contract line, treated as one unit rather than skipped,
  becomes the cheapest thing in the region and wins every price comparison.
  ESI does return zero-quantity lines. `price / qty` also raises on it.

30/30 on the pre-conversion battery, 12/12 in shape on the conversion one.

## The SDE tables are not in the migration history, on purpose

Worth knowing before writing any cross-backend fixture that touches them:
`0001_baseline` deliberately excludes `sde_*` and says so — CCP drops and
rebuilds the static data wholesale on every SDE build, so it is created by
`apply_sde_schema()` and kept out of the history.

The consequence for tests is that `upgrade_to_head` alone gives a database with
no `sde_types`, and `search_public_contracts` joins it while
`get_contract_items` LEFT JOINs it for the `#id` fallback — so a fixture with
only the migrations silently loses both behaviours rather than failing loudly.

`apply_sde_schema()` is no use on Postgres either: it compiles its DDL against
the SQLite dialect explicitly. Going through the metadata gives the same tables
in whichever dialect the engine is bound to:

```python
from app.db.schema import metadata, SDE_TABLES
metadata.create_all(eng, tables=[metadata.tables[n] for n in sorted(SDE_TABLES)])
```

`tests/test_contracts_helper_on_postgres.py::_build_sde_tables` is the worked
example.

## contract_id is two widths, deliberately

`public_contracts.contract_id` is `Integer` while
`contract_items_cache.contract_id` is `BigInteger` — the same identifier at two
widths, which reads like an oversight and is not. The v0.9.57 int64 audit
measured the largest real contract id at **234,465,667, or 10.9%** of the int32
ceiling, against `character_id` at 98.9% which is why *that* one was widened.

`test_public_contract_ids_are_int32_on_purpose` carries the reasoning so the
next person to notice finds it attached rather than re-deriving it. If contract
ids ever approach 2**31 both columns move together — and note the asymmetry
runs in the safe direction: the character-side cache already accepts values the
public-contract side would reject.
