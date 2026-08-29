"""Type a receipt in by hand. Same fields, same row, no vision model, no file.

The Upload page needs a photo and an extraction call before it can show you a
form. Neither is always available: a receipt that arrived as an email total, one
whose paper is long gone, an expense you are back-filling from a statement, or
simply a day when the model is down. This page writes exactly the same
`receipts` row from typed values, so everything downstream — the balance, the
Receipts editor, withdrawals, the audit packet — treats it as an ordinary
receipt.

The one real difference is that there is no document behind it. That is a
weakness in an audit, not a bug, so the Export page's packet check already
reports it as "no image" and the Receipts page can attach the photo later
without changing the receipt_id.
"""

from datetime import date, datetime, timezone

import streamlit as st

from core import auth, ledger, store
from core.models import (
    CATEGORIES,
    CONFIDENCE_LEVELS,
    NARROW_ELIGIBILITY,
    PAYMENT_LABELS,
    PAYMENT_METHODS,
    Receipt,
    money,
    money_str,
)
from core.util import sha256_hex

st.set_page_config(page_title="Manual entry — HSAVault", page_icon="✍️", layout="wide")

auth.require_login()
st.title("✍️ Manual entry")
store.show_flash()

if store.settings().ready():
    st.warning("Connect Google first — see **Settings**.")
    st.stop()

settings = store.settings()
existing = store.receipts()
store.show_offline()

st.caption(
    "No upload, no extraction — you type the values and they are saved verbatim. "
    "Edit them later on **Receipts** like any other receipt, and attach the photo "
    "there if one turns up."
)


def content_hash(service_date, provider: str, amount, patient: str) -> str:
    """Stand-in for the file hash a typed receipt does not have.

    `file_hash` is required by Receipt.validate() and is what
    `ledger.is_duplicate` matches on, so leaving it blank would both fail
    validation and quietly opt every hand-entered receipt out of duplicate
    detection — which is where the duplicates actually come from, since typing
    the same expense in twice leaves no identical file to notice.

    Derived from the fields that identify the expense, so the same expense typed
    twice collides exactly the way re-uploading the same photo does. Like a file
    hash, it is not recomputed when the receipt is later edited: it records what
    was filed, it is not a live index.
    """
    parts = ["manual", str(service_date or ""), provider.strip().lower(),
             money_str(amount), patient.strip().lower()]
    return sha256_hex("|".join(parts).encode())


with st.form("manual_receipt", clear_on_submit=True):
    col1, col2 = st.columns(2)
    with col1:
        provider = st.text_input("Provider")
        service_date = st.date_input(
            "Service date (not the date you are typing this)",
            value=date.today(),
            format="YYYY-MM-DD",
        )
        amount = st.text_input("Amount", placeholder="42.18")
        category = st.selectbox("Category", CATEGORIES, index=CATEGORIES.index("Other"))
    with col2:
        default_pm = settings.default_payment_method
        payment_method = st.radio(
            "**How did you pay?** (this is the field that matters most)",
            PAYMENT_METHODS,
            index=PAYMENT_METHODS.index(default_pm) if default_pm in PAYMENT_METHODS else 0,
            format_func=lambda m: PAYMENT_LABELS[m],
        )
        patient = st.text_input("Patient", value=settings.default_patient)
        # Same conservative default as an extraction that came back with nothing:
        # a typed receipt says the person was sure of the *numbers*, which is not
        # the same as being sure the expense is HSA-eligible.
        confidence = st.selectbox("Eligibility confidence", CONFIDENCE_LEVELS, index=2)

    description = st.text_area("What is this for, in plain English?", height=70)
    notes = st.text_area("Notes", height=70)
    submitted = st.form_submit_button("💾 Save receipt", type="primary")

# The warnings sit outside the form because a form only re-runs on submit, so a
# selection made inside it cannot change anything above the button until then.
if category in NARROW_ELIGIBILITY:
    st.warning(
        "Eligibility for insurance premiums is narrow — only specific premium types "
        "qualify. Check IRS Publication 969 for your situation."
    )
if payment_method == "hsa_card":
    st.info(
        "HSA card: already paid from the HSA. This receipt is audit documentation "
        "and will **not** count toward your claimable balance."
    )
else:
    st.success(
        "Out of pocket: this **adds to your claimable balance** and can be reimbursed "
        "to yourself later, even years from now."
    )

if submitted:
    file_hash = content_hash(service_date, provider, money(amount), patient)
    duplicate = ledger.is_duplicate(existing, file_hash)
    receipt = Receipt(
        file_hash=file_hash,
        service_date=service_date,
        upload_date=datetime.now(timezone.utc),
        provider=provider.strip(),
        amount=money(amount),
        category=category,
        description=description.strip(),
        payment_method=payment_method,
        patient=patient.strip(),
        eligibility_confidence=confidence,
        notes=notes.strip(),
    )
    receipt.record_edit(
        {"provider": receipt.provider, "amount": money_str(receipt.amount)},
        note="entered by hand — no document attached",
    )
    errors = receipt.validate()

    if duplicate:
        st.error(
            "**Duplicate blocked.** The same provider, date, amount and patient are "
            "already in your index — filing it twice would inflate your claimable "
            f"balance. Existing record: **{duplicate.provider or '—'}**, "
            f"{duplicate.service_date or 'undated'}, "
            f"${duplicate.amount if duplicate.amount is not None else '—'} "
            f"({PAYMENT_LABELS.get(duplicate.payment_method, '')}). Change a field, or "
            "edit that receipt on **Receipts**."
        )
    elif errors:
        st.error("Fix these first: " + "; ".join(errors))
    else:
        try:
            with st.spinner("Appending to the index…"):
                store.save_receipt(receipt)
        except Exception as exc:  # noqa: BLE001
            st.error(f"Save failed: {exc}")
        else:
            # Everything worth saying goes in the flash. An st.warning() here
            # would be drawn and then thrown away by the st.rerun() below —
            # the same disappearing-confirmation bug store.flash() exists for,
            # and the no-amount case is precisely when it matters.
            store.flash(
                f"Saved — {receipt.provider or 'receipt'} filed under {receipt.tax_year}. "
                + (
                    "Saved without an amount — it counts for nothing until you add one, "
                    "and it is listed in the dashboard warnings panel. "
                    if receipt.amount is None
                    else ""
                )
                + "No document is attached, so it will show as \"no image\" in the audit "
                "packet check until you add one on **Receipts**."
            )
            st.rerun()

st.divider()
st.caption(
    "Not tax advice. A hand-entered receipt carries no original document — attach the "
    "photo on **Receipts** if you find it."
)
