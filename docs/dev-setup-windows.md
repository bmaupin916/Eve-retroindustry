# Development Setup — Windows

Running EVE Retroindustry from source on Windows 10/11.

## 1. Install Python

Requires **Python 3.11+**; CI builds on **3.12**, so that's the safest choice.

Check what you have:

```powershell
python --version
```

If it's missing or older, install 3.12 from [python.org](https://www.python.org/downloads/) and tick
**"Add python.exe to PATH"** on the first installer screen.

## 2. Open PowerShell in the repo

```powershell
cd C:\path\to\Eve-retroindustry
```

## 3. Create and activate a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell refuses with *"running scripts is disabled on this system"*, run this once and retry:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

The prompt should now be prefixed with `(.venv)`.

## 4. Install dependencies

```powershell
pip install --upgrade pip
pip install -r requirements.txt
```

Small and quick now — the desktop GUI stack (pywebview, PyQt6, QtWebEngine) was
retired in v0.9.28 along with the desktop app, and it accounted for most of the
old ~150 MB install.

> **You do need to run `import_sde.py` once.** The app no longer copies a prebuilt
> `sde_base.db` into place — that file is a test fixture now, not a runtime source, so a
> fresh `eve_cache.db` has no game data until you import some. Every page redirects to
> `/setup` until you have.

It downloads CCP's static data export itself and needs nothing that isn't already in
`requirements.txt`. If you already have an archive under `data/sde-archives/`, point at it
with `--zip` and the import takes seconds instead of minutes:

```powershell
python import_sde.py --out sde_base.db --fresh
```

That fetches the newest build (~95 MB, cached under `data/sde-archives/`) and rebuilds the bundle in a
couple of seconds. Pin a build with `--build 3470007`, or reuse an archive already on disk with
`--zip`. The build number is recorded in the database, which is how the app decides whether a user's
copy is stale — see `app/sde/feed.py`.

> Earlier versions of this script parsed a 150 MB `types.yaml` out of a hand-populated `data/` folder
> using PyYAML, which is not in `requirements.txt`. That is why this section used to say "do not run
> it". Both problems are gone.

## 5. Run the dev server

```powershell
python web.py
```

Uvicorn starts on `127.0.0.1:8000` with auto-reload. Open **http://localhost:8000**.

Every page redirects to `/setup` until you have imported static data — the app no longer copies a
prebuilt SDE into place. `/setup` tells you the command to run.

Equivalent, if you prefer the explicit form:

```powershell
uvicorn app.web.main:app --reload --port 8000
```

## 6. Log in to EVE

Open <http://localhost:8000/> and you will be redirected to EVE SSO, then back to
`/callback` — a route on this app, which is where the session cookie is set.

For local development a fallback SSO client ID is built in, so there is nothing
to register. It applies **only** when the callback is on `localhost`; any other
callback requires the deployment to name its own application via `EVE_CLIENT_ID`,
because an EVE application has exactly one registered callback URL and CCP
matches it as an exact string.

The one thing that can block it:

- **The registered callback must be exactly `http://localhost:8000/callback`.**
  If you run the dev server on another port, or the built-in application's
  registration has moved, the token exchange fails. Set `EVE_CALLBACK_URL` to
  whatever is actually registered.

If you cannot log in at all, mint a session without SSO:

```powershell
python -m app.web.bootstrap
```

It prints a single-use link valid for ten minutes.

Add alts with **+ Add Character** in the character dropdown. Note that only the
instance owner can hold a session — adding a character stores its tokens but is
not a second way to log in.

## 7. Run the tests

```powershell
pip install -r requirements-dev.txt
pytest
```

The test fixtures build a temp database from the committed `sde_base.db`, so no login or network is
needed.

### The Postgres half

The app runs on SQLite and Postgres, and roughly a third of the suite is parameterised over both.
Without a Postgres server those halves **skip silently**, so a suspiciously fast run means a stopped
container rather than a fast machine — read the skip count, not the clock. A healthy full run today
is about six minutes and reports **2 skipped**. Both are real platform facts rather than deferred
work, and it is worth knowing which two so a third stands out: one is POSIX file modes, which
Windows does not enforce, and the other is a test that lowers a SQLite compile-time limit and has
no Postgres equivalent, so it skips on that backend's half. A count above two means something
else is skipping — most likely the Postgres container.

```powershell
docker start eve-pg
```

If the container does not exist yet:

```powershell
docker run -d --name eve-pg -e POSTGRES_PASSWORD=eve -e POSTGRES_USER=eve -e POSTGRES_DB=eve_retroindustry -p 5433:5432 postgres:17
```

**The host port must stay below 49152.** Windows' dynamic port range is 49152–65535 and WinNAT
reserves blocks inside it at boot, so a port up there can be taken away by a reboot — see
[working-notes.md](working-notes.md) for the incident that moved it to 5433.

## Local data files

All written next to the repo root in dev mode, or into `EVE_APP_DIR` when it is set:

| File | Contents |
|---|---|
| `eve_cache.db` | SDE tables, blueprints, assets, prices, projects, OAuth tokens |
| `.eve_config.json` | SSO client ID |
| `icon_cache/` | Item icons, portraits and corp logos |

They're all in `.gitignore`. Deleting `eve_cache.db` resets the app to a first-run state: the schema
is rebuilt by the baseline migration, but the static data is **not** — re-run `import_sde.py`, or
point it at an archive already under `data/sde-archives/` with `--zip`, which takes seconds. You'll
need to log in again, and that drops the owner claim, so the first character to log in afterwards
takes the instance.

## Next time

```powershell
cd C:\path\to\Eve-retroindustry
.\.venv\Scripts\Activate.ps1
python web.py
```
