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

PyQt6 + QtWebEngine account for most of the ~150 MB and several minutes — they're only needed for the
desktop-window mode in step 6. For browser-only use you can skip them:

```powershell
Get-Content requirements.txt | Where-Object { $_ -notmatch '^(pywebview|PyQt6|QtPy)' } | Set-Content requirements-web.txt
pip install -r requirements-web.txt
```

> You don't need to run `import_sde.py` for normal development. `sde_base.db` is committed at the repo
> root and the app copies the SDE tables out of it into `eve_cache.db` automatically on first startup.

It is safe to run when you *do* want to refresh the game data — it downloads CCP's static data export
itself and needs nothing that isn't already in `requirements.txt`:

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

The first request populates `eve_cache.db` from the bundled SDE — the app redirects to `/setup` until
that finishes, which takes a few seconds.

Equivalent, if you prefer the explicit form:

```powershell
uvicorn app.web.main:app --reload --port 8000
```

## 6. Optional — run as the desktop app

```powershell
python launcher.py
```

Same FastAPI server, wrapped in a pywebview window with the system tray icon — the way the packaged
release behaves. Requires the PyQt6 packages from step 4.

## 7. Log in to EVE

Click **Log In** in the top right. A default SSO client ID is compiled into
`app/auth/token_store.py`, so there's no need to register an application at
developers.eveonline.com.

Two things that can block the callback:

- **Port 5173 must be free.** The OAuth callback server binds `http://localhost:5173/callback`
  (`CALLBACK_PORT` in `app/auth/esi_oauth.py`). Vite dev servers commonly squat there.
- **Allow Python through the Windows Firewall** if a prompt appears.

Add alts with **+ Add Character** in the character dropdown.

## 8. Run the tests

```powershell
pip install -r requirements-dev.txt
pytest
```

The test fixtures build a temp database from the committed `sde_base.db`, so no login or network is
needed.

## Local data files

All written next to the repo root in dev mode (the frozen builds use a per-user app dir instead):

| File | Contents |
|---|---|
| `eve_cache.db` | SDE tables, blueprints, assets, prices, projects, OAuth tokens |
| `.eve_config.json` | SSO client ID |
| `icon_cache/` | Item icons, portraits and corp logos |

They're all in `.gitignore`. Deleting `eve_cache.db` resets the app to a first-run state — it will
rebuild from `sde_base.db` and you'll need to log in again.

## Next time

```powershell
cd C:\path\to\Eve-retroindustry
.\.venv\Scripts\Activate.ps1
python web.py
```
