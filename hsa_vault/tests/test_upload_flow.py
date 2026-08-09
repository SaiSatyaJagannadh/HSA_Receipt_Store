"""The uploader has to empty itself once a batch is filed.

Before this, a saved receipt left its card on screen reading "Saved. Remove it
from the uploader above to clear this card" — the app finished its half of the
job and handed the cleanup back to the user. Worse, the stale card sat directly
above the next receipt's form, so a two-file batch looked like it had failed
half way.

Streamlit cannot drop one file from a file_uploader's value and the widget
cannot be emptied by writing to its session state, so the page re-keys the
widget with a round number. These tests pin that: a fully-filed batch comes back
as an empty drop zone, a partly-filed one does not, and a duplicate keeps its
card rather than being silently swept away.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from core import config, extraction, store
from core.models import Receipt
from core.util import sha256_hex

UPLOAD_PAGE = str(Path(__file__).resolve().parents[1] / "pages" / "1_Upload.py")

# .pdf so the page takes the "no inline preview" branch — this exercises the
# save flow, not image decoding.
FILE_A = ("receipt_a.pdf", b"%PDF-1.4 first receipt")
FILE_B = ("receipt_b.pdf", b"%PDF-1.4 second receipt")


@pytest.fixture
def page(monkeypatch):
    """Upload page with Google and the vision model mocked out."""
    committed: list[Receipt] = []

    settings = config.Settings(
        google_credentials_json=__file__,
        drive_folder_id="folder",
        sheet_id="sheet",
        nvidia_api_key="",
    )
    monkeypatch.setattr(config, "load_settings", lambda: settings)
    monkeypatch.setattr(store, "receipts", lambda refresh=False: [])
    monkeypatch.setattr(store, "clients", lambda: (None, None))
    monkeypatch.setattr(store, "commit_receipt", lambda r, b, n: committed.append(r) or r)
    monkeypatch.setattr(extraction, "normalize", lambda data, name: [("image/png", b"x")])
    monkeypatch.setattr(
        extraction,
        "extract",
        lambda *a, **k: extraction.Extraction(
            data={"provider": "Test Clinic", "total_amount": "25.00"}, raw="{}"
        ),
    )

    at = AppTest.from_file(UPLOAD_PAGE, default_timeout=60)
    at.run()
    assert not at.exception
    return at, committed


def add(at, *files):
    for name, content in files:
        at.file_uploader[0].upload(name, content)
    at.run()
    assert not at.exception


def save_first(at):
    at.button[0].click()
    at.run()
    assert not at.exception


def uploaded_names(at):
    """Filenames the uploader is currently holding."""
    value = at.file_uploader[0].value
    return [f.name for f in (value or [])]


def test_a_filed_batch_leaves_an_empty_drop_zone(page):
    """The regression: one file in, saved, uploader back to empty."""
    at, committed = page
    add(at, FILE_A)
    assert uploaded_names(at) == ["receipt_a.pdf"]

    save_first(at)

    assert committed, "the receipt never reached store.commit_receipt"
    assert uploaded_names(at) == [], (
        "the uploader still holds the saved file — the user has to clear it by hand"
    )


def test_the_save_still_confirms_itself(page):
    """Auto-clearing must not eat the confirmation, or the save goes silent again."""
    at, _ = page
    add(at, FILE_A)
    save_first(at)
    assert at.success, "clearing the uploader discarded the save confirmation"
    assert "Saved" in at.success[0].value


def test_no_leftover_card_tells_the_user_to_clean_up(page):
    at, _ = page
    add(at, FILE_A)
    save_first(at)
    body = " ".join(m.value for m in at.markdown) + " ".join(s.value for s in at.success)
    assert "Remove it from the uploader" not in body


def test_a_half_filed_batch_keeps_the_unsaved_file(page):
    """Clearing on every save would throw away files the user has not got to yet."""
    at, committed = page
    add(at, FILE_A, FILE_B)
    assert len(uploaded_names(at)) == 2

    save_first(at)

    assert len(committed) == 1
    assert uploaded_names(at) == ["receipt_a.pdf", "receipt_b.pdf"], (
        "saving one file must not empty the uploader while another is pending"
    )
    # The filed one is gone from the review list; the pending one still has a form.
    headings = [m.value for m in at.markdown]
    assert not any("receipt_a.pdf" in h for h in headings), "spent card was left on screen"
    assert any("receipt_b.pdf" in h for h in headings), "pending card disappeared"


def test_second_save_then_empties_the_uploader(page):
    at, committed = page
    add(at, FILE_A, FILE_B)
    save_first(at)
    save_first(at)
    assert len(committed) == 2
    assert uploaded_names(at) == [], "the finished batch did not clear"


def test_a_duplicate_keeps_its_card(monkeypatch, page):
    """A blocked duplicate is an error to read, not clutter to sweep away."""
    at, _ = page
    existing = Receipt(
        file_hash=sha256_hex(FILE_A[1]),
        service_date=date(2026, 1, 1),
        upload_date=datetime.now(timezone.utc),
        provider="Already Filed",
        amount=Decimal("10.00"),
        category="Other",
        payment_method="out_of_pocket",
    )
    monkeypatch.setattr(store, "receipts", lambda refresh=False: [existing])

    add(at, FILE_A)
    assert uploaded_names(at) == ["receipt_a.pdf"], "the duplicate was silently discarded"
    assert any("Duplicate blocked" in e.value for e in at.error)
