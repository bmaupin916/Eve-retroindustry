# EVE Retroindustry

A self-hosted industry service for EVE Online — blueprint cost analysis, bill of materials expansion, Jita market pricing, asset tracking, contract browsing, planetary interaction timers, and production project management. Multi-character support: load all your alts and switch between them per page.

> **Note on the project.** I build this primarily for my own EVE career — features land when I need them, and priorities follow whatever I'm doing in-game. It's shared publicly as-is: if you find it useful, you're welcome to use it. There's no support commitment or roadmap promise, but bug reports and ideas are welcome via [Issues](https://github.com/EVERetroIndustry/Eve-retroindustry/issues).

> **The desktop app is retired.** Versions up to v0.9.26 shipped as a Windows
> installer, a Linux AppImage and an experimental APK. Those are gone: there is
> no installer, no bundled browser, no in-app updater. It is a web service you
> run yourself, behind your own TLS, reachable from any of your machines.
> Released desktop builds keep working but receive no updates.

**[Screenshots → everetroindustry.github.io](https://everetroindustry.github.io)**

![Dashboard — multi-character overview](docs/screenshots/dashboard.png)

---

## Features

- **Multi-character Dashboard** — log in any number of alts via EVE SSO; see all characters at a glance with portrait, corporation, current docked location, the skill in training with a live countdown, asset count, and estimated net worth. A **Total available cash** tile sums wallet ISK across every character
- **Production Planner** — enter any ship or component, pick a station, get a full bill of materials with Jita buy/sell prices, your asset coverage, manufacturing job time and fees (EIV × SCI × facility tax × SCC), profit vs. market and vs. stock, and the cheapest make-vs-buy decomposition. Inputs can be priced at **Jita sell** (instant-buy) or **Jita buy** (buy orders, how manufacturers actually source), and the **make-vs-buy optimiser weighs job install fees** — so it only builds a component when building genuinely wins
- **Blueprint Library** — full character (and alt) blueprint list with ME/TE levels, BPO vs BPC, runs remaining, organised by station and container
- **Asset Tracking** — character + corporation inventory grouped by location and container (incl. all corp hangar divisions), with estimated ISK value per stack and per station

![Production Plan — Raven (ME 10 / TE 20)](docs/screenshots/production-plan.png)

- **Jita Price Cache** — fetches live market data from ESI, caches locally, refresh on demand; secondary trade hubs (Amarr / Dodixie / Rens / Hek) and any custom station/citadel can be pulled in for side-by-side price comparison. Click any item for a **price-history chart** and the **live regional order book**, as you'd see it in-game

![Prices — Jita + secondary hubs, filtered to the Battleship group](docs/screenshots/prices.png)

- **Structure & Rig Modelling** — supports Raitaru / Azbel / Sotiyo / Athanor / Tatara with per-slot rig selection; ME/TE bonuses applied correctly with security multiplier (highsec 1.0× / lowsec 1.9× / null 2.1×)
- **Production Projects** — save a plan as a project, track which jobs are done, and get a unified shopping list across multi-stage manufacturing
- **Market Orders** — open buy/sell orders for every character and corporation, split into active vs. completed/expired. Active orders show the ISK **still on the market** (unsold units × price) with a per-section total, an in-game style **days/hours expiry countdown**, and clicking an item opens the order book with **your own order highlighted** so you can see where you sit among the competition

![Market Orders — active buy/sell across all characters](docs/screenshots/orders.png)

- **Industry Jobs** — running and finished manufacturing/reaction jobs, with per-character slot usage (used / available, derived from skills)

![Industry Jobs — running jobs with per-character slot usage](docs/screenshots/jobs.png)

- **Planetary Interaction** — every character's colonies in one place, à la RIFT: extractor programs with a **live countdown to expiry** (red when expired, amber under 24 h, sorted soonest-first), what each head is pulling, the colony's **factory production chains** (output ← inputs, straight from the SDE), stored contents, and an estimated output value per day. A dashboard tile and a nav badge warn you when extractors are about to run dry

![Planetary Interaction — colonies, extractor timers and factory chains](docs/screenshots/planets.png)

- **Contracts** — browse your own **personal + corporation** contracts, plus a **public contract browser**: index a whole region once, then search it locally by item, type, or price (ESI exposes no contract search, so the region is fully indexed into a local cache). Public contract prices can be pulled straight into the Production Planner for a side-by-side profit comparison against market prices
- **Wallet** — personal and corporation wallet balances
- **Margin Tracker** — a watchlist of build margins, priced entirely from cache, with a daily snapshot behind the change and 7-day-average columns. Every figure is net of the install fee, of invention (datacores, decryptor and the failed attempts, amortised over the invented BPC's runs), and of what it costs to sell — sales tax always, broker's fee only when you list an order
- **Reactions Board** *(beta)* — every published reaction priced and ranked in one page, *including the ones losing money*, which a watchlist structurally cannot show you. Costed on direct inputs at market; rows with an unpriced input or no real bid are demoted and labelled rather than allowed to top the ranking. Filtering to **Composites** switches to a two-model layout — buy the intermediates versus build them from raw — with the Build Advantage between them, export freight, and ISK per slot-hour over a 7-day window. Early days: the sell-advantage column needs a secondary hub fetched before it reports anything, import freight is not charged yet, player-owned structures are not selectable as a sell venue, and only Composites has the two-model layout

![Assets — inventory across all characters and corporation hangars](docs/screenshots/assets.png)

---

## Running it

It runs as one process behind nginx. See **[docs/deploy-vps.md](docs/deploy-vps.md)**
for the full walkthrough — service user, systemd unit, TLS, and the one step that
catches everyone: EVE compares the callback URL against your application
registration as an exact string, and an application has only one, so moving the
app is a cutover rather than an addition.

Locally, for development, see below.

---

## Development Setup

Requires Python 3.11+.

```bash
git clone https://github.com/EVERetroIndustry/Eve-retroindustry.git
cd Eve-retroindustry
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Import the Static Data Export (SDE) — required, not optional. The app ships no
game data and cannot calculate anything without it:

```bash
python import_sde.py
```

It pulls a build-pinned archive straight from CCP, so what you get is current
rather than as current as the last release. Until it has run, every page
redirects to a page telling you so.

Run the dev server:

```bash
uvicorn app.web.main:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000). On a fresh install you land on a
one-time setup page that walks through registering your own EVE application and stores
the Client ID for you — there is no bundled one to borrow, because an application has
exactly one registered callback URL and so cannot be shared between deployments.

That page is offered on **localhost only**, and only until it is done. A server
deployment sets `EVE_CLIENT_ID` in its environment instead; see
[docs/deploy-vps.md](docs/deploy-vps.md).

After setup you are sent to EVE SSO and back to `/callback`, which is where the session
cookie is set — so the callback URL registered at
[developers.eveonline.com](https://developers.eveonline.com/) has to be exactly
`http://localhost:8000/callback`, or set `EVE_CALLBACK_URL` to whatever you did
register. **The first character to log in claims the instance**; other characters can be
added and their data synced, but only the owner holds a session.

If you cannot log in at all — usually because that registration does not match
yet — mint a session directly:

```bash
python -m app.web.bootstrap
```

It prints a single-use link valid for ten minutes.

---

## Releasing

There are no build artifacts. Deploying is `git pull`, `pip install -r
requirements.txt` and a service restart — see
[docs/deploy-vps.md](docs/deploy-vps.md).

Version tags are still cut for the changelog and to mark what is deployed:

```bash
git tag v0.x.y && git push origin v0.x.y
```

---

## Tech Stack

| Layer | Library |
|---|---|
| Web framework | FastAPI + Uvicorn |
| Templates | Jinja2 + Bootstrap 5 (dark) |
| Database | SQLite via sqlite3; schema declared once as SQLAlchemy Core metadata |
| Migrations | Alembic (`app/db/schema.py` is the single source of truth) |
| EVE API | ESI (esi.evetech.net) |
| HTTP client | httpx (async) |
| Auth | EVE SSO (OAuth2 PKCE), JWKS-verified tokens, DB-backed sessions |
| Deployment | nginx + TLS, systemd |

---

## Data & Privacy

The desktop app could promise that nothing left your machine. A hosted service
cannot make that claim, and this README used to lead with it, so here is the
accurate version: **your data lives on whatever server you deploy this to.**
Self-hosted, that is a machine you control; it is not the same promise.

| File | Contents |
|---|---|
| `eve_cache.db` | Blueprints, assets, prices, projects, OAuth tokens for all characters |
| `.eve_config.json` | EVE SSO client ID only |
| `icon_cache/` | Item icons, portraits and logos, downloaded once from the EVE image server |

Nothing is sent anywhere other than the official EVE Online ESI API
(`esi.evetech.net`), the EVE SSO login server (`login.eveonline.com`) and CCP's
SDE feed (`developers.eveonline.com`).

The database holds every character's refresh token. The app restricts it to the
owning user, and all 24 requested scopes are read-only — someone who obtained it
would get complete financial and asset intelligence, but could not move ISK,
assets or jobs. Tokens are not yet encrypted at rest.

Static data is fetched once and kept locally — item icons and portraits, station/planet names, jump distances, and market history (revalidated with ETags, so unchanged data costs no download). Bootstrap and its icon font are bundled, not loaded from a CDN, so the interface renders without a network connection.

---

## Legal

EVE Online® and Fenris Creations™ and all related logos and other elements are trademarks of Fenris Creations (formerly CCP Games). All rights are reserved worldwide. This application is not endorsed by or affiliated with Fenris Creations.

Market data and character information are fetched from the [EVE Swagger Interface (ESI)](https://esi.evetech.net) under the EVE Online developer license.

---

## License

MIT — see [LICENSE](LICENSE)
