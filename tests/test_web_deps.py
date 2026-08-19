"""`app/web/deps.py` is the leaf that lets W6 split `main.py` into routers.

Every router will need `get_conn()` and `_tr()`. While those lived in
`main.py`, a router importing them would import the module that imports the
router — so they moved to `deps.py`, and `deps.py` must stay importable on its
own. That is not a style preference: the moment it imports `app.web.main`, the
cycle is back and the split stops working, in a way that shows up as an
ImportError halfway through a later commit rather than here.

These tests assert the property, not the file layout.
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MAIN = REPO / "app" / "web" / "main.py"
DEPS = REPO / "app" / "web" / "deps.py"


def test_deps_imports_without_dragging_in_main():
    """Run in a subprocess: by the time this test runs, a fixture has already
    imported `main`, so an in-process check would pass no matter what."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import sys; import app.web.deps;"
         " sys.exit(1 if 'app.web.main' in sys.modules else 0)"],
        cwd=str(REPO), capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        "app.web.deps pulled in app.web.main — the import cycle W6 exists to "
        f"avoid is back.\n{proc.stdout}\n{proc.stderr}"
    )


def _names_imported_from_deps() -> set[str]:
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.web.deps":
            return {a.asname or a.name for a in node.names}
    raise AssertionError("main.py no longer imports from app.web.deps")


def test_main_reuses_the_shared_helpers_rather_than_copying_them(app_module):
    """Identity, not equality.

    A move that leaves a stale copy behind in `main.py` still passes every
    route test — both versions work — right up until one of them is changed.
    `_SDE_READY` is the sharpest case: it is a one-element list precisely so
    the setup gate and whoever flips it share the object. Two lists is a fresh
    install that never leaves /setup.
    """
    from app.web import deps

    for name in sorted(_names_imported_from_deps()):
        assert getattr(app_module, name) is getattr(deps, name), (
            f"main.{name} is not deps.{name} — it was copied, not imported"
        )


def test_main_does_not_shadow_anything_it_imports_from_deps():
    """A later router commit that leaves one definition behind in `main.py`
    would silently shadow the import, and Python would not say a word."""
    imported = _names_imported_from_deps()
    tree = ast.parse(MAIN.read_text(encoding="utf-8"))

    shadowed = []
    for node in tree.body:                     # module level only
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in imported:
                shadowed.append((node.name, node.lineno))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if isinstance(t, ast.Name) and t.id in imported:
                    shadowed.append((t.id, node.lineno))

    assert not shadowed, f"redefined after importing from deps: {shadowed}"


def test_deps_declares_the_helpers_the_routers_will_need():
    """The four the split actually blocks on. Named individually because
    "deps.py exists" is not the property — "a router can render a page and
    open the database without importing main" is."""
    from app.web import deps

    for name in ("get_conn", "_tr", "templates", "_SDE_READY"):
        assert hasattr(deps, name), f"app.web.deps is missing {name}"
