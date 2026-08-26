"""Mint a session without going through SSO.

    python -m app.web.bootstrap

There is exactly one way into the app — EVE SSO — and it depends on the redirect
URI registered with CCP matching EVE_CALLBACK_URL exactly. An application has one
registered URI, so moving the app from a laptop to a domain is a cutover, and for
the minutes between changing one and the other there is no way to log in. This is
the way back in.

It prints a single-use link. The link carries a token, not a session: the token
is consumed by ``/auth/bootstrap``, which is what actually sets the cookie, so a
URL left in shell history is worthless once used and worthless after ten minutes
either way.

Requires access to the app's database, which is the point — it is available to
whoever runs the server and to nobody else. On SQLite that means filesystem
access to the file; on Postgres it means the deployment's `EVE_DATABASE_URL`
with its credentials. Same property, and the same set of people.

**It talks to whichever database the app is configured for.** Until v0.9.74 this
opened `eve_cache.db` by path and never consulted `database_url()`, so on a
Postgres deployment it either died with "No database at …, start the app once
first" — untrue, the app was running — or, worse, found a stale SQLite file left
over from a local run and minted a token into a database the app never reads.
The link would simply not work, with no error. That mattered more than it
sounds: this script is one of exactly two ways out of the SSO lockout at a
domain cutover, and the cutover is the moment it is needed.
"""
from __future__ import annotations

import argparse
import secrets
import time

from sqlalchemy import text

from app.db.conn import connect, dbapi
from app.web import security
from app.db.schema import ensure_schema as ensure_db_schema

# Short, because the intended gap between running this and clicking the link is
# seconds. A token that outlives the terminal it was printed in is a liability.
TOKEN_TTL = 600.0


def ensure_bootstrap_table(conn) -> None:
    """Schema shim. The table lives in app/db/schema.py; this only guarantees it exists.

    Dialect-guarded like every other shim: `ensure_schema` memoises by asking
    `PRAGMA database_list` which file it has, which is a syntax error on
    Postgres. There the table comes from the migration history.
    """
    if conn.engine.dialect.name != "sqlite":
        return
    ensure_db_schema(dbapi(conn))


def issue_token(conn, character_id: int) -> str:
    ensure_bootstrap_table(conn)
    conn.execute(text("DELETE FROM app_bootstrap WHERE created_at < :cutoff"),
                 {"cutoff": time.time() - TOKEN_TTL})
    token = secrets.token_urlsafe(32)
    conn.execute(
        text("INSERT INTO app_bootstrap (token, character_id, created_at)"
             " VALUES (:token, :character_id, :created_at)"),
        {"token": token, "character_id": character_id, "created_at": time.time()},
    )
    conn.commit()
    return token


def redeem_token(conn, token: str | None) -> int | None:
    """Consume a bootstrap token and return its character id, or None."""
    if not token:
        return None
    ensure_bootstrap_table(conn)
    row = conn.execute(
        text("SELECT character_id, created_at FROM app_bootstrap"
             " WHERE token = :token"), {"token": token},
    ).fetchone()
    # Delete on sight, whether or not it turns out to be usable — one attempt is
    # all a single-use token gets.
    conn.execute(text("DELETE FROM app_bootstrap WHERE token = :token"),
                 {"token": token})
    conn.commit()
    if row is None or time.time() - float(row[1]) > TOKEN_TTL:
        return None
    return int(row[0])


def _open_conn():
    """A connection to whatever database this deployment uses.

    Deliberately `connect()` and not a path: `database_url()` is the only thing
    that honours `EVE_DATABASE_URL`, and this script has to reach the same store
    the running app does. The old version hardcoded the SQLite file — see the
    module docstring for what that did on Postgres.

    The "start the app once first" guard is gone with it. It tested for a file,
    which is not a question that has an answer on Postgres; a database that is
    not reachable now fails on connect with the driver's own message, which says
    considerably more than the guard did.
    """
    return connect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--character-id", type=int,
                        help="Character to sign in as. Defaults to the instance owner.")
    parser.add_argument("--base-url", default=None,
                        help="Where the app is reachable (default: from EVE_CALLBACK_URL).")
    args = parser.parse_args()

    conn = _open_conn()
    try:
        security.ensure_sessions_table(conn)

        character_id = args.character_id or security.get_owner_id(conn)
        if character_id is None:
            row = conn.execute(text(
                "SELECT character_id, character_name FROM characters"
                " ORDER BY added_at LIMIT 1")).fetchone()
            if row is None:
                raise SystemExit(
                    "This instance has no characters and no owner yet, so there is "
                    "nothing to sign in as. Complete SSO once first."
                )
            character_id = int(row[0])
            print(f"No owner set; using the first character on file: {row[1]} ({character_id})")

        row = conn.execute(
            text("SELECT character_name FROM characters"
                 " WHERE character_id = :cid"), {"cid": character_id},
        ).fetchone()
        if row is None:
            raise SystemExit(f"No character {character_id} in this database.")

        token = issue_token(conn, character_id)
    finally:
        conn.close()

    base = args.base_url
    if not base:
        from app.auth.esi_oauth import callback_url
        base = callback_url().rsplit("/callback", 1)[0]

    print()
    print(f"Sign in as {row[0]} ({character_id}) by opening this once, within "
          f"{int(TOKEN_TTL / 60)} minutes:")
    print()
    print(f"    {base.rstrip('/')}/auth/bootstrap?token={token}")
    print()


if __name__ == "__main__":
    main()
