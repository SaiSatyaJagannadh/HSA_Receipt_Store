"""Typing a receipt in by hand must produce the same row an upload would.

The Manual entry page exists for the receipts that never had a usable photo —
an emailed total, paper that is long gone, a back-fill from a statement — and
for the days the vision model is unavailable. Its whole value is that nothing
downstream can tell the difference, so these check the row it writes, not the
form it draws.

The duplicate case is the one that matters most. An uploaded receipt is
de-duplicated by the hash of its file; a typed one has no file, so without a
stand-in every hand-entered receipt would silently opt out of duplicate
detection — on exactly the route where duplicates come from, since typing the
same expense in twice leaves no identical bytes to notice.
"""

from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from core import config, store
from core.models import Receipt
from core.util import sha256_hex

MANUAL_PAGE = str(Path(__file__).resolve().parents[1] / "pages" / "2_Manual_Entry.py")


@pytest.fixture
def page(monkeypatch):
    """The manual page with Sheets mocked out. Returns (AppTest, saved receipts)."""
    saved: list[Receipt] = []
    index: list[Receipt] = []
    committed: list[tuple[Receipt, list[str]]] = []

    settings = config.Settings(
        google_credentials_json=__file__,
        drive_folder_id="folder",
        sheet_id="sheet",
        nvidia_api_key="",
        default_patient="Tester",
    )
    monkeypatch.setattr(config, "load_settings", lambda: settings)
    monkeypatch.setattr(store, "receipts", lambda refresh=False: index)
    monkeypatch.setattr(store, "clients", lambda: (None, None))
    monkeypatch.setattr(store, "save_receipt", lambda r: saved.append(r))

    def fake_commit(receipt, data, name, extra_pages=None):
        receipt.drive_file_id = "drive1"
        receipt.drive_link = "https://drive/1"
        committed.append((receipt, [name, *(n for _, n in (extra_pages or []))]))
        saved.append(receipt)
        return receipt

    monkeypatch.setattr(store, "commit_receipt", fake_commit)

    at = AppTest.from_file(MANUAL_PAGE, default_timeout=60)
    at.run()
    assert not at.exception
    return at, saved, index, committed


def fill(at, provider="Dr. Ruiz", amount=42.18, category="Dental",
         payment="out_of_pocket", patient="Tester"):
    at.text_input[0].set_value(provider)       # provider
    at.text_input[1].set_value(patient)        # patient
    at.number_input[0].set_value(amount)       # amount
    at.selectbox[0].set_value(category)        # category
    at.radio[0].set_value(payment)
    at.text_area[0].set_value("Two fillings")  # description
    at.run()
    assert not at.exception


def submit(at):
    at.button[0].click()
    at.run()
    assert not at.exception


def test_a_typed_receipt_is_saved_as_an_ordinary_row(page):
    at, saved, _, _ = page
    fill(at)
    submit(at)

    assert saved, "nothing reached store.save_receipt"
    receipt = saved[0]
    assert receipt.provider == "Dr. Ruiz"
    assert receipt.amount == Decimal("42.18")
    assert receipt.category == "Dental"
    assert receipt.payment_method == "out_of_pocket"
    assert receipt.patient == "Tester", "the default patient from settings was ignored"
    assert receipt.description == "Two fillings"
    assert not receipt.validate(), receipt.validate()


def test_it_carries_a_file_hash_even_with_no_file(page):
    """Blank would fail validation and skip duplicate detection entirely."""
    at, saved, _, _ = page
    fill(at)
    submit(at)
    assert len(saved[0].file_hash) == 64, "no stand-in hash for a receipt with no file"


def test_no_drive_file_is_claimed(page):
    """A row citing a Drive file that was never uploaded is worse than no file."""
    at, saved, _, _ = page
    fill(at)
    submit(at)
    assert saved[0].drive_file_id == ""
    assert saved[0].drive_link == ""


def test_the_tax_year_follows_the_service_date(page):
    at, saved, _, _ = page
    fill(at)
    at.date_input[0].set_value(__import__("datetime").date(2024, 7, 9))
    at.run()
    submit(at)
    assert saved[0].service_date.year == 2024
    assert saved[0].tax_year == 2024


def test_an_hsa_card_receipt_stays_out_of_the_balance(page):
    at, saved, _, _ = page
    fill(at, payment="hsa_card")
    submit(at)
    assert saved[0].claimable == Decimal("0.00"), "an HSA-card receipt was made claimable"


def test_the_save_confirms_itself_after_the_rerun(page):
    """store.flash, not st.success — the rerun would otherwise eat it."""
    at, _, _, _ = page
    fill(at)
    submit(at)
    assert at.success, "the save gave no confirmation"
    assert any("Saved" in s.value for s in at.success)


def test_the_same_expense_typed_twice_is_blocked(page):
    """The duplicate an identical-file check can never see."""
    at, saved, index, _ = page
    fill(at)
    submit(at)
    assert len(saved) == 1

    # What the Sheet now holds. The second entry is judged against it.
    index.extend(saved)
    fill(at)
    submit(at)

    assert len(saved) == 1, "the same expense was filed twice"
    assert any("Duplicate blocked" in e.value for e in at.error)


def test_a_different_amount_is_not_a_duplicate(page):
    """Two visits to one provider on one day are legitimate — do not block them."""
    at, saved, index, _ = page
    fill(at)
    submit(at)
    index.extend(saved)

    fill(at, amount=9.99)
    submit(at)
    assert len(saved) == 2, "a genuinely different expense was refused"


def test_the_untouched_default_amount_is_refused(page):
    """The amount box opens at 0.00, which is not an expense anyone incurred.

    A defaulted figure is only safe if forgetting to change it cannot file a
    wrong number. $0.00 in the index looks like a real receipt, counts as one in
    every count, and proves nothing at audit.
    """
    at, saved, _, _ = page
    fill(at, amount=0.00)
    submit(at)
    assert not saved, "a zero-dollar receipt was filed"
    assert any("still 0.00" in e.value for e in at.error)


def test_a_blank_provider_is_refused(page):
    """The provider is what a packet is grouped and searched by."""
    at, saved, _, _ = page
    fill(at, provider="   ")
    submit(at)
    assert not saved
    assert any("Provider is required" in e.value for e in at.error)


def test_a_punctuation_only_name_is_refused(page):
    """"-" passes a non-empty test and identifies nothing."""
    at, saved, _, _ = page
    fill(at, provider="- - -")
    submit(at)
    assert not saved
    assert any("least one letter or number" in e.value for e in at.error)


def test_a_blank_patient_is_refused(page):
    at, saved, _, _ = page
    fill(at, patient="")
    submit(at)
    assert not saved
    assert any("Patient is required" in e.value for e in at.error)


def test_the_amount_is_stored_as_a_decimal_not_a_float(page):
    """number_input hands back a float; nothing but Decimal may reach the Sheet."""
    at, saved, _, _ = page
    fill(at, amount=10.005)
    submit(at)
    assert isinstance(saved[0].amount, Decimal)
    assert saved[0].amount == Decimal("10.01"), "not ROUND_HALF_UP at 2dp"


# --- attaching the missing document later ----------------------------------


class FakeDrive:
    def __init__(self):
        self.uploads = []

    def year_folder(self, year):
        return str(year)

    def archive_folder(self):
        return "archive"

    def upload(self, data, name, folder):
        self.uploads.append(name)
        return {"id": f"id{len(self.uploads)}", "webViewLink": f"https://drive/{name}"}


def test_the_first_page_attached_to_a_typed_receipt_becomes_page_one(monkeypatch):
    """Otherwise the photo uploads and the receipt still reads as "no image".

    drive_file_id and drive_link back the Drive link, the audit packet's first
    image and the ZIP filename, and packet_gaps() reports "no image" purely from
    drive_file_id — so filing the original under extra_file_ids would store the
    document and leave every one of those still broken.
    """
    drive = FakeDrive()
    monkeypatch.setattr(store, "clients", lambda: (None, drive))
    monkeypatch.setattr(store, "save_receipt", lambda r: None)

    receipt = Receipt(file_hash="h" * 64, provider="Dr. Ruiz", amount=Decimal("42.18"))
    store.attach_pages(receipt, [(b"photo", "front.jpg"), (b"photo2", "back.jpg")])

    assert receipt.drive_file_id == "id1", "the original was not promoted to page one"
    assert receipt.drive_link.startswith("https://"), "no Drive link for the promoted page"
    assert receipt.extra_file_ids == ["id2"]
    assert not drive.uploads[0].startswith("p1__"), (
        f"page one was named as an extra page: {drive.uploads[0]}"
    )


def test_attaching_to_a_receipt_that_already_has_pages_is_unchanged(monkeypatch):
    """The upload path must not change: page one keeps its file."""
    drive = FakeDrive()
    monkeypatch.setattr(store, "clients", lambda: (None, drive))
    monkeypatch.setattr(store, "save_receipt", lambda r: None)

    receipt = Receipt(file_hash="h" * 64, drive_file_id="original", drive_link="https://drive/orig")
    store.attach_pages(receipt, [(b"photo", "back.jpg")])

    assert receipt.drive_file_id == "original", "an existing page one was overwritten"
    assert receipt.drive_link == "https://drive/orig"
    assert receipt.extra_file_ids == ["id1"]
    assert drive.uploads[0].startswith("p2__"), drive.uploads[0]


# --- the optional attachment -----------------------------------------------

# .pdf so the page takes no image-decoding path — this exercises the save, not
# the renderer.
PHOTO = ("front.pdf", b"%PDF-1.4 the receipt")
BACK = ("back.pdf", b"%PDF-1.4 page two")


def attach(at, *files):
    for name, content in files:
        at.file_uploader[0].upload(name, content)
    at.run()
    assert not at.exception


def test_an_attached_file_goes_to_drive_with_the_row(page):
    """Otherwise the page can only ever produce undocumented receipts."""
    at, saved, _, committed = page
    fill(at)
    attach(at, PHOTO)
    submit(at)

    assert committed, "the file never reached store.commit_receipt"
    receipt, names = committed[0]
    assert names == ["front.pdf"]
    assert receipt.drive_file_id, "the row was written with no Drive file"
    assert receipt.provider == "Dr. Ruiz", "attaching a file lost the typed fields"


def test_an_attached_receipt_is_hashed_by_its_bytes(page):
    """The same photo must collide whether it is filed from here or from Upload.

    A synthetic content hash here would let the identical file be saved twice —
    once per page — with neither noticing, which is the exact double-claim that
    hashing exists to stop.
    """
    at, saved, _, _ = page
    fill(at)
    attach(at, PHOTO)
    submit(at)
    assert saved[0].file_hash == sha256_hex(PHOTO[1])


def test_a_file_already_filed_from_upload_is_blocked_here(page):
    at, saved, index, _ = page
    index.append(
        Receipt(file_hash=sha256_hex(PHOTO[1]), provider="Already Filed",
                amount=Decimal("10.00"))
    )
    fill(at)
    attach(at, PHOTO)
    submit(at)
    assert not saved, "the same file was filed a second time"
    assert any("Duplicate blocked" in e.value for e in at.error)


def test_several_files_stay_one_receipt(page):
    """Same rule as the Upload page: a receipt shot in halves is one record."""
    at, saved, _, committed = page
    fill(at)
    attach(at, PHOTO, BACK)
    submit(at)

    assert len(committed) == 1, f"expected one receipt, got {len(committed)}"
    _, names = committed[0]
    assert names == ["front.pdf", "back.pdf"], f"the second page was dropped: {names}"


def test_saving_hands_back_an_empty_drop_zone(page):
    """A file left in the uploader would silently attach to the next entry too."""
    at, _, _, _ = page
    fill(at)
    attach(at, PHOTO)
    submit(at)
    assert not (at.file_uploader[0].value or []), "the saved file is still loaded"


def test_no_attachment_never_touches_drive(page):
    at, saved, _, committed = page
    fill(at)
    submit(at)
    assert saved and not committed, "a Drive upload was attempted with no file"
    assert saved[0].drive_file_id == ""
