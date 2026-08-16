"""Dataclasses + validation + row (de)serialization.

Money is Decimal everywhere in Python and a plain 2dp string in Sheets, so no
float ever touches a dollar amount.
"""

from __future__ import annotations  # `date: date | None` needs lazy annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

CATEGORIES = [
    "Physician",
    "Dental",
    "Vision",
    "Prescription",
    "OTC Medication",
    "Medical Devices/Supplies",
    "Lab/Diagnostic",
    "Hospital/Surgery",
    "Mental Health",
    "Physical Therapy",
    "Chiropractic",
    "Insurance Premium (limited eligibility)",
    "Mileage/Travel",
    "Other",
]

NARROW_ELIGIBILITY = {"Insurance Premium (limited eligibility)"}

PAYMENT_METHODS = ["out_of_pocket", "hsa_card"]
PAYMENT_LABELS = {
    "out_of_pocket": "Out of pocket (claimable)",
    "hsa_card": "HSA card (already paid from HSA)",
}
CONFIDENCE_LEVELS = ["certain", "likely", "review"]

RECEIPT_COLUMNS = [
    "receipt_id",
    "file_hash",
    "drive_file_id",
    "drive_link",
    "service_date",
    "upload_date",
    "provider",
    "amount",
    "category",
    "description",
    "payment_method",
    "reimbursed",
    "reimbursement_date",
    "reimbursement_amount",
    "patient",
    "tax_year",
    "eligibility_confidence",
    "notes",
    "extraction_raw",
    "deleted",
    "edit_history",
    # Appended, never inserted: read_tab() maps columns positionally from this
    # list and pads short rows, so an older row simply reads back as "".
    "extra_file_ids",
]

REIMBURSEMENT_COLUMNS = [
    "reimbursement_id",
    "date",
    "amount",
    "method",
    "covered_receipt_ids",
    "notes",
]

CONTRIBUTION_COLUMNS = ["date", "amount", "source", "tax_year"]

TABS = {
    "receipts": RECEIPT_COLUMNS,
    "reimbursements": REIMBURSEMENT_COLUMNS,
    "contributions": CONTRIBUTION_COLUMNS,
}


# --- scalar coercion -------------------------------------------------------


def money(value) -> Decimal | None:
    """Anything -> 2dp Decimal, or None. Never returns a float.

    ROUND_HALF_UP, not Decimal's default banker's rounding — half-cents on a
    receipt round up, the way a register does.
    """
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    text = str(value).replace("$", "").replace(",", "").strip()
    if not text:
        return None
    try:
        return Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def money_str(value) -> str:
    amount = money(value)
    return "" if amount is None else f"{amount:.2f}"


def parse_date(value) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def parse_datetime(value) -> datetime | None:
    """Tolerant: a hand-edited Sheet must never crash the reader."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        day = parse_date(value)
        return datetime(day.year, day.month, day.day) if day else None


def safe_url(value) -> str:
    """Only http(s) survives. drive_link is rendered as a clickable link but the
    Sheet is hand-editable, so a `javascript:` URL pasted into that column would
    otherwise become a one-click script in my own browser."""
    text = str(value or "").strip()
    return text if text.lower().startswith(("http://", "https://")) else ""


def parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "1", "y"}


def _date_str(value: date | None) -> str:
    return value.isoformat() if value else ""


# --- receipts --------------------------------------------------------------


@dataclass
class Receipt:
    receipt_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    file_hash: str = ""
    drive_file_id: str = ""
    drive_link: str = ""
    service_date: date | None = None
    upload_date: datetime | None = None
    provider: str = ""
    amount: Decimal | None = None
    category: str = "Other"
    description: str = ""
    payment_method: str = "out_of_pocket"
    reimbursed: bool = False
    reimbursement_date: date | None = None
    reimbursement_amount: Decimal | None = None
    patient: str = "self"
    tax_year: int | None = None
    eligibility_confidence: str = "review"
    notes: str = ""
    extraction_raw: str = ""
    deleted: bool = False
    edit_history: str = "[]"
    # Extra Drive files for a receipt photographed in several parts. The first
    # page stays in drive_file_id; these are the rest, in page order.
    extra_file_ids: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.amount = money(self.amount)
        self.reimbursement_amount = money(self.reimbursement_amount)
        if isinstance(self.service_date, str):
            self.service_date = parse_date(self.service_date)
        if isinstance(self.reimbursement_date, str):
            self.reimbursement_date = parse_date(self.reimbursement_date)
        if self.service_date and not self.tax_year:
            self.tax_year = self.service_date.year

    # -- derived ------------------------------------------------------------

    @property
    def claimable(self) -> Decimal:
        """Dollars still claimable from the HSA for this receipt.

        hsa_card receipts are claimable=0 by definition: the HSA already paid.
        Partially-reimbursed out-of-pocket receipts keep the remainder.
        """
        if self.deleted or self.payment_method != "out_of_pocket" or self.reimbursed:
            return Decimal("0.00")
        if self.amount is None:
            return Decimal("0.00")
        return (self.amount - (self.reimbursement_amount or Decimal("0"))).quantize(
            Decimal("0.01")
        )

    def validate(self) -> list[str]:
        errors = []
        if not self.file_hash:
            errors.append("file_hash is required")
        if self.category not in CATEGORIES:
            errors.append(f"unknown category: {self.category}")
        if self.payment_method not in PAYMENT_METHODS:
            errors.append(f"unknown payment_method: {self.payment_method}")
        if self.eligibility_confidence not in CONFIDENCE_LEVELS:
            errors.append(f"unknown eligibility_confidence: {self.eligibility_confidence}")
        if self.amount is not None and self.amount < 0:
            errors.append("amount cannot be negative")
        if self.reimbursement_amount is not None and self.amount is not None:
            if self.reimbursement_amount > self.amount:
                errors.append("reimbursement_amount exceeds amount")
        if self.reimbursed and self.payment_method == "hsa_card" and self.reimbursement_date:
            errors.append("hsa_card receipts are not reimbursed via withdrawal")
        if self.service_date and self.tax_year and self.service_date.year != self.tax_year:
            errors.append("tax_year does not match service_date")
        return errors

    def history(self) -> list[dict]:
        try:
            return json.loads(self.edit_history or "[]")
        except json.JSONDecodeError:
            return []

    def record_edit(self, changes: dict, note: str = "") -> None:
        entries = self.history()
        entries.append(
            {
                "ts": datetime.now(timezone.utc).isoformat(),
                "changes": {k: str(v) for k, v in changes.items()},
                "note": note,
            }
        )
        self.edit_history = json.dumps(entries)

    # -- serialization ------------------------------------------------------

    def to_row(self) -> list[str]:
        return [
            self.receipt_id,
            self.file_hash,
            self.drive_file_id,
            self.drive_link,
            _date_str(self.service_date),
            self.upload_date.isoformat() if self.upload_date else "",
            self.provider,
            money_str(self.amount),
            self.category,
            self.description,
            self.payment_method,
            "TRUE" if self.reimbursed else "FALSE",
            _date_str(self.reimbursement_date),
            money_str(self.reimbursement_amount),
            self.patient,
            str(self.tax_year or ""),
            self.eligibility_confidence,
            self.notes,
            self.extraction_raw,
            "TRUE" if self.deleted else "FALSE",
            self.edit_history,
            ",".join(self.extra_file_ids),
        ]

    @classmethod
    def from_row(cls, row: dict) -> "Receipt":
        tax_year = row.get("tax_year")
        return cls(
            receipt_id=row.get("receipt_id", "") or str(uuid.uuid4()),
            file_hash=row.get("file_hash", ""),
            drive_file_id=row.get("drive_file_id", ""),
            drive_link=safe_url(row.get("drive_link")),
            service_date=parse_date(row.get("service_date")),
            upload_date=parse_datetime(row.get("upload_date")),
            provider=row.get("provider", ""),
            amount=money(row.get("amount")),
            category=row.get("category") or "Other",
            description=row.get("description", ""),
            payment_method=row.get("payment_method") or "out_of_pocket",
            reimbursed=parse_bool(row.get("reimbursed")),
            reimbursement_date=parse_date(row.get("reimbursement_date")),
            reimbursement_amount=money(row.get("reimbursement_amount")),
            patient=row.get("patient", ""),
            tax_year=int(tax_year) if str(tax_year or "").strip().isdigit() else None,
            eligibility_confidence=row.get("eligibility_confidence") or "review",
            notes=row.get("notes", ""),
            extraction_raw=row.get("extraction_raw", ""),
            deleted=parse_bool(row.get("deleted")),
            edit_history=row.get("edit_history") or "[]",
            extra_file_ids=[
                x.strip() for x in (row.get("extra_file_ids") or "").split(",") if x.strip()
            ],
        )

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Reimbursement:
    reimbursement_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    date: date | None = None
    amount: Decimal | None = None
    method: str = ""
    covered_receipt_ids: list[str] = field(default_factory=list)
    notes: str = ""

    def to_row(self) -> list[str]:
        return [
            self.reimbursement_id,
            _date_str(self.date),
            money_str(self.amount),
            self.method,
            ",".join(self.covered_receipt_ids),
            self.notes,
        ]

    @classmethod
    def from_row(cls, row: dict) -> "Reimbursement":
        ids = [x.strip() for x in (row.get("covered_receipt_ids") or "").split(",") if x.strip()]
        return cls(
            reimbursement_id=row.get("reimbursement_id", "") or str(uuid.uuid4()),
            date=parse_date(row.get("date")),
            amount=money(row.get("amount")),
            method=row.get("method", ""),
            covered_receipt_ids=ids,
            notes=row.get("notes", ""),
        )


@dataclass
class Contribution:
    date: date | None = None
    amount: Decimal | None = None
    source: str = "payroll"
    tax_year: int | None = None

    def __post_init__(self):
        self.amount = money(self.amount)
        if self.date and not self.tax_year:
            self.tax_year = self.date.year

    def to_row(self) -> list[str]:
        return [
            _date_str(self.date),
            money_str(self.amount),
            self.source,
            str(self.tax_year or ""),
        ]

    @classmethod
    def from_row(cls, row: dict) -> "Contribution":
        tax_year = row.get("tax_year")
        return cls(
            date=parse_date(row.get("date")),
            amount=money(row.get("amount")),
            source=row.get("source", "payroll"),
            tax_year=int(tax_year) if str(tax_year or "").strip().isdigit() else None,
        )
