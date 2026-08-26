# Working notes

Lessons that outlive the step that produced them. Most were extracted from
`docs/step-4-worklist.md` when Step 4 closed at v0.9.76 and that file was
deleted; the design itself lives in
[design-hosted-v2.md](design-hosted-v2.md).

Everything here was **measured**, not reasoned about. That is the point of
keeping it: each one is a case where the obvious reading was wrong.

---

## The thing to read first

`tests/conftest.py`. The suite spent at least one session writing to the real
`eve_cache.db`, because `EVE_APP_DIR` was set inside a fixture and fixtures run
after collection — by which time a test module with a module-level app import
had already bound the path. It cost three real characters and their refresh
tokens.

Two guards exist and both are mutation-checked: conftest sets the environment at
import, and `pytest_collection_finish` refuses to run if `database_path()` or
`app_dir()` answer anything but the test directory.

**The class is closed, not just guarded.** Nothing resolves a writable path at
import any more: `app/db/location.py` exposes `app_dir()` and `database_path()`,
and `deps.DB_ABS`, `location.DB_PATH`, `deps._APP_DIR` and
`token_store.CONFIG_PATH` are all gone. `tests/test_web_deps.py` scans the
package for module-level assignments that read `EVE_APP_DIR` — that scan is what
found the `token_store` one, which held the refresh-token config path and nobody
had thought of.

`EVE_BUNDLE_DIR` is still frozen, deliberately: it points at the code, and
getting it wrong renders a 500 rather than deleting a database.

**Never add a module-level path constant derived from `EVE_APP_DIR`.**

---

## Postgres for the test suite

Roughly a third of the suite runs twice, once per backend. Without the container
the Postgres half **skips silently** — a fast run can mean a broken container
rather than a fast machine, so check the skip count, not the clock.

```bash
docker start eve-pg
```

If the container is gone entirely:

```bash
docker run -d --name eve-pg -e POSTGRES_PASSWORD=eve -e POSTGRES_USER=eve -e POSTGRES_DB=eve_retroindustry -p 5433:5432 postgres:17
```

**The host port is 5433, and it must stay below 49152.** It was 55432 until
2026-08-25, when `docker start` began failing with *"bind: An attempt was made to
access a socket in a way forbidden by its access permissions"*. That is not
Docker: Windows' TCP dynamic port range is 49152–65535, and WinNAT/Hyper-V
reserves blocks inside it at boot. One boot happened to reserve 55357–55456,
which swallowed 55432. Any port in that range can be taken away on any reboot,
so the fix was to move below the range rather than to restart `winnat`.

To confirm a reservation rather than something listening:

```bash
netsh interface ipv4 show excludedportrange protocol=tcp
```

---

## How to work on this without wasting an afternoon

Measured from the session that built most of Step 4, by timing every tool call in
the transcript rather than counting them: **4.7 hours were spent inside tool
calls, and 3.5 of those hours were 71 whole-suite pytest runs — 74% of all
working time spent waiting for tests.** The 209 targeted runs in the same session
cost 25 minutes between them. Most of the 71 told us nothing.

* **Run the targeted file while working; run the full suite once, before the
  commit.** Not after every edit. `pytest tests/test_x.py` is two seconds; the
  full suite is about six minutes.
* **Mutation-test against the targeted file too.** A mutation batch is 5–8 runs;
  at six minutes each that is most of an hour to learn something a two-second run
  tells you.
* **Never build Python source in a bash heredoc.** The tool collapses
  backslashes, so a two-character escape sequence arrives in the file as a real
  newline and the source no longer parses. Use an editor tool for anything
  containing escapes, regexes, or nested quotes. Four separate repair cycles in
  one session before the lesson stuck.
* **Never `git add -A`.** It swept unrelated files into a commit twice, each
  needing a reset and a re-commit. Name the paths.
* **`pytest.ini` already sets `-q`.** Passing it again gives `-qq`, which
  suppresses the summary line — and a truncated capture then gets counted by
  hand, which is how a 1,515-test suite was once reported as 1,371 passing.
* **When a test is flaky, probe the state — do not re-run the suite.** The
  database-contamination bug took eight full runs to corner and would have taken
  one query: "what is in this table before the failing test?"

---

## Selectors: a quiet under-report looks exactly like a clean result

This failure mode showed up at least six times in one step, in six costumes: a
`-k "[sqlite]"` filter that silently selected 37 of 45 tests; a bare
`grep -rn "dbapi("` that answered 2 on a fully converted tree because it matched
the definition and its docstring; `grep 'conn.execute("'` finding two raw
statements in a file that held four; a mutation battery naming a **nonexistent
test file**, which would have scored 19/19 because pytest exits non-zero on a
missing path; a regex over attribute access that could not see
`monkeypatch.setattr(mod, "name", ...)` patching **by string**; and a test
selection that left out the only file issuing the request under test.

**The fix is the same every time: measure the selector, not just its output.**
Count what it returned and compare against what you expected to be there. Assert
that the paths a battery names actually exist.

### Count statements with an AST, not a grep

Most statements in this codebase are spread across lines, which is what defeats
the grep:

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
which is where the `IN ({ph})` placeholder patterns lived, and those are the ones
that needed an expanding bindparam rather than a mechanical rewrite. The healthy
answer is now **8**, all `PRAGMA`s; `tests/test_sql_portability.py` runs this
scan and fails on a ninth.

---

## Two ways a test can guard nothing

* **Patch the module that *calls*, not the module that *defines*.** A router
  doing `from app.esi.client import esi_client` binds the function object at
  import. `monkeypatch.setattr(app.esi.client, "esi_client", stub)` rebinds the
  source and leaves every importer pointing at the original, so the guard passes
  no matter what the page does. (The same fact is why an aliased re-export must
  be patched on the alias, and why `raising=False` is the right way to keep such
  a guard alive across a rename.)
* **A stub that raises proves nothing inside a handler that swallows.** Route
  handlers wrap their bodies in `except Exception` to turn a failure into an
  error banner, which catches the stub's `AssertionError` along with everything
  else — 200 returned, test green, page calling ESI on every request. Have the
  stub append to a list and assert the list is empty; that survives being caught.

The general shape: **a guard that cannot fail looks exactly like a guard that
passes.** The only way to tell them apart is to break the thing on purpose and
watch.

---

## int64 ids: a whole class of column is declared too narrow

*(Referenced from `app/db/schema.py` and migration 0009.)*

**Found by running the `industry_helper` rig tests against Postgres for the first
time — eight failures there, none on SQLite.** Every one was
`NumericValueOutOfRange: integer out of range`, from saving a rig configuration
against a station id of 1035466617946.

`station_rigs.location_id` was declared `Integer`. SQLite's INTEGER is a
variable-width 64-bit type and the declaration is advisory, so it stored the
value happily and had done for the life of the project. A Postgres INTEGER is
exactly 32 bits. An Upwell structure id is around 1.03e12, and Upwell structures
are the only things that can carry rigs at all — so on Postgres that table
accepted NPC stations and rejected every station a player could actually
configure.

Fixed for that one column in migration 0009, and for the other **23 in v0.9.57**
(migration 0010). `tests/test_int64_columns_on_postgres.py` writes a real
oversized value into each and reads it back, on both backends.

**The criterion had to change on contact.** The plan was "widen whatever ESI
declares int64". Asking the live OpenAPI spec what CCP actually declares gives
`location_id`, `character_id`, `type_id`, `contract_id`, `corporation_id`,
`group_id`, `order_id`, `item_id`, `category_id` — which is *everything*,
`type_id` and `group_id` included. So the declared type cannot decide this; taken
literally it means widening every column in the schema, which is the reflex the
exercise was supposed to avoid. **CCP declaring int64 means they have reserved
the room, not that they are using it.**

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

Three things fell out of measuring that no amount of reading would have given:

* **The market volume columns were the worst of it, and were not on the list at
  all** — the list was about ids. `market_price_cache.volume` is seven days of
  regional trade and `jita_available` is every Jita sell order's remaining units
  summed; both are over the ceiling today for Tritanium, Pyerite and Mexallon,
  which is most of what an industry tool prices.
* **`margin_snapshot.item_id` must not be widened.** It is `margin_watchlist.id`
  — our own autoincrement row number, not an EVE asset id. It is the one column
  whose name would have got it swept in by a name-based pass.
* **`character_id` is the only preventive one.** 2,124,549,094 still *fits*, so a
  test writing a real id passes with or without the fix. The test writes
  2,200,000,000 instead — the id of a character created a little way into the
  future, near enough that ~23 million signups close the gap. That is stated in
  the file rather than glossed, because a test that cannot fail is not evidence.

Left alone, with reasons in migration 0010's docstring: `type_id`, `group_id`,
`category_id`, `region_id`, `solar_system_id`, `planet_id`, `contract_id`, every
`id` we mint ourselves, and the count columns. `quantity` is the one to re-check
one day — ESI declares it int64 and an asset stack can hold billions of units —
but nothing was measured over the ceiling.

**The general lesson, which is the reusable part:** SQLite ignores the width in a
column declaration and Postgres enforces it, so *every* integer column in this
schema is unverified until something writes a real-world-sized value to it on
Postgres. **Type declarations are not portable claims; they are portable only
once a test has pushed a realistic value through them.**

### contract_id is two widths, deliberately

`public_contracts.contract_id` is `Integer` while
`contract_items_cache.contract_id` is `BigInteger` — the same identifier at two
widths, which reads like an oversight and is not. The measurement above put the
largest real contract id at 10.9% of the ceiling, against `character_id` at
98.9%, which is why that one was widened and this one was not.
`test_public_contract_ids_are_int32_on_purpose` carries the reasoning so the next
person to notice finds it attached rather than re-deriving it. If contract ids
ever approach 2\*\*31 both columns move together — and the asymmetry runs in the
safe direction: the character-side cache already accepts values the
public-contract side would reject.

---

## Mutating the wrong artifact proves nothing, quietly

Reverting `station_rigs.location_id` to `Integer` **in the declaration** failed
nothing on either backend, and the first reading of that was "the test is
decorative". It was not: the Postgres fixture builds its tables with
`upgrade_to_head`, so the column type there comes from the *migration*, and the
declaration is only consulted by `create_all` and by
`test_the_migrations_match_the_declaration`.

Two artifacts, two separate claims, each needing its own mutation:

* neuter migration 0009 → **8 Postgres failures, 0 SQLite**. That asymmetry is
  the evidence that the cross-backend half tests a backend difference rather than
  running the same assertions twice.
* revert the declaration → **1 failure**, from the guard that keeps declaration
  and history in step.

Before concluding a mutation proves an assertion is decorative, check that it
changed the artifact the test actually reads. Related: **a crash is not
coverage** — renaming a function proves reachability and nothing more; blanking
its return value is what proves an assertion exists.

## Mutation scripts: write bytes, or they rewrite line endings

A harness that restores its target with `pathlib.write_text` uses `os.linesep`,
so on Windows it hands back CRLF whatever it read. For a file already stored CRLF
that is invisible; for an LF file it flips them, and `git status` then shows the
file modified while `git diff` shows nothing at all, because git normalises on
the way to the index. Harmless, but it looks exactly like a mutation that failed
to restore. Read and write bytes, or pass `newline=""`.

---

## Blanket exemptions in a scanner hide the thing the scanner is for

`test_no_insert_or_replace_survives` once tripped on a **docstring** explaining
why `INSERT OR REPLACE` had been removed — not a use of it. The scan had met this
twice before and patched around it twice, the second time by skipping the whole
of `app/db/schema.py`, which meant a real offender anywhere in the schema file
would have gone unreported for as long as the exemption stood.

The precise version: blank out docstring bodies via the AST (keeping line
numbers), scan everything else, drop the whole-file skip. Verified by planting a
real offender in both files and confirming both are caught.

**The general form:** when a check produces a false positive for the third time,
make the check understand the distinction rather than adding a third exemption.
An exemption scoped to a whole file is a hole, not a tuning.

---

## The rule the design doc keeps rediscovering

**A name that claims something about the world needs an assertion about the
world.** It has now cost, at minimum: `test_esi_client_sends_user_agent` (asked
the wrapper about itself while eleven callers bypassed it),
`test_local_development_still_works_without_configuration` (checked which
constant came back, not whether login worked),
`test_the_page_renders` (passed against a board that returned at a guard), and
the whole of Step 4's statement count — 8 raw statements, every module portable,
the suite green on both backends, and the app unable to *start* on Postgres
because nothing imported it.

Corollary, from the same step: **"all traffic goes through X" is a claim about
call sites, and only a call-site scan pins it.**

---

## Still open, carried forward

* **Step 3 is still not deployed.** All of this plumbing was built ahead of the
  hosted tool it plumbs for, which inverts the order §11 argues for.
* **Open question 8 — verify sales tax and broker's fee in game** against the
  wallet journal. Only the user can do this one, and every profit figure in the
  tool inherits the answer.
* **The characters were not restored.**
  `eve_cache.db.bak-before-character-reset` still holds Astroasia and Tracy Juan
  with their refresh tokens if signing in again turns out not to be enough.
  `app_owner` is empty, so the first real login claims the instance.
