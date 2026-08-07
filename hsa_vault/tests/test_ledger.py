"""The balance number has to be trustworthy. These tests are why it is."""

from datetime import date
from decimal import Decimal

import pytest

from core import ledger
from core.models import Contribution, Receipt


def receipt(**kwargs) -> Receipt:
    defaults = dict(
        file_hash=kwargs.pop("file_hash", "h" + str(id(kwargs))),
        service_date=date(2026, 3, 14),
        provider="Test Clinic",
        amount=Decimal("100.00"),
        category="Physician",
        payment_method="out_of_pocket",
    )
    defaults.update(kwargs)
    return Receipt(**defaults)


# --- the headline number ---------------------------------------------------


def test_unreimbursed_balance_excludes_hsa_card_receipts():
    """The single most important rule: HSA-card spend is already paid by the HSA.
    Counting it would let me claim the same dollar twice."""
    receipts = [
        receipt(amount=Decimal("100.00"), payment_method="out_of_pocket"),
        receipt(amount=Decimal("250.00"), payment_method="hsa_card"),
        receipt(amount=Decimal("75.50"), payment_method="hsa_card"),
    ]
    assert ledger.unreimbursed_balance(receipts) == Decimal("100.00")


def test_unreimbursed_balance_excludes_already_reimbursed():
    receipts = [
        receipt(amount=Decimal("100.00")),
        receipt(
            amount=Decimal("300.00"),
            reimbursed=True,
            reimbursement_amount=Decimal("300.00"),
            reimbursement_date=date(2026, 5, 1),
        ),
    ]
    assert ledger.unreimbursed_balance(receipts) == Decimal("100.00")


def test_unreimbursed_balance_excludes_archived():
    receipts = [receipt(amount=Decimal("100.00")), receipt(amount=Decimal("999.00"), deleted=True)]
    assert ledger.unreimbursed_balance(receipts) == Decimal("100.00")


def test_unreimbursed_balance_ignores_missing_amounts():
    receipts = [receipt(amount=None), receipt(amount=Decimal("40.00"))]
    assert ledger.unreimbursed_balance(receipts) == Decimal("40.00")


def test_balance_is_decimal_not_float():
    """Three amounts that would drift if summed as floats."""
    receipts = [receipt(amount=Decimal(x)) for x in ("0.10", "0.20", "0.30")]
    assert ledger.unreimbursed_balance(receipts) == Decimal("0.60")


# --- partial reimbursement -------------------------------------------------


def test_partial_reimbursement_leaves_remainder_claimable():
    r = receipt(amount=Decimal("300.00"), reimbursement_amount=Decimal("50.00"), reimbursed=False)
    assert r.claimable == Decimal("250.00")
    assert ledger.unreimbursed_balance([r]) == Decimal("250.00")


def test_allocate_withdrawal_smaller_than_selection_is_partial():
    old = receipt(amount=Decimal("100.00"), service_date=date(2025, 1, 1))
    new = receipt(amount=Decimal("100.00"), service_date=date(2026, 1, 1))
    allocations = ledger.allocate_reimbursement([new, old], Decimal("150.00"))

    # Oldest first.
    assert [a[0] for a in allocations] == [old, new]
    assert allocations[0][1] == Decimal("100.00") and allocations[0][2] is True
    assert allocations[1][1] == Decimal("50.00") and allocations[1][2] is False


def test_applying_a_partial_allocation_keeps_the_receipt_claimable():
    r = receipt(amount=Decimal("100.00"))
    ledger.apply_allocation(r, Decimal("40.00"), False, date(2026, 6, 1))
    assert r.reimbursed is False
    assert r.reimbursement_amount == Decimal("40.00")
    assert r.claimable == Decimal("60.00")


def test_two_partial_allocations_accumulate_and_settle():
    r = receipt(amount=Decimal("100.00"))
    ledger.apply_allocation(r, Decimal("40.00"), False, date(2026, 6, 1))
    ledger.apply_allocation(r, Decimal("60.00"), True, date(2026, 7, 1))
    assert r.reimbursement_amount == Decimal("100.00")
    assert r.reimbursed is True
    assert r.claimable == Decimal("0.00")


def test_allocation_stops_when_the_withdrawal_runs_out():
    a = receipt(amount=Decimal("50.00"), service_date=date(2025, 1, 1))
    b = receipt(amount=Decimal("50.00"), service_date=date(2025, 6, 1))
    c = receipt(amount=Decimal("50.00"), service_date=date(2025, 9, 1))
    allocations = ledger.allocate_reimbursement([a, b, c], Decimal("60.00"))
    assert len(allocations) == 2
    assert c not in [x[0] for x in allocations]


# --- multi-receipt withdrawals ---------------------------------------------


def test_one_withdrawal_covering_several_receipts_zeroes_the_balance():
    receipts = [
        receipt(amount=Decimal("100.00"), service_date=date(2026, 1, 1)),
        receipt(amount=Decimal("250.50"), service_date=date(2026, 2, 1)),
        receipt(amount=Decimal("49.50"), service_date=date(2026, 3, 1)),
    ]
    total = ledger.unreimbursed_balance(receipts)
    assert total == Decimal("400.00")

    for r, applied, fully in ledger.allocate_reimbursement(receipts, total):
        ledger.apply_allocation(r, applied, fully, date(2026, 4, 1))

    assert ledger.unreimbursed_balance(receipts) == Decimal("0.00")
    assert all(r.reimbursed for r in receipts)


# --- guards ----------------------------------------------------------------


def test_hsa_card_receipts_never_appear_in_the_selection_list():
    receipts = [
        receipt(payment_method="hsa_card"),
        receipt(payment_method="out_of_pocket"),
    ]
    selectable = ledger.selectable_for_reimbursement(receipts)
    assert len(selectable) == 1
    assert selectable[0].payment_method == "out_of_pocket"


def test_applying_to_an_hsa_card_receipt_raises():
    with pytest.raises(ValueError, match="hsa_card"):
        ledger.apply_allocation(
            receipt(payment_method="hsa_card"), Decimal("10.00"), True, date(2026, 1, 1)
        )


def test_applying_to_an_already_reimbursed_receipt_raises():
    r = receipt(reimbursed=True, reimbursement_amount=Decimal("100.00"))
    with pytest.raises(ValueError, match="already"):
        ledger.apply_allocation(r, Decimal("10.00"), True, date(2026, 1, 1))


def test_fully_reimbursed_receipts_are_not_selectable():
    r = receipt(reimbursed=True, reimbursement_amount=Decimal("100.00"))
    assert ledger.selectable_for_reimbursement([r]) == []


# --- duplicate detection ---------------------------------------------------


def test_duplicate_hash_is_rejected():
    existing = receipt(file_hash="abc123")
    assert ledger.is_duplicate([existing], "abc123") is existing
    assert ledger.is_duplicate([existing], "different") is None


def test_duplicate_detection_still_catches_archived_receipts():
    """Re-uploading something I deliberately archived should still be blocked."""
    archived = receipt(file_hash="abc123", deleted=True)
    assert ledger.is_duplicate([archived], "abc123") is archived


def test_empty_hash_never_matches():
    assert ledger.is_duplicate([receipt(file_hash="")], "") is None


# --- tax year boundaries ---------------------------------------------------


def test_tax_year_is_derived_from_service_date_not_upload_date():
    from datetime import datetime, timezone

    r = Receipt(
        file_hash="x",
        service_date=date(2025, 12, 31),
        upload_date=datetime(2026, 4, 10, tzinfo=timezone.utc),
        amount=Decimal("10.00"),
    )
    assert r.tax_year == 2025


def test_receipts_on_either_side_of_new_year_land_in_different_years():
    dec = receipt(service_date=date(2025, 12, 31), amount=Decimal("10.00"))
    jan = receipt(service_date=date(2026, 1, 1), amount=Decimal("20.00"))
    totals = ledger.totals_by_year([dec, jan])
    assert totals[2025]["total"] == Decimal("10.00")
    assert totals[2026]["total"] == Decimal("20.00")


def test_totals_by_year_tracks_claimable_separately():
    receipts = [
        receipt(service_date=date(2026, 1, 1), amount=Decimal("100.00")),
        receipt(service_date=date(2026, 2, 1), amount=Decimal("100.00"), payment_method="hsa_card"),
    ]
    totals = ledger.totals_by_year(receipts)
    assert totals[2026]["total"] == Decimal("200.00")
    assert totals[2026]["claimable"] == Decimal("100.00")


# --- reporting helpers -----------------------------------------------------


def test_category_totals():
    receipts = [
        receipt(category="Dental", amount=Decimal("300.00")),
        receipt(category="Dental", amount=Decimal("100.00")),
        receipt(category="Vision", amount=Decimal("50.00")),
    ]
    totals = ledger.totals_by_category(receipts)
    assert totals["Dental"] == Decimal("400.00")
    assert list(totals)[0] == "Dental"  # sorted descending


def test_monthly_series_buckets_by_service_month():
    receipts = [
        receipt(service_date=date(2026, 1, 5), amount=Decimal("10.00")),
        receipt(service_date=date(2026, 1, 25), amount=Decimal("15.00")),
        receipt(service_date=date(2026, 2, 3), amount=Decimal("20.00")),
    ]
    series = ledger.monthly_series(receipts)
    assert series["2026-01"] == Decimal("25.00")
    assert series["2026-02"] == Decimal("20.00")


def test_projection_compounds_the_balance():
    values = ledger.projection(Decimal("1000.00"), 0.07, [10])
    assert values[10] == Decimal("1967.15")


def test_projection_of_zero_stays_zero():
    assert ledger.projection(Decimal("0.00"), 0.07, [5, 20]) == {
        5: Decimal("0.00"),
        20: Decimal("0.00"),
    }


def test_contributions_are_summed_per_tax_year():
    contributions = [
        Contribution(date=date(2026, 1, 1), amount=Decimal("350.00")),
        Contribution(date=date(2026, 2, 1), amount=Decimal("350.00")),
        Contribution(date=date(2025, 6, 1), amount=Decimal("999.00")),
    ]
    assert ledger.contributions_for_year(contributions, 2026) == Decimal("700.00")


# --- warnings --------------------------------------------------------------


def test_warnings_flag_missing_data_and_review_status():
    today = date(2026, 6, 1)
    flagged = ledger.warnings(
        [receipt(amount=None, eligibility_confidence="review", service_date=None)], today
    )
    problems = flagged[0]["problems"]
    assert "missing amount" in problems
    assert "missing service date" in problems
    assert "flagged for review" in problems


def test_warnings_flag_stale_unreimbursed_receipts():
    today = date(2026, 6, 1)
    stale = receipt(service_date=date(2025, 1, 1), eligibility_confidence="certain")
    fresh = receipt(service_date=date(2026, 5, 1), eligibility_confidence="certain")
    flagged = ledger.warnings([stale, fresh], today)
    assert len(flagged) == 1
    assert "unreimbursed for over 12 months" in flagged[0]["problems"]


def test_stale_hsa_card_receipts_are_not_flagged_as_unreimbursed():
    today = date(2026, 6, 1)
    old_card = receipt(
        service_date=date(2020, 1, 1),
        payment_method="hsa_card",
        eligibility_confidence="certain",
    )
    assert ledger.warnings([old_card], today) == []
