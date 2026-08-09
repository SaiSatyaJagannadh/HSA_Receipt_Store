"""The confirmation-after-rerun bug, enforced at the source level.

st.success() immediately before st.rerun() never reaches the browser — the rerun
discards the page mid-render. It appeared at six call sites, was fixed at six,
and then turned up a seventh time on Reimbursements, which is the page where a
silent save is most expensive.

Per-page AppTest coverage did not catch the seventh (Reimbursements needs a
data_editor selection to reach the save path, which AppTest cannot drive). So
this checks the shape of the code instead: no page may pair the two calls, and
any page that queues a flash must also render one.
"""

import ast
from pathlib import Path

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
PAGES = sorted(APP_DIR.glob("pages/*.py")) + [APP_DIR / "app.py"]


def calls(node) -> str | None:
    """'st.rerun' / 'store.flash' for an expression statement, else None."""
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return None
    func = node.value.func
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    return None


def bodies(tree):
    """Every statement list in the module — a rerun can be nested in any block."""
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if isinstance(block, list):
                yield block


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_no_success_immediately_before_a_rerun(page):
    tree = ast.parse(page.read_text())
    for block in bodies(tree):
        for first, second in zip(block, block[1:]):
            if calls(first) == "st.success" and calls(second) == "st.rerun":
                pytest.fail(
                    f"{page.name}:{first.lineno} st.success() is discarded by the "
                    f"st.rerun() on line {second.lineno}. Use store.flash() instead."
                )


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_a_page_that_queues_a_flash_also_renders_one(page):
    source = page.read_text()
    if "store.flash(" in source:
        assert "store.show_flash()" in source, (
            f"{page.name} queues a flash but never calls store.show_flash(), "
            "so the confirmation is queued and never shown."
        )


def test_offline_reason_retracts_once_sheets_is_reachable_again():
    """The banner describes the data in hand, so a recovered read must clear it."""
    import streamlit as st

    from core import store

    st.session_state.clear()
    assert store.offline_reason() is None

    st.session_state[store._OFFLINE] = "connection refused"
    assert store.offline_reason() == "connection refused"

    # Reading it must not clear it — every rerun serving the cached list still
    # has to say so. Only a successful fetch retracts the banner.
    assert store.offline_reason() == "connection refused"
    st.session_state.pop(store._OFFLINE, None)
    assert store.offline_reason() is None
