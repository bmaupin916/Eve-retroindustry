# Deploying

EVE Retroindustry is a hosted web service. There is no desktop build any more: no
installer, no AppImage, no APK, no in-app updater. You run one process behind
nginx and update it with `git pull`.

This guide replaces the SSH-tunnel version. That one existed because the app had
no authentication and could not safely be exposed; it now has session login, CSRF
tokens and Host validation, so it can be.

## What it needs

- Debian 12 / Ubuntu 22.04+ (Python 3.11+; Ubuntu 24.04 ships 3.12)
- ~1 GB RAM, ~3 GB disk — the SDE, market caches and icons grow over time
- A domain name pointing at the box
- Outbound HTTPS to `esi.evetech.net`, `login.eveonline.com` and
  `developers.eveonline.com` (the SDE feed). That is everything it talks to.

## Before you start: the callback URL

This is the one step that will bite you, so do it deliberately.

EVE compares the `redirect_uri` against your application's registration **as an
exact string**, and an application has **one** registered URI. So moving the app
is a cutover, not an addition:

1. Go to <https://developers.eveonline.com/> → your application.
2. Set the callback URL to `https://YOUR-DOMAIN/callback`.
3. Set `EVE_CALLBACK_URL` to the identical string (step 5 below).

Between changing the registration and the app running at the new address, SSO
login will not work. That is expected, and it is what `python -m app.web.bootstrap`
is for — see [Locked out](#locked-out).

## 1. System packages

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip git nginx
```

## 2. Service user and clone

```bash
sudo adduser --system --group --home /opt/eve-retroindustry eve
sudo -u eve git clone https://github.com/EVERetroIndustry/Eve-retroindustry.git /opt/eve-retroindustry
```

## 3. Virtual environment

`requirements.txt` no longer carries the desktop GUI stack, so this is a small
install now.

```bash
cd /opt/eve-retroindustry
sudo -u eve python3 -m venv .venv
sudo -u eve .venv/bin/pip install -r requirements.txt
```

## 4. Import the SDE

```bash
sudo -u eve .venv/bin/python import_sde.py
```

Takes a few minutes and several hundred MB of downloads. The build number it
pins is recorded in the database, so a later run knows whether it has anything
to do.

## 5. Configuration

Everything that differs between a laptop and a server is an environment
variable. Put them in `/etc/eve-retroindustry.env`, owned by root, mode 0600:

```ini
# Where the app is reachable. The Host header is checked against this, which is
# what stops DNS rebinding — do not use a wildcard.
EVE_ALLOWED_HOSTS=industry.example.com

# Must match the callback URL registered with CCP, exactly.
EVE_CALLBACK_URL=https://industry.example.com/callback

# Session cookies get the Secure flag. Set this once TLS is terminating, and
# not before, or you will not be able to log in over plain HTTP.
EVE_COOKIE_SECURE=1

# Pin the account owner. Without it, the first character to complete SSO claims
# the instance — fine on a laptop, a real window on a public box between
# deploying and logging in. Set it to your own character id.
EVE_OWNER_CHARACTER_ID=

# Writable data directory: database, config, caches.
EVE_APP_DIR=/opt/eve-retroindustry
```

## 6. systemd unit

`/etc/systemd/system/eve-retroindustry.service`:

```ini
[Unit]
Description=EVE Retroindustry
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=eve
Group=eve
WorkingDirectory=/opt/eve-retroindustry
EnvironmentFile=/etc/eve-retroindustry.env
ExecStart=/opt/eve-retroindustry/.venv/bin/uvicorn app.web.main:app \
          --host 127.0.0.1 --port 8000 --proxy-headers \
          --forwarded-allow-ips 127.0.0.1
Restart=on-failure
RestartSec=5

# The database holds every refresh token. The app chmods it 0600 itself; these
# keep the rest of the process boxed in.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/eve-retroindustry

[Install]
WantedBy=multi-user.target
```

It still binds `127.0.0.1`. nginx is the only thing that talks to it.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now eve-retroindustry
sudo systemctl status eve-retroindustry
```

## 7. nginx and TLS

```bash
sudo apt install -y certbot python3-certbot-nginx
```

`/etc/nginx/sites-available/eve-retroindustry`:

```nginx
server {
    listen 80;
    server_name industry.example.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Price refreshes stream progress over SSE and must not be buffered.
        proxy_buffering off;
        proxy_read_timeout 600s;
    }
}
```

`proxy_set_header Host $host` matters: the app checks the Host header against
`EVE_ALLOWED_HOSTS`, and the default `$proxy_host` would make every request look
like it was for `127.0.0.1:8000`.

```bash
sudo ln -s /etc/nginx/sites-available/eve-retroindustry /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d industry.example.com
```

Certbot rewrites the file for TLS and sets up renewal. Set `EVE_COOKIE_SECURE=1`
only after this succeeds.

## 8. First login

Open `https://industry.example.com/`. You will be redirected to EVE SSO and back
to `/callback`, which sets the session cookie.

If you left `EVE_OWNER_CHARACTER_ID` empty, the first character through claims
the instance and the log says so — fill the variable in with that id and
restart, so it cannot be claimed by anyone else later.

## Locked out

If SSO cannot be used — the registered callback does not match yet, or you are
mid-cutover — mint a session directly on the box:

```bash
sudo -u eve EVE_APP_DIR=/opt/eve-retroindustry .venv/bin/python -m app.web.bootstrap
```

It prints a single-use link, valid for ten minutes. Minting one requires
filesystem access to the database, so it grants nothing to anyone who does not
already have the server.

## Updating

```bash
cd /opt/eve-retroindustry
sudo -u eve git pull
sudo -u eve .venv/bin/pip install -r requirements.txt
sudo systemctl restart eve-retroindustry
```

Re-run `import_sde.py` after an EVE expansion. It compares the build number and
exits quickly when there is nothing new.

## What an attacker gets

Worth being accurate about, because the previous version of this document was
not. All 24 requested scopes are **read-only**, `esi-planets.manage_planets.v1`
included. Someone with your session or your database gets complete financial and
asset intelligence, and can exfiltrate refresh tokens — but cannot move ISK,
assets or jobs.

Tokens are still stored unencrypted; encryption at rest arrives with
multi-tenancy, when the database stops holding only your own tokens. Until then
the protections are file permissions, one account, and a box only you can reach.
