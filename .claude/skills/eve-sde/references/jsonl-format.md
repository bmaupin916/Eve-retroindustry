# JSONL Source Format

CCP publishes the SDE as a zip of YAML files and also as a **JSONL** export —
one file per data domain, newline-delimited JSON, one record per line. Same
data, far faster to stream-parse than nested multi-document YAML.

**This is the format this project consumes.** `import_sde.py` reads the JSONL
export directly, pinned to a build number, fetched by `app/sde/feed.py`. So
everything below describes our own input, not a foreign toolchain — the
conventions in particular (`_key`, localized dicts, null-vs-missing) are traps
that importer has to handle.

## File layout

The export is ~102 files, named by domain:
`types.jsonl`, `groups.jsonl`, `categories.jsonl`, `dogmaAttributes.jsonl`,
`dogmaEffects.jsonl`, `blueprints.jsonl`, `mapSolarSystems.jsonl`,
`npcCorporations.jsonl`, `certificates.jsonl`, `skins.jsonl`, etc. See
[table-map.md](table-map.md) for the full list and what each becomes in SQL.

## Record shape

Every record is a single JSON object, one per line. Read one directly rather
than guessing field names/shapes:

```bash
python -c "import json,sys; print(json.dumps(json.loads(open(sys.argv[1],encoding='utf-8').readline()),indent=2))" types.jsonl
```

```json
{
  "_key": 0,
  "groupID": 0,
  "mass": 1.0,
  "name": {
    "de": "#System", "en": "#System", "es": "#System",
    "fr": "#Système", "ja": "#システム", "ko": "#항성계", "ru": "#Система", "zh": "#星系"
  },
  "portionSize": 1,
  "published": false
}
```

(This particular record — typeID 0 — is a placeholder/internal type, not a
real in-game item; expect a mix of real and internal rows in most files.)

## Two conventions used in every file

### 1. `_key` is the primary key

Not `id`, not the table's usual PK column name (`typeID`, `groupID`, ...).
Every record's identity is `_key`. The loader remaps it on the way into SQL:

```python
stmt = insert(invTypes).values(typeID=typedata['_key'], ...)
```

### 2. Localized strings are language-keyed dicts

Fields like `name` and `description` aren't plain strings — they're dicts
keyed by language code (`en`, `de`, `es`, `fr`, `ja`, `ko`, `ru`, `zh`, ...).
Pick a language explicitly:

```python
def _en(d, language='en'):
    if isinstance(d, dict):
        return d.get(language) or d.get('en')
    return d
```

**Gotcha**: a field that's an explicit JSON `null` (not just absent) makes
`.get('name', {})` return `None`, not the `{}` fallback — the fallback only
applies when the key is *missing*, not when its value is `null`. Code that
chains another `.get()` onto that will crash. Guard with `or`:

```python
name = (typedata.get('name') or {}).get(language)
```

This same null-vs-missing trap applies to any nested dict field, not just
localized strings (e.g. a ship's `colorHull` sub-object in `types.jsonl` can
itself be `null`).

## Reading a JSONL file in Python

```python
import json

def read_jsonl(path):
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)

for record in read_jsonl('types.jsonl'):
    type_id = record['_key']
    name = (record.get('name') or {}).get('en')
```

## Reading one without a shell toolbox

`head`/`grep`/`wc` are not available on every machine this project runs on, and
`sqlite3` is not on PATH on the current Windows box. Python is, everywhere:

```bash
# Find a specific record by _key (e.g. typeID 587, the Rifter)
python -c "import json;[print(json.dumps(json.loads(l),indent=2)) for l in open('types.jsonl',encoding='utf-8') if '\"_key\": 587,' in l]"
```

Usually the faster answer is to skip the JSONL entirely and query the imported
database — see the parent skill's "Querying it".
