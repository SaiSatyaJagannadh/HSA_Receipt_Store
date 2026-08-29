"""Mark out-of-pocket receipts as reimbursed and record the withdrawal.

hsa_card receipts never appear in the selection list — they cannot be reimbursed
without double-claiming.
"""

from datetime import date
from decimal import Decimal

import pandas as pd
import streamlit as st

from core import auth, ledger, store
from core.models import Reimbursement, money

st.set_page_config(page_title="Reimbursements — HSAVault", page_icon="💸", layout="wide")

auth.require_login()
st.title("💸 Reimbursements")
store.show_flash()

if store.settings().ready():
    st.warning("Connect Google first — see **Settings**.")
    st.stop()

receipts = store.receipts()
store.show_offline()
claimable = ledger.selectable_for_reimbursement(receipts)
balance = ledger.unreimbursed_balance(receipts)

# Namespaced so a cache key can never collide with a widget key — see CLAUDE.md.
_SELECTION = "_hsa_reimbursement_selection"

st.metric("Unreimbursed claimable balance", f"${balance:,.2f}")

if not claimable:
    st.success("Nothing left to reimburse. Every out-of-pocket receipt is settled.")
else:
    st.caption(
        f"{len(claimable)} out-of-pocket receipt(s) with a remaining claim. "
        "HSA-card receipts are excluded by design."
    )

    table = pd.DataFrame(
        [
            {
                "Select": False,
                "Date": r.service_date,
                "Provider": r.provider or "—",
                "Category": r.category,
                "Amount": float(r.amount or 0),
                "Already reimbursed": float(r.reimbursement_amount or 0),
                "Still claimable": float(r.claimable),
                "id": r.receipt_id,
            }
            for r in sorted(claimable, key=lambda r: (r.service_date or date.max))
        ]
    )

    edited = st.data_editor(
        table,
        hide_index=True,
        width="stretch",
        disabled=[c for c in table.columns if c != "Select"],
        column_config={
            "Amount": st.column_config.NumberColumn(format="$%.2f"),
            "Already reimbursed": st.column_config.NumberColumn(format="$%.2f"),
            "Still claimable": st.column_config.NumberColumn(format="$%.2f"),
            "id": None,
        },
        key=_SELECTION,
    )

    selected_ids = list(edited[edited["Select"]]["id"])
    by_id = {r.receipt_id: r for r in claimable}
    selected = [by_id[i] for i in selected_ids]
    selected_total = sum((r.claimable for r in selected), Decimal("0"))

    st.divider()
    if not selected:
        st.info("Tick the receipts this withdrawal covers.")
    else:
        st.subheader(f"{len(selected)} receipt(s) selected — ${selected_total:,.2f} claimable")

        with st.form("mark_reimbursed"):
            c1, c2, c3 = st.columns(3)
            withdrawal_date = c1.date_input("Withdrawal date", value=date.today(), format="YYYY-MM-DD")
            amount_text = c2.text_input("Withdrawal amount", value=f"{selected_total:.2f}")
            method = c3.text_input("Method", value="HSA transfer to checking")
            notes = st.text_area("Notes", height=70)
            st.caption(
                "A withdrawal smaller than the selected total is applied oldest-first; "
                "the last receipt it touches stays partially claimable for the remainder."
            )
            confirmed = st.checkbox("I have actually made this withdrawal")
            submitted = st.form_submit_button("Mark reimbursed", type="primary")

        if submitted:
            withdrawal = money(amount_text)
            if not confirmed:
                st.error("Confirm the withdrawal actually happened first.")
            elif withdrawal is None or withdrawal <= 0:
                st.error("Enter a positive withdrawal amount.")
            elif withdrawal > selected_total:
                st.error(
                    f"Withdrawal (${withdrawal:,.2f}) exceeds the selected claimable total "
                    f"(${selected_total:,.2f}). Select more receipts or lower the amount."
                )
            else:
                allocations = ledger.allocate_reimbursement(selected, withdrawal)
                record = Reimbursement(
                    date=withdrawal_date,
                    amount=withdrawal,
                    method=method.strip(),
                    covered_receipt_ids=[r.receipt_id for r, _, _ in allocations],
                    notes=notes.strip(),
                )
                try:
                    # store.record_reimbursement owns the write order — the
                    # withdrawal row lands before any receipt is marked, so a
                    # partial failure can only ever leave the balance too high.
                    store.record_reimbursement(record, allocations, withdrawal_date)
                except store.PartialReimbursement as exc:
                    st.error(
                        f"**Partly applied.** The ${withdrawal:,.2f} withdrawal is recorded, "
                        f"but only {exc.applied} of {exc.total} receipt(s) were marked "
                        f"before the write failed: {exc.cause}\n\n"
                        "Your claimable balance is therefore too **high**, not too low — "
                        "nothing has been double-claimed. Finish the remaining receipts in "
                        "the Sheet's `receipts` tab (`reimbursed`, `reimbursement_amount`, "
                        "`reimbursement_date`), or delete the withdrawal row from the "
                        "`reimbursements` tab and record it again once Google is reachable."
                    )
                except Exception as exc:  # noqa: BLE001
                    st.error(f"Nothing was changed — the withdrawal could not be recorded: {exc}")
                else:
                    partial = [r for r, _, full in allocations if not full]
                    store.flash(
                        f"Recorded ${withdrawal:,.2f} across {len(allocations)} receipt(s)."
                        + (f" {len(partial)} left partially claimable." if partial else "")
                    )
                    # Settled receipts drop out of the list, and data_editor keys
                    # its edits by row position — leaving the old ticks behind
                    # would re-select whatever receipt slid up into those rows.
                    st.session_state.pop(_SELECTION, None)
                    st.rerun()

# --- history ---------------------------------------------------------------

st.divider()
st.subheader("Withdrawal history")
try:
    history = store.reimbursements()
except Exception as exc:  # noqa: BLE001
    st.error(f"Could not read the reimbursements tab: {exc}")
    history = []

if not history:
    st.caption("No withdrawals recorded yet.")
else:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Date": rb.date,
                    "Amount": float(rb.amount or 0),
                    "Method": rb.method,
                    "Receipts covered": len(rb.covered_receipt_ids),
                    "Notes": rb.notes,
                }
                for rb in sorted(history, key=lambda x: (x.date is None, x.date), reverse=True)
            ]
        ),
        hide_index=True,
        width="stretch",
        column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")},
    )

st.caption("Not tax advice.")
