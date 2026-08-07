"""Filterable receipt browser + detail view with edit history and soft delete."""

from decimal import Decimal

import pandas as pd
import streamlit as st

from core import auth, ledger, store
from core.models import (
    CATEGORIES,
    CONFIDENCE_LEVELS,
    NARROW_ELIGIBILITY,
    PAYMENT_LABELS,
    PAYMENT_METHODS,
    money,
)

st.set_page_config(page_title="Receipts — HSAVault", page_icon="📂", layout="wide")

auth.require_login()
st.title("📂 Receipts")
store.show_flash()

if store.settings().ready():
    st.warning("Connect Google first — see **Settings**.")
    st.stop()

all_receipts = store.receipts()
show_archived = st.sidebar.checkbox("Show archived receipts", value=False)
pool = all_receipts if show_archived else ledger.active(all_receipts)

if not pool:
    st.info("No receipts yet.")
    st.stop()

# --- filters ---------------------------------------------------------------

years = sorted({r.tax_year for r in pool if r.tax_year}, reverse=True)
providers = sorted({r.provider for r in pool if r.provider})
patients = sorted({r.patient for r in pool if r.patient})
amount_low, amount_high = ledger.amount_bounds(pool)

with st.sidebar:
    st.header("Filters")
    f_years = st.multiselect("Tax year", years)
    f_categories = st.multiselect("Category", CATEGORIES)
    f_providers = st.multiselect("Provider", providers)
    f_patients = st.multiselect("Patient", patients)
    f_payment = st.multiselect(
        "Payment method", PAYMENT_METHODS, format_func=lambda m: PAYMENT_LABELS[m]
    )
    f_reimbursed = st.selectbox("Reimbursed", ["Any", "Yes", "No", "Partial"])
    lo, hi = st.slider(
        "Amount range",
        min_value=amount_low,
        max_value=amount_high,
        value=(amount_low, amount_high),
    )
    query = st.text_input("Search provider + description").strip().lower()


def matches(r) -> bool:
    if f_years and r.tax_year not in f_years:
        return False
    if f_categories and r.category not in f_categories:
        return False
    if f_providers and r.provider not in f_providers:
        return False
    if f_patients and r.patient not in f_patients:
        return False
    if f_payment and r.payment_method not in f_payment:
        return False
    if f_reimbursed == "Yes" and not r.reimbursed:
        return False
    if f_reimbursed == "No" and (r.reimbursed or r.reimbursement_amount):
        return False
    if f_reimbursed == "Partial" and not (not r.reimbursed and r.reimbursement_amount):
        return False
    if r.amount is not None and not (lo <= float(r.amount) <= hi):
        return False
    if query and query not in f"{r.provider} {r.description}".lower():
        return False
    return True


filtered = [r for r in pool if matches(r)]

total = sum((r.amount or Decimal("0") for r in filtered), Decimal("0"))
claimable = sum((r.claimable for r in filtered), Decimal("0"))
a, b, c = st.columns(3)
a.metric("Matching receipts", len(filtered))
b.metric("Total", f"${total:,.2f}")
c.metric("Still claimable", f"${claimable:,.2f}")

if not filtered:
    st.info("Nothing matches those filters.")
    st.stop()

table = pd.DataFrame(
    [
        {
            "Date": r.service_date,
            "Provider": r.provider or "—",
            "Category": r.category,
            "Amount": float(r.amount) if r.amount is not None else None,
            "Payment": "💳 HSA card" if r.payment_method == "hsa_card" else "💵 Out of pocket",
            "Reimbursed": "Yes" if r.reimbursed else ("Partial" if r.reimbursement_amount else "No"),
            "Patient": r.patient,
            "Archived": "Yes" if r.deleted else "",
            "Drive": r.drive_link,
            "id": r.receipt_id,
        }
        for r in sorted(filtered, key=lambda r: (r.service_date is None, r.service_date), reverse=True)
    ]
)

st.dataframe(
    table,
    hide_index=True,
    width="stretch",
    column_config={
        "Amount": st.column_config.NumberColumn(format="$%.2f"),
        "Drive": st.column_config.LinkColumn("Drive", display_text="open"),
        "id": None,
    },
)

# --- detail view -----------------------------------------------------------

st.divider()
st.subheader("Receipt detail")

by_id = {r.receipt_id: r for r in filtered}


def describe(rid: str) -> str:
    r = by_id[rid]
    amount = f"${r.amount:.2f}" if r.amount is not None else "no amount"
    return f"{r.service_date or 'undated'} — {r.provider or 'unknown'} — {amount}"


selected_id = st.selectbox("Pick a receipt", list(by_id), format_func=describe)
receipt = by_id[selected_id]

image_col, edit_col = st.columns([1, 2])

with image_col:
    if receipt.drive_link:
        st.link_button("Open original in Drive ↗", receipt.drive_link)
    if receipt.drive_file_id and st.checkbox("Load image inline", key=f"img_{selected_id}"):
        try:
            _, drive = store.clients()
            st.image(drive.download(receipt.drive_file_id), width="stretch")
        except Exception as exc:  # noqa: BLE001
            st.caption(f"Could not render inline: {exc}")
    st.caption(f"SHA-256 `{receipt.file_hash[:24]}…`")
    st.caption(f"Receipt ID `{receipt.receipt_id}`")

with edit_col:
    with st.form(f"edit_{selected_id}"):
        c1, c2 = st.columns(2)
        with c1:
            provider = st.text_input("Provider", value=receipt.provider)
            service_date = st.date_input(
                "Service date", value=receipt.service_date, format="YYYY-MM-DD"
            )
            amount = st.text_input(
                "Amount", value=f"{receipt.amount:.2f}" if receipt.amount is not None else ""
            )
            category = st.selectbox(
                "Category", CATEGORIES, index=CATEGORIES.index(receipt.category)
            )
        with c2:
            payment_method = st.radio(
                "Payment method",
                PAYMENT_METHODS,
                index=PAYMENT_METHODS.index(receipt.payment_method),
                format_func=lambda m: PAYMENT_LABELS[m],
            )
            patient = st.text_input("Patient", value=receipt.patient)
            confidence = st.selectbox(
                "Eligibility confidence",
                CONFIDENCE_LEVELS,
                index=CONFIDENCE_LEVELS.index(receipt.eligibility_confidence),
            )
        description = st.text_area("Description", value=receipt.description, height=70)
        notes = st.text_area("Notes", value=receipt.notes, height=70)
        if category in NARROW_ELIGIBILITY:
            st.warning("Insurance premium eligibility is narrow — verify before claiming.")
        submitted = st.form_submit_button("Save changes", type="primary")

    if submitted:
        changes = {}
        for field, new in {
            "provider": provider.strip(),
            "service_date": service_date,
            "amount": money(amount),
            "category": category,
            "payment_method": payment_method,
            "patient": patient.strip(),
            "eligibility_confidence": confidence,
            "description": description.strip(),
            "notes": notes.strip(),
        }.items():
            if getattr(receipt, field) != new:
                changes[field] = new
                setattr(receipt, field, new)
        if receipt.service_date:
            receipt.tax_year = receipt.service_date.year
        errors = receipt.validate()
        if errors:
            st.error("; ".join(errors))
        elif not changes:
            st.info(
                "No edits to save — every field still matches what is stored. "
                "(If you just saved, that worked; this is the already-saved state.)"
            )
        else:
            receipt.record_edit(changes, note="manual edit")
            store.save_receipt(receipt)
            store.flash(f"Updated {len(changes)} field(s): {', '.join(sorted(changes))}.")
            st.rerun()

history = receipt.history()
with st.expander(f"Edit history ({len(history)} entries)"):
    if not history:
        st.caption("No edits recorded since this receipt was saved.")
    for entry in reversed(history):
        st.write(f"**{entry['ts']}** — {entry.get('note', '')}")
        st.json(entry.get("changes", {}), expanded=False)

with st.expander("Raw extraction output"):
    st.code(receipt.extraction_raw or "(none)", language="json")

# --- soft delete -----------------------------------------------------------

st.divider()
if not receipt.deleted:
    with st.expander("🗑️ Archive this receipt"):
        st.warning(
            "Archiving flags the row as deleted and moves the file to "
            "`HSA_Vault/_archive/`. Nothing is ever hard-deleted from Drive."
        )
        reason = st.text_input("Reason (recorded in edit history)", key=f"reason_{selected_id}")
        confirm = st.text_input(
            "Type ARCHIVE to confirm", key=f"confirm_{selected_id}"
        )
        if st.button("Archive receipt", disabled=confirm != "ARCHIVE"):
            store.archive_receipt(receipt, reason)
            store.flash("Archived.")
            st.rerun()
else:
    st.info("This receipt is archived. Its file is in `HSA_Vault/_archive/`.")

st.caption("Not tax advice.")
