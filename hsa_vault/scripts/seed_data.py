"""Populate the Sheet with realistic sample data so the UI isn't empty before you
upload anything real.

    python -m scripts.seed_data          # append sample rows
    python -m scripts.seed_data --purge  # remove them again

Seeded receipts have no Drive file: the browser and dashboard work, but audit
packet pages for them show metadata only. Every seeded row is tagged in `notes`.
"""

import argparse
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from core import config
from core.models import Contribution, Receipt, Reimbursement
from core.sheets import SheetsClient
from core.util import sha256_hex

TAG = "[SEED]"

SAMPLES = [
    ("CVS Pharmacy", "42.18", "Prescription", "Amoxicillin 500mg, 20 count", "hsa_card", "certain"),
    ("Dr. Elena Ruiz DDS", "310.00", "Dental", "Cleaning and two fillings", "out_of_pocket", "certain"),
    ("Bayview Optometry", "189.50", "Vision", "Annual eye exam and contact lens fitting", "out_of_pocket", "certain"),
    ("Northside Family Medicine", "125.00", "Physician", "Office visit, sinus infection", "out_of_pocket", "certain"),
    ("Quest Diagnostics", "84.30", "Lab/Diagnostic", "Comprehensive metabolic panel", "hsa_card", "certain"),
    ("Target", "23.97", "OTC Medication", "Allergy tablets and saline spray", "out_of_pocket", "likely"),
    ("Peak Physical Therapy", "220.00", "Physical Therapy", "Four sessions, shoulder rehab", "out_of_pocket", "certain"),
    ("Calm Waters Counseling", "150.00", "Mental Health", "Therapy session", "out_of_pocket", "certain"),
    ("Mercy General Hospital", "1240.75", "Hospital/Surgery", "Outpatient procedure, facility fee", "out_of_pocket", "certain"),
    ("Walgreens", "18.49", "Medical Devices/Supplies", "Blood pressure cuff", "out_of_pocket", "likely"),
    ("Alignment Chiropractic", "95.00", "Chiropractic", "Adjustment", "out_of_pocket", "review"),
    ("Unlabeled receipt", "", "Other", "Faded thermal paper, provider unreadable", "out_of_pocket", "review"),
]


def build_receipts() -> list[Receipt]:
    today = date.today()
    receipts = []
    for index, (provider, amount, category, description, method, confidence) in enumerate(SAMPLES):
        service_date = today - timedelta(days=30 * index + 5)
        receipts.append(
            Receipt(
                file_hash=sha256_hex(f"seed-{index}-{provider}".encode()),
                service_date=service_date,
                upload_date=datetime.now(timezone.utc),
                provider=provider,
                amount=Decimal(amount) if amount else None,
                category=category,
                description=description,
                payment_method=method,
                patient="self" if index % 3 else "spouse",
                eligibility_confidence=confidence,
                notes=f"{TAG} sample data — no Drive file attached",
            )
        )
    # One partially reimbursed receipt, so the balance math has something to chew on.
    partial = receipts[3]
    partial.reimbursement_amount = Decimal("50.00")
    partial.reimbursement_date = today - timedelta(days=20)
    # One fully reimbursed receipt.
    done = receipts[2]
    done.reimbursed = True
    done.reimbursement_amount = done.amount
    done.reimbursement_date = today - timedelta(days=15)
    return receipts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--purge", action="store_true", help="delete seeded rows instead")
    args = parser.parse_args()

    settings = config.load_settings()
    missing = settings.ready()
    if missing:
        print("Not configured — missing: " + ", ".join(missing))
        return 1

    client = SheetsClient(settings.sheet_id, config.build_credentials(settings.service_account_json))
    client.ensure_tabs()

    if args.purge:
        rows = client.read_tab("receipts")
        removed = 0
        for row in rows:
            if TAG in (row.get("notes") or ""):
                number = client.row_number_of("receipts", row["receipt_id"])
                if number:
                    client.update_row("receipts", number, [""] * len(row))
                    removed += 1
        print(f"Blanked {removed} seeded receipt row(s).")
        print("Note: rows are blanked, not deleted — tidy them in the Sheet if you like.")
        return 0

    receipts = build_receipts()
    for receipt in receipts:
        client.append_row("receipts", receipt.to_row())
    print(f"Added {len(receipts)} sample receipts.")

    today = date.today()
    reimbursements = [
        Reimbursement(
            reimbursement_id=str(uuid.uuid4()),
            date=today - timedelta(days=15),
            amount=Decimal("239.50"),
            method="HSA transfer to checking",
            covered_receipt_ids=[receipts[2].receipt_id, receipts[3].receipt_id],
            notes=f"{TAG} covers one full receipt and part of another",
        )
    ]
    for rb in reimbursements:
        client.append_row("reimbursements", rb.to_row())
    print(f"Added {len(reimbursements)} sample reimbursement.")

    contributions = [
        Contribution(date=date(today.year, month, 1), amount=Decimal("350.00"), source="payroll")
        for month in range(1, min(today.month, 12) + 1)
    ]
    contributions.append(
        Contribution(date=date(today.year, 3, 15), amount=Decimal("500.00"), source="personal")
    )
    for c in contributions:
        client.append_row("contributions", c.to_row())
    print(f"Added {len(contributions)} sample contributions.")
    print("\nRun `streamlit run app.py` to see it populated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
