"""A withdrawal must be on record before any receipt is marked as paid by it.

Sheets has no transaction. A multi-receipt withdrawal is N+1 separate writes, and
any one of them can fail — a dropped connection, a quota refusal, a revoked token.
So the write order is not a style question: it decides which half of the operation
survives a failure.

Marking receipts first (what this page used to do) means a crash leaves receipts
flagged reimbursed with no withdrawal row anywhere. The claimable balance drops and
nothing in the vault records where the money went — and the audit packet, which is
the artifact this whole app exists to produce, would understate reimbursements.

Recording the withdrawal first means a crash leaves the money on record with some
receipts not yet marked: the balance reads too HIGH, the gap is visible in the
withdrawal history, and nothing has been double-claimed. That is the recoverable
direction, and these tests hold the code to it.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from core import ledger, store
from core.models import Receipt, Reimbursement


def make_receipt(amount: str, day: int) -> Receipt:
    return Receipt(
        file_hash=f"{day:064d}",
        drive_file_id=f"f{day}",
        service_date=date(2026, 1, day),
        upload_date=datetime.now(timezone.utc),
        provider=f"Clinic {day}",
        amount=Decimal(amount),
        category="Other",
        payment_method="out_of_pocket",
    )


@pytest.fixture
def journal(monkeypatch):
    """Records the order of writes, and can be told to fail on the Nth receipt."""
    log: list[str] = []
    fail_on = {"receipt": None, "reimbursement": False}

    def save_reimbursement(rb):
        if fail_on["reimbursement"]:
            raise RuntimeError("sheets unavailable")
        log.append("reimbursement")

    def save_receipt(r):
        seen = sum(1 for entry in log if entry.startswith("receipt"))
        if fail_on["receipt"] == seen:
            raise RuntimeError("sheets unavailable")
        log.append(f"receipt:{r.receipt_id[:8]}")

    monkeypatch.setattr(store, "save_reimbursement", save_reimbursement)
    monkeypatch.setattr(store, "save_receipt", save_receipt)
    return log, fail_on


def build(*receipts, total: str):
    withdrawal = Decimal(total)
    allocations = ledger.allocate_reimbursement(list(receipts), withdrawal)
    record = Reimbursement(
        date=date(2026, 2, 1),
        amount=withdrawal,
        method="transfer",
        covered_receipt_ids=[r.receipt_id for r, _, _ in allocations],
    )
    return record, allocations


def test_the_withdrawal_is_written_before_any_receipt(journal):
    """The regression. Order is the entire fix."""
    log, _ = journal
    a, b = make_receipt("40.00", 1), make_receipt("60.00", 2)
    record, allocations = build(a, b, total="100.00")

    store.record_reimbursement(record, allocations, date(2026, 2, 1))

    assert log[0] == "reimbursement", (
        f"a receipt was marked before the withdrawal was recorded: {log}"
    )
    assert len(log) == 3


def test_a_failure_part_way_leaves_the_money_on_record(journal):
    log, fail_on = journal
    a, b, c = make_receipt("10.00", 1), make_receipt("20.00", 2), make_receipt("30.00", 3)
    record, allocations = build(a, b, c, total="60.00")
    fail_on["receipt"] = 1  # blow up on the second receipt

    with pytest.raises(store.PartialReimbursement) as caught:
        store.record_reimbursement(record, allocations, date(2026, 2, 1))

    assert "reimbursement" in log, "the withdrawal row was lost with the failure"
    assert caught.value.applied == 1
    assert caught.value.total == 3


def test_a_partial_failure_leaves_the_balance_high_not_low(journal):
    """The safe direction: unclaimed dollars, never dollars claimed twice."""
    _, fail_on = journal
    a, b = make_receipt("10.00", 1), make_receipt("20.00", 2)
    record, allocations = build(a, b, total="30.00")
    fail_on["receipt"] = 1

    with pytest.raises(store.PartialReimbursement):
        store.record_reimbursement(record, allocations, date(2026, 2, 1))

    remaining = ledger.unreimbursed_balance([a, b])
    assert remaining == Decimal("20.00"), (
        "the unmarked receipt must stay claimable — it was not actually reimbursed"
    )
    assert remaining > Decimal("0.00"), "balance must err high, never low"


def test_nothing_is_marked_if_the_withdrawal_row_itself_fails(journal):
    log, fail_on = journal
    a = make_receipt("10.00", 1)
    record, allocations = build(a, total="10.00")
    fail_on["reimbursement"] = True

    with pytest.raises(RuntimeError):
        store.record_reimbursement(record, allocations, date(2026, 2, 1))

    assert log == [], "receipts were marked even though the withdrawal never landed"
    assert ledger.unreimbursed_balance([a]) == Decimal("10.00")


def test_the_happy_path_marks_every_receipt_and_returns_the_count(journal):
    log, _ = journal
    a, b = make_receipt("10.00", 1), make_receipt("20.00", 2)
    record, allocations = build(a, b, total="30.00")

    applied = store.record_reimbursement(record, allocations, date(2026, 2, 1))

    assert applied == 2
    assert log == ["reimbursement", f"receipt:{a.receipt_id[:8]}", f"receipt:{b.receipt_id[:8]}"]
    assert ledger.unreimbursed_balance([a, b]) == Decimal("0.00")


def test_a_partial_withdrawal_leaves_the_remainder_claimable(journal):
    """Unchanged behaviour, held here because the write order now runs through store."""
    _, _ = journal
    a = make_receipt("100.00", 1)
    record, allocations = build(a, total="40.00")

    store.record_reimbursement(record, allocations, date(2026, 2, 1))

    assert a.reimbursed is False
    assert ledger.unreimbursed_balance([a]) == Decimal("60.00")
