"""Images: type icons, character portraits, corp and alliance logos.

Moved out of `main.py` unchanged (W6). `/portrait` is a character thing and
`/icon` is a market thing, but they are one router because they are one cache:
the same disk directory, the same size whitelist, the same concurrency
semaphore, the same magic-byte sniff on whatever comes back from
images.evetech.net.

Splitting them would mean two copies of that, which is how the two would drift.
"""
from __future__ import annotations

import asyncio
import os
import time as _time

import httpx

from fastapi import APIRouter

from app.esi.client import esi_client
from app.db.location import app_dir

router = APIRouter()


# ── Character portraits / corp logos: local cache with a long TTL ────────────
# Same reasoning as type icons, but these CAN change (a player updates a portrait,
# a corp changes its logo), so they get a long TTL instead of being immutable.
_PORTRAIT_TTL = 30 * 86400          # 30 days on disk and in the browser
_PORTRAIT_KINDS = {                 # url segment -> (esi path, flavour)
    "characters":   "portrait",
    "corporations": "logo",
    "alliances":    "logo",
}


@router.get("/portrait/{kind}/{entity_id}")
async def entity_portrait(kind: str, entity_id: int, size: int = 32):
    """Serve a character portrait / corp or alliance logo from the local cache."""
    from fastapi.responses import Response, FileResponse
    flavour = _PORTRAIT_KINDS.get(kind)
    if not flavour:
        return Response(status_code=404)
    if size not in _ICON_SIZES:
        size = 32
    headers = {"Cache-Control": f"public, max-age={_PORTRAIT_TTL}"}

    variant = f"{kind}-{flavour}"
    hit = _cached_icon(entity_id, size, variant)
    if hit and (_time.time() - os.path.getmtime(hit[0])) < _PORTRAIT_TTL:
        return FileResponse(hit[0], media_type=hit[1], headers=headers)

    try:
        async with _ICON_SEM:
            async with esi_client(timeout=15, follow_redirects=True) as client:
                r = await client.get(
                    f"https://images.evetech.net/{kind}/{entity_id}/{flavour}",
                    params={"size": size},
                )
        kind_bytes = _sniff_image(r.content) if r.status_code == 200 else None
        if not kind_bytes:
            # Expired copy beats no picture at all when the fetch fails.
            if hit:
                return FileResponse(hit[0], media_type=hit[1], headers=headers)
            return Response(status_code=404 if 400 <= r.status_code < 500 else 502)
        ext, mime = kind_bytes
        os.makedirs(_icon_dir(), exist_ok=True)
        path = f"{_icon_base(entity_id, size, variant)}.{ext}"
        tmp = f"{path}.{os.getpid()}.part"
        with open(tmp, "wb") as fh:
            fh.write(r.content)
        os.replace(tmp, path)
        return Response(content=r.content, media_type=mime, headers=headers)
    except Exception as exc:
        print(f"[portrait] {kind}/{entity_id} failed: {exc}", flush=True)
        if hit:
            return FileResponse(hit[0], media_type=hit[1], headers=headers)
        return Response(status_code=502)


# ── Type icons: local disk cache + long-lived browser caching ────────────────
# The Prices table alone carries ~1,900 item icons. Loading them straight from
# images.evetech.net cost ~1s of the page's load time (measured: `load` dropped
# from ~1300ms to ~280ms with images off) and made the app depend on the network
# for something that never changes. Icons are keyed by (type_id, size) and are
# immutable, so we proxy them once to disk and then answer locally with an
# immutable Cache-Control — after the first visit the browser stops asking at all.

def _icon_dir() -> str:
    """Resolved per call, like every other writable path."""
    return os.path.join(app_dir(), "icon_cache")


_ICON_SIZES = {32, 64, 128, 256, 512}          # sizes the image server publishes
_ICON_VARIANTS = {"icon", "render", "bp", "bpc", "relic"}   # type image flavours
_ICON_SEM = asyncio.Semaphore(8)               # don't open a socket per visible row
_ICON_MISSING: set[tuple[int, int, str]] = set()  # upstream 404s — don't retry all session
_ICON_MAX_BYTES = 512 * 1024                    # sanity cap per icon


def _icon_base(type_id: int, size: int, variant: str) -> str:
    return os.path.join(_icon_dir(), f"{type_id}_{variant}_{size}")


# Icons are PNG but renders are JPEG, so the served content-type has to follow the
# actual bytes rather than being hard-coded. Sniffing also double-checks we only
# ever cache real images.
_IMG_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png", "image/png"),
              (b"\xff\xd8\xff", "jpg", "image/jpeg"),
              (b"GIF8", "gif", "image/gif"))


def _sniff_image(data: bytes) -> tuple[str, str] | None:
    for magic, ext, mime in _IMG_MAGIC:
        if data.startswith(magic):
            return ext, mime
    return None


def _cached_icon(type_id: int, size: int, variant: str) -> tuple[str, str] | None:
    """Return (path, mime) for an already-cached image, or None."""
    base = _icon_base(type_id, size, variant)
    for ext, mime in (("png", "image/png"), ("jpg", "image/jpeg"), ("gif", "image/gif")):
        p = f"{base}.{ext}"
        if os.path.isfile(p):
            return p, mime
    return None


def _icon_missing_marker(type_id: int, size: int, variant: str) -> str:
    return _icon_base(type_id, size, variant) + ".404"


@router.get("/icon/{type_id}")
async def type_icon(type_id: int, size: int = 32, v: str = "icon"):
    """Serve a type image from the local cache, fetching it once if needed.
    `v` selects the flavour (icon / render / bp / bpc / relic)."""
    from fastapi.responses import Response, FileResponse
    if size not in _ICON_SIZES:
        size = 32
    variant = v if v in _ICON_VARIANTS else "icon"
    headers = {"Cache-Control": "public, max-age=31536000, immutable"}

    hit = _cached_icon(type_id, size, variant)
    if hit:
        return FileResponse(hit[0], media_type=hit[1], headers=headers)
    # Negative results are remembered on disk too: assets rows ask for `render`
    # first and most items have none, so without this every restart would re-ask
    # upstream for thousands of images that will never exist.
    if (type_id, size, variant) in _ICON_MISSING or os.path.isfile(
            _icon_missing_marker(type_id, size, variant)):
        _ICON_MISSING.add((type_id, size, variant))
        return Response(status_code=404)

    try:
        async with _ICON_SEM:
            hit = _cached_icon(type_id, size, variant)
            if hit:                          # filled in while we waited
                return FileResponse(hit[0], media_type=hit[1], headers=headers)
            async with esi_client(timeout=15, follow_redirects=True) as client:
                r = await client.get(
                    f"https://images.evetech.net/types/{type_id}/{variant}",
                    params={"size": size},
                )
        # Any 4xx means this image will never exist: 404 = unknown type, 400 =
        # the type has no such flavour (most items have no `render`). Remember it
        # so a page full of render-less items doesn't re-ask on every load; the
        # <img onerror> handlers already hide a missing icon.
        if 400 <= r.status_code < 500:
            _ICON_MISSING.add((type_id, size, variant))
            try:
                os.makedirs(_icon_dir(), exist_ok=True)
                open(_icon_missing_marker(type_id, size, variant), "wb").close()
            except Exception:
                pass
            return Response(status_code=404)
        if r.status_code != 200 or not r.content or len(r.content) > _ICON_MAX_BYTES:
            return Response(status_code=502)
        kind = _sniff_image(r.content)
        if not kind:                         # 200 but not an image → never cache it
            print(f"[icon] {type_id}@{size}/{variant}: non-image body, not cached", flush=True)
            return Response(status_code=502)
        ext, mime = kind
        os.makedirs(_icon_dir(), exist_ok=True)
        path = f"{_icon_base(type_id, size, variant)}.{ext}"
        tmp = f"{path}.{os.getpid()}.part"   # atomic: never serve a half-written file
        with open(tmp, "wb") as fh:
            fh.write(r.content)
        os.replace(tmp, path)
        return Response(content=r.content, media_type=mime, headers=headers)
    except Exception as exc:
        print(f"[icon] fetch failed for {type_id}@{size}: {exc}", flush=True)
        return Response(status_code=502)
