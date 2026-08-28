"""The market-quality page renders what it claims to.

`/prices/groups` is the first consumer of the market tree. A page test here is
not duplicating `test_market_group_stats.py`: that one proves the numbers, this
one proves the numbers reach the template and arrive labelled.

The label is the part worth a test. The stored volume column is a **seven-day**
sum, not the thirty-day mean §9.4 specifies, and a column headed "Volume / day"
with no window stated is a figure nobody measured. That is the same failure as
the reactions board's Sell Advantage column, which shipped and was always blank.
"""
from __future__ import annotations

import re


def _data_rows(html: str) -> list[str]:
    """The `<tr>` rows of the group table, excluding the header."""
    body = re.search(r"<tbody>(.*?)</tbody>", html, re.S)
    return re.findall(r"<tr>.*?</tr>", body.group(1), re.S) if body else []


def test_the_page_renders_one_row_per_market_root(client):
    """Counted, not sampled.

    Asserting a group name appears somewhere in the page is satisfied by the
    navigation, by an error page, or by the word turning up in a tooltip — an
    assertion that passes for reasons other than the one it names. The row
    count cannot.
    """
    r = client.get("/prices/groups")
    assert r.status_code == 200
    assert len(_data_rows(r.text)) == 19, "expected one row per market root"
    assert "Ships" in r.text


def test_the_page_states_the_window_it_averaged_over(client):
    """A window-less "per day" is the claim this page must not make."""
    r = client.get("/prices/groups")
    assert r.status_code == 200
    assert "7-day" in r.text, "the volume window is not stated anywhere on the page"


def test_the_page_reports_coverage_rather_than_implying_it(client):
    """"3 of 47 priced" is a different statement from a bare median."""
    r = client.get("/prices/groups")
    assert re.search(r"\d[\d,]*\s*/\s*\d[\d,]*", r.text), (
        "no priced/total pair rendered — the medians are unqualified")


def test_a_branch_can_be_drilled_into(client):
    """Group 4 is Ships — eleven sub-groups, and the page must show *those*.

    Asserting only that "Ships" appears would pass at the root level too, which
    is the state this test exists to distinguish from.
    """
    r = client.get("/prices/groups?g=4")
    assert r.status_code == 200
    rows = "".join(_data_rows(r.text))
    assert "Battleships" in rows and "Capital Ships" in rows
    assert len(_data_rows(r.text)) == 11

    # The breadcrumb must name where you are, not merely offer a way back.
    # "All groups" alone is rendered unconditionally, so asserting it proves
    # nothing about the trail.
    crumb = re.search(r'<ol class="breadcrumb.*?</ol>', r.text, re.S)
    assert crumb, "no breadcrumb rendered"
    assert "All groups" in crumb.group(0)
    assert "Ships" in crumb.group(0), "the breadcrumb does not name the current group"


def test_an_unknown_group_is_empty_rather_than_an_error(client):
    """A stale bookmark should render the empty state, not a 500."""
    r = client.get("/prices/groups?g=-1")
    assert r.status_code == 200
    assert "No sub-groups here" in r.text


def test_the_page_makes_no_esi_call_while_rendering(client):
    """Cache-only, like every other page since Step 4.

    `tests/test_cache_only_routes.py` scans the handler for fetchers; this is
    the behavioural half — the page answers with the network unavailable
    because the fixture's ESI stubs would raise if it tried.
    """
    r = client.get("/prices/groups")
    assert r.status_code == 200
    assert "Market quality" in r.text


def test_the_page_states_both_windows_it_mixes(client):
    """Two different windows sit in one table and must both be named.

    Volume is a 7-day figure from the price snapshot; volatility, trend and
    competition are 30-day figures from the daily history. A reader who assumes
    one window for the row is reading two of the columns wrong.
    """
    r = client.get("/prices/groups")
    assert "7-day" in r.text
    assert "30-day" in r.text


def test_measured_is_reported_separately_from_priced(client):
    """History arrives twenty types a round, so the two coverages diverge.

    One number for both would hide which half a blank column is missing.
    """
    r = client.get("/prices/groups")
    for heading in ("Priced", "Measured", "Volatility", "Trend", "Competition"):
        assert f">{heading}<" in r.text, f"no {heading} column"
