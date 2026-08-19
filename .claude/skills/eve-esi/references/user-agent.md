# User-Agent

Every ESI request should identify the calling application. CCP uses this to
contact developers when an app is misbehaving, before resorting to a ban.

## Which header to use

```
Can you set HTTP headers?
├── No  → use the `user_agent` query parameter (URL-encode it)
└── Yes → Is this a browser application?
          ├── Yes → use `X-User-Agent`
          └── No  → use `User-Agent`
```

Browser apps specifically need `X-User-Agent` because Chrome/Chromium silently
drop a manually-set `User-Agent` header on `fetch()` requests — it's not
forbidden anymore per spec, but Chrome still strips it in practice.

If none of the above are available (rare), fall back to the `user_agent` query
parameter — it must be URL-encoded like any other query param.

## What to include

Include one or more of, in order of preference:

- Email address (**strongly preferred**): `foo@example.com`
- App name + version (**strongly preferred**): `AppName/1.2.3`
- Source code URL: `+https://github.com/your/repository`
- Discord username: `discord:username`
- EVE character name: `eve:charactername`

## Multi-component apps (plugins/libraries)

List components narrow → broad (most specific first), and always include the
actual source of the request:

```
PluginName/1.2.3 (foo@example.com; +https://github.com/) AppName/1.2.3 LibraryName/1.2.3
```

## Real examples from existing tools

```
AllianceAuth/1.2.3 (foo@example.com; +https://gitlab.com/allianceauth/allianceauth) DjangoESI/1.2.3
eveseat:eveapi/5.0.22 (admin contact: foo@example) (https://github.com/eveseat/seat) eveseat:seat/5.0.x-dev
RIFT/1.2.3 (foo@example.com)
```

## Minimal request example

```python
import requests

response = requests.get(
    "https://esi.evetech.net/latest/status/",
    headers={
        "User-Agent": "MyTool/1.0.0 (foo@example.com; +https://github.com/me/my-tool)",
        "X-Compatibility-Date": "2025-09-30",
    },
)
```
