"""Route modules split out of `app/web/main.py` (W6, `docs/design-hosted-v2.md` §11).

Each module here exposes a `router` that `main.py` includes. They may import
from `app.web.deps`, never from `app.web.main` — that is what keeps the split
possible; see the docstring in `deps.py`.

`tests/test_route_inventory.py` pins the complete route table, so a decorator
lost in a move fails there rather than 404ing on a page nobody visits.
"""
