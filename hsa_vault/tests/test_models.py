from datetime import date, datetime, timezone
from decimal import Decimal

from core.models import (
    RECEIPT_COLUMNS,
    Contribution,
    Receipt,
    Reimbursement,
    money,
    parse_bool,
    parse_date,
)


# --- money -----------------------------------------------------------------


def test_money_strips_formatting():
    assert money("$1,234.50") == Decimal("1234.50")


def test_money_quantizes_to_two_places():
    assert money("10.005") == Decimal("10.01")
    assert money(7) == Decimal("7.00")


def test_money_returns_none_for_unparseable_input():
    for value in ("", None, "n/a", "unknown"):
        assert money(value) is None


def test_money_never_returns_a_float():
    assert isinstance(money("3.33"), Decimal)


# --- dates -----------------------------------------------------------------


def test_parse_date_accepts_common_receipt_formats():
    expected = date(2026, 3, 14)
    for text in ("2026-03-14", "03/14/2026", "03/14/26", "Mar 14, 2026", "14 Mar 2026"):
        assert parse_date(text) == expected


def test_parse_date_returns_none_for_garbage():
    assert parse_date("sometime last spring") is None


def test_parse_bool():
    assert parse_bool("TRUE") and parse_bool("yes") and parse_bool(True)
    assert not parse_bool("FALSE") and not parse_bool("") and not parse_bool(None)


# --- receipt round trip ----------------------------------------------------


def test_row_round_trip_preserves_every_field():
    original = Receipt(
        file_hash="a" * 64,
        drive_file_id="drive123",
        drive_link="https://drive.google.com/x",
        service_date=date(2026, 3, 14),
        upload_date=datetime(2026, 3, 15, 9, 30, tzinfo=timezone.utc),
        provider="CVS Pharmacy",
        amount=Decimal("42.18"),
        category="Prescription",
        description="Amoxicillin",
        payment_method="hsa_card",
        reimbursed=True,
        reimbursement_date=date(2026, 4, 1),
        reimbursement_amount=Decimal("42.18"),
        patient="self",
        eligibility_confidence="certain",
        notes="note",
    )
    restored = Receipt.from_row(dict(zip(RECEIPT_COLUMNS, original.to_row())))
    assert restored.to_row() == original.to_row()
    assert restored.amount == Decimal("42.18")
    assert restored.service_date == date(2026, 3, 14)
    assert restored.reimbursed is True


def test_amounts_survive_the_sheet_as_strings():
    """Sheets returns everything as text; nothing may become a float on the way back."""
    row = dict(zip(RECEIPT_COLUMNS, Receipt(file_hash="h", amount=Decimal("0.10")).to_row()))
    assert row["amount"] == "0.10"
    assert isinstance(Receipt.from_row(row).amount, Decimal)


def test_from_row_tolerates_short_and_empty_rows():
    receipt = Receipt.from_row({"receipt_id": "abc", "file_hash": "h"})
    assert receipt.category == "Other"
    assert receipt.payment_method == "out_of_pocket"
    assert receipt.amount is None


# --- validation ------------------------------------------------------------


def test_valid_receipt_has_no_errors():
    assert Receipt(file_hash="h", amount=Decimal("10.00")).validate() == []


def test_validation_catches_bad_enums():
    receipt = Receipt(file_hash="h", category="Snacks", payment_method="crypto")
    errors = receipt.validate()
    assert any("category" in e for e in errors)
    assert any("payment_method" in e for e in errors)


def test_validation_rejects_missing_hash():
    assert any("file_hash" in e for e in Receipt().validate())


def test_validation_rejects_over_reimbursement():
    receipt = Receipt(
        file_hash="h", amount=Decimal("50.00"), reimbursement_amount=Decimal("75.00")
    )
    assert any("exceeds" in e for e in receipt.validate())


def test_validation_rejects_negative_amount():
    assert any("negative" in e for e in Receipt(file_hash="h", amount=Decimal("-1")).validate())


def test_validation_catches_tax_year_drift():
    receipt = Receipt(file_hash="h", service_date=date(2026, 1, 1))
    receipt.tax_year = 2025
    assert any("tax_year" in e for e in receipt.validate())


# --- edit history ----------------------------------------------------------


def test_edit_history_appends_and_survives_serialization():
    receipt = Receipt(file_hash="h", provider="Old")
    receipt.record_edit({"provider": "New"}, note="manual edit")
    restored = Receipt.from_row(dict(zip(RECEIPT_COLUMNS, receipt.to_row())))
    history = restored.history()
    assert len(history) == 1
    assert history[0]["changes"]["provider"] == "New"
    assert history[0]["note"] == "manual edit"


def test_history_tolerates_corrupt_json():
    receipt = Receipt(file_hash="h", edit_history="{not json")
    assert receipt.history() == []


# --- other models ----------------------------------------------------------


def test_reimbursement_round_trip_with_multiple_receipt_ids():
    from core.models import REIMBURSEMENT_COLUMNS

    original = Reimbursement(
        date=date(2026, 5, 1),
        amount=Decimal("400.00"),
        method="transfer",
        covered_receipt_ids=["a", "b", "c"],
        notes="quarterly catch-up",
    )
    restored = Reimbursement.from_row(dict(zip(REIMBURSEMENT_COLUMNS, original.to_row())))
    assert restored.covered_receipt_ids == ["a", "b", "c"]
    assert restored.amount == Decimal("400.00")


def test_contribution_derives_tax_year():
    assert Contribution(date=date(2026, 2, 1), amount=Decimal("350")).tax_year == 2026


def test_from_row_tolerates_a_hand_edited_upload_date():
    from core.models import parse_datetime

    assert parse_datetime("2026-03-15T09:30:00+00:00") is not None
    assert parse_datetime("03/15/2026") == datetime(2026, 3, 15)
    assert parse_datetime("who knows") is None
    assert Receipt.from_row({"file_hash": "h", "upload_date": "garbage"}).upload_date is None


def test_drive_link_only_accepts_http_urls():
    """The Sheet is hand-editable and drive_link renders as a clickable link."""
    from core.models import safe_url

    assert safe_url("https://drive.google.com/file/d/abc") == "https://drive.google.com/file/d/abc"
    assert safe_url("javascript:alert(document.cookie)") == ""
    assert safe_url("JaVaScRiPt:alert(1)") == ""
    assert safe_url("data:text/html;base64,PHNjcmlwdD4=") == ""
    assert safe_url("file:///etc/passwd") == ""
    assert safe_url(None) == ""
    assert Receipt.from_row({"file_hash": "h", "drive_link": "javascript:alert(1)"}).drive_link == ""


def test_the_phone_inbox_folder_survives_a_settings_round_trip(tmp_path, monkeypatch):
    """Bulk Import pre-fills its scan from this, so a phone-capture routine does
    not require pasting a 33-character Drive folder ID every week."""
    from core import config

    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", path)
    monkeypatch.delenv("HSA_INBOX_FOLDER_ID", raising=False)

    config.save_settings(config.Settings(inbox_folder_id="folder-abc", sheet_id="s"))
    assert config.load_settings().inbox_folder_id == "folder-abc"


def test_the_inbox_folder_is_optional(tmp_path, monkeypatch):
    """It must never become a precondition for using the app."""
    from core import config

    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "none.json")
    monkeypatch.delenv("HSA_INBOX_FOLDER_ID", raising=False)
    settings = config.Settings(
        google_credentials_json=__file__, drive_folder_id="d", sheet_id="s"
    )
    assert settings.inbox_folder_id == ""
    assert settings.ready() == [], "a missing inbox folder blocked Google access"


def test_edit_history_reads_as_english_not_json():
    """Raw JSON under the edit form is noise at the moment of editing.

    record_edit() stringifies everything for JSON safety, so booleans arrive as
    "True"/"False" and payment methods as their wire values — neither is what
    the user clicked.
    """
    from core.models import describe_edit

    when, what = describe_edit(
        {
            "ts": "2026-08-16T23:01:00+00:00",
            "changes": {"payment_method": "hsa_card", "deleted": "False", "amount": "27.40"},
            "note": "manual edit",
        }
    )

    assert "T" not in when and "+00:00" not in when, f"raw ISO timestamp shown: {when}"
    assert "HSA card" in what, "the wire value was shown instead of the form's label"
    assert "Archived → no" in what, '"False" was shown instead of a readable value'
    assert "Amount → 27.40" in what
    assert "manual edit" in what


def test_a_hand_broken_timestamp_does_not_crash_the_history():
    """The Sheet is hand-editable, so anything can end up in that column."""
    from core.models import describe_edit

    when, what = describe_edit({"ts": "not a date", "changes": {}, "note": "restored"})
    assert when == "not a date"
    assert what == "restored"
