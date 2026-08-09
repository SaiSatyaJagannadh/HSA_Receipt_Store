"""Archiving a receipt has to be reversible from inside the app.

The data model always treated archiving as a soft delete — `deleted` is a flag and
the file only moves to `_archive/` — but the UI exposed no way back, so a mis-click
was permanent as far as any user could tell. It also sat at the bottom of the page
inside a collapsed expander behind a type-the-word-ARCHIVE gate: friction out of
all proportion to an action that changes one boolean.

These cover both halves: the store can undo an archive and puts the file back
where an upload would have put it, and the page offers the round trip in two
clicks with no typing.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from core import config, ledger, store
from core.models import Receipt

RECEIPTS_PAGE = str(Path(__file__).resolve().parents[1] / "pages" / "2_Receipts.py")


class RecordingDrive:
    """Remembers where each file was moved, so a restore can be checked."""

    def __init__(self):
        self.moves: list[tuple[str, str]] = []

    def move(self, file_id, parent):
        self.moves.append((file_id, parent))

    def year_folder(self, year):
        return f"year-{year}"

    def archive_folder(self):
        return "archive"

    def download(self, fid):
        raise RuntimeError("no network in tests")


def make_receipt(**kw) -> Receipt:
    base = dict(
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
    base.update(kw)
    return Receipt(**base)


# --- store level -----------------------------------------------------------


@pytest.fixture
def wired_store(monkeypatch):
    drive = RecordingDrive()
    saved: list[Receipt] = []
    monkeypatch.setattr(store, "clients", lambda: (None, drive))
    monkeypatch.setattr(store, "save_receipt", lambda r: saved.append(r))
    return drive, saved


def test_restore_clears_the_flag_and_puts_the_file_back(wired_store):
    drive, saved = wired_store
    receipt = make_receipt(deleted=True, tax_year=2026)

    store.restore_receipt(receipt)

    assert receipt.deleted is False
    assert saved == [receipt], "the restore was never written to the Sheet"
    assert drive.moves == [("f1", "year-2026")], (
        "a restored file must go back to its year folder, not stay in _archive"
    )


def test_restore_is_recorded_in_the_edit_history(wired_store):
    receipt = make_receipt(deleted=True)
    store.restore_receipt(receipt, reason="mis-click")
    entry = receipt.history()[-1]
    # record_edit stringifies values so the history stays JSON- and Sheet-safe.
    assert entry["changes"] == {"deleted": "False"}
    assert entry["note"] == "mis-click"


def test_archive_then_restore_returns_the_receipt_to_the_balance(wired_store):
    """The point of the round trip: the money comes back."""
    receipt = make_receipt()
    assert ledger.unreimbursed_balance([receipt]) == Decimal("25.46")

    store.archive_receipt(receipt)
    assert ledger.unreimbursed_balance([receipt]) == Decimal("0.00")

    store.restore_receipt(receipt)
    assert ledger.unreimbursed_balance([receipt]) == Decimal("25.46")


def test_restore_lands_in_the_same_folder_an_upload_would_use(wired_store):
    """Restore and commit must agree, or a restored file goes somewhere new."""
    drive, _ = wired_store
    receipt = make_receipt(tax_year=None, service_date=date(2027, 2, 1), deleted=True)
    store.restore_receipt(receipt)
    assert drive.moves == [("f1", "year-2027")]


# --- page level ------------------------------------------------------------


@pytest.fixture
def page(monkeypatch):
    """Receipts page over one receipt, capturing archive/restore calls."""
    archived: list[Receipt] = []
    restored: list[Receipt] = []
    state = {"receipt": make_receipt()}

    settings = config.Settings(
        google_credentials_json=__file__,
        drive_folder_id="folder",
        sheet_id="sheet",
        nvidia_api_key="",
    )
    monkeypatch.setattr(config, "load_settings", lambda: settings)
    monkeypatch.setattr(store, "receipts", lambda refresh=False: [state["receipt"]])
    monkeypatch.setattr(store, "clients", lambda: (None, None))
    monkeypatch.setattr(store, "save_receipt", lambda r: None)

    def fake_archive(r, reason=""):
        r.deleted = True
        archived.append(r)
        return r

    def fake_restore(r, reason=""):
        r.deleted = False
        restored.append(r)
        return r

    monkeypatch.setattr(store, "archive_receipt", fake_archive)
    monkeypatch.setattr(store, "restore_receipt", fake_restore)

    def build(deleted=False):
        state["receipt"] = make_receipt(deleted=deleted)
        at = AppTest.from_file(RECEIPTS_PAGE, default_timeout=60)
        if deleted:
            at.session_state["_hsa_show_archived"] = True
        at.run()
        assert not at.exception
        return at

    return build, archived, restored


def click(at, label):
    matches = [b for b in at.button if b.label == label]
    assert matches, f"no button labelled {label!r}; have {[b.label for b in at.button]}"
    matches[0].click()
    at.run()
    assert not at.exception
    return at


def test_archiving_takes_two_clicks_and_no_typing(page):
    build, archived, _ = page
    at = build()

    at = click(at, "🗑️ Archive")
    assert not archived, "one click must not archive — it only asks"
    assert at.warning, "no confirmation was shown"

    at = click(at, "Archive it")
    assert archived, "confirming did not archive the receipt"


def test_the_type_ARCHIVE_gate_is_gone(page):
    build, _, _ = page
    at = click(build(), "🗑️ Archive")
    labels = [t.label for t in at.text_input]
    assert not any("Type ARCHIVE" in (label or "") for label in labels), labels


def test_cancelling_backs_out_without_archiving(page):
    build, archived, _ = page
    at = click(build(), "🗑️ Archive")
    at = click(at, "Cancel")
    assert not archived
    assert not at.warning, "the confirmation panel was left on screen"
    assert [b for b in at.button if b.label == "🗑️ Archive"], "the archive button did not come back"


def test_archiving_confirms_itself(page):
    build, _, _ = page
    at = click(build(), "🗑️ Archive")
    at = click(at, "Archive it")
    assert at.success, "the archive saved silently"
    assert "Archived" in at.success[0].value


def test_an_archived_receipt_offers_a_way_back(page):
    build, _, restored = page
    at = build(deleted=True)

    assert not [b for b in at.button if b.label == "🗑️ Archive"], (
        "an already-archived receipt should not offer to archive again"
    )
    at = click(at, "♻️ Restore")
    assert restored, "Restore did not call store.restore_receipt"
    assert at.success and "Restored" in at.success[0].value
