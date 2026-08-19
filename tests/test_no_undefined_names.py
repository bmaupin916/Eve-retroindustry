"""No module may reference a name that does not exist.

Two of these were sitting in `main.py` when W6 started, and neither had ever
failed loudly:

* `_bg_fetch_prices` called `_esi_client()` — the import is `esi_client`. The
  body is wrapped in `except Exception: pass`, and `NameError` is an
  `Exception`, so the background price warm-up after a search silently did
  nothing at all.
* `_resolve_corp_container_names` posted `owned_ids`, which only its character
  twin ever builds. Same swallow: `except Exception: custom_names = {}`, so
  every corp container fell back to its bare type name.

A broad `except` turns a typo into a feature that quietly does not work, and
Python will not tell you at import time. W6 makes this sharper: moving a
handler to a router without its imports produces exactly this failure, on a
route no test happens to hit.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TARGETS = ["app", "tests", "migrations", "import_sde.py"]


def _pyflakes(paths: list[str]) -> list[str]:
    try:
        import pyflakes  # noqa: F401
    except ImportError:
        pytest.skip("pyflakes is not installed (see requirements-dev.txt)")
    proc = subprocess.run(
        [sys.executable, "-m", "pyflakes", *paths],
        cwd=str(REPO), capture_output=True, text=True,
    )
    return proc.stdout.splitlines()


def test_nothing_references_an_undefined_name():
    found = [l for l in _pyflakes(TARGETS) if "undefined name" in l]
    assert not found, "undefined names:\n  " + "\n  ".join(found)


def test_no_local_variable_shadows_an_import_it_then_uses():
    """pyflakes calls this "local variable defined in enclosing scope referenced
    before assignment" — the other way a moved function stops working."""
    found = [l for l in _pyflakes(TARGETS)
             if "referenced before assignment" in l or "undefined local" in l]
    assert not found, "\n  ".join(found)
