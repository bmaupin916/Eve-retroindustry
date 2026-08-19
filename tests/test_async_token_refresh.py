"""W9: nothing on the event loop may refresh a token synchronously.

`token_store.get_valid_token()` does a blocking `httpx.post` to EVE SSO with a
15-second timeout whenever the stored access token has expired — and EVE access
tokens last about twenty minutes, so on any page that has been open a while
this is the normal path, not the exceptional one.

Called from an `async def`, that post blocks the whole event loop. Not the
request: the loop. Every other request in the process waits, including the ones
that need no token at all. This is the v0.9.22 bug class, and the codebase
already had `_valid_token_async` written for exactly this reason — it just was
not used at the call sites.

The scan below is the net. A list of known offenders would go stale; what has
to stay true is that no coroutine calls the blocking function.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# The blocking refresh, under every name it is imported as.
BLOCKING = {"get_valid_token", "_get_valid_token_for"}

# `deps._valid_token_async` is the async wrapper: it calls the blocking
# function on purpose, inside `asyncio.to_thread`. That is the fix, not a
# violation of it.
ALLOWED = {("app/web/deps.py", "_valid_token_async")}


def _calls_inside_coroutines(path: Path) -> list[str]:
    """Names from BLOCKING called anywhere inside an `async def` body."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    rel = path.relative_to(REPO).as_posix()
    found = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            fn = sub.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name in BLOCKING and (rel, node.name) not in ALLOWED:
                found.append(f"{rel}:{sub.lineno} in async def {node.name}()")
    return found


def _scan() -> list[str]:
    found = []
    for path in sorted((REPO / "app").rglob("*.py")):
        found += _calls_inside_coroutines(path)
    return found


def test_the_scan_recognises_a_blocking_refresh_when_it_sees_one(tmp_path):
    """A positive control: the test below expects to find nothing, which is the
    shape that keeps passing once the detector stops detecting."""
    bad = tmp_path / "bad.py"
    bad.write_text(
        "async def handler(conn, cid):\n"
        "    return get_valid_token(conn, cid)\n", encoding="utf-8")
    good = tmp_path / "good.py"
    good.write_text(
        "async def handler(cid):\n"
        "    return await _valid_token_async(cid)\n", encoding="utf-8")

    # relative_to() needs them under REPO, so compare the parsing directly.
    def calls(p):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        return [c for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)
                for c in ast.walk(n)
                if isinstance(c, ast.Call)
                and (getattr(c.func, "id", None) or getattr(c.func, "attr", None)) in BLOCKING]

    assert calls(bad), "the scan missed a blocking refresh inside a coroutine"
    assert not calls(good), "the scan flagged the async wrapper, which is the fix"


def test_no_coroutine_refreshes_a_token_synchronously():
    """The property W9 is about.

    When this fails, the fix is `await _valid_token_async(char_id)` — it takes
    no connection, because it opens its own inside the worker thread (sqlite3
    objects belong to one thread).
    """
    offenders = _scan()
    assert not offenders, (
        f"{len(offenders)} synchronous token refreshes on the event loop:\n  "
        + "\n  ".join(offenders)
        + "\nUse `await _valid_token_async(char_id)` from app.web.deps."
    )


# ── and the wrapper genuinely keeps the loop running ─────────────────────────

def test_a_refresh_does_not_stop_the_event_loop(app_module, monkeypatch):
    """The scan proves nothing calls the blocking function. This proves the
    thing it calls instead actually yields.

    `get_valid_token` is stubbed with a *blocking* sleep — the same shape as the
    15-second httpx.post it really does. If `_valid_token_async` ran that
    inline, the loop would sit still and the counter below would never move.
    """
    import asyncio
    import time

    import app.auth.token_store as ts
    from app.web import deps

    def _slow_refresh(conn, char_id):
        time.sleep(0.30)             # blocking, on purpose
        return "fresh-token"

    monkeypatch.setattr(ts, "get_valid_token", _slow_refresh)
    monkeypatch.setattr(deps, "_get_valid_token_for", _slow_refresh)

    async def scenario():
        task = asyncio.create_task(deps._valid_token_async(900000001))
        ticks = 0
        while not task.done():
            await asyncio.sleep(0.01)
            ticks += 1
        return ticks, await task

    ticks, token = asyncio.run(scenario())

    assert token == "fresh-token"
    assert ticks > 5, (
        f"the loop only got {ticks} turns during a 300 ms refresh — it was "
        "blocked, which is the whole of W9"
    )


def test_several_characters_refresh_concurrently(app_module, monkeypatch):
    """The pages that need a token usually need several.

    Three characters refreshing in series is three round trips end to end;
    `asyncio.gather` over the async wrapper makes it one. The call sites that
    used to do this in a `for` loop or a generator now gather.
    """
    import asyncio
    import time

    import app.auth.token_store as ts
    from app.web import deps

    def _slow_refresh(conn, char_id):
        time.sleep(0.20)
        return f"token-{char_id}"

    monkeypatch.setattr(ts, "get_valid_token", _slow_refresh)
    monkeypatch.setattr(deps, "_get_valid_token_for", _slow_refresh)

    async def scenario():
        started = time.monotonic()
        tokens = await asyncio.gather(*[deps._valid_token_async(c) for c in (1, 2, 3)])
        return time.monotonic() - started, tokens

    elapsed, tokens = asyncio.run(scenario())

    assert tokens == ["token-1", "token-2", "token-3"]
    assert elapsed < 0.5, (
        f"three 200 ms refreshes took {elapsed:.2f}s — they ran in series"
    )
