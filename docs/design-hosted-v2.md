# EVE Retroindustry v2 — Hosted Platform Design

Target architecture and feature plan for moving from a single-user desktop app to a
hosted, multi-tenant industry platform with corporation and alliance coordination.

Written 2026-08-17 from a planning session. The line that used to sit here — *"Status:
**design**. Nothing here is built"* — stopped being true a long way back, so the status
now lives in §11 where the steps are, and is summarised as:

| | Step | State |
|---|---|---|
| 0 | Land the WIP | ✅ v0.9.23 |
| 1 | Correctness sprint | ✅ v0.9.24–26 |
| 2 | Security baseline + ESI citizenship | ✅ v0.9.27 |
| 3 | **Go hosted, single-user** | ⚠️ **code only — never deployed** |
| 4 | Platform foundations | ✅ v0.9.76 |
| 5 | Multi-tenancy | ⬜ next |
| 6 | Groups + coordination MVP | ⬜ |
| 7 | Feature buildout | 🟡 0 of 8 complete; reactions board in beta |

**Step 3 is the one to read first.** It is the only ✅-adjacent step that is not real: the
desktop app is deleted and the hosted deployment was never done, so everything built since
runs on one laptop with no fallback. Steps 4 through 7 are being built *ahead of* the tool
they are for, which inverts the order this document argues for in "Why this order".

`docs/deploy-vps.md` is rewritten for this design rather than obsoleted by it;
`docs/working-notes.md` holds the measured lessons that outlived the step that found them.

---

## 1. The decision

The desktop app is retired. The product becomes a hosted web service.

**What goes away:** PyInstaller builds, the Windows installer, the AppImage, the Android
APK, `launcher.py` (pywebview/PyQt6), the tray icon, the in-app updater and the
`/api/version/*` endpoints, the `localhost:5173` callback dance, and the SSH-tunnel
deployment story.

**What is given up:** the "nothing leaves your machine" privacy promise, which is
currently the README's strongest claim. There are existing users on released builds;
"hand your ESI tokens to my server" is a materially different proposition from "runs
locally". **A migration story for existing users is a design input, not an afterthought.**

**What it buys:** corp and alliance coordination — the feature set in §7, which cannot
exist in a single-user local app.

---

## 2. The one thing that makes this feasible

The domain modules are already headless, take a `db_path`, and are unit-tested without
the web app: `app/bom`, `app/manufacturing`, `app/planetary`, `app/market`. The PI spec
insisted on this explicitly. That is the reusable core and it is the part that took the
effort.

What gets rewritten is the web layer, the data model and the sync strategy — not the
industry maths.

---

## 3. ESI budget — corrected understanding

An early assumption in planning was that all users would share one ESI budget and that
this was the binding constraint. That is **wrong for the traffic that matters**, per the
comment at `app/esi/client.py:157`:

> Public routes are bucketed by `<sourceIP>`… Supplying an access token moves us to
> `<sourceIP>:<applicationID>` — the same size bucket, but ours alone. It does NOT
> multiply the budget: per CCP's docs only *authenticated routes* key on characterID.

Consequences:

* **Authenticated per-character routes** (assets, jobs, wallet, blueprints, skills,
  planets) key on characterID → they scale with users rather than competing.
* **Public routes** (market prices, history) key on IP — but that data is *shared* and
  cached once for everybody, so it does not multiply with users either.
* **The error limit is per-IP and genuinely shared.** One character with a dead token
  spraying 403s can error-limit every user. This is a fault-isolation problem: quarantine
  characters that error repeatedly, do not let them keep firing.

A dedicated VPS IP is also an improvement over the desktop situation — no CGNAT
strangers, no other EVE tools on the same machine competing for the bucket.

### The inversion this forces

Pages currently fetch ESI inline during render (`/assets` at `main.py:3091` calls
`fetch_assets` mid-request; the dashboard fetches skills at `main.py:1807`). With one
user that is a spinner. With fifty it means every page load is ESI traffic and one user
browsing hard degrades everyone.

**A background sync worker owns all ESI traffic. Every route reads cache only.**

The template already exists in the codebase. `app/manufacturing/margins.py` opens with
*"Cache-only. Nothing here fetches."* and explains the reasoning, including the
discipline of reporting what it *could not* price rather than silently showing zero.
That module is the pattern for the whole hosted app.

`app/esi/client.py` already has the hard parts — error-limit governor, per-group token
buckets, low-water backoff. What it lacks is a **scheduler**: priorities, per-user
fairness, and a budget it plans against rather than reacts to.

### The exact bucket model (CCP, authoritative)

Per [ESI rate limiting](https://developers.eveonline.com/docs/services/esi/rate-limiting/),
a bucket is a (`rate limit group`, `userID`) pair, where:

* **Authenticated routes:** `userID` = `<applicationID>:<characterID>`
* **Unauthenticated routes:** `userID` = `<sourceIP>`, or `<sourceIP>:<applicationID>` when
  a token is supplied

CCP states the reason directly: *"to ensure popular apps have to obey by the same limits as
newly created apps."* So **a hosted service with 100 users gets 100 separate buckets** on
authenticated routes — confirming the corrected view above. It is a floating (sliding)
window; `X-Ratelimit-Limit` is formatted `150/15m`.

**Token costs make ETags worth double what they look like:**

| Response | Tokens | CCP's reasoning |
|---|---:|---|
| 2XX | 2 | |
| **3XX** | **1** | *"Promote the use of `If-Modified-Since` and `If-Match`"* |
| **4XX** | **5** | *"Discourage hitting user-errors"* (429s exempt) |
| 5XX | 0 | server-side, not your fault |

A 304 costs **half** what a 200 costs. Conditional requests are not merely polite here —
they double effective throughput. The app already does this for market history via
`market_hist_etag`; extending ETags across every cached fetch is a direct capacity win.

And a 4XX costs **2.5×** a success, which quantifies the fault-isolation point: a character
with a dead refresh token spraying 403s drains the bucket faster than useful work fills it.
Quarantine on repeated 4XX.

**The older error limit still applies to routes not yet on bucket limiting:** at most 100
non-2xx/3xx responses per minute, after which ESI returns **420 on all routes** — including
ones that are bucket-limited. Headers `X-ESI-Error-Limit-Remain` / `-Reset`, and they are
*mutually exclusive* with the `X-Ratelimit-*` headers, so the client must handle both
schemes and not assume one implies the other. `client.py` already models both; this confirms
that was right.

**Scheduler design, straight from CCP's best practices:**

> Use staggered scheduling for periodic requests when possible. Ideally not `*/5` cronjobs.
> But rather: 5 minutes after the last job was finished.

That is the sync worker's cadence rule: **delay-after-completion, not fixed-interval**, with
jitter across users so syncs do not align. Plus: don't operate at the limit, slow down as
`X-Ratelimit-Remaining` approaches zero, spread rather than burst, and respect `expires`.

**Caching is a ban risk, not just etiquette.** CCP: *"Circumventing the ESI caching can get
you banned from ESI."* Never re-request before the `expires` header. One subtlety worth
building in: for paginated resources the `last-modified` header should be identical across
all pages — differing values mean the data refreshed mid-pagination and the result set is
inconsistent. That matters for assets, which are paginated and which the app stitches
together.

---

## 4. Security baseline

These findings apply to the current codebase. Items 1–5 are worth fixing regardless of
the migration; items 6–8 are new obligations that hosting creates.

### Current findings

1. **No authentication of any kind.** The only HTTP middleware is `_setup_gate`
   (`main.py:213`). No session, no auth check, no CSRF, no Host validation. The
   `active_char` cookie is a display preference, not identity.

2. **DNS rebinding / CSRF affects desktop users today.** Binding `127.0.0.1` stops remote
   connections but not the browser the user is already running. Any site they visit can
   issue cross-origin POSTs to `127.0.0.1:8000`. The sharpest target is
   `/api/version/download` → `/api/version/apply` (`main.py:7332`), which fetches an
   update script and executes it.

3. **The OAuth callback server binds every interface.** `_DualStackCallbackServer(("::",
   5173))` with `IPV6_V6ONLY` cleared (`esi_oauth.py:193`). The IPv4 fallback at line 205
   correctly binds `127.0.0.1` — so the fallback path is safer than the primary. On a VPS
   this listens on the public IP for the whole 120-second login window, and
   `docs/deploy-vps.md` warns about port 8000 but never mentions 5173.

4. **The OAuth `state` is generated and never validated.** Created at `esi_oauth.py:237`
   and `:344`, captured at `:120`, never compared. PKCE happens to block the obvious code
   injection — an injected code was issued against the attacker's challenge and fails
   exchange against our verifier — but the handler accepts the first `GET /callback` from
   anyone and shuts down, which combined with (3) is a trivial remote login-DoS.

5. **Refresh tokens are plaintext and the DB has no permissions set.**
   `.eve_config.json` gets `chmod 0o600` (`token_store.py:86`) but only holds the client
   ID. `eve_cache.db`, which holds every refresh token, gets nothing.

6. **Full tracebacks are returned to the client on 500** (`main.py:209`).

7. **No User-Agent is sent on any ESI request.** `app/esi/client.py` sets none; the only
   `User-Agent` in the codebase is for the GitHub version check (`main.py:7172`). CCP's
   [best practices](https://developers.eveonline.com/docs/services/esi/best-practices/) say
   requests **should** identify the application, and that *"Not abiding to the information
   transmitted can lead to your app being banned."* Expected form:

   ```
   EVE-Retroindustry/0.9.22 (brian.maupin@gmail.com; +https://github.com/EVERetroIndustry/Eve-retroindustry)
   ```

   Minor for a desktop app where each user has their own IP. **For a hosted service it is a
   pre-launch requirement** — all traffic arrives from one IP under one applicationID, and
   an unidentified high-volume source with no contact path is exactly what gets banned.

8. **State validation is required by CCP, not merely advisable.** The SSO documentation is
   explicit: *"the application must verify that the state parameter matches the one it
   sent."* This upgrades finding (4) from defence-in-depth to a compliance gap.

9. **SSO endpoint URLs are hardcoded.** `esi_oauth.py:48-50` pins `AUTH_URL` and
   `TOKEN_URL`. CCP publishes a discovery document at
   `https://login.eveonline.com/.well-known/oauth-authorization-server` and says the URLs
   *"may change in the future… it is recommended to always fetch them from the endpoint"* —
   cached for a reasonable period. The JWKS URI for token verification comes from the same
   place, so this is a prerequisite for finding (7) in the next list anyway.

**Correction to `docs/deploy-vps.md`:** it states an attacker can "act with your stored
ESI tokens". All 24 requested scopes are read-only, including
`esi-planets.manage_planets.v1`. An attacker gets complete financial and asset
intelligence and can exfiltrate refresh tokens, but cannot move ISK or assets. The doc
should be corrected so it is trusted on the parts that are accurate.

### New obligations from hosting

7. **JWT signature verification becomes mandatory.** `esi_oauth.py:287` and `:437` both
   use `jwt.decode(..., verify_signature=False)`. Defensible today — the token arrives
   over TLS from the token endpoint and is only used to read one's own character name.
   Once that JWT decides *who you are and what you can see*, it must be validated against
   the EVE SSO JWKS with `iss` / `aud` / `exp` checks.

8. **Custodianship of other people's tokens.** Encryption at rest, a revocation path, and
   a stated data policy. Plus the developer-license implications of holding third-party
   data.

---

## 5. Identity, scopes and groups

### Identity

**EVE SSO authenticates a character, not an account.** ESI deliberately exposes no way to
determine which characters share an EVE account — that would break alt anonymity. So "your
EVE account is your login" cannot be built literally.

The model instead:

* First character logged in **becomes** the app account and its main.
* Additional characters are **linked** by authorising each one through SSO, which proves
  control.
* Main is a pointer that can be moved at any time.
* Session is a signed cookie issued after SSO — `HttpOnly`, `SameSite=Strict`. The EVE JWT
  authenticates the login; the app's own session carries it afterward.

The callback becomes `https://<domain>/callback` on the service's own registered
application. The `_DEFAULT_CLIENT_ID` compiled into `token_store.py:35` cannot serve a
hosted multi-user service.

### Scope selection

**EVE SSO scopes are fixed at authorization time.** A scope cannot be added to an existing
token — changing scopes is always a re-authorization producing a new refresh token that
replaces the old one. So the feature is a page listing what each capability needs, what is
granted, and a button that re-runs SSO with the new set.

Two things make it clean:

* **The JWT's `scp` claim lists the granted scopes.** No probing, no discovering by 403.
  Another reason (7) above matters.
* **Every fetcher must degrade per-scope.** A missing scope must be distinguishable from a
  transient failure — the same discipline `fetch_industry_jobs` already applies by
  returning `None` rather than `[]`, and the same discipline `margins.py` applies when
  reporting what it could not price. A wallet page for someone who never granted wallet
  access says "not authorized", not zero and not an error.

This makes a **feature → required-scopes map** a first-class concept. A builder who only
wants to claim alliance project lines grants identity plus `read_character_jobs`; a breach
then leaks that, not their net worth. Minimum scope becomes a property of the system rather
than a principle in a document.

### Bring-your-own client ID

Technically viable — PKCE means no client secret, so a user registers their own application
pointing at the service callback and supplies only a client ID. Four constraints:

* **Refresh tokens are bound to the issuing client ID.** Changing it kills every linked
  character. Must be a deliberate action with an explicit warning, not a field that saves
  on blur (contrast `main.py:1351`).
* **Chicken-and-egg on first login** — the service cannot use a user's client ID before it
  knows who they are. First login always uses the service client ID; BYO is an account
  setting applied when re-linking characters.
* **Service traffic and user traffic must split.** Shared market fetching always uses the
  *service* client ID; only per-character calls use that character's. `get_client_id()` is
  currently global (`token_store.py:88`).
* **Most users will not register an application.** It must be optional with a working
  default.

**Recommendation: drop the per-user variant entirely.** CCP's SSO documentation positions
PKCE as *"mostly aimed at mobile and desktop applications that cannot securely store the
client secret"*, with the plain Authorization Code flow — client ID **and secret**, basic
auth on the token endpoint — as the flow for web applications. A hosted service *can* store
a secret, so it should use the confidential-client flow.

Which kills per-user BYO: under a confidential client, a user bringing their own application
would have to hand over their **client secret**, which CCP says *"must be kept private"*.
Asking users to surrender a private credential is not a feature. The rate-limit argument is
gone too — buckets already key on `applicationID:characterID`, so a separate application ID
buys nothing per character (§3).

What survives is *self-hosting*: one config value for a deployment, not a per-account
setting. That delivers the sovereignty story at a fraction of the cost.

**Shipped in v0.9.28** as `EVE_CLIENT_ID`. The framing that makes it obvious: an application
has exactly one registered callback URL and CCP matches `redirect_uri` as a string, so **the
client ID owns the callback URL** — the two are halves of one registration and neither is a
property of this code. A deployment therefore cannot borrow another's application, and the
repository must not appear to offer one. The old `_DEFAULT_CLIENT_ID` fallback did exactly
that: a second deployment would have sent its users to a consent screen naming the reference
application, then failed the exchange against a callback it did not control. It is now
reachable only when the callback is on `localhost`, and `/auth/login` refuses with an
actionable message rather than starting a flow that could not succeed.

Nothing else in `app/` contains a deployment-specific literal, and a test enumerates every
URL in the package to keep it that way.

One registration detail that shapes §5's scope selection: **an application can only request
scopes assigned to it at registration.** So the registered application declares the full
superset, and per-user scope selection requests subsets of it. The design works, but the
superset has to be declared up front.

### Groups

* Character → corporation from `/characters/{id}/`; corporation → alliance from
  `/corporations/{id}/`. Both public and authoritative.
* **Membership is dynamic.** Leaving a corp must revoke access to corp projects, so
  membership needs scheduled revalidation — another job for the sync worker — not a
  `corporation_id` written once at login.
* **Corp roles** are available via `esi-characters.read_corporation_roles.v1` (already in
  the scope list), so Director is checkable.
* **"Alliance leader" has no clean ESI concept.** The usable proxy is *director in the
  executor corporation* (`alliance.executor_corporation_id`). Decide this deliberately.

---

## 6. Multi-tenancy

This is the expensive, unglamorous part, and it is where the project can hurt real people.

There is **no ownership concept anywhere in the current schema**. `production_projects`,
`project_plans`, `project_jobs`, `project_shopping` and `margin_watchlist` are all global.
`app_defaults` is a single key/value table, so the entire app has one build station. And
`main.py` has 18 sites that query characters on the assumption every character in the DB
belongs to the same person.

Requirements:

* Every table carries an account scope; every query filters on it. **One missed filter is
  a data leak.**
* Aggregation views that must not expose detail (see §7) are **separate read models**, not
  templates that decline to print a column.
* **Real migrations.** Two tables have SQLAlchemy models (`type_cache`,
  `blueprint_cache`); everything else is raw `CREATE TABLE IF NOT EXISTS`, and the SDE
  refresh compares row counts to decide whether to reimport. That works when a user can
  delete their DB; it does not work when the DB holds an alliance's project history.
* **Engine.** SQLite with `NullPool` and a 30s busy timeout may hold for a small group,
  mostly-reads, in WAL. What pushes it over is the sync worker writing continuously while
  everyone reads. Design data access so the answer can change; the migration story matters
  more than the engine choice today.
* **Synchronous token refresh blocks the event loop.** `main.py:1385` documents it —
  `get_valid_token()` does a synchronous `httpx.post` and "calling it inline on the async
  loop froze the whole app". `_valid_token_async` wraps it in a thread; v0.9.22 was a fix
  for this class. Every remaining synchronous path becomes a shared stall. Fix properly,
  do not wrap.

---

## 7. Production scheduling (the flagship feature)

The problem: five people each build 100 battleships and the alliance ends up with 500.
No tool solves this because no tool has both the build plan and live job data.

### The chain

```
order        alliance posts:     200 Ravens, deadline
commitment   corp signs up:      corp1 → 100, corp2 → 100
assignment   director assigns:   char A → 25 Ravens; char B → the Ferrogel reactions
observation  ESI job matched to an assignment
```

Status rolls **up** by aggregation. Detail does **not** roll up.

### Information boundary

The alliance view is deliberately coarse — counts by state, an ETA, per-corp totals. No
character, no facility, no location. This is a feature, not a simplification: builders will
not adopt a tool that exposes their structures and capabilities to the whole alliance, and
structure locations are operational security in EVE.

Enforce it in the read model. If the alliance query cannot select a character or facility
column, nobody can add it to a template later.

### What already exists

`app/manufacturing/schedule.py` is further along than the feature needs:

* `Job` — type_id, activity, runs, seconds, is_capital → the unit of assignment
* `SlotLimits` — manufacturing/reaction pools, capital as a *subset* of manufacturing
* `schedule_level` / `_pack_manufacturing` — slot-constrained bin packing returning a
  **makespan**. This is the ETA engine.
* `group_jobs` + `JOB_CATEGORIES` — reactions → components → capitals → end product. This
  is the natural assignment granularity, because it is how alliances actually divide
  labour.
* `split_runs` / `max_runs_per_job` — job splitting

`fetch_industry_jobs` passes ESI through raw with `include_completed=True`, giving
`job_id`, `product_type_id`, `runs`, `activity_id`, `start_date`, `end_date`, `status`,
`facility_id`.

### The hard problem: jobs carry no project tag

Nothing in the ESI payload says *why* a job was started. If a character is assigned 25
Ravens and also builds 10 for themselves, matching must be inferred from character +
product + activity + runs + time window, and that inference is ambiguous by construction.

**Chosen approach: auto-match with a visible ledger.** Match automatically, show the
builder exactly which jobs were counted with a one-click "not this one", and flag ambiguous
cases (more matching jobs than assigned quantity) rather than guessing. Greedy silent
matching over-credits personal builds, which returns the leader to trusting self-reports;
mandatory confirmation adds a step people skip when busy.

Two robustness requirements:

* **Track by `job_id` and persist every job ever seen with its final state.** A short job
  can start and deliver entirely between two syncs, and hosted sync intervals are measured
  in tens of minutes. "Completed" must be durable, not derived from having observed the job
  while running.
* **`fetch_industry_jobs` returning `None` on failure rather than `[]` is load-bearing.**
  Conflating "fetch failed" with "no jobs" marks in-progress work as vanished. This is
  already deliberate in the docstring; it needs a comment saying why, because it is the
  kind of thing that gets "simplified" later.

### Honest ETAs

The estimate is two different things: running jobs have real `end_date`s, unstarted work
has a `schedule_level` projection that is only as good as the assumed slot counts. A single
confident date hides that.

Present it as it decomposes: *50 built · 30 in build (longest ends in 4 days) · 120 not
started (~21 days at current slots)*. Same information, degrades honestly, and makes the
bottleneck visible — which is the leader's actual decision.

### Open questions

* **Built vs delivered.** A completed job means the hull is in someone's hangar, not that
  the alliance has it. Over-supply is solved by tracking builds; "do we have 200 Ravens" is
  a delivery question. Observable via corp assets and corp contracts (scopes exist), but it
  is a second state machine.
* **Over- and under-commitment.** Total commitments against an order must be visible and
  capped or loudly flagged. The messy case — a corp commits to 100, builds 60, goes quiet —
  and reallocation of an abandoned commitment is what decides whether this survives contact
  with a real alliance.

---

## 8. SDE import gaps — the highest-leverage file

Four additions to `import_sde.py`, none depending on the hosted migration, each turning
numbers that are currently wrong or absent into correct ones.

| Addition | Source | Unlocks | Status |
|---|---|---|---|
| `invention` + `copying` activities | `blueprints` | Correct T2 costs everywhere; research planner | ✅ v0.9.25 |
| `typeMaterials` | `typeMaterials` | Refine calculator, ore valuation, mining ledger, alchemy | ✅ v0.9.26 |
| `portionSize` | `types` | Refine batching | ✅ v0.9.26 |
| `marketGroups` | `marketGroups` | Market hierarchy and group-level BI | ✅ v0.9.26 |

**All four landed.** 47,051 reprocessing yields, 2,106 market groups under 19 roots, and
`portion_size` on every type. Two things worth recording from doing it:

* **The golden fixtures hold exactly.** Plagioclase (0.35 m³, 100 per batch, 175 Tritanium
  + 70 Mexallon → 5.0 and 2.0 per m³) and Spodumain (30 / 0.625 / 0.1 / 0.05 / 0.025 per
  m³) match CCP's export, ore.cerlestes.de and the DARK spreadsheet — three independent
  sources agreeing. A test now pins the *shipped* `sde_base.db` against those numbers, so a
  future import that silently switches per-unit for per-batch, or assembled for packaged
  volume, fails rather than quietly rescaling every ore figure by 100.
* **Appendix A's batch rule is confirmed by data**, not just quoted: `portion_size` is 100
  for ore and compressed ore, 1 for ice (Glacial Mass, White Glaze).

The market tree comes out as the shape §9.4 assumed: Ships → Battleships → {Advanced,
Faction, Precursor, Standard} Battleships, with `has_types` marking the leaves that hold
items so a browser never offers an empty branch.

Already in flight (uncommitted): `volume` on `sde_types` from `types.yaml`, with a
`_volume_column_present` guard so an older DB degrades rather than reporting a wrong
density. That is the pattern for all of these.

### Invention specifically — ✅ done, v0.9.25

Implemented in `app/manufacturing/invention.py`, charged to every T2 row in the margin
tracker. Four things the SDE settled that guidance would have got wrong:

* **Runs per BPC is data.** The T2 blueprint's `max_production_limit` is the real number.
  "10, or 1 for ships and rigs" holds for most, but ~145 of the 1,209 invented blueprints
  are 5, 20, 200 or 300. The entire invention cost is divided by this, so the rule of thumb
  misprices those by up to 300x. Ships really are 1 run — in 121 cases out of 121.
* **Decryptors are data too.** `sde_decryptors`, from dogma attributes 1112/1113/1114/1124.
  There are **64**, not the 8 every guide lists. Every value in Appendix A's table below is
  confirmed exactly by the SDE.
* **The science skills come from the datacores, via `requiredSkill1` — never from names.**
  The datacore is `Datacore - Gallentean Starship Engineering`; the skill is `Gallente
  Starship Engineering`. Amarrian/Amarr disagree the same way. Name matching silently drops
  one of the two science skills for **every Amarr and Gallente T2 ship**, understating the
  success chance by ~4 points (the Ishtar read 33.6% instead of 37.9%).
* **Nor from the blueprint's own skill list.** That list carries gating prerequisites which
  do not affect the odds (`Capital Ship Construction`, on 95 blueprints), and it does not
  always agree with the datacores — Multispectrum Coating II consumes Molecular Engineering
  datacores while its blueprint lists Nanite Engineering.

Two edge cases handled: `Datacore - Triglavian Quantum Engineering` has no dogma record at
all, so the importer falls back to a name match (which is exact for that one); and seven
legacy blueprints carry no encryption skill, which is a real answer rather than a lookup
failure. A property test asserts all 1,200 invented products resolve exactly two science
skills — it is what caught both bugs above.

The base probabilities in the SDE match EVE University's published table exactly, which is
a useful cross-check but never the source.

✅ **Charged per node since v0.9.29** — and the limitation was worse than this section
described. `BOMResolver` now applies invention at every manufacturing node, root or nested;
`build_invention_params` (in `margins.py`, shared with the plan route) turns the stored
defaults into parameters so both pages price datacores identically.

Two corrections to what was written here before:

* **`/plan` was not "understating builds containing T2 parts" — it was charging invention
  nowhere at all.** The v0.9.25 work lived in `compute_margin`, and the planner never called
  it. So every T2 item `/plan` priced had a free blueprint, the product itself included.
  That is ~1,200 invented products, not an edge case, and it is the larger half of the bug.
* **"Capital builds are the obvious case" is wrong.** Measured against the SDE: 130 products
  carry an invented item in their BOM, and **no capital group appears among them**. Capital
  components are ordinary T1 blueprints, so a capital tree never meets invention. The real
  set is faction and "Edition" variants built from a T2 item — `'Augmented' Ogre` from
  `Ogre II`, `Kronos Police Edition` from a `Kronos`, `Dark Blood Dual Giga Beam Laser` from
  the T2 laser — plus pirate drones like the Aralez. Narrower than claimed, and mostly items
  a typical industrialist never builds.

Two things the wiring turned up that were not in the plan. The make-vs-buy optimizer had to
learn the term as well: its `make_cost` already added `job_fee` precisely so a component only
wins on an all-in comparison, and invention is the same class of term and the *larger* one on
a T2 item — left out, it would have over-selected "make" on exactly the components this
change prices. And the datacore price basis was hardcoded to `sell` while every other
material on the row followed the configured `input_basis`; it now follows `input_basis` too,
which makes invention slightly cheaper for anyone sourcing from buy orders.

### Invention specifically

`import_sde.py:283` imports only `manufacturing` and `reaction`. So a T2 item resolves its
materials but the invented BPC is free — no datacores, no decryptors, no probability, no
copy time. **Every T2 profit figure the app shows is optimistic**, and `/margins` now
surfaces that error across a whole watchlist at once.

The schema needs no migration: `sde_blueprint_products` already has a `probability` column
and every table is keyed on `activity`. The modelling work is expected attempts =
`1 / (base_prob × decryptor × (1 + enc/40 + (sci1 + sci2)/30))`, then amortising datacores
and decryptor over `runs_per_BPC × output_qty`, as a per-node cost in `BOMResolver`.

**Why this ranks first for varied, small-batch industry:** `margins.py:240` resolves a
single run with `runs_per_job=None`, taking the worst ME rounding — a deliberate
conservative choice that *penalises* T1 rows. Meanwhile T2 rows are *flattered* by the free
BPC. Sorted by margin, T2 beats T1 partly for reasons that are not real. For a tool whose
job is ranking candidates, a systematic bias between classes of candidate is worse than a
missing column, because a missing column is visible.

**The 1-run BPC amplifies this.** Invented BPCs are 10 runs for most items but **1 run for
ships and rigs**. So the entire invention cost of a T2 ship lands on a single hull, while a
T2 module spreads it over ten. Any invention model that ignores this misranks T2 ships
against T2 modules by roughly an order of magnitude. Default output is also ME 2 / TE 4,
not ME 0 and not ME 10 — the watchlist default for a T2 row should be 2.

### Not an import gap, but the same class of error: trading taxes

**Sales tax and broker's fees are not modelled anywhere.** `margins.py:279` is:

```python
row.profit = sell - row.material_cost - row.job_fee
```

No sales tax, no broker fee. The only occurrences of those terms in the codebase are wallet
journal *labels* (`app/character/wallet.py:150-151`). Per EVE University's Trading page:

* **Sales tax base is 7.5%** — raised from 4% in Version 22.02 (2025-03-12) — reduced 11%
  per level of Accounting, so 3.37% at Accounting V.
* **Broker's fee** in NPC stations is `3% − 0.3%×BrokerRelations − 0.03%×faction standing
  − 0.02%×corp standing`; in Upwell structures it is a flat `0.5% SCC surcharge + owner %`
  and **skills do not apply**.
* A seller pays **10.5%** of sale price at zero skills, **4.875%** at Accounting V +
  Broker Relations V, and **4.375%** with max standings on top.

  ⚠️ **Corrected 2026-08-18.** This bullet previously read "between 5.1% (max skills) and
  11%; 4.6% with max standings", quoting the Trading page's prose. Those figures are stale
  and contradict the sales-tax formula printed on the *same* page — they only resolve at a
  **8%** base, not the current 7.5%. Verified against the Version 22.02 patch note
  (2025-03-12, "Sales Tax has been increased from 4% to 7.5%") and the EVE Uni *Tax* page.
  The decisive cross-check is the broker floor: the wiki states 1% minimum at Broker
  Relations V with max faction and corp standings, and `3 − 1.5 − 0.3 − 0.2 = 1.0` exactly,
  which confirms the per-level and per-standing coefficients independently. Implemented in
  `app/market/taxes.py`; treat the wiki's *formulas* as authoritative and its *worked
  totals* as suspect.

So **every margin, profit, ISK/hour and profit-per-m³ figure in the app is currently
overstated by roughly 5–11% of the sale price.** That is broader than the invention gap —
it hits T1 and T2, manufacturing and reactions, every row on the margin tracker and every
`/plan` result. It is also cheap to fix: two settings (Accounting level, Broker Relations
level + structure/standings) and one subtraction, though it should distinguish *sell to buy
orders* (sales tax only, no broker fee) from *place a sell order* (both, plus relist risk).

The relist fee is worth modelling eventually but not at first:

```
relist = (100% − (50% + 6%×AdvBrokerRelations)) × broker% × new_value
       + broker% × max(new_value − old_value, 0)
```

**Confirmed correct meanwhile:** the app's `SCC = 0.04` (`margins.py:29`) matches the
current 4% SCC surcharge on industry jobs — which went 0.25% → 0.75% (2023-07) → 1.5%
(2023-09) → 4% (2024-02). The industry SCC surcharge and the market sales tax are different
things and both are needed.

### Fuzzwork closes every one of these — and the YAML pipeline with them

<https://www.fuzzwork.co.uk/dump/latest/csv/> publishes the SDE as **per-table CSVs**,
rebuilt on every SDE release (checked 2026-08-17, dated the same day), with published
md5sums and versioned build directories (`3470007_20260817_140000/`) that allow pinning to
an exact SDE build.

Every gap in this document is a small CSV:

| File | Size | Closes |
|---|---:|---|
| `invTypeMaterials.csv` | 941K | Reprocessing yields — refine calc, mining ledger, alchemy |
| `invMarketGroups.csv` | 163K | Market hierarchy (§9.4) |
| `industryActivityProbabilities.csv` | 36K | Invention base chance |
| `invTypes.csv` | 19M | `portionSize`, `volume`, `marketGroupID` |
| `industryActivity{,Materials,Products,Skills}.csv` | ~2M | **All** activities — invention, copying, research_material, research_time |

And several things this document had marked "needs a hardcoded table" or "new data need"
turn out to be published data:

* **`compressibleTypes.csv`** (3.5K) — the ore → compressed-ore mapping flagged as a data
  need in §9.3.
* **`invMetaTypes.csv` / `invMetaGroups.csv`** — meta level and T1/T2/faction/storyline
  classification. Precisely the "faction / advanced / standard battleships" tier the market
  rebuild wants: market groups give the tree, meta groups give the tier *within* it.
* **`dgmTypeAttributes.csv`** (16M) — dogma attributes, so the decryptor stats in
  Appendix A become data rather than a hardcoded table.
* **`staStations.csv`** (1.0M) — NPC station reprocessing efficiency and take, which the
  refine calculator needs for its 30–50% base.
* **`planetResources.csv`** (1.0M) — **check whether this replaces the hardcoded matrix in
  `planet_data.py`.** At 1 MB it looks like per-planet-instance data rather than a
  planet-type → resource matrix, in which case it is *better* than what the PI spec assumed
  was unavailable. Verify before relying on it.
* `mapSolarSystems.csv`, `mapSolarSystemJumps.csv`, `mapRegions.csv` — security status and
  the jump graph, behind the existing distances API.

**The bigger prize: `import_sde.py`'s YAML pipeline is on a deprecated format.** It parses
`blueprints.yaml` / `types.yaml` / `groups.yaml` / `planetSchematics.yaml` from a `data/`
directory, needing a ~150 MB `types.yaml` and libyaml to be tolerable. CCP has since moved
SDE distribution to **JSONL** — Fuzzwork's current loader is
[`jsonl-evesde`](https://github.com/fuzzysteve/jsonl-evesde), while the older
[`yamlloader`](https://github.com/fuzzysteve/yamlloader) and `SDE-loaders` are both
**archived**.

Two ways forward, either of which beats the status quo: consume Fuzzwork's CSVs (download,
checksum, load), or follow CCP to JSONL. For a hosted service this is the difference between
a manual SDE refresh ritual and a cron job.

**But CCP now ships first-party automation, and that should be the primary.** The
[static data docs](https://developers.eveonline.com/docs/services/static-data/) describe
exactly what an automated importer needs:

* **Latest build number** — `…/static-data/tranquility/latest.jsonl`, in the record keyed
  `sde`
* **Build-pinned downloads** — `…/eve-online-static-data-<build>-<variant>.zip`, plus
  `…-latest-jsonl.zip` / `…-latest-yaml.zip` shorthands that redirect
* **A changes feed** — `…/static-data/tranquility/changes/<build>.jsonl`, whose `_meta`
  record names the previous build. This enables *incremental* SDE updates rather than full
  reimports.
* **A schema changelog** — `…/schema-changelog.yaml`, so a schema change is a notification
  rather than a mystery breakage
* **Full ETag and Last-Modified support**, and resources only change when they actually
  change

Both **JSON Lines and YAML** are still published, so `import_sde.py` is not broken — but CCP
says plainly that *"reading large YAML files can be memory-intensive and slow"* and
recommends JSONL for large datasets. That is precisely the 150 MB `types.yaml` problem.

**✅ Done — v0.9.24.** Implemented in `app/sde/feed.py` (fetching) and a rewritten
`import_sde.py` (loading). Build 3470007 imports in **1.7 seconds** against a YAML pipeline
whose own progress message read "147 MB, this takes a while", and PyYAML — an undeclared
dependency the setup docs warned about — is gone entirely. Everything CCP documented is
real and works: `latest.jsonl`, build-pinned archives, the changes feed with `schemaChanged`
flags, ETags. `--zip` re-imports from a cached archive with no network at all.

Three things that surfaced only by doing it:

* **`packagedVolume` is not `volume`.** The v0.9.23 import took `volume`, which for a ship is
  the *assembled* figure — an assembled Nidhoggur is 11,250,000 m³ against 1,300,000 packaged,
  and it is the packaged one that decides what a hauler carries. 829 types differ, all of
  them ships and containers, i.e. precisely where profit-per-m³ gets used. The column is now
  `sde_types.packaged_volume`. Caught before it ever rendered a number, because the column
  was still NULL everywhere.
* **Row counts cannot detect staleness, structurally.** Build 3470007 has *five fewer*
  blueprint material rows than the bundle before it — CCP dropped Isogen from the Venture
  and rebalanced four others. The refresh gate fired only when the bundle had *more* rows,
  so a build that **removes** something could never reach a user. `sde_types` now carries a
  build number and the gate compares that first. This retires W10 ahead of Step 4.
* **`planetSchematics.types` is a list, not a dict**, and the tables must keep the exact
  column names the app already queries (`required_level`, `time_bonus_pct`). Both were got
  wrong first time; both are now pinned by tests.

**Original recommendation, for the record.** Take the SDE from **CCP directly**, using the
build-number and changes feeds, moving to JSONL. That gets automation, incremental updates, schema-change
warnings and ETags with **no third-party dependency at all** — which matters, because both
spreadsheets analysed in §9.2 and §9.3 died from renting their data, and this document
should not recommend re-renting it.

Fuzzwork then becomes what it is best at: a **convenience layer**. The per-table CSVs are
ideal for quickly answering "what's in `invTypeMaterials`" during development, for
prototyping before the importer exists, and as a cross-check that a locally derived table
matches what everyone else in the ecosystem sees. Pin and checksum if any of it is ever
used in production, but the ambition should be that none of it is.

### Half of invention's cost model is already cached and unused

`sci_cache` already holds **all six activity cost indices** for 5,485 systems —
`manufacturing`, `reaction`, `copying`, `invention`, `researching_material_efficiency`,
`researching_time_efficiency`. `industry_helper.py:523` fetches the whole
`GET /industry/systems/` payload and stores every `cost_indices` entry, not just the two the
app currently reads.

So the system cost indices for invention, copying and both research types are **already in
the database, refreshed on the normal cycle, and never queried**. When invention and the
research planner land, that half of their job-cost model costs nothing.

### Third-party industry APIs — reference, not dependency

[api.eve-industry.org](https://api.eve-industry.org/) is an XML API (XPath-friendly, built
for spreadsheets) sourced from the Fuzzwork datadump, offering three things:

1. **System cost index** per system for manufacturing, TE research, ME research, copying,
   reverse engineering, invention and reactions
2. **Base job installation cost** (EIV) per blueprint
3. **Mineral compression** — minerals → compressed ore quantities

Its significance here is diagnostic rather than practical: endpoints 1 and 2 are exactly the
two dead custom functions from the reactions spreadsheet (`getSystemCostIndex`,
`getAdjustedPrices`). Somebody rebuilt the thing that killed that sheet as a service.

**The app should not consume it.** It already has both natively — `sci_cache` above, and
`adjusted_price_cache` — fetched straight from ESI. Use this to cross-check job-cost
arithmetic during development, nothing more. The same reasoning applies to Fuzzwork's
market aggregates (§9.4).

---

## 9. Feature set

### 9.1 Reactions board

~80% built already. `margins.py` handles reactions end to end: separate reaction station,
its own system cost index and facility tax, reaction TE multiplier, reaction ME bonus,
`reaction_time` from `sde_blueprints`, branching on `node.activity == "reaction"`.

**Key insight: the entire reaction space is ~119 published products** — small enough to
price *the whole board* every refresh. That gives a complete ranked leaderboard with
unprofitable reactions explicitly marked — the "what NOT to make" half, which a curated
watchlist structurally cannot provide because you would have to know to add the loser first.

> **Built to beta — v0.9.29, `/reactions`.** Costing verified, three advertised features not
> yet working — see Step 7. The count above was **111** and is wrong: Intermediate
> Materials 41, Composite 17, Biochemical 32, Hybrid Polymers 9 and Molecular-Forged 12 sum
> to 111 and omit the eighth group, the **8 Unrefined Mineral** (alchemy) products. Verified
> against the SDE at 119. They ship flagged rather than dropped — their output is meant to
> be reprocessed, so the price shown is not why the reaction is run, and silently removing
> eight rows would recreate the blind spot the board exists to remove.
>
> Three things the build settled that the spec did not anticipate:
>
> * **The board must not reuse `compute_margin`.** It resolves to raw, and a Tungsten
>   Carbide run needs 5 Nitrogen Fuel Blocks against a job that yields 40 — `resolve()`
>   correctly refuses to run 1/8 of a job, so the tree charges a whole 40-block run *three
>   times over* (directly, inside Rolled Tungsten Alloy, inside Sulfuric Acid). 120 blocks of
>   inputs to consume 15, and the row read **-247%** where buying the inputs yields **+14%**
>   net. Right for a build plan, wrong for a rate — this is the batching/floor caveat, and it
>   is why §9.2 defines Build Advantage as a *delta between two figures* rather than one
>   number. The board costs direct inputs at market; build-from-raw is deferred until the
>   batching question is settled rather than shipped as a known-bad column.
> * **`find_blueprint()` is the only safe way to reach a reaction's blueprint.** Tungsten
>   Carbide has two — the real formula (yield 10,000, 10,800 s) and a leftover *Test Reaction
>   Blueprint* (yield 20, 360 s). A product+blueprint query returns 120 rows with one product
>   twice, and timing a job with one recipe while costing it with the other is invisible in
>   the output.
> * **A ranking is a recommendation, so it has to be earned.** The first working board put
>   *Pure Strong X-Instinct Booster* on top at 92.9% margin and 24 billion ISK/month — with
>   one input unpriced (costed at zero) and a sell price of 5.0M against a **34.7k buy**, a
>   144× spread that is one stale order rather than a market. Rows whose cost is incomplete
>   or whose output has no real bid are now demoted below fully-priced ones and labelled,
>   never hidden. Without that the page's headline advice was to react something unsellable.

Missing half: **"what to buy and when"** — the combined moon-goo input list for the
reactions currently worth running, with each input flagged as historically cheap or dear.
`price_history_cache` exists; `margins.py` already computes a 7-day average and
day-over-day delta, just pointed at outputs.

### 9.2 The reactions spreadsheet model

Source: `Updated Master Reactions Sheet.xlsx` (Google Sheets export, formulas dead).

**Why it died is the point: every broken dependency is something the app already owns.**

| Dead in the sheet | Live in the app |
|---|---|
| `getSystemCostIndex(system, "reaction")` | `sci_cache`, from ESI |
| `getAdjustedPrices(...)` | `adjusted_price_cache` |
| `JANICE_PRICER(...)` — third-party API | `market_price_cache`, hub caches, order books |
| `QUERY(...)` — Google-only | SQL |
| the "Cachebuster" column | a real cache with ETags and TTLs |

The model was sound. It rented its data.

**Normalization** (`Industry Outputs`):

```
Adjusted Time  = base time × (manufacturing ? mfg multiplier : reaction multiplier)
Jobs Per Month = FLOOR(2592000 / Adjusted Time, 1)      -- 30 days, floored
Gross          = Price × Jobs Per Month × Quantity
ISK/Slot-Hour  = Gross/Hr − Cost/Hr
```

The `FLOOR` matters: a 25-day job fits once in 30 days, and a pure rate model silently
credits it with 1.2. `margins.py`'s Profit/hour is a pure rate and does not capture this.
Both are defensible; the floored-period view is the familiar one.

**The four decision metrics:**

1. **ISK per Slot-Hour** — headline ranking
2. **Margin** — net over cost
3. **Build Advantage** = margin(build from raw) − margin(buy intermediates). One number for
   the whole vertical-integration decision; negative means buy the intermediates. The
   app's optimiser already decides make-vs-buy per node but decides *for* you — exposing
   the delta shows how close the call was, which is what matters for a standing decision.
4. **Sell advantage** = `(local_sell − (jita_sell − shipping)) / local_sell` — local null
   hub versus hauling to Jita, net of freight.

**To adopt:**

* **Split `input_basis` into three.** The sheet distinguishes raw inputs (BUYS — patient),
  intermediates (SELLS — instant), and outputs (SELLS). `app_defaults` has one toggle.
  This materially changes cost.
* **Freight as ISK per m³, import and export separately** (750/750 in the sheet). Matches
  how alliance jump-freight is actually priced. `shipping = volume × rate`.
* **Configurable local hub.** Already supported machinery — `hub_price_cache`,
  `/api/prices/custom`, station volume fetching. Only the comparison columns and freight
  term are missing.

**Already agreeing:** `app/web/industry_helper.py:125` documents reaction rigs scaling on a
different security table (lowsec ×1.0, null/WH ×1.1) than manufacturing (×1.9 / ×2.1),
verified against EVE Ref — exactly the sheet's `F6:G7` vs `F20:G22`. Tatara 25%, Sotiyo
30%, Athanor 0% all match. And `Industry Inputs.E` is `IFS(C=1, 1, ...)` — the ME floor
rule the PI spec called out for Robotics in fuel blocks, already implemented in
`_apply_me`.

**New data need:** `Alchemy Refines` applies a flat `0.55 × quantity` to turn unrefined
intermediates into refined ones — a reprocessing step inside the reaction chain, so alchemy
needs `typeMaterials.yaml` too.

### 9.3 Ore cluster — refine calculator, mining ledger, appraisal

All three sit on the same data and compose into one decision: **refine, sell raw, or sell
compressed?**

#### Refine calculator

Needs `typeMaterials` (yields) and `portionSize` (batch). **Portion size is the correctness
trap** — a partial batch cannot be refined, so quantities floor to whole portions and the
remainder stays ore.

Formula: base structure rate → Reprocessing (3%/level) → Reprocessing Efficiency (2%/level)
→ ore-specific skill (2%/level) → implants → station tax against standings.

**Two advantages over standalone calculators:** the app already holds the character's real
skills (`char_skills_cache`, `esi-skills.read_skills.v1`) so it computes the yield *that
character* gets rather than assuming all V; and it already models structure rigs with
security multipliers, tested for ME/TE, which reprocessing rigs reuse directly.

#### Mining ledger

Source: `DARK Mining Dashboard TEMPLATE.xlsx`.

The scope `esi-industry.read_character_mining.v1` is **already requested**
(`esi_oauth.py:60`) and never used anywhere in the codebase.

Three endpoints:

* `/characters/{id}/mining/` — personal daily ledger by ore type and system
* `/corporation/{id}/mining/extractions/` — moon chunk schedule. Same countdown-to-expiry
  pattern already built for PI extractors on `/planets`, sorted soonest-first.
* `/corporation/{id}/mining/observers/{id}/` — moon mining ledger **with per-character
  attribution**. Requires a corp role, covered by
  `esi-characters.read_corporation_roles.v1`. Same shape as the build-claiming problem:
  verified contribution rather than self-report. Feeds directly into §7.

**Metric spine from the spreadsheet:** date × character → m³ mined, ISK value, and refined
material breakdown; daily series rolled up weekly and monthly; **All Characters vs
individual** selector. Covers ore, ice, moon ore *and* gas — the ESI ledger returns all of
it (the sheet's ProcessedData has 60+ material columns including Fullerites and
Cyto/Mykoserocin).

Its enrichment is `RefineData.yield × FLOOR(quantity / portion_size)` — the correct
batching rule.

**Where the app improves on it:** the sheet applies **no refining efficiency at all** — the
mineral columns are raw yields at an implicit 100%, which nobody achieves. With real skills
and structure the same dashboard becomes accurate rather than an upper bound.

**It rented its data too:** GESI as a Google add-on, per-character auth through a third
party, character names typed by hand to match in-game exactly, a 48,046-row static
`ItemIDs` dump with stale prices, and a hardcoded `RefineData` table that breaks on the
next ore rebalance. The instructions include *"REFRESH the entire page to clear out any
errors"*.

#### Cross-validation (ready-made test fixtures)

`RefineData` checked against `ore.cerlestes.de`:

* **Plagioclase** — 0.35 m³, portion 100, 175 Tritanium + 70 Mexallon → per m³: 175/35 =
  **5.0**, 70/35 = **2.0**. Cerlestes: 5.000, 2.000.
* **Spodumain** — 16 m³, portion 100, 48000/1000/160/80/40 → per m³: **30 / 0.625 / 0.1 /
  0.05 / 0.025**. Cerlestes: identical.

Two independent sources agree and both reconstruct exactly from `typeMaterials` +
`portionSize` + `volume`. This confirms the import plan is sufficient and supplies golden
tests — in the same spirit as the PI spec's "5 P2/hr and 3 P3/hr or the cycle maths is
wrong".

#### Appraisal (Janice-like)

Pricing, name resolution and volume are all solved or in flight. **The work is the
parser**: EVE emits several tab-separated formats (inventory, cargo scan, contract,
fitting, multibuy) plus loose `Name x123` shapes. Quantity separators are locale-dependent
and a localised client emits localised type names — decide early whether that is supported
or an explicit limitation.

**Differentiator: depth-aware pricing.** Appraising 10 million Tritanium at best sell price
is fiction. Walk the order book and return the actual fill price plus how far down it goes.

Hosted + groups turns Janice's shareable link into saved appraisals scoped to a corp or
alliance, and pairs naturally with the existing public contract browser — "is this contract
worth taking".

#### Compression is a linear program, not a lookup

Steve Ronuken's [`compression`](https://github.com/fuzzysteve/compression) (and
[`eve-python-compression`](https://github.com/fuzzysteve/eve-python-compression)) frames the
problem properly: *given your skills, refining location, and required mineral or ice-product
quantities, work out the minimal-volume set of compressed ore or ice to buy.* A PHP frontend
delegates to a FastAPI service using **PuLP with the CBC solver**.

* **Objective:** minimise total cargo volume
* **Constraints:** required mineral quantities, yields implied by skills and refining
  location, compressed-ore compatibility

This is worth adopting because it connects several parts of the plan at once. A production
plan's shopping list currently says "you need 4.2M Tritanium and 900k Isogen"; the useful
answer is *"buy this set of compressed ore, it is the smallest volume that satisfies the
requirement"* — which then feeds hauling at ISK per m³ (§9.2) and the refine calculator's
yield model. The app is already FastAPI and Python, so PuLP drops in without a second
service.

Note this is also the inverse of `api.eve-industry.org`'s third endpoint, which does the
same minerals → compressed-ore conversion.

#### Prior art: plundering.rocks

[`plunderingrocks`](https://github.com/fuzzysteve/plunderingrocks) is an existing EVE mining
ledger (MIT). Worth reading before building §9.3's ledger — particularly for how it handles
the ESI ledger's date granularity and per-character aggregation.

#### Reference: ore.cerlestes.de

Design lessons:

* **Per-m³ as the comparison basis** — mining and hauling are volume-constrained, so
  yield-per-m³ is the only fair comparison between 0.1 m³ Veldspar and 40 m³ Mercoxit.
  `margins.py` already has `profit_per_m3`; using one frame across ore, margins and
  appraisal gives the app a single mental model.
* **Volume-weighted percentile pricing** — their basis is Sell/Buy at the 98th and 90th
  percentile weighted by order volume, not just min/max. **This is bigger than the ore
  table**: the whole app currently prices at best bid/ask, which is optimistic for any real
  quantity. Adding percentiles to `market_price_cache` improves every ISK figure at once.
* **"Compare prices to refined value"** with raw / compressed / batch-compressed columns
  side by side — the right output shape for the refine calculator. Needs an ore →
  compressed-ore type mapping.
* **Static availability matrix** (security space, faction) is not in the SDE — same
  situation as PI's planet-type/resource matrix, so the same solution: one documented
  static table like `planet_data.py`.

**Ore yields get rebalanced** — their "legacy variant names" toggle exists because CCP
renamed them. Anything hardcoded goes stale; anything derived from the SDE survives the
patch.

### 9.4 Market / prices rebuild

Current state: a flat list. `sde_types.market_group_id` is populated — 19,667 types across
1,616 groups — but **there is no market-group table**. `marketGroups.yaml` is not imported,
so there is a leaf pointer and no tree, no names, no parents.

**The tree is the aggregation axis, not just navigation.** With the hierarchy, KPIs compute
at group level — "Battleships: median margin, total ISK/day traded, 3 of 47 currently worth
building" — which turns the screen from *lookup* into *scanning*. Every other screen answers
"what is this worth"; this one's unique job is **market quality**.

Proposed KPIs. Raw material is already in `price_history_cache.data_json` (ESI's daily
series carries `average`, `highest`, `lowest`, `order_count`, `volume`) and almost none of
it is currently derived beyond charts.

| KPI | Formula | Question |
|---|---|---|
| Daily volume | mean `volume` over 30d | Does anyone buy this? |
| **Days to clear** | qty ÷ daily volume | Can I sell 200, or am I holding stock for a month? |
| Depth | units within 5% of best price | How far will I move the price? |
| Spread | (sell_min − buy_max) / sell_min | Wide = illiquid or trader-controlled |
| Volatility | 30d stdev ÷ mean | Is this margin real or a spike? |
| Trend | 7d avg vs 30d avg | Rising or falling into my build time |
| Competition | `order_count` | How contested is the sell wall? |
| Regional edge | (local_sell − (jita_sell − freight)) / local_sell | Sell at home or haul |

**Days to clear ranks first.** It is the honest counterweight to margin: a 40% margin on
something trading three units a day is not a business, and nothing in the app currently
says so.

Practical: materialise these into a stats table on sync rather than computing from
`data_json` per page load. Market history is a public per-IP route and 19,667 types is far
too many to sweep blindly — prioritise watchlist items, active projects, and groups
actually being browsed.

#### References for the market work

**The Oz Report** — <https://www.theoz.space/> · weekly market insights (Twitch/YouTube/
podcast) plus a weekly dashboard of "the most relevant market movements of the week".
Reported to surface *items in shortest supply, underpriced items, highest volume*, and Oz
is known for extrapolating null/lowsec conditions from Jita activity. The dashboard itself
is hosted on Quantum Anomaly's platform and its exact columns were not retrievable from the
landing page — **worth opening directly before finalising the KPI set.**

The important lesson is the *angle*: Oz's work is **derived market signals, not price
lookup** — supply scarcity, mispricing, volume concentration. That is the same framing as
"market quality" above, and it independently validates ranking by liquidity and supply
rather than by price.

**theoz.space/tools** is also a useful map of the ecosystem:

| Tool | Relevance |
|---|---|
| **ADAM4EVE** — "Margin Finder", **"Material Influence"** | Material Influence is a KPI worth stealing outright (below) |
| **Mokaam.DK** | Bulk historical market extraction without per-item queries |
| **MER in Power BI** | CCP's Monthly Economic Report as regional context |
| EVE Cookbook · EVE Webtools PI · Quantum Anomaly | Adjacent build/PI/store tooling |

**New KPI from ADAM4EVE — input sensitivity.** "Material Influence" measures how much a
material's price movement moves a product's build cost. For a builder that is more
actionable than any output metric: knowing Ferrogel is 60% of a capital component's cost
tells you exactly which input to watch and when a margin is about to evaporate. The app
already has the full BOM tree, so this is a per-node cost share — cheap to compute, and not
in the KPI table above.

**Fuzzwork market aggregates** — <https://market.fuzzwork.co.uk/aggregates/?region={id}&types={ids}>
· returns, for **both buy and sell** sides: weighted average, max, min, **standard
deviation**, **median**, volume, order count, and **95th percentile**. Accepts a region,
system or station.

That is almost exactly the aggregate set proposed above, from someone who has run it at
scale for years — good independent confirmation that median, stddev and percentile are the
right shape, and that they should be **precomputed per type** rather than derived per query.
[`FuzzMarket`](https://github.com/fuzzysteve/FuzzMarket) (Python, MIT) is the open-source
implementation and is worth reading before writing the stats materialiser.

**Do not consume it as a data source.** The documentation asks directly: *"Please don't do
this every 30 minutes. Get the data yourself, direct from CCP. It'll be fresher and more
reliable."* The app already fetches ESI directly — that is correct and should stay. Take the
metric definitions, not the feed.

**EVE Guru** — <https://eveguru.online/> · commercial (30-day trial, then 200M ISK/month),
Windows + Linux. Manufacturing planner and trading tool: profitable-to-build ranked on Jita
demand, dynamic shopping lists, T1 and T2 (including BPC from BPO), reaction facility
planning with lowsec system and station bonuses, profit net of taxes and broker fees,
character skills/standings/attributes in job costing, buy and sell margins across the major
hubs, a market-order analyzer, and **minimum trade volume filtering for demand assessment**
— which is the same liquidity gate as *days to clear*.

Worth noting for positioning: EVE Guru's **planned** features are things this app has
already built or has in flight — job-slot consideration (`schedule.py`), a corporation asset
manager, and manufacturing profiles with facility and rig configuration (already modelled,
and better documented — see `industry_helper.py:125`). Its T2 handling does include BPC
production, which is the invention gap in §8.

---

### 9.5 Discord bot

Requested 2026-08-17, not yet designed. Recorded here so the shape is not re-derived later.

**Why it fits.** The coordination flow in §7 is mostly *notification*: an alliance posts an
order, corps commit, a director assigns, a job starts, a job finishes. Every one of those is
an event someone needs to hear about, and alliances already live in Discord. A builder who
has to remember to open a web page is a builder who does not see the order. This is the
difference between a tool people check and a tool that reaches them.

**What it should do**, roughly in value order:

1. **Announce and chase orders.** New alliance order → post to a channel. Corp commits →
   confirm. Assignment made → DM the assignee. Job overdue or unstarted → nudge.
2. **Report progress at the coarse level §7 specifies** — "50 in build, 25 days remaining",
   never "player 1 is building this at that station". The alliance read model is already
   designed to be safe to show broadly, which makes it the natural thing to post publicly.
3. **Answer queries in channel** — price, margin, what a build needs. Cheap once the read
   models exist, and the most likely thing to get used daily.
4. **Alerts** that already have somewhere to go: PI extractor expiry, job completion.

**Three things that decide the design:**

* **It is a second front-end, not a second application.** It must read the same read models
  the web UI reads. Any logic reimplemented in the bot is a place the two can disagree, and
  a coordination tool that disagrees with itself is worse than no tool.
* **Identity mapping is the real work.** A Discord user has to be linked to an account, and
  therefore to characters and a corp/alliance role. That is a linking flow with its own
  verification, and it inherits every tenancy question in §6 — a bot that answers the wrong
  person's data is the same leak as the web UI doing it, with a wider audience.
* **Channel scope is a permission boundary.** Alliance-level figures posted into a corp
  channel are a disclosure. Whatever posts where has to be derived from the group model,
  not from whoever invited the bot.

**Where it lands.** After **Step 6** — the bot is a view onto orders, commitments and
assignments, so it needs those to exist. Two things it depends on earlier: the sync worker
in Step 4 has to emit events rather than only refreshing caches (a bot that polls the
database for changes is a bot that misses them), and Step 5's account scoping is what makes
"who is this Discord user" answerable at all. Worth keeping in mind when designing the
worker, because retrofitting event emission is more work than building it in.

**Hosting note:** a bot is a long-lived gateway connection, so it is a second process
alongside the web app on the VPS, with its own restart and secret handling.

---

### 9.6 Dashboard — from landing page to actual dashboard

Requested 2026-08-17. The dashboard today is character-centric: a card per character with
corp, wallet, location and skill queue, plus asset value, available cash, PI extractors and
a prices-updated stamp. It answers *"what do I have?"*. It should also answer **"what should
I do next?"** — which is what a dashboard is for.

**Widgets to start with** (small, each earning its square):

| Widget | Shows | Data source |
|---|---|---|
| Top margins | Best 3–5 rows from the watchlist | `margin_snapshot` |
| Job status | Running / finishing soon / ready to deliver | `char_industry_jobs_cache` |
| PI status | Extractors expiring, colonies idle | `_pi_alert_summary()` |
| Build capacity | Slots used vs configured | jobs + `app_defaults` slots |
| Market freshness | How stale the prices behind every figure are | `market_price_cache` |

**The pattern to preserve is already there and is the hard part.** `dashboard()` renders
from cache only and never calls ESI; `/api/dashboard/live` fills the ESI-backed fields
afterwards. That exists because a slow or rate-limited ESI used to make the whole app look
frozen. Every widget must therefore have a **cache-only first render**, with anything
needing ESI arriving in the second phase. A widget that blocks the home page on a fetch
re-creates precisely the bug this design already solved.

**Widgets read existing read models; they do not invent queries.** Two of the five are
nearly free today:

* **Top margins must read `margin_snapshot`, not recompute.** `build_view_model` prices the
  entire watchlist — a full BOM resolve per item — which is acceptable on a page you visit
  deliberately and *not* acceptable on the home page. The daily snapshot already exists for
  the Δ and rolling-average columns, and reading the latest row per item is one indexed
  query. This also makes the widget honest about being a daily reading rather than implying
  a live figure.
* **PI status already has `_pi_alert_summary()`**, cache-only, currently feeding the navbar
  badge. The widget is a second view onto the same call.

**Two things to decide when it is built**, neither urgent:

* **Configurability.** Which widgets, in what order. `app_defaults` is a key/value table
  chosen precisely so additions do not need a migration, so a stored layout costs nothing.
  Worth resisting a general-purpose drag-and-drop grid: a fixed set with show/hide is most
  of the value for a fraction of the work.
* **Ownership after Step 5.** Every widget is a per-account view, so they must go through
  the account-scoped query layer like everything else. Cheaper to write them that way than
  to retrofit — but note this only matters once tenancy exists, so building widgets *before*
  Step 5 is fine as long as they use the same helpers the rest of the app does.

**Where it lands.** Not blocking anything, and not dependent on the hosted migration —
these are reads over data the app already holds. Natural home is **Step 7**, but the two
cheap widgets (top margins, PI status) could ride along earlier if the dashboard is being
touched anyway. Worth doing after Step 4, though: the sync worker changes when caches are
filled, and a dashboard built on the current refresh timing may need re-tuning afterwards.

---

## 10. Cross-cutting decisions

* **Pricing model.** Three input bases (raw / intermediate / output, each buy-or-sell);
  volume-weighted percentiles alongside best bid/ask; freight as ISK per m³ each way;
  configurable local hub alongside Jita.
* **Ranking metrics.** ISK per slot-hour as the headline; margin; build advantage; sell
  advantage; days to clear.
* **Cache-only reads.** Every route. `margins.py` is the reference implementation,
  including reporting what could not be priced.
* **Derive from the SDE, never hardcode.** Ore yields, reaction ratios and PI schematics
  all get rebalanced. The two static tables that are genuinely not in the SDE (PI planet
  resources, ore availability by space) live in documented modules.

---

## 11. Step-by-step plan

Planning context that shapes the ordering: **hosted only** (the desktop pipeline is
retired, not maintained in parallel), this is a **fork** of an existing project, and there
is **exactly one user**. The last point is the decisive one — there is no user-migration
story, nothing breaks anyone else when it breaks, and the tool can be hosted single-user
long before it is multi-tenant. Every step below leaves a working tool in daily use.

### Pre-existing worry register

Known problems carried into this design (all found in the codebase before or at the start
of this work), each mapped to the step that retires it:

| # | Worry | Retired in |
|---|---|---|
| W1 | No authentication of any kind; the deploy doc itself warns of it | **Step 2 — done, v0.9.27** |
| W2 | CSRF / DNS-rebinding exposure; `/api/version/apply` executes a downloaded script | **Done — CSRF and Host checks v0.9.27; the endpoint deleted v0.9.28** |
| W3 | OAuth callback binds all interfaces; `state` generated but never validated | **Step 2 — done, v0.9.27** |
| W4 | Refresh tokens plaintext; no file permissions on `eve_cache.db` | Permissions **done, v0.9.27**; encryption at rest still Step 5 |
| W5 | Full tracebacks returned to clients on 500 | **Step 2 — done, v0.9.27** |
| W6 | `main.py` at 7,352 lines / 80 routes | **Done — Step 4, v0.9.31.** 822 lines and 3 routes at the split (835 today — the sync worker's shutdown hook arrived after); eleven routers under `app/web/routers/`, plus `app/web/deps.py` for what they share. The route table is pinned by `tests/test_route_inventory.py` and did not change. |
| W7 | Three substantial features sitting uncommitted (PI planner, margins, job splitting) | **Step 0 — done, v0.9.23** |
| W8 | Invention absent from the data model — every T2 figure optimistic | **Done — root v0.9.25, whole tree v0.9.29** |
| W9 | Synchronous token refresh blocks the event loop (the v0.9.22 bug class) | **Done — Step 4, v0.9.32.** 22 call sites across six routers now `await _valid_token_async()`. `tests/test_async_token_refresh.py` scans for the blocking call inside any coroutine and asserts the loop keeps turning during a refresh. |
| W10 | No migration system; SDE refresh decided by row-count comparison | **Done.** SDE half in Step 1 (build numbers, v0.9.24); migrations in Step 4 (Alembic, v0.9.30) |
| W11 | SQLite under a continuously writing sync worker | **Done — Step 4, v0.9.38.** WAL, `busy_timeout=30000` and `synchronous=NORMAL` were already set at every path that writes, but nothing asserted them on the raw handle the eleven routers open, and the two connection layers had never been run against one file at the same time. `tests/test_sqlite_under_the_worker.py` now does exactly that — the worker's engine path and a page's raw handle, writing concurrently — and every behavioural assertion in it fails when the pragmas are removed. It also scans `app/` so no third `sqlite3.connect` site can start writing without a busy timeout. **Scoped deliberately:** this vouches for one user against a local file. Many concurrent writers is Step 5, and Postgres is that answer, not a pragma. |

Found during the design work itself (same class, listed for completeness): trading
taxes absent from every profit figure (Step 1), no ESI User-Agent (Step 2), hardcoded SSO
endpoints (Step 2), deprecated YAML SDE pipeline (Step 1).

**Found in v0.9.29, all in code that had already shipped.** Listed because the pattern
matters more than any one of them: every single one was in a step marked done, and every one
was invisible until the app was actually *run*.

| Defect | Shipped in | Effect |
|---|---|---|
| Bundled client ID pointed at the retired `:5173` callback | v0.9.28 | **No login was possible at all**, in any environment, since that release |
| `/settings` offered as the fix for a missing client ID | v0.9.27 | Session required to set the thing that grants sessions — a closed loop |
| Adding a second character reported "Login failed. Nothing was changed." | v0.9.28 | It had succeeded; the message was wrong twice over |
| A refused login kept the stranger's refresh token | v0.9.28 | R4 custody with no consent, reachable by anyone with the URL |
| SDE tables present-but-empty could never self-heal | pre-existing | Sent the user to re-download an SDE already in the repo |
| 11 CLI call sites bypassing `esi_client()` | pre-existing | Unidentified, unpinned, ungoverned traffic to CCP — Step 2's finding 7 was only half closed |
| `sales_method: immediate` priced output at the **sell** price | v0.9.26 | Profit overstated twice for anyone selling into buy orders |

**The lesson, stated once so it is not relearned:** three of these survived because a test
asserted something narrower than its name claimed — `test_esi_client_sends_user_agent` asked
the wrapper about itself while eleven callers bypassed it;
`test_local_development_still_works_without_configuration` only checked which constant came
back; `test_the_page_renders` passed against a board that returned at a guard and never ran a
query. **A name that claims something about the world needs an assertion about the world.**

### The steps

**Step 0 — Land the WIP. ✅ Done — v0.9.23.** The three in-flight features shipped as
four commits plus a release: PI planner, margin tracker, job splitting/slot scheduling,
and the app-wide defaults page they share. 875-line diff cleared; margins is the
reference pattern for everything later. *(W7)*

Landing it surfaced one thing worth carrying forward. `sde_types` gained a packaged-volume
column, and the bundled-SDE refresh **could not see it**: the gate triggers on more rows or
a missing *table*, so a release that only adds a *column* left every existing install
behind, silently, forever. Fixed here (the gate compares columns too, one-directionally,
so a column only the user has does not re-trigger on every startup) and covered by
`tests/test_sde_refresh.py`. This is W10 — "SDE refresh decided by row-count comparison" —
arriving two steps early and confirming it is real rather than theoretical. The Step 1
importer rework should treat build-pinning as the actual fix; this gate is a patch on a
mechanism that wants replacing.

Note the live install still shows no profit-per-m³: the column exists in the schema but
`sde_base.db` predates it, so the value lands only when the SDE is re-imported and the
bundle rebuilt. The page says so rather than showing a blank column.

**Step 1 — Correctness sprint. ✅ Done — v0.9.24–v0.9.26.** Every number the tool reports
changed, and four of them were wrong before:

| Item | Shipped | Was |
|---|---|---|
| Trading taxes in every profit figure | v0.9.26 branch, `app/market/taxes.py` | Profit overstated by 4.4–10.5% of sale price |
| SDE importer on CCP's JSONL feed, build-pinned | v0.9.24 | Deprecated YAML, undeclared PyYAML dep, silently stale |
| `packaged_volume` (was assembled `volume`) | v0.9.24 | 8.65× wrong on a Nidhoggur |
| Invention + copying activities | v0.9.25 | Every T2 blueprint free |
| `typeMaterials`, `portionSize`, `marketGroups` | v0.9.26 | Absent |

Not done, and deliberately: the **reactions board**, which was always conditional ("if
appetite allows"). It belongs with Step 7's feature buildout now that the data exists.

Two threads were recorded in §14 rather than closed. The first — invention charged at the
root only — is **closed in v0.9.29**, and closing it found that `/plan` had been charging
invention nowhere at all (§8). The sales tax and broker's fee rates are still unconfirmed
against the live game. *(W8, and the SDE half of W10)*

**Step 2 — Security baseline + ESI citizenship. ✅ Done — v0.9.27.** All eight items,
in dependency order. The findings in §4 were re-verified against the tree first; every one
of them still held.

| Finding | Shipped | Was |
|---|---|---|
| 7 — no ESI User-Agent | `app/version.py` builds one; wired into ESI, both SSO token exchanges, the refresh, and the SDE feed | Unidentified traffic under one applicationID |
| 6 — tracebacks to clients | Opaque 500; detail stays in the log, `EVE_DEBUG_ERRORS=1` restores it | Paths, SQL and locals in the response body |
| 5 — DB permissions | `harden_db_permissions()`, re-applied after the SDE swap | Every refresh token world-readable |
| 9 — hardcoded SSO URLs | `app/auth/sso_metadata.py`, cached an hour, falls back to the old literals | Pinned; CCP says they may move |
| 7(new) — unverified JWT | `app/auth/jwt_verify.py`: signature + `iss`/`aud`/`exp` | `verify_signature=False` at two call sites |
| 4, 8 — `state` unchecked | Constant-time compare in both flows | Generated, returned, never compared |
| 3 — callback on all interfaces | Two loopback sockets driven as one unit | `("::", 5173)` with `IPV6_V6ONLY` cleared |
| 1, 2 — no auth, no CSRF, no Host check | `app/web/security.py`: DB-backed sessions, per-session CSRF, Host allowlist | Nothing at all |

Three things this turned up that were not in the plan:

* **CCP's own metadata document is wrong about signing algorithms.** It advertises
  `id_token_signing_alg_values_supported: ["HS256"]`; the live JWKS serves RS256 and
  ES256. HS256 is symmetric and could never appear in a JWKS. An implementation that
  trusted the advertised list would be open to algorithm confusion — a token forged by
  HMAC-ing with the RSA *public* key. The algorithm list is therefore **pinned in code,
  not discovered**, and a test forges exactly that token and asserts rejection.
* **Binding `::1` does not accept IPv4, even with `IPV6_V6ONLY` cleared** (measured).
  IPv4-mapped `127.0.0.1` is `::ffff:127.0.0.1`, a different address, so the obvious
  one-line fix for finding 3 would have broken login for anyone whose browser resolves
  `localhost` to IPv4 — the exact bug the dual-stack server was written to prevent. Two
  sockets is the only way to get both loopbacks without also getting the world.
* **Verifying RS256 needs `cryptography`**, which was not a dependency. Added.

Recorded rather than closed: **owner-on-first-login is trust-on-first-use.** With no
`EVE_OWNER_CHARACTER_ID` set, the first character to complete SSO claims the instance —
fine on a laptop, a real if narrow window between deploying and first login. Step 3 should
set that variable as part of deploying. *(W1–W5)*

**Reopened and closed properly, 2026-08-18: finding 7 was only closed on the paths the
tests looked at.** Going to answer §14's compatibility-date question, the audit found
**eleven** `httpx.AsyncClient()` constructions outside `esi_client()` — three in `main.py`,
eight in `plan.py`, the two CLI entry points. Every call they made reached CCP with no
`User-Agent`, no `X-Compatibility-Date` and neither rate-limit governor: unidentified
traffic, unpinned behaviour, and no 420/429 handling on the tools most likely to hammer
`/universe/names/` in a loop.

The instructive part is why the suite missed it. `test_esi_client_sends_user_agent` asserts
the wrapper sets the header, and its docstring called that "the path all ESI traffic takes"
— a claim about the *callers* that the test never checked. A test that interrogates a
component about itself cannot see who declines to use it. Replaced with a source scan over
`app/` plus both entry points, exempting only the wrapper and the SDE feed's sync client;
`_bg_fetch_prices` also lost a dead `import httpx`. Worth carrying into Step 4: "all traffic
goes through X" is a claim about call sites, and only a call-site check pins it.

**Step 3 — Go hosted, single-user. ⚠️ NOT DONE — code only; still running on localhost.**
v0.9.28 finished everything that could be done without the VPS. The deployment itself — DNS,
certificates, the six environment variables, the callback re-registration, pinning
`EVE_OWNER_CHARACTER_ID` — is still outstanding, deliberately parked on 2026-08-18 to keep a
day low-friction. **Read this as "the strategic decision is coded but not made real."** The
hosted-only bet in §1 is not hedged by anything: the desktop app is already deleted, so until
this step lands the tool runs on one laptop with no fallback. That is R3's failure mode
described exactly, and it gets more expensive the longer Step 7 items are added ahead of it.

The v0.9.29 session found the cost of shipping code that had never been run end to end: five
separate defects in this step's own work, none of them in the plan (see the box below).

Deleted: `launcher.py`, `login.py`, the PyInstaller spec, the Inno Setup installer, both
build scripts, `android-poc/`, the release and Android CI workflows, the in-app updater and
its three `/api/version/*` endpoints, the tray and webview stack from `requirements.txt`,
and the server-side "open this in your system browser" opener with its host allowlist.
`main.py` lost ~270 lines; `esi_oauth.py` went from 626 to 260.

**The callback stopped being a second server.** The local listener on port 5173 existed
because a desktop app has nowhere to receive a redirect. `GET /callback` is now a route on
the app, which deletes the two loopback sockets Step 2 had just carefully built, the
15-minute watchdog, the cancel endpoint, the waiting page and the status endpoint it
polled. The state check survived in a better form: a login in flight is stored as
`state → PKCE verifier`, so a callback carrying a state we did not issue has nothing to
exchange with. That also fixed a wart nobody had filed — the old flow held one global lock
for up to fifteen minutes, so closing the SSO tab blocked the next login attempt.

**`EVE_CALLBACK_URL` is a cutover, not a setting.** CCP compares `redirect_uri` against the
application registration as an exact string and an application has exactly one. So there is
an unavoidable window, while the registration and the deployment disagree, in which SSO
cannot work — and since Step 2 made a session mandatory, that window is a lockout.
`python -m app.web.bootstrap` mints a single-use ten-minute sign-in link and needs
filesystem access to the database, which is the property that makes it safe to keep.

`docs/deploy-vps.md` is rewritten: the SSH tunnel is gone, nginx + TLS is the deployment,
and the section claiming an attacker could "act with your stored ESI tokens" is corrected —
all 24 scopes are read-only. The README no longer promises that nothing leaves your
machine, because that is the thing this step trades away. *(W2's worst endpoint deleted
rather than defended)*

Remaining, for the VPS session: point DNS, run certbot, set the six environment variables,
change the callback registration, and pin `EVE_OWNER_CHARACTER_ID` after first login.

**A deployment bug this step's work found, fixed in v0.9.74.** `python -m app.web.bootstrap` — one of the two documented ways out of the SSO lockout named above — opened `eve_cache.db` **by path** and never consulted `database_url()`. On a Postgres deployment it would either die with *"No database at …. Start the app once first"* while the app was running fine, or, if a stale SQLite file were left over from local development, mint a token into a database the app never reads: a link that simply does not work, with no error anywhere. The escape hatch failed at exactly the moment it exists for. It now uses `connect()`. The module's stated security property was SQLite-only too — "requires filesystem access to the app's database" — and now reads for both: on Postgres the equivalent is the deployment's `EVE_DATABASE_URL` and its credentials, the same set of people.

**Everything above was verified working on localhost on 2026-08-18** — first-run setup, SSO,
ownership claim, adding alts. That is new: before that session none of Step 3 had ever
completed a login, in any environment.

⚠️ **The cutover already bit — on localhost, not the VPS. Fixed in v0.9.29.** This section
treated the callback change as VPS work, but the *default* callback moved too: from the
`:5173` listener to `http://localhost:8000/callback`. `get_client_id()` fell back to a
bundled application whenever the callback was on localhost, and an EVE application has
exactly **one** registered redirect URI — which for the bundled one is still the old value,
and is not ours to edit. So a plain `python web.py` had been unable to log in since v0.9.28,
failing at CCP with *"The redirect URL does not match any of the configured values for this
client"*: an error naming neither the cause nor the fix.

The fallback is removed rather than repointed — it could not be made to work for two
deployments at once, which is the whole reason §5's "bring-your-own client ID" exists.
`get_client_id()` now returns `None`, and the settings page no longer claims a built-in
Client ID means "no setup needed".

**And an unconfigured localhost install gets a form, not an instruction.** Telling a first
run to set an environment variable and restart is a poor answer when the app is already open
in front of you, so `/auth/login` redirects to `/setup/client-id`: the registration steps,
the exact callback URL to copy, the full scope list, and a field that stores the ID. The
route is **sessionless by necessity** — the client ID is what makes a session possible — so
it is fenced twice instead, and both fences are tested:

* **Loopback Host only.** An endpoint that skips authentication *and* writes configuration
  has no business on a public host; a server deployment sets `EVE_CLIENT_ID` and gets a page
  naming that variable. Note the fence is the Host being loopback, not the allowlist —
  `testserver` is an allowed Host in the suite and still gets a 404, which is what pins the
  two checks apart.
* **Only while unconfigured.** Once an ID exists the route 404s, so it cannot be used to
  repoint a live install. 404 rather than 403: there is nothing left to do there, and
  saying "forbidden" invites probing.

Because public paths return before the gate's CSRF check, the POST has no session-bound
token to verify — so it checks `Origin` instead. A browser always sends it on a cross-origin
POST and a page cannot forge or suppress it, which is precisely the ambient-authority attack
that matters for a form reachable at `http://localhost:8000` from any site the user has
open. An absent `Origin` (curl, tests) is allowed.

**Switching applications invalidates every stored refresh token.** A refresh token is bound
to the client ID that issued it, so `grant_type=refresh_token` against a different
application fails — characters authenticated under the bundled ID must be re-added once you
register your own. This is a property of OAuth, not of the removal: it applies to anyone
making this switch, and it applies again at the VPS cutover. Removing the fallback surfaced
it, by breaking `test_token_refresh_is_serialized`, which had been quietly depending on the
bundled ID to supply *a* client ID at all. Worth stating in the deploy guide before the VPS
session rather than discovering it there.

Two more things worth carrying forward. **The lockout is real and the settings page is not
the way out** — `/settings` is behind the session gate, a session needs SSO, so the escape is
`EVE_CLIENT_ID` in the environment or `python -m app.web.bootstrap`. And the test that
should have caught the whole thing was named
`test_local_development_still_works_without_configuration` while only asserting which
constant came back; the name was a claim about the world that went false without the
assertion noticing. Same shape as the `esi_client()` docstring in Step 2 — twice in one day,
so it is worth stating as a rule: **a name that claims something about the world needs an
assertion about the world.**

**Step 4 — Platform foundations. ✅ Done — v0.9.76.** Postgres + Alembic
(decide once, not hedged); async token refresh done properly; background sync worker with
delay-after-completion + jitter; cache-only routes; ETags on every fetch; 4XX quarantine
per character; ~~verify the OpenAPI/compatibility-date situation before writing the
worker~~; split `main.py` into routers while every route is being touched anyway.
*(W6, W9, W10, W11)*

| Item | State |
|---|---|
| **Schema declared once** | ✅ **done.** `app/db/schema.py` holds all 51 tables as SQLAlchemy Core metadata. It replaced 34 DDL statements in 14 modules, 20 `ensure_*()` functions, 8 `ALTER TABLE` probes and a second copy of the SDE schema in `import_sde.py` — 549 lines deleted. Pinned by a call-site scan: no DDL may exist outside that module. **Six more shims went in v0.9.76** — `ensure_bp_table`, `ensure_assets_table`, `ensure_corp_assets_table`, `ensure_skills_table`, `ensure_project_tables`, `ensure_user_tables` — and the reason for checking rather than assuming is that `git log -S` put their last caller's removal in `5644579`, the commit that introduced migrations. They were debt that commit left, not debt the query conversion created; deleting them on a hunch would have cemented a regression instead if it had been the other way round. One of the six was also missing the dialect guard its five siblings carry, so it was dead code with a live Postgres bug inside it. |
| **Migrations** | ✅ **done.** Alembic, baseline `5c9156e72c43`, run at startup. Pre-Alembic databases are stamped rather than rebuilt. `test_the_migrations_match_the_declaration` fails if the declaration and the history drift. |
| **Postgres itself** | ✅ **done — statements v0.9.74, the app actually starting v0.9.75** (the gap between those two is the paragraph below the table, and it is the useful part). The declaration emits Postgres DDL, `EVE_DATABASE_URL` is the seam, and every hand-written statement now goes through `text()` with named binds. The count went **~316 → 145 (v0.9.65) → 8**, and the eight left are not query code: `PRAGMA`s in SQLAlchemy's connect-event handlers (`app/db/conn.py`), the same three inside `get_conn()` (`app/web/deps.py`, which go when it does), and `PRAGMA database_list` as `_ensure`'s memo key (`app/db/schema.py`, guarded by a dialect check at every caller). `tests/test_sql_portability.py` pins that with three scans — one that fails on any new raw statement, one that fails when the exemption list goes stale, and a positive control, because a scan whose healthy result is a short known list looks exactly like a scan that has stopped reading the tree.

**The flip was written down as atomic and was not.** A converted module takes its connection from `app.db.conn.connect()` while the rest still calls `get_conn()`, and both work against one database at once — which is what made it possible to go unit by unit rather than in one commit. Order mattered: **by router**, with the helper that holds its SQL, and the cache readers coming along for free. Converting `deps.py`'s readers first would have forced five routers to open their own connections in code about to be re-touched.

**Four SQLite-only constructs surfaced, and only two of them announce themselves.** `PRAGMA database_list` in a schema shim and `sqlite_master` as a table-existence probe both raise on Postgres — the second would have failed `/planets` on every request, from a table check rather than a query. The other two are silent: `LIKE` is case-insensitive on SQLite and case-sensitive on Postgres, so the plan form's product search rested on *two* SQLite behaviours where only one (`COLLATE NOCASE`) was visible in the SQL; and `json_array_length` accepts a TEXT column on SQLite but wants an explicit `::json` cast on Postgres. A conversion that translated only what it could see would have shipped both. |
| **Async token refresh** (W9) | ✅ **done.** `get_valid_token()` does a blocking 15 s `httpx.post` on expiry, and EVE access tokens last ~20 minutes, so this was the normal path rather than the exceptional one — it stopped the whole loop, not just the request. `_valid_token_async` already existed; it simply was not used at the call sites. Four of them called it *twice per character* inside a generator, once for the condition and once for the value; those now gather. |
| **Background sync worker** | ✅ **done.** `app/sync/worker.py`: one task, characters in turn, delay-*after-completion* plus ±20% jitter — a fixed schedule overlaps itself when a fetch runs long, and characters seeded at process start would otherwise come due in the same second forever. It refreshes blueprints, assets, skills, industry jobs and corp assets, and emits to `sync_events` when a collection's identity moves. "Changed" is an order-independent fingerprint of the collection, not the JSON: ESI promises no order, so comparing bodies would emit every round, and comparing counts would miss one item swapped for another. First sight is never a change — a restart must not announce everything the account owns as newly acquired. One broken character does not stop the others, and the transport's quarantine already stops a revoked token spending the error budget, so the loop needs no backoff of its own. Default-on; `EVE_SYNC_WORKER=0` switches it off. |
| **Cache-only routes** | ✅ **done — v0.9.55.** `/jobs` was the worked example: `char_jobs_cache` filled by the worker, the page reads it and never calls ESI, and it says how old the answer is — a cache-only page with no age on it is indistinguishable from a stale one. `tests/test_cache_only_routes.py` scans every route handler and fails on any that reaches ESI while rendering; what is left is named there with a reason each, so the list shrinks deliberately rather than by pattern. The scan follows same-file helpers but does **not** cross modules — `POST /prices/refresh` fetches through `prices_helper` and is invisible to it, which is written down rather than papered over. The eight pages this row used to list as outstanding — assets, blueprints, wallet, orders, contracts, planets, PI planner, plan — were all converted by v0.9.55; every name still in `ALLOWED` is now a **deliberate** exemption with a reason attached (a button the user pressed, an image proxy, a name-resolution the answer to which *is* the fetch), and `test_the_exemption_list_has_no_dead_entries` fails if one of them stops being consulted, so the list cannot rot into a place where handlers hide. |
| **ETags on every fetch** | ✅ **done.** In the transport, so every fetch gets it and no caller has to learn about 304 — a hit is replayed as the 200 it was, X-Pages included, which the paginated fetchers read to decide how many more requests to make. A 304 costs one token of the error budget instead of two and carries no body. Keyed on the URL plus a hash of the credential, because corp endpoints answer differently per member; the token itself is never stored. LRU, 32 MB. `app/market/prices.py` keeps its own persisted store — history has to be recomputed against a moving 7-day window rather than replayed — and a request that already carries If-None-Match is left alone. |
| **4XX quarantine per character** | ✅ **done.** ESI keeps one error budget for the whole client and a 4xx costs 5 tokens of it, so a character who removed the app in-game answers 401/403 forever and quietly spends the budget everyone else needs. After three consecutive refusals the transport answers that entity locally and stops putting its requests on the wire; any 2xx clears it, backoff lengthens 60s → 1h. Keyed on the entity in the URL rather than the token, because a bearer token is opaque to the transport and `/corporations/` calls are made with some member's. |
| **Split `main.py`** (W6) | ✅ **done.** 7,112 → 822 lines (784 today — v0.9.76 removed 113 unused imports the split had left behind; tree-wide pyflakes went 153 → 44 with them, and the three survivors are genuine re-exports the tests reach as `app_module.X`, now carrying a comment saying pyflakes should report exactly those three). Eleven routers, one commit each, route table checked after every one. `app/web/deps.py` holds what more than one router needs — `get_conn`, `_tr`, the template filters, the active character — and may never import `main`, which is what makes the split possible. |

Compatibility dates are struck through because that question was answered on 2026-08-18
and was already handled — see §14.

**The last statement was converted at v0.9.74 and the app still could not start on
Postgres.** Worth stating plainly, because the count is the metric this step was steered by
and the count was *right*: 8 statements, all PRAGMAs, every module portable, the whole suite
green on both backends. What no test asked was whether the process comes up. `app/db/database.py`
passed `connect_args={"timeout": 30.0}` unconditionally — a SQLite DBAPI argument — so
`create_engine` raised `ProgrammingError: invalid connection option "timeout"` **at import
time**, and `app/web/main.py` imports `get_session` from that module. Every page, on every
route, on a real deployment. It was invisible because the cross-backend tests each build
their own engine and never import the app's, which is exactly the shape of the failures §11
already collects: *a name that claims something about the world needs an assertion about the
world*, and "runs on Postgres" is a claim about the world that 1,400 passing statement-level
tests did not make.

Fixed in v0.9.75, and the fix came with the assertion that was missing:
`tests/test_app_on_postgres.py` starts the **real app in a subprocess** — one database per
process is a property of the app, so this cannot be done in-process alongside the SQLite
suite — copies `sde_base.db` into a per-process Postgres schema, and requests 20 pages.
All 20 serve 200. That is the first time any part of this project has been observed running
on Postgres rather than inferred to.

**And v0.9.76 closed four loose ends the conversion had surfaced but not fixed**, each of
which turned out wider than its description: a **live 500** on `/assets`, `/blueprints` and
`/plan` (below), the PI expiry ambiguity pinned as a reviewed decision rather than left to
be rediscovered, the six dead schema shims, and main.py's dead imports.

**The 500 was not one reader's bug, it was one line's ordering.** `load_cached_blueprints`
caught `ValueError, TypeError, KeyError` around a parse, so a payload that was valid JSON
but not a list of dicts should have read as "never synced" like every other corrupt cache.
It escaped as a 500 instead, and the reason is that `_parse_assets` opens with
`item["item_id"]` — a subscript, so a non-dict raises `TypeError`, caught — while
`_parse_blueprints` opens with `item.get("quantity", -1)` — an attribute access, so the same
payload raises `AttributeError`, not caught. The two functions are otherwise identical. The
assets readers were therefore correct **by accident**, and a refactor that reordered two
fields would have broken them silently. All three now catch both, so the guarantee no longer
depends on which access happens to come first.

**Two bugs surfaced by declaring the schema**, both live, neither visible from reading the
code that caused them:

* **Every SDE refresh silently un-indexed the database.** `_refresh_sde_from_bundle()`
  replays each table's DDL from `sqlite_master WHERE type='table'` after a `DROP TABLE`.
  Dropping a table drops its indexes; the stored table DDL does not carry them. The bundled
  `sde_base.db` ships with six and the live database had **zero**. Three of the six were
  redundant with a primary-key prefix; the three that were not include `idx_bp_product`,
  which is "which blueprint makes this item" — a full scan of `sde_blueprint_products` on
  every node of every bill of materials. A market-group lookup was measured at **2.1 ms**
  scanning 52,848 rows.
* **Four tables only existed if you had visited the right page.** `public_contracts`,
  `public_contract_items`, `public_contract_meta` and `route_jump_cache` were created
  lazily on first use, so two installs of the same version had different schemas — and the
  migration baseline would have been "whatever tables this database happens to have".

Both are the same shape as the v0.9.29 defects: invisible until something *ran*, in code
long marked done. The pattern now has a name in this project — **a name that claims
something about the world needs an assertion about the world** — and the two tests added
here are call-site scans for exactly that reason.

**Step 5 — Multi-tenancy.** Accounts, character linking, main-character pointer; every
table gains an owner scope; all reads through an account-scoped query layer with a CI lint
against raw table access; the two-synthetic-accounts leak test on every route. Still one
real user — but the schema is now safe for a second.

Step 4 leaves this cheaper than it was scoped. Three of its outputs are the same machinery
Step 5 asks for: one connection abstraction (`app/db/conn.connect()`) so there is a single
place an owner scope can be threaded through; `text()` with named binds everywhere, so
adding `AND owner_id = :owner` is an edit to a statement rather than a rewrite of a string;
and `tests/test_sql_portability.py`, which is already **the CI lint in the shape Step 5
describes** — an AST scan over `app/` that fails on any statement outside a named exemption
list. Pointing it at unscoped table access is a change of predicate, not a new mechanism.
The **order** note stands, though: Step 3 is still undone, so tenancy would land on a tool
that has never run anywhere but a laptop.

**Step 6 — Groups + coordination MVP.** Corp/alliance membership sync with revalidation;
roles; orders → commitments → assignments → job verification with the visible match
ledger; the coarse alliance read model. Joiners grant minimal scopes (identity +
`read_character_jobs`). **Pilot in own corp and measure the match-correction rate before
any alliance rollout.**

**Step 7 — Feature buildout. 0 of 8 complete; 1 in beta.** Market BI rebuild on the group
tree; refine calculator; mining ledger; appraisal; compression LP. Ordered by appetite — each
is independent once Steps 1 and 4 exist.

**Reactions board — 🟡 beta, v0.9.29, `/reactions`.** Initial deployment, not a finished
feature. Taken out of order, ahead of Steps 4-6, as a low-friction day rather than plan
progress. The costing engine is verified (see §9.1) and the page is usable, but it has not
been run against a real week of decisions, and three things it advertises do not work yet:

| Gap | State |
|---|---|
| **Sell advantage** | Column exists and is always `—`. `hub_price_cache` is empty until a hub is fetched on /prices, so the comparison has never once produced a number. Untested against real data. |
| **Import freight** | Setting is stored and never charged. Which inputs get hauled differs between the two costing models — the whole moon-goo chain under raw, only the intermediates under buy — and guessing would quietly bias every margin. |
| **Player-owned structure as a sell venue** | Not wired. `fetch_structure_market` exists but needs a market fetch tied to a docked character; the venue picker offers the four NPC hubs only. |
| **Layouts for the other five groups** | Only Composite has the two-model layout. The other 102 products use the generic one. |

Also unvalidated: the **Build Advantage** figure. The raw-cost engine cross-checks well (a
fuel block's build cost lands within 3% of its market price) but nobody has yet acted on the
number and found out whether it says what it should. It is built on pre-Step-4 foundations,
so it inherits SQLite, the unsplit `main.py` and the synchronous refresh; nothing about it
blocks Step 4, but it is one more page Step 4 will have to move.

### Why this order

* Steps 0–2 are small and improve the tool that exists **now**.
* Step 3 makes the strategic decision (hosted-only) real early, while the surface is still
  single-user — auth can be simple because there is one account to protect.
* Step 4 is the long plumbing stretch, but it is done *on a tool already in daily hosted
  use*, not ahead of one.
* Tenancy (5) lands before the first outside user exists, never retrofitted after.
* The flagship (6) ships as a pilot with real builders before the alliance depends on it.

### Rough effort

**Unit: a "session"** — one focused working block of the kind that produced Steps 0 and 1
together. That is the only calibration point that exists, and it flatters the remaining
work: Steps 0 and 1 were deliberately sequenced first *because* they were small and
self-contained. Nothing after Step 3 is like them.

Measured against the codebase as of v0.9.26: **17,575 lines of Python**, `main.py` at
**7,413** of them (42%), **77 routes** of which **22 are POST**, **310 raw `.execute()`
calls**, **47 tables**.

| Step | Effort | Confidence | What drives it |
|---|---|---|---|
| 2 — Security baseline | **1–2 sessions** | High | Bounded and well understood. CSRF is only 22 POST routes; most items are an hour each. Session auth and JWKS verification are the substantial two. |
| 3 — Go hosted | **1–2 sessions** + your VPS time | Med-high | Mostly *deletion*, which is fast. The unknown is deployment: DNS, certificates and the real SSO callback are elapsed time, not coding time. |
| 4 — Platform foundations | **3–5 sessions** | **Low** | The wall. 310 queries and 47 tables to move to Postgres, plus ~45 SQLite-specific statements (`INSERT OR REPLACE` ×25, `PRAGMA` ×12, `AUTOINCREMENT` ×4). Splitting a 7,413-line module is mechanical but large. Widest range of any step. |
| 5 — Multi-tenancy | **2–3 sessions** | Medium | Touches every table and every read, but the pattern is uniform once the query layer exists. The leak tests are what make it slow to call done. |
| 6 — Groups + coordination | **3–4 sessions** to MVP | Low-med | The flagship, and the least designed. Plus **weeks of calendar time** piloting in your own corp — the match-correction rate cannot be rushed. |
| 7 — Feature buildout | **8–14 sessions** for all of it | Medium | A menu, not a step. Each item is independent and separately estimable (below). |

**Step 7, itemised** — pick, do not commit to the total:

| Feature | Effort |
|---|---|
| Dashboard widgets (§9.6) | 0.5–1 |
| Refine calculator | 0.5–1 |
| Reactions board | 🟡 **beta v0.9.29** — 1 session spent, as estimated; finishing it is a second |
| Appraisal tool | 1 |
| Market BI rebuild (§9.4) | 1–2 |
| Mining ledger (§9.3) | 1–2 |
| Compression LP | 1–2 |
| Discord bot (§9.5) | 2–3 — identity linking is most of it |

**Totals.** Steps 2–3 (a real hosted tool, secured): **2–4 sessions**. Steps 2–6 (the
coordination product): **10–16 sessions** plus pilot time. Everything including all of
Step 7: **18–30 sessions**.

**Read these as ranges, not dates.** Two honest caveats. Step 4 is the least certain
estimate here and the most likely to double — a Postgres port surfaces its problems while
you are doing it, not while you are planning it. And Steps 0–1 each turned up real bugs
that were not in the plan (the packaged-volume error, the row-count refresh gate, the
datacore name mismatch); there is no reason to think the later steps are cleaner, so
assume some fraction of every estimate goes to things nobody has found yet.

---

## 12. Appendix A — verified game formulas

Sourced from the EVE University wiki (Industry, Trading, Invention, Research,
Manufacturing, Reprocessing), read 2026-08-17. **These are for cross-checking and for
modelling the parts that are not in the SDE.** Anything the SDE carries — recipes, yields,
times, ranks, probabilities — must still be read from the SDE, because all of it gets
rebalanced. Several of these wiki pages carry "needs updating" banners and the ore tables
contain visible transcription errors, so treat them as secondary to the SDE and to in-game
values.

### Industry job cost

```
Total job cost = EIV × ((SCI × structure bonus) + facility tax + SCC surcharge + alpha tax)
EIV            = Σ(material qty × adjusted price)     -- at ME 0, no bonuses
SCI            = system job-hours / universe job-hours, trailing 28 days
```

* Facility tax: **0.25% fixed at NPC stations**, owner-set in structures, **capped at 10%**
* **SCC surcharge: 4%** (0.25% → 0.75% 2023-07 → 1.5% 2023-09 → 4% 2024-02)
* Alpha clone tax: 0.25%, alphas only

Matches the app's current implementation.

### Material efficiency

* ME research: −1% per level, 10 levels max (−10%). TE: −2% per level, max −20%.
* **Rounding is per job, after multiplying by runs** — three runs in one job can consume
  less than three single-run jobs.
* **No material reduces below 1 per run at any ME.** Ten Jaguars always need ten Rifters.
  (Third independent confirmation, after the PI spec and the reactions sheet's
  `IFS(C=1,1,…)`.)

### Invention

```
Success = Base × (1 + (Sci1 + Sci2)/30 + Encryption/40) × (1 + DecryptorProbMod/100)
```

3.333% per science level, 2.5% per encryption level.

Base chance — **read from `sde_blueprint_products.probability`, do not hardcode**; listed
here only as a sanity check: modules/rigs/ammo 34% · frigates & destroyers 30% · cruisers,
battlecruisers, barges, haulers, intact relics 26% · battleships 22% · malfunctioning
relics 21% · freighters 18% · wrecked relics 14%.

Output BPC: **10 runs for most items, 1 run for ships and rigs**, ME 2 / TE 4 before
decryptors.

| Decryptor | Prob % | Runs | ME | TE |
|---|---:|---:|---:|---:|
| Accelerant | +20 | +1 | +2 | +10 |
| Attainment | +80 | +4 | −1 | +4 |
| Augmentation | −40 | +9 | −2 | +2 |
| Optimized Attainment | +90 | +2 | +1 | −2 |
| Optimized Augmentation | −10 | +7 | +2 | 0 |
| Parity | +50 | +3 | +1 | −2 |
| Process | +10 | 0 | +3 | +6 |
| Symmetry | 0 | +2 | +1 | +8 |

Cost model: datacores **and** the decryptor **and** one T1 BPC run are consumed whether the
job succeeds or fails. So expected cost per successful BPC is
`(datacores + decryptor + copy cost) / success chance`, amortised over
`runs × units_per_run`.

**Trap:** the EIV for an *invention* job is computed from the **T2** item's manufacturing
inputs; the EIV for a *copying* job uses the **T1** item's. Easy to get backwards.

### Research and copying

Base durations for a rank-1 blueprint, multiplied by blueprint rank:

| Level | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|---|
| Seconds | 105 | 250 | 595 | 1 414 | 3 360 | 8 000 | 19 000 | 45 255 | 107 700 | 256 000 |

```
Research cost = process_time_value × SCI × structure bonus
process_time_value(n−1 → n) = (base_n − base_{n−1}) × 0.02105 × Σ(qty × adjusted price)
process_time_value(0 → n)   =  base_n              × 0.02105 × Σ(qty × adjusted price)

Copy cost = SCI × copies × runs_per_copy × 0.02 × Σ(qty × adjusted price)
```

Copy time is **80% of build time per run**, before skills and structure bonuses, and is
**not** affected by TE. Total copy duration = base copy time × copies × runs per copy.
Max runs per copy is `sde_blueprints.max_production_limit`, already imported.

Skills: Science −5%/level copy time · Metallurgy −5%/level ME research · Research
−5%/level TE research · Advanced Industry −3%/level on all manufacturing and research.

### Reprocessing

NPC station:

```
Yield = base(30–50%) × (1 + R×0.03) × (1 + Re×0.02) × (1 + Op×0.02) × (1 + implant)
```

Upwell structure:

```
Yield = (50 + Rm) × (1 + Sec) × (1 + Sm)
      × (1 + R×0.03) × (1 + Re×0.02) × (1 + Op×0.02) × (1 + implant)
```

| Term | Values |
|---|---|
| `Rm` rig | 0 none · 1 T1 · 3 T2 |
| `Sec` | 0.00 high · 0.06 low · 0.12 null/WH — **only if rigs are fitted** |
| `Sm` structure | 0.00 other Upwell · 0.02 Athanor · 0.055 Tatara |
| `R` / `Re` / `Op` | Reprocessing · Reprocessing Efficiency · ore-specific skill, 0–5 |
| implant | 0 / 0.01 / 0.02 / 0.04 — Zainou 'Beancounter' RX-801/802/804 |

Reference points: 50% NPC station at max skills = **70%**; T2-rigged Tatara in null with max
skills and RX-804 = **90.6%**.

**Scrapmetal (modules and ships) is a different formula** — only the Scrapmetal Processing
skill applies, structure/rig bonuses and implants do not, max **55%**:

```
Yield = base × (1 + Scrapmetal Processing × 0.02)
```

**Batch sizes:** 100 units for ore and compressed ore · **1 unit** for batch-compressed ore
and for **all ice**. Types cannot be combined to make a batch — 75 Veldspar plus 25 Dense
Veldspar is not a batch. Compressed, uncompressed and batch-compressed yield the same per
batch.

NPC reprocessing tax: 5% at zero standing, falling to 0% at 6.67 standing (higher of
personal or corp standing).

### Sales tax and broker's fees

There is no single "market tax" — these are two separate charges with different trigger
conditions, and the distinction is the whole reason `SellingCosts` carries them apart.
**Sales tax** (the wallet journal calls it *transaction tax*) is paid by the seller on every
completed sale, deducted automatically before the ISK lands. **Broker's fee** is paid only
when you *place* an order — sell into someone else's buy order and you pay none. So "dump to
buy orders" and "list and wait" are genuinely different numbers.

```
sales_tax%  = 7.5% × (1 − 0.11 × Accounting)
broker%     = 3% − 0.3%×BrokerRelations − 0.03%×faction_standing − 0.02%×corp_standing   (NPC)
broker%     = 0.5% + owner%                                                              (Upwell, no skills)
relist      = (100% − (50% + 6%×AdvBrokerRelations)) × broker% × new_value
            + broker% × max(new_value − old_value, 0)
```

Minimum broker and relist fee: 100 ISK. Sales tax is always paid to the SCC and appears in
the wallet as "transaction tax".

Three properties of the broker's fee that are easy to model wrongly:

* **Standings are read unmodified.** The fee ignores Connections, Diplomacy and every other
  standing skill, while the character sheet shows the *modified* figure by default. Entering
  the modified number inflates the discount and understates the fee. The settings fields say
  "(unmodified)" and `taxes.py` carries the same warning at the coefficients.
* **It is charged on order creation, not on sale**, for any duration longer than "immediate"
  — and it is **not refunded** if the order is cancelled or expires. So it is sunk at listing
  time, which is why selling into an existing buy order carries sales tax alone. Buy orders
  pay it too; the app models the seller's side only.
* **Upwell structures ignore skills and standings entirely** — owner-set rate plus the SCC
  surcharge. Applying Broker Relations there would understate the cost of trading in nullsec.

Confirmed against the EVE University *Tax* page, 2026-08-18: the NPC equation and the 1%
floor at Broker Relations V with maximum standings both match what ships.

---

## 13. Standing risks

Distinct from §11's worry register, which lists *code* problems that specific steps retire.
These are risks to the plan itself; none of them is closed by shipping a step.

**R1 — Multi-tenancy fails silently, and the failure is someone else's wallet.**
18 query sites assume every character belongs to one person; no table has an owner column.
This is not hard work, it is *thorough* work, and thoroughness across 80 routes is exactly
where a solo developer under no deadline misses one filter. There is no error when it goes
wrong — just a corpmate seeing ISK they should not. Step 5's scoped query layer, CI lint and
two-account leak test exist specifically to make this a property of the code rather than a
property of attention. **Do not let Step 6 begin until that test passes.**

**R2 — The flagship rests on an inference that is ambiguous by construction.**
ESI industry jobs carry no project tag and never will, so matching is inferred from
character + product + runs + time window. "Auto-match with a visible ledger" (§7) is the
best available answer but it is **unvalidated socially**: if leaders do not trust the
numbers they return to spreadsheets, and the whole reason to be hosted evaporates.
Everything in Steps 1–5 is load-bearing plumbing for this. Mitigation is the corp pilot in
Step 6 with the correction-rate measured before any alliance rollout — learn it at ten
builders, not two hundred.

**R3 — The plan is larger than a solo hobby project sustains.**
Eleven features plus a platform migration plus permanent new obligations — token custody,
membership revalidation, migrations, an ops burden that does not end — on a project whose
README says features land when they are needed. Retiring the desktop removes the fallback.
The failure mode is not technical: it is stalling mid-migration with neither a maintained
desktop app nor a launched service. §11's ordering is the mitigation — every step leaves a
working, in-use tool — but the risk is real and should be re-read at each step boundary.

**R4 — Custodianship arrives before the systems for it.**
The moment a second person logs in, this holds someone else's refresh tokens. Encryption at
rest, a revocation path and a stated data policy are Step 5/6 work that is easy to defer
past the first joiner. Do not.

---

## 14. Open questions

1. Built vs delivered as separate states in production scheduling (§7)?
2. Localised client names in the appraisal parser — support or explicit limitation?
3. Alliance-leader authority: is "director in the executor corp" the accepted proxy?
4. Reallocation policy for abandoned commitments.
5. ~~Does `planetResources` replace the hardcoded matrix in `planet_data.py`?~~ **No — answered 2026-08-18.** `planetResources.jsonl` is 25,798 records keyed by *planet id* carrying `power` and `workforce`: the Equinox sovereignty system, nothing to do with PI extraction. The P0-per-planet-type matrix in `planet_data.py` stays hardcoded.
6. Reactions board — inside Step 1, or its own step? It is the biggest quick win and the
   most tempting scope creep inside a correctness sprint.
7. ~~**Wire invention into `BOMResolver`.**~~ **Done 2026-08-18 (v0.9.29).** Charged at
   every manufacturing node, and the optimizer counts it on the make side. Doing it
   corrected two claims in §8: `/plan` was charging invention *nowhere*, not merely missing
   nested components, and the affected nested set is 130 products of which none is a
   capital. See §8 for both.
8. **Verify the sales tax and broker's fee rates in game.** `app/market/taxes.py` ships 7.5% sales tax,
   3% NPC broker fee, −11%/level Accounting and −0.3%/level Broker Relations. These come
   from the Version 22.02 patch note and the EVE University *Tax* page, cross-checked
   against the 1% NPC broker floor — but the *Trading* page on the same wiki was found to
   carry stale worked totals, so secondary sources have already failed once here. The
   check is cheap and conclusive: sell one item, then compare the wallet journal's
   transaction tax and broker fee against what the tool predicts. Re-check after any patch
   touching market fees. Until then every profit figure inherits this assumption.

9. **An uninvited character's refresh token is stored before it is refused.**
   `complete_login()` calls `save_tokens()` and *then* `/callback` checks
   `may_sign_in()` — so a stranger who reaches the URL and completes SSO has their
   character row and refresh token written into the owner's database, and only the
   session is denied. Found 2026-08-18 while fixing the add-character flow, which
   relies on exactly that ordering (the alt must be stored for "Add Character" to
   mean anything).

   Harmless on localhost, and no data flows *to* the stranger. It mattered at Step 3's
   VPS: **R4 arriving uninvited** — custody of someone else's ESI token with no consent,
   no policy and no revocation path, obtained by anyone who finds the URL.

   **Closed 2026-08-18.** The refusal branch now discards what `complete_login()` just
   wrote, so a refused login leaves nothing behind. The subtlety is *which* characters
   may be deleted: only one this login created. `/callback` snapshots the known
   character IDs **before** the exchange, because afterwards a first-time arrival and a
   re-authentication are indistinguishable — and the owner's session can plausibly
   expire during the round trip to EVE, which would otherwise turn a refused re-auth
   into silent deletion of a real character. Two tests hold the line apart: an
   uninvited character must be gone, a previously-added one must survive.

### Reference material not yet read

Relevant to the plan, unread as of this writing:

* EVE University wiki: **Compression**, **Moon mining**, **Hauling** (freight rates for the
  sell-advantage metric in §9.2), **Reactions**
* ~~**ESI versioning via compatibility dates**~~ — **answered 2026-08-18, and it was
  already handled.** `app/esi/client.py` pins `X-Compatibility-Date: 2026-07-17` on every
  client it builds; URLs stay on `/latest`, where the header takes precedence. So Step 4
  does *not* touch every fetcher, and the sync worker inherits the pin for free. The date is
  a deliberate-upgrade knob, not something to discover at runtime.

  Checking it did turn up a real gap, though — see the note below.
