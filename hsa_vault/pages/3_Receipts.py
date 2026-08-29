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
    describe_edit,
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
store.show_offline()
archived_count = sum(1 for r in all_receipts if r.deleted)
# Keyed, so the live count in the label cannot reset the widget: without a key
# Streamlit derives identity from the label, and archiving something would change
# the count, change the label, and silently flip the view back off.
show_archived = st.sidebar.checkbox(
    f"Show archived receipts ({archived_count})" if archived_count else "Show archived receipts",
    value=False,
    key="_hsa_show_archived",
)
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

# --- archive / restore -----------------------------------------------------
# Archiving is fully reversible — `deleted` is a flag and the file only moves to
# _archive/ — so making the user type ARCHIVE was friction out of all proportion
# to the risk, and it lived at the bottom of the page inside a collapsed
# expander. Worse, nothing exposed the reverse, so a mis-click was permanent from
# inside the app. One click to ask, one to confirm, and a way back.
_PENDING = "_hsa_pending_archive"
# A file_uploader cannot be emptied by writing to its state; re-keying it is the
# only way to hand back an empty drop zone after a successful attach.
_ATTACH_ROUND = "_hsa_attach_round"
confirming = st.session_state.get(_PENDING) == selected_id

status_col, action_col = st.columns([3, 1])
with status_col:
    if receipt.deleted:
        st.caption(
            "📦 **Archived** — the original sits in `HSA_Vault/_archive/` and is excluded "
            "from your balance. Nothing was deleted from Drive."
        )
    else:
        st.caption(f"Active — {PAYMENT_LABELS[receipt.payment_method]}.")
with action_col:
    if receipt.deleted:
        if st.button("♻️ Restore", type="primary", width="stretch"):
            with st.spinner("Moving the file back…"):
                store.restore_receipt(receipt)
            store.flash("Restored — it counts toward your balance again.")
            st.rerun()
    elif not confirming:
        if st.button("🗑️ Archive", width="stretch"):
            st.session_state[_PENDING] = selected_id
            st.rerun()

if confirming and not receipt.deleted:
    with st.container(border=True):
        st.warning(
            f"Archive **{receipt.provider or 'this receipt'}**? It stops counting toward "
            "your balance. Nothing is deleted — you can restore it from this page."
        )
        reason = st.text_input(
            "Reason (optional — recorded in the edit history)", key=f"reason_{selected_id}"
        )
        yes, no = st.columns(2)
        if yes.button("Archive it", type="primary", width="stretch"):
            with st.spinner("Archiving…"):
                store.archive_receipt(receipt, reason)
            st.session_state.pop(_PENDING, None)
            store.flash("Archived. Tick **Show archived receipts** in the sidebar to restore it.")
            st.rerun()
        if no.button("Cancel", width="stretch"):
            st.session_state.pop(_PENDING, None)
            st.rerun()

image_col, edit_col = st.columns([1, 2])

with image_col:
    # Every page, not just the first. A multi-page receipt used to show a single
    # "open in Drive" link pointing at page 1, which reads as the other pages
    # having failed to upload.
    page_ids = [i for i in [receipt.drive_file_id, *receipt.extra_file_ids] if i]
    if len(page_ids) > 1:
        st.caption(f"📎 {len(page_ids)} pages attached to this receipt.")
    if receipt.drive_link:
        st.link_button("Open original in Drive ↗", receipt.drive_link)

    if page_ids and st.checkbox("Load images inline", key=f"img_{selected_id}"):
        try:
            _, drive = store.clients()
            for n, file_id in enumerate(page_ids, start=1):
                with st.spinner(f"Downloading page {n} of {len(page_ids)}…"):
                    image_bytes = drive.download(file_id)
                st.image(
                    image_bytes,
                    caption=describe(selected_id)
                    if len(page_ids) == 1
                    else f"page {n} of {len(page_ids)}",
                    width="stretch",
                )
                # Removing the wrong photo has to be possible from where you are
                # looking at it. Only offered when another page would remain —
                # store.detach_page refuses to leave a receipt with no document.
                if len(page_ids) > 1 and st.button(
                    f"🗑️ Remove page {n}", key=f"rm_{selected_id}_{file_id}"
                ):
                    try:
                        store.detach_page(receipt, file_id)
                    except store.LastPage as exc:
                        st.warning(str(exc))
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Could not remove that page: {exc}")
                    else:
                        store.flash(
                            f"Page {n} removed and moved to _archive/ — it is not deleted."
                        )
                        st.rerun()
        except Exception as exc:  # noqa: BLE001
            st.caption(f"Could not render inline: {exc}")

    # Kept outside the edit form on purpose: a file_uploader inside st.form only
    # delivers its files on submit, which makes attaching feel like it did
    # nothing until you also save an unrelated field.
    with st.expander(f"📎 Attach more photos ({len(page_ids)} now)"):
        st.caption(
            "For a receipt you photographed in parts and filed before noticing. "
            "The pages join this same record — the receipt ID, and any withdrawal "
            "already pointing at it, are unchanged."
        )
        more = st.file_uploader(
            "Extra pages",
            type=["jpg", "jpeg", "png", "heic", "heif", "pdf"],
            accept_multiple_files=True,
            key=f"_hsa_attach_{selected_id}_{st.session_state.get(_ATTACH_ROUND, 0)}",
        )
        if more and st.button(f"Attach {len(more)} page(s)", type="primary", key=f"att_{selected_id}"):
            try:
                with st.spinner("Uploading to Drive…"):
                    store.attach_pages(receipt, [(f.getvalue(), f.name) for f in more])
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not attach: {exc}")
            else:
                store.flash(f"Attached {len(more)} page(s) to this receipt.")
                st.session_state[_ATTACH_ROUND] = st.session_state.get(_ATTACH_ROUND, 0) + 1
                st.rerun()

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

# Out of the editing area on purpose. This is a record of what happened, not
# something you act on while filling in the form above, and it used to sit
# directly under it as raw JSON blobs.
st.divider()
history = receipt.history()
with st.expander(f"📜 History and diagnostics ({len(history)} edit(s))"):
    history_tab, raw_tab = st.tabs(["What changed", "Raw extraction output"])
    with history_tab:
        if not history:
            st.caption("No edits recorded since this receipt was saved.")
        for entry in reversed(history):
            when, what = describe_edit(entry)
            st.markdown(f"**{when}** — {what}")
    with raw_tab:
        st.caption(
            "Exactly what the vision model returned, kept verbatim so a bad "
            "extraction can be diagnosed rather than guessed at."
        )
        st.code(receipt.extraction_raw or "(none)", language="json")

st.divider()
st.caption("Not tax advice. Archiving never deletes anything from Drive.")
