# Running on a VPS (headless Linux)

Running EVE Retroindustry as a long-lived service on a remote Debian/Ubuntu box, so it keeps syncing
and you can reach it from any machine.

> ## Read this first — the app has no authentication
>
> There is no login, password or session check in front of the web UI. Anyone who can reach port 8000
> can read your assets and wallets, browse your blueprints, and act with your stored ESI tokens.
> **Never bind it to a public interface and never open port 8000 in the firewall.** This guide keeps
> the server on `127.0.0.1` and reaches it over an SSH tunnel. If you'd rather expose it properly, see
> [Optional: nginx + TLS + basic auth](#optional-nginx--tls--basic-auth) — but the tunnel is simpler
> and strictly safer.

## Requirements

- Debian 12 / Ubuntu 22.04+ (Python 3.11+; Ubuntu 24.04 ships 3.12)
- ~1 GB RAM, ~3 GB disk (the SDE, market caches and icon cache grow over time)
- Outbound HTTPS to `esi.evetech.net` and `login.eveonline.com` — that's all the app talks to

## 1. System packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
python3 --version   # must be 3.11 or newer
```

## 2. Create a service user and clone

```bash
sudo adduser --system --group --home /opt/eve-retroindustry eve
sudo -u eve git clone https://github.com/EVERetroIndustry/Eve-retroindustry.git /opt/eve-retroindustry
cd /opt/eve-retroindustry
```

## 3. Virtual environment and dependencies

Skip the desktop GUI stack — `pywebview`, `PyQt6`, `PyQt6-WebEngine` and `QtPy` are only used by
`launcher.py`, they pull ~150 MB and a pile of X11 libraries you don't want on a server:

```bash
sudo -u eve python3 -m venv /opt/eve-retroindustry/.venv
sudo -u eve /opt/eve-retroindustry/.venv/bin/pip install --upgrade pip
grep -vE '^(pywebview|PyQt6|QtPy)' requirements.txt | sudo -u eve tee requirements-server.txt >/dev/null
sudo -u eve /opt/eve-retroindustry/.venv/bin/pip install -r requirements-server.txt
```

> **Do not run `import_sde.py`.** It rebuilds the game database from raw SDE YAML in a `data/` folder
> that isn't in the repo. `sde_base.db` is committed at the repo root and the app bootstraps
> `eve_cache.db` from it on first startup.

## 4. Smoke-test it by hand

```bash
sudo -u eve /opt/eve-retroindustry/.venv/bin/uvicorn app.web.main:app --host 127.0.0.1 --port 8000
```

Leave it running and, from another shell on the VPS:

```bash
curl -sI http://127.0.0.1:8000/ | head -1
```

Expect a `200` or a `307` redirect to `/setup` (the SDE bootstrap gate). Then Ctrl-C.

## 5. Install the systemd service

```bash
sudo tee /etc/systemd/system/eve-retroindustry.service >/dev/null <<'EOF'
[Unit]
Description=EVE Retroindustry
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=eve
Group=eve
WorkingDirectory=/opt/eve-retroindustry
ExecStart=/opt/eve-retroindustry/.venv/bin/uvicorn app.web.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/eve-retroindustry

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now eve-retroindustry
systemctl status eve-retroindustry
```

Note there is **no `--reload`** — that's a dev-only flag and it doubles memory use.

`WorkingDirectory` matters: `eve_cache.db`, `.eve_config.json` and `icon_cache/` are written relative
to it. To keep data outside the checkout instead, add
`Environment="EVE_APP_DIR=/var/lib/eve-retroindustry"` and create that directory owned by `eve` —
the app reads `EVE_APP_DIR` for all writable state, while still reading `sde_base.db` from the repo.

## 6. Reach it from your laptop over SSH

Forward **both** ports — 8000 for the UI, 5173 for the EVE SSO callback:

```bash
ssh -L 8000:127.0.0.1:8000 -L 5173:127.0.0.1:5173 you@your-vps
```

From Windows PowerShell the command is identical (OpenSSH ships with Windows 10+). Leave that session
open and browse to **http://localhost:8000**.

## 7. Log in to EVE

The login flow needs a little care on a headless box:

1. Click **Log In**. The server tries to spawn a browser with `xdg-open`, which fails on a VPS — that's
   expected and harmless.
2. The waiting page offers an **"Open in this window"** link. Click it. That takes *your* browser to
   `login.eveonline.com`.
3. After you authorise, EVE SSO redirects your browser to `http://localhost:5173/callback`. Your SSH
   tunnel forwards that to the callback server listening on the VPS, which stores the tokens and sends
   you back to the app.

**Port 5173 must be free on your laptop** for the duration — the redirect goes to localhost first, and
anything else holding that port (a Vite dev server, typically) will swallow the callback. If login
times out, that's the first thing to check.

The callback URL is fixed at `http://localhost:5173/callback` (`CALLBACK_PORT` in
`app/auth/esi_oauth.py`), because it's registered against the SSO client ID compiled into
`app/auth/token_store.py`. Changing it means registering your own application at
developers.eveonline.com *and* editing that constant.

## Day-to-day operations

**Logs**

```bash
journalctl -u eve-retroindustry -f
journalctl -u eve-retroindustry --since '1 hour ago'
```

**Update to a newer version**

```bash
cd /opt/eve-retroindustry
sudo -u eve git pull
grep -vE '^(pywebview|PyQt6|QtPy)' requirements.txt | sudo -u eve tee requirements-server.txt >/dev/null
sudo -u eve .venv/bin/pip install -r requirements-server.txt
sudo systemctl restart eve-retroindustry
```

The in-app updater is for the packaged desktop builds — on a source checkout, use `git pull`. A newer
`sde_base.db` is picked up automatically on the next start: the app compares type/group counts and
refreshes the SDE tables in place, leaving your characters, prices and projects untouched.

**Back up**

`eve_cache.db` holds everything that matters — characters, OAuth tokens, cached prices and saved
projects. SQLite is being written to live, so copy it properly:

```bash
sudo -u eve sqlite3 /opt/eve-retroindustry/eve_cache.db ".backup '/opt/eve-retroindustry/backup.db'"
```

Treat that file as a secret: it contains refresh tokens for every character you've added.

**Reset**

Stop the service, delete `eve_cache.db`, start it again — the app rebuilds from `sde_base.db` and you
re-add characters.

## Optional: nginx + TLS + basic auth

Only if you really want browser access without a tunnel. The app trusts every request it receives, so
the reverse proxy is the *only* thing standing between the internet and your ESI tokens.

```bash
sudo apt install -y nginx apache2-utils certbot python3-certbot-nginx
sudo htpasswd -c /etc/nginx/.htpasswd yourname
```

```nginx
server {
    server_name eve.example.com;

    auth_basic           "EVE Retroindustry";
    auth_basic_user_file /etc/nginx/.htpasswd;

    location / {
        proxy_pass         http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header   Upgrade    $http_upgrade;   # SSE / setup progress stream
        proxy_set_header   Connection "";
        proxy_set_header   Host       $host;
        proxy_read_timeout 300s;                       # long ESI syncs
        proxy_buffering    off;                        # SSE
    }
}
```

```bash
sudo certbot --nginx -d eve.example.com
```

Keep the uvicorn bind on `127.0.0.1` so the only way in is through nginx, and keep port 8000 closed in
your firewall/security group.

**This does not solve login.** The SSO callback still lands on `localhost:5173`, so adding a character
continues to require the SSH tunnel from step 6. Do the tunnel once to add your characters; after that,
token refresh is automatic and the web UI works fine over nginx alone.
