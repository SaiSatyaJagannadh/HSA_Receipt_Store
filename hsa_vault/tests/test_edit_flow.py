"""Editing a receipt must save it AND say so.

Reported as "unable to change, getting nothing changed when modifying it". The
write was never broken: st.success() was called immediately before st.rerun(),
so the confirmation was discarded before the browser painted it. A save looked
identical to a no-op, so the natural response was to press Save again — and the
second press really did find nothing to change, which printed the message that
made it look like the app was refusing the edit.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from core import config, store
from core.models import Receipt

RECEIPTS_PAGE = str(Path(__file__).resolve().parents[1] / "pages" / "2_Receipts.py")


def make_receipt() -> Receipt:
    return Receipt(
        file_hash="a" * 64,
        drive_file_id="f1",
        drive_link="https://drive.google.com/file/d/f1",
        service_date=date(2026, 6, 30),
        upload_date=datetime.now(timezone.utc),
        provider="Amazon",
        amount=Decimal("25.46"),
        category="Other",
        payment_method="out_of_pocket",
        patient="Tester",
        description="desc",
        notes="",
    )


@pytest.fixture
def page(monkeypatch):
    """Renders the Receipts page over one receipt, capturing what gets saved."""
    saved: list[Receipt] = []
    receipt = make_receipt()

    settings = config.Settings(
        google_credentials_json=__file__,
        drive_folder_id="folder",
        sheet_id="sheet",
        nvidia_api_key="",
    )
    monkeypatch.setattr(config, "load_settings", lambda: settings)
    monkeypatch.setattr(store, "receipts", lambda refresh=False: [receipt])
    monkeypatch.setattr(store, "clients", lambda: (None, None))
    monkeypatch.setattr(store, "save_receipt", lambda r: saved.append(r))

    at = AppTest.from_file(RECEIPTS_PAGE, default_timeout=60)
    at.run()
    assert not at.exception
    return at, saved, receipt


def submit(at):
    [b for b in at.button if b.label == "Save changes"][0].click()
    at.run()
    assert not at.exception


# Each editable field, and how to change it in the form.
EDITS = {
    "provider": lambda at: [t for t in at.text_input if t.label == "Provider"][0].set_value("New Co"),
    "amount": lambda at: [t for t in at.text_input if t.label == "Amount"][0].set_value("99.99"),
    "patient": lambda at: [t for t in at.text_input if t.label == "Patient"][0].set_value("Someone"),
    "payment_method": lambda at: at.radio[0].set_value("hsa_card"),
    "category": lambda at: [s for s in at.selectbox if s.label == "Category"][0].set_value("Dental"),
    "eligibility_confidence": lambda at: [
        s for s in at.selectbox if s.label == "Eligibility confidence"
    ][0].set_value("likely"),
    "service_date": lambda at: at.date_input[0].set_value(date(2026, 1, 15)),
    "description": lambda at: at.text_area[0].set_value("new description"),
    "notes": lambda at: at.text_area[1].set_value("new notes"),
}


@pytest.mark.parametrize("field", list(EDITS))
def test_every_editable_field_saves(page, field):
    at, saved, _ = page
    EDITS[field](at)
    submit(at)
    assert saved, f"editing {field} did not reach store.save_receipt"


@pytest.mark.parametrize("field", list(EDITS))
def test_a_save_always_confirms_itself(page, field):
    """The regression. A save that reports nothing is indistinguishable from a
    no-op, which is exactly what the bug report described."""
    at, _, _ = page
    EDITS[field](at)
    submit(at)
    confirmations = [s.value for s in at.success]
    assert confirmations, (
        f"editing {field} saved silently — no confirmation survived the rerun"
    )
    assert field in confirmations[0], f"confirmation does not name {field}: {confirmations[0]}"


def test_submitting_untouched_form_saves_nothing_and_says_why(page):
    at, saved, _ = page
    submit(at)
    assert not saved
    assert not at.success
    assert at.info, "an unchanged submit must explain itself, not fail silently"
    assert "already-saved" in at.info[0].value


def test_edit_is_recorded_in_history(page):
    at, saved, receipt = page
    EDITS["provider"](at)
    submit(at)
    assert saved[0] is receipt
    assert receipt.provider == "New Co"
    assert receipt.edit_history, "an edit must leave an audit trail"


def test_flash_survives_a_rerun_and_shows_once():
    """store.flash outlives the rerun that st.success could not, then clears."""
    import streamlit as st

    st.session_state.clear()
    store.flash("Updated 1 field(s).")
    assert store._FLASH in st.session_state  # queued, not yet rendered
    store.show_flash()
    assert store._FLASH not in st.session_state  # and not shown twice
