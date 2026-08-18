# Step 4 — working checklist

Delete this file when Step 4 closes. It is a worklist, not design: the design
lives in [design-hosted-v2.md](design-hosted-v2.md) §11.

## Where the last session ended

* Branch `docs/hosted-v2-design` at **5911a72**, pushed to `fork`. `origin/main`
  untouched at `7cce487`.
* **438 tests green.** 8 skip unless a Postgres is reachable.
* Step 4 is **2 of 8** items, plus most of the Postgres groundwork.
* Local `eve_cache.db` is stamped at baseline `5c9156e72c43`, all six SDE
  indexes present, 3 characters intact.

To bring the Postgres tests back:

```bash
docker run -d --name eve-pg -e POSTGRES_PASSWORD=eve -e POSTGRES_USER=eve -e POSTGRES_DB=eve_retroindustry -p 55432:5432 postgres:17
```

## Order, and why it is this order

1. **W6 first.** 105 of the ~316 statements are inside `main.py`. Converting
   them there, in a 7,112-line file, during a cutover that cannot be done
   halfway, is the highest-risk way to do it.
2. **Then the query conversion.** It is atomic — `get_conn()` is shared by
   every module, so the moment it returns a SQLAlchemy `Connection`, every
   query must already speak named binds. Small reviewable routers make that
   survivable.
3. **Then the worker and the rest**, which are new code rather than moves.

---

## 1. W6 — split `main.py` into routers

**Net:** `tests/test_route_inventory.py`. 80 routes by exact path and method,
no duplicates, every handler named. Mutation-checked.

**Method: one router per commit.** Move, run `pytest
tests/test_route_inventory.py`, run the full suite, commit. Each step is
independently revertable — which matters, because step 2 is not.

**Do this before moving any route.** The routers will all need `get_conn`,
`_tr`, `_isk`, `_isk0`, `_wants_html` and friends, which are module-level in
`main.py` today. Moving a route without moving those first means either a
circular import or a copy. Give them a home — `app/web/deps.py` — as commit
one, with `main.py` importing from it, and no routes moved at all. That commit
should be a pure no-op to the route table.

**Suggested order**, leaf-most first so each move touches fewer shared helpers:

| # | Router | Routes | Notes |
|---|---|---|---|
| 1 | `prices` | 12 | includes 3 SSE streams — check `StreamingResponse` still returns from a router |
| 2 | `projects` | 8 | |
| 3 | `contracts` | 5 | |
| 4 | `planets` / PI | 3 | |
| 5 | `characters` | ~10 | assets, wallet, orders, blueprints, portrait |
| 6 | `industry` | ~10 | station rigs, plan, jobs |
| 7 | `auth` + `setup` | 8 | `/callback` sets the session cookie — exercise a real login after |
| 8 | remainder | ~24 | dashboard, settings, margins, reactions, about, sync |

**Stays in `main.py`:** the `app` object itself (the `app_module` test fixture
imports it), the startup handler, and the `_setup_gate` / Host-check
middleware.

**Traps:**

* Templates call `url_for` with **handler names**. Renaming a function while
  moving it breaks a page with no test failure. The inventory test checks names
  exist, not that they are unchanged — so do not rename.
* `_SDE_READY` and the other module-level `list[bool]` flags are read by both
  the middleware and the routers. They belong in `deps.py`, not duplicated.
* Two decorators on one path is the classic botched move. FastAPI serves the
  first and the second becomes live-looking dead code; the inventory test
  catches it.

**Done when:** `main.py` is plausibly under ~1,000 lines, the inventory test is
untouched and green, and W6 can be marked closed in the worry register.

---

## 2. The query conversion — atomic

**Target:** `app/db/conn.py`, already proven on both backends
(`tests/test_db_conn.py`, `tests/test_postgres_schema.py`).

`?` + tuple → `:name` + dict, then `get_conn()` returns a SQLAlchemy
`Connection`. Roughly 316 statements.

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
  so the readers of results do not have to change.
* **`upsert()` still emits `?`** by design, because its callers pass tuples. It
  has to start emitting named binds in the same commit as its call sites.
* **`cursor.lastrowid`** has no direct equivalent; use `RETURNING id`.

**Verify:** full suite on SQLite, then the same suite with
`EVE_DATABASE_URL` pointed at the container. Both must pass before the commit
lands.

---

## 3. Remaining Step 4 items

Ordered by dependency, not by size.

* **W9 — async token refresh.** The current refresh is synchronous and blocks
  the event loop; this is the v0.9.22 bug class and it is still live.
* **Background sync worker** — delay-after-completion plus jitter, so N
  characters do not stampede ESI in lockstep. **Decide the event model before
  writing it**: §9.5 needs it to emit events rather than only fill caches,
  because a bot that polls the database for changes is a bot that misses them.
  Retrofitting that is more work than building it in.
* **Cache-only routes** — no route fetches from ESI on the request path.
  `margins.py` is the reference, including how it reports what it could not
  price.
* **ETags on every fetch.**
* **4XX quarantine per character** — one revoked token must not burn the shared
  error budget for everyone.

---

## Honest scope note

The design doc estimates 3–5 sessions for all of Step 4, at **low confidence**,
and calls it the item most likely to double. One long session produced 2 of 8
items plus the Postgres groundwork.

W6 and the query conversion together are a realistic session. Expecting W6, the
conversion, the worker, ETags, quarantine and cache-only routes in one is not —
and the worker in particular is new design work, not a move.

If the goal is to *close* Step 4 rather than progress it, the thing to protect
is the sequencing: W6 → conversion → worker. Doing the conversion before the
split, or the worker before the conversion, is how the estimate doubles.

## Still unresolved

* **Step 3 is still not deployed.** All of this plumbing is being built ahead of
  the hosted tool it is plumbing for, which inverts the order §11 argues for.
* **Open question 8** — verify sales tax and broker's fee in game against the
  wallet journal. Only you can do this one.
