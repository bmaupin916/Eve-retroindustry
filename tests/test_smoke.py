"""Smoke tests: every page renders (HTTP 200) with no leftover non-English text."""
import re

import pytest

ROUTES = [
    "/", "/plan", "/prices", "/blueprints", "/assets", "/assets?view=all",
    "/wallet", "/orders", "/jobs", "/contracts", "/contracts/public",
    "/projects", "/about", "/settings", "/margins",
    # Pure SDE maths, no ESI — safe to render in a smoke test, unlike /planets.
    "/pi-planner",
    "/pi-planner?target=Nitrogen+Fuel+Block&qty=50000&period=week&derate=60&ccu=5&me=10",
]
CZECH = re.compile(r"[áéíýóúůžščřďťňěÁÉÍÝÓÚŽŠČŘĎŤŇĚ]")


@pytest.mark.parametrize("path", ROUTES)
def test_page_renders(client, path):
    r = client.get(path)
    assert r.status_code == 200, f"{path} -> {r.status_code}\n{r.text[:400]}"


@pytest.mark.parametrize("path", ROUTES)
def test_page_is_english(client, path):
    r = client.get(path)
    m = CZECH.search(r.text)
    assert not m, f"Non-English text on {path}: …{r.text[max(0, m.start()-40):m.start()+40]}…"
