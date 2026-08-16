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

    def fake_commit(receipt, data, name, extra_pages=None):
        committed.append((receipt, [n for _, n in (extra_pages or [])]))
        return receipt

    monkeypatch.setattr(store, "commit_receipt", fake_commit)
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


def group_checkbox(at):
    return next((c for c in at.checkbox if "one" in c.label and "receipt" in c.label), None)


def test_two_files_can_be_saved_as_a_single_receipt(page):
    """A long receipt photographed in halves is one receipt, not two.

    Saved as two, each half carries only part of the information — and the total
    is normally printed once, on the last page, so the first half saves with no
    amount at all and the balance is wrong in the safe-looking direction.
    """
    at, committed = page
    add(at, FILE_A, FILE_B)

    box = group_checkbox(at)
    assert box is not None, "no way to say two photos are one receipt"
    box.check()
    at.run()
    assert not at.exception

    save_first(at)

    assert len(committed) == 1, f"expected one receipt, got {len(committed)}"
    receipt, extra_names = committed[0]
    assert extra_names == ["receipt_b.pdf"], (
        f"the second page was not uploaded with the receipt: {extra_names}"
    )
    assert receipt.provider == "Test Clinic"


def test_ungrouped_files_still_save_as_separate_receipts(page):
    """The default must not change: two unrelated receipts stay two receipts."""
    at, committed = page
    add(at, FILE_A, FILE_B)

    save_first(at)

    assert len(committed) == 1
    assert committed[0][1] == [], "an unticked batch bundled files into one receipt"


def test_grouping_reads_every_page_in_one_model_call(page, monkeypatch):
    """Both images must reach the model together, or it cannot see the total.

    extract() has always accepted several images and its prompt already says they
    are one receipt; the page just never passed more than one.
    """
    seen: list[int] = []
    monkeypatch.setattr(
        extraction,
        "extract",
        lambda pages, *a, **k: seen.append(len(pages))
        or extraction.Extraction(data={"provider": "Test Clinic"}, raw="{}"),
    )
    at, _ = page
    add(at, FILE_A, FILE_B)
    box = group_checkbox(at)
    box.check()
    at.run()

    assert seen and max(seen) == 2, (
        f"the model was called with {seen} page(s) — a grouped receipt must send all of them"
    )
