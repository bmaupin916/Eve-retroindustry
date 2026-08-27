"""The "Profit by sell price" table must not hide itself when there is no price.

The table is computed in the browser and exists precisely so the "Fetch price"
and "Fetch contract" buttons have somewhere to write. It was gated on
`result.revenue`, which comes from the product's Jita sell price — so on any
product the market does not carry it was not rendered at all, and
`_recomputeProfitRow` (which does `querySelector('#profit-compare tr[data-src=…]')`
then returns early when the row is absent) had nowhere to put a fetched number.

The feature that supplies a missing price was hidden exactly when the price was
missing. A supercarrier is the everyday case: never on a market order, and the
one kind of build where knowing the profit at a given sell price matters most.

Gated on there being a plan instead.
"""
from __future__ import annotations

import re

import pytest

#: Rendered only inside the block under test.
TABLE = 'id="profit-compare"'


#: A titan. Chosen by *measuring* the fixture rather than reasoning about it:
#: two earlier drafts guessed wrong (Tritanium is seeded outright; a Wasp II
#: gets a price from elsewhere and came back at 1.6M), and both passed while
#: never reaching the branch they were named after. Every other candidate tried
#: — Nidhoggur, Raitaru, Rifter, Bantam, a Standup rig — also has a price here.
#: The premise is asserted by the first test below so a future fixture change
#: fails loudly instead of quietly hollowing this file out.
UNPRICED_PRODUCT = "Avatar"


def _plan(client, product, qty="1"):
    return client.post("/plan", data={"product": product, "qty": qty,
                                      "station": "60003760", "mode": "full",
                                      "runs_per_job": "0", "form_me": "0"})


def _plan_view(client, product, qty="1"):
    """The view model behind the render, so a test can check its own premise."""
    from app.web.routers import plan as plan_router

    captured = {}
    original = plan_router._tr

    def spy(name, request, context):
        captured["view"] = context
        return original(name, request, context)

    plan_router._tr = spy
    try:
        r = _plan(client, product, qty)
    finally:
        plan_router._tr = original
    return r, (captured.get("view", {}).get("result") or {})


def test_the_case_under_test_really_has_no_price(client):
    """The premise, pinned.

    The first draft of this file planned Tritanium and asserted the table was
    present. It passed — and went on passing with the bug put back, because
    `conftest._seed` prices type ids 34-38 and Tritanium is one of them. The
    test never reached the no-price branch it was named after. So the product
    is chosen for being outside that set, and that fact is asserted here rather
    than trusted.
    """
    _, result = _plan_view(client, UNPRICED_PRODUCT)

    assert result, "the plan did not build, so the rest of this file proves nothing"
    assert result.get("sell_price") is None, "the fixture now prices this product"
    assert result.get("revenue") is None, "revenue exists, so the old gate would pass"


def test_the_table_renders_when_the_product_has_no_market_price(client):
    """The bug: gated on `revenue`, this section vanished for exactly the
    products a price most needs fetching for."""
    r = _plan(client, UNPRICED_PRODUCT)

    assert r.status_code == 200
    assert TABLE in r.text, (
        "the profit comparison table is missing, so Fetch price has nowhere to "
        "write — this is the bug, not a cosmetic difference")


def test_the_fetch_buttons_have_a_row_to_write_into(client):
    """`_recomputeProfitRow` looks up a row by `data-src`. A table rendered
    without the rows the JS addresses would satisfy the test above and still
    leave the buttons inert."""
    r = _plan(client, UNPRICED_PRODUCT)

    body = r.text
    table_at = body.find(TABLE)
    assert table_at != -1
    tbody = body[table_at:table_at + 4000]

    for src in ("jita", "market", "contract"):
        assert f'data-src="{src}"' in tbody, f"no row for the {src!r} source"


def test_the_client_side_calculator_is_still_handed_its_inputs(client):
    """`window._planCalc` is emitted inside the same block. Ungating the table
    without it would render a table the JS cannot fill."""
    r = _plan(client, UNPRICED_PRODUCT)

    assert "window._planCalc" in r.text
    assert re.search(r"qty:\s*\d", r.text), "the calculator got no quantity"


def test_a_product_that_does_not_resolve_renders_no_table(client):
    """The gate still gates — otherwise "render it always" would pass every
    assertion above while putting an empty comparison on the error page.

    A name the SDE has never heard of leaves `plan_data` as None, so the
    enclosing `{% if result %}` is what refuses here; the inner gate is about
    a plan that exists but has no price. An *empty* product is not the case to
    use: the form field is required, so FastAPI answers 422 and no template is
    rendered at all.
    """
    r = _plan(client, "zzqq no such product")

    assert r.status_code == 200
    assert TABLE not in r.text
