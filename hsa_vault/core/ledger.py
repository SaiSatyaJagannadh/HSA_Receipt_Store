"""All balance math. Pure functions over lists of Receipt — no I/O, no globals.

This is the module the tests trust; everything the dashboard prints comes from here.
"""

from collections import OrderedDict, defaultdict
from datetime import date, timedelta
from decimal import Decimal

from .models import Contribution, Receipt

ZERO = Decimal("0.00")


def active(receipts: list[Receipt]) -> list[Receipt]:
    return [r for r in receipts if not r.deleted]


def unreimbursed_balance(receipts: list[Receipt]) -> Decimal:
    """The headline number: out-of-pocket dollars not yet withdrawn from the HSA.

    hsa_card receipts contribute zero — the HSA already paid at the register, and
    counting them would double-claim.
    """
    return sum((r.claimable for r in active(receipts)), ZERO).quantize(Decimal("0.01"))


def is_duplicate(receipts: list[Receipt], file_hash: str) -> Receipt | None:
    """Duplicate detection is by content hash, including soft-deleted receipts."""
    for r in receipts:
        if r.file_hash and r.file_hash == file_hash:
            return r
    return None


def totals_by_year(receipts: list[Receipt]) -> "OrderedDict[int, dict]":
    buckets: dict[int, dict] = defaultdict(
        lambda: {"count": 0, "total": ZERO, "reimbursed": ZERO, "claimable": ZERO}
    )
    for r in active(receipts):
        year = r.tax_year or (r.service_date.year if r.service_date else 0)
        bucket = buckets[year]
        bucket["count"] += 1
        bucket["total"] += r.amount or ZERO
        bucket["reimbursed"] += r.reimbursement_amount or ZERO
        bucket["claimable"] += r.claimable
    return OrderedDict(sorted(buckets.items(), reverse=True))


def totals_by_category(receipts: list[Receipt], year: int | None = None) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for r in active(receipts):
        if year and r.tax_year != year:
            continue
        totals[r.category] += r.amount or ZERO
    return dict(sorted(totals.items(), key=lambda kv: kv[1], reverse=True))


def monthly_series(receipts: list[Receipt]) -> "OrderedDict[str, Decimal]":
    """Spend per YYYY-MM bucket, keyed by service date."""
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for r in active(receipts):
        if not r.service_date:
            continue
        totals[r.service_date.strftime("%Y-%m")] += r.amount or ZERO
    return OrderedDict(sorted(totals.items()))


def contributions_for_year(contributions: list[Contribution], year: int) -> Decimal:
    return sum(
        (c.amount or ZERO for c in contributions if c.tax_year == year), ZERO
    ).quantize(Decimal("0.01"))


def projection(balance: Decimal, annual_rate: float, years: list[int]) -> dict[int, Decimal]:
    """Illustration only. Compound the unclaimed balance at a user-set rate."""
    rate = Decimal(str(1 + annual_rate))
    return {
        y: (balance * (rate**y)).quantize(Decimal("0.01")) for y in years
    }


def warnings(receipts: list[Receipt], today: date | None = None) -> list[dict]:
    """Everything that needs my attention before an audit does."""
    today = today or date.today()
    stale_before = today - timedelta(days=365)
    out = []
    for r in active(receipts):
        problems = []
        if r.amount is None:
            problems.append("missing amount")
        if r.service_date is None:
            problems.append("missing service date")
        if r.eligibility_confidence == "review":
            problems.append("flagged for review")
        if (
            r.payment_method == "out_of_pocket"
            and not r.reimbursed
            and r.service_date
            and r.service_date < stale_before
        ):
            problems.append("unreimbursed for over 12 months")
        if problems:
            out.append({"receipt": r, "problems": problems})
    return out


def selectable_for_reimbursement(receipts: list[Receipt]) -> list[Receipt]:
    """Only out-of-pocket receipts with something left to claim. hsa_card never appears."""
    return [r for r in active(receipts) if r.claimable > ZERO]


def allocate_reimbursement(
    receipts: list[Receipt], withdrawal_total: Decimal
) -> list[tuple[Receipt, Decimal, bool]]:
    """Spread a withdrawal over selected receipts, oldest service date first.

    Returns (receipt, dollars_applied, fully_covered). A withdrawal smaller than
    the selected total leaves the last touched receipt partially reimbursed and
    still claimable for the remainder; later receipts get nothing.
    """
    remaining = money_or_zero(withdrawal_total)
    ordered = sorted(receipts, key=lambda r: (r.service_date or date.max, r.receipt_id))
    allocations = []
    for r in ordered:
        if remaining <= ZERO:
            break
        claim = r.claimable
        if claim <= ZERO:
            continue
        applied = min(claim, remaining)
        remaining -= applied
        allocations.append((r, applied.quantize(Decimal("0.01")), applied == claim))
    return allocations


def apply_allocation(
    receipt: Receipt, applied: Decimal, fully_covered: bool, when: date
) -> Receipt:
    """Mutate a receipt to record its share of a withdrawal. Guards double-claiming."""
    if receipt.payment_method != "out_of_pocket":
        raise ValueError("hsa_card receipts cannot be reimbursed")
    if receipt.reimbursed:
        raise ValueError("receipt is already fully reimbursed")
    prior = receipt.reimbursement_amount or ZERO
    receipt.reimbursement_amount = (prior + applied).quantize(Decimal("0.01"))
    receipt.reimbursement_date = when
    receipt.reimbursed = fully_covered
    receipt.record_edit(
        {"reimbursement_amount": receipt.reimbursement_amount, "reimbursed": receipt.reimbursed},
        note="reimbursement applied",
    )
    return receipt


def money_or_zero(value) -> Decimal:
    if value is None:
        return ZERO
    return Decimal(str(value)).quantize(Decimal("0.01"))


def amount_bounds(receipts: list[Receipt]) -> tuple[float, float]:
    """(low, high) for the amount-range filter, guaranteed low < high.

    Streamlit's slider rejects min_value == max_value, which happens with a
    single receipt or several of the same amount — a real state, not an edge
    case, and the one every brand-new vault starts in.
    """
    amounts = [float(r.amount) for r in receipts if r.amount is not None]
    if not amounts:
        return 0.0, 1.0
    low, high = min(amounts), max(amounts)
    if high <= low:
        high = low + 1.0
    return low, high
