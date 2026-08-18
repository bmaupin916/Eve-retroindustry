"""CCP's first-party static data feed — build numbers, archives, changes.

The importer used to parse YAML out of a `data/` directory that somebody had to
download and unpack by hand. That is why `docs/dev-setup-windows.md` tells you
not to run it: the input is not in the repo, and it needs PyYAML, which is not
in `requirements.txt`. For a hosted service that ritual has to become a cron
job, so the source of the data moves here.

CCP publishes everything needed for that at
<https://developers.eveonline.com/docs/services/static-data/>:

* `latest.jsonl` — the current build number, in the record keyed `sde`
* `eve-online-static-data-<build>-jsonl.zip` — **build-pinned** archives, so an
  import is reproducible instead of "whatever was live that day"
* `changes/<build>.jsonl` — what changed against the previous build, whose
  number is in the `_meta` record. This is what makes incremental refresh
  possible later; nothing here depends on it yet.
* ETag and Last-Modified on everything, so polling is cheap

**JSONL rather than YAML** because CCP says plainly that reading their large
YAML files is slow and memory-hungry, and they are right: `types.yaml` is 150 MB
that has to be materialised whole. The JSONL equivalent streams a record at a
time and never holds more than one in memory.

Nothing in this module writes to the database — it hands back paths and
generators, and `import_sde.py` decides what to do with them.
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from dataclasses import dataclass
from typing import Callable, Iterator

import httpx

SDE_BASE = "https://developers.eveonline.com/static-data/tranquility"

# CCP asks for an identifying User-Agent on their services. The app does not set
# one on ESI yet (that is Step 2's work); there is no reason to repeat the
# omission in new code.
USER_AGENT = (
    "EVE-Retroindustry/import_sde "
    "(brian.maupin@gmail.com; +https://github.com/EVERetroIndustry/Eve-retroindustry)"
)

# The build number is stamped inside every archive, in this dataset. Checking it
# after download is a better integrity test than a checksum would be: the ETag on
# a multipart upload is not an md5 of the content, and this verifies we got the
# build we *asked for*, not merely an intact file.
BUILD_DATASET = "_sde"


@dataclass(frozen=True)
class Build:
    number: int
    release_date: str = ""

    def __str__(self) -> str:
        return f"{self.number} ({self.release_date})" if self.release_date else str(self.number)


def _client(timeout: float = 60.0) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,          # the -latest- shorthands redirect
        headers={"User-Agent": USER_AGENT},
    )


def _jsonl(text: str) -> Iterator[dict]:
    for line in text.splitlines():
        line = line.strip()
        if line:
            yield json.loads(line)


def latest_build(timeout: float = 30.0) -> Build:
    """The build number currently published for Tranquility."""
    with _client(timeout) as c:
        r = c.get(f"{SDE_BASE}/latest.jsonl")
        r.raise_for_status()
        for rec in _jsonl(r.text):
            if rec.get("_key") == "sde":
                return Build(int(rec["buildNumber"]), str(rec.get("releaseDate") or ""))
    raise RuntimeError("latest.jsonl carried no record keyed 'sde'")


def archive_url(build: int, variant: str = "jsonl") -> str:
    return f"{SDE_BASE}/eve-online-static-data-{build}-{variant}.zip"


def changes(build: int, timeout: float = 30.0) -> list[dict]:
    """What changed in `build` relative to its predecessor.

    Returns the raw records. The first is `_meta`, carrying `lastBuildNumber`;
    the rest are one per dataset with some of `added` / `changed` / `removed` /
    `schemaChanged`. A `schemaChanged` flag is the interesting one — it is the
    early warning that a field this importer reads may have moved.
    """
    with _client(timeout) as c:
        r = c.get(f"{SDE_BASE}/changes/{build}.jsonl")
        if r.status_code == 404:
            return []
        r.raise_for_status()
        return list(_jsonl(r.text))


def schema_changed_datasets(build: int) -> set[str]:
    """Datasets whose *shape* changed in this build, not just their contents."""
    return {
        rec["_key"] for rec in changes(build)
        if rec.get("_key") != "_meta" and rec.get("schemaChanged")
    }


def download_archive(
    build: int,
    dest_dir: str,
    variant: str = "jsonl",
    progress: Callable[[int, int], None] | None = None,
    timeout: float = 300.0,
) -> str:
    """Download one build's archive into `dest_dir`, returning its path.

    A previously downloaded archive is reused if it is intact and stamps the
    build we asked for; anything else is re-fetched. Downloads land on a
    `.part` file and are renamed only once complete, so an interrupted run
    cannot leave a truncated archive that looks finished.
    """
    os.makedirs(dest_dir, exist_ok=True)
    path = os.path.join(dest_dir, f"eve-online-static-data-{build}-{variant}.zip")

    if os.path.exists(path) and verify_archive(path) == build:
        return path

    part = path + ".part"
    with _client(timeout) as c:
        with c.stream("GET", archive_url(build, variant)) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            with open(part, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)

    got = verify_archive(part)
    if got != build:
        os.remove(part)
        raise RuntimeError(
            f"downloaded archive reports build {got}, expected {build}")
    os.replace(part, path)
    return path


def archive_build(path: str) -> Build | None:
    """The build stamped inside an archive, or None if it is unreadable.

    Doubles as the integrity check — a truncated or corrupt zip raises on open
    and comes back as None rather than being trusted. Returns the whole Build so
    an import from a local archive records the same release date as one that
    went through `latest_build()`.
    """
    try:
        with zipfile.ZipFile(path) as z:
            for rec in records(z, BUILD_DATASET):
                if rec.get("_key") == "sde":
                    return Build(int(rec["buildNumber"]),
                                 str(rec.get("releaseDate") or ""))
    except (zipfile.BadZipFile, OSError, KeyError, ValueError, json.JSONDecodeError):
        return None
    return None


def verify_archive(path: str) -> int | None:
    """Just the build number — see `archive_build`."""
    build = archive_build(path)
    return build.number if build else None


def records(archive: zipfile.ZipFile | str, dataset: str) -> Iterator[dict]:
    """Stream one dataset's records.

    `dataset` is the bare name — "types", not "types.jsonl". A dataset the
    archive does not carry yields nothing rather than raising, so a build that
    drops a file degrades to "imported nothing for it" instead of taking the
    whole import down.

    Never materialises the file: `types.jsonl` alone is 150 MB uncompressed and
    the point of moving off YAML was to stop holding that in memory.
    """
    own = isinstance(archive, str)
    z = zipfile.ZipFile(archive) if own else archive
    try:
        name = f"{dataset}.jsonl"
        if name not in z.namelist():
            return
        with z.open(name) as raw:
            for line in io.TextIOWrapper(raw, "utf-8"):
                line = line.strip()
                if line:
                    yield json.loads(line)
    finally:
        if own:
            z.close()


def datasets(archive: zipfile.ZipFile | str) -> list[str]:
    """Every dataset name in an archive, without the .jsonl suffix."""
    own = isinstance(archive, str)
    z = zipfile.ZipFile(archive) if own else archive
    try:
        return sorted(n[:-6] for n in z.namelist() if n.endswith(".jsonl"))
    finally:
        if own:
            z.close()


def en(field) -> str:
    """The English string out of a localised name/description field.

    Every human-readable field in the SDE is a dict of language code to string.
    Older exports sometimes carried a bare string, and a few records have no
    English at all, so this tolerates both rather than raising mid-import.
    """
    if isinstance(field, dict):
        return field.get("en") or ""
    return str(field) if field else ""
