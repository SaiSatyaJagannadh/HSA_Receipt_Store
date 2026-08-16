"""HSAVault — dashboard. Everything here is recomputed from the Sheet on load."""

from datetime import date
from decimal import Decimal

import pandas as pd
import streamlit as st

from core import auth, ledger, store
from core.models import PAYMENT_LABELS

st.set_page_config(page_title="HSAVault", page_icon="🧾", layout="wide")

auth.require_login()

FOOTER = (
    "HSAVault is a personal record-keeping tool. It is **not tax advice**. "
    "Categories follow IRS-published expense categories; confirm your own eligibility "
    "with a qualified tax professional. Your receipts live in your own Google Drive — "
    "this app is an index over them, not their only home."
)


def footer():
    st.divider()
    st.caption(FOOTER)


def require_setup() -> bool:
    missing = store.settings().ready()
    if missing:
        st.warning(
            "Not connected to Google yet. Missing: " + ", ".join(missing) + ".\n\n"
            "Open **Settings** in the sidebar, or fill in `.env` (see README)."
        )
        footer()
        return False
    return True


st.title("🧾 HSAVault")
store.show_flash()

if require_setup():
    with st.spinner("Reading your index from Google Sheets…"):
        receipts = store.receipts()

    store.show_offline()

    # --- orphan repair check, once per session ----------------------------
    if "_hsa_orphans_checked" not in st.session_state:
        st.session_state["_hsa_orphans_checked"] = True
        try:
            # A full Drive listing. Without the spinner the first load of the
            # dashboard just sits there looking hung.
            with st.spinner("Checking Drive for unindexed files…"):
                st.session_state["_hsa_orphans"] = store.find_orphans()
        except Exception:
            st.session_state["_hsa_orphans"] = []
    if st.session_state.get("_hsa_orphans"):
        with st.container(border=True):
            st.error(
                f"⚠️ {len(st.session_state['_hsa_orphans'])} file(s) in Drive have no row in the "
                "index. This happens if a Drive upload succeeded but the Sheets write "
                "failed. Repair them in **Bulk Import**."
            )

    settings = store.settings()
    balance = ledger.unreimbursed_balance(receipts)

    # --- headline ----------------------------------------------------------
    left, right = st.columns([2, 3])
    with left:
        st.metric("Unreimbursed claimable balance", f"${balance:,.2f}")
        st.caption(
            "Out-of-pocket receipts you have not yet paid yourself back for. "
            "HSA-card receipts are excluded — the HSA already paid those."
        )
    with right:
        active = ledger.active(receipts)
        card_total = sum(
            (r.amount or Decimal("0") for r in active if r.payment_method == "hsa_card"),
            Decimal("0"),
        )
        a, b, c = st.columns(3)
        a.metric("Receipts on file", len(active))
        b.metric("Documented HSA-card spend", f"${card_total:,.2f}")
        c.metric(
            "Total documented",
            f"${sum((r.amount or Decimal('0') for r in active), Decimal('0')):,.2f}",
        )

    st.divider()

    # --- by tax year -------------------------------------------------------
    st.subheader("By tax year")
    years = ledger.totals_by_year(receipts)
    if years:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Tax year": year or "—",
                        "Receipts": data["count"],
                        "Total": float(data["total"]),
                        "Reimbursed": float(data["reimbursed"]),
                        "Still claimable": float(data["claimable"]),
                    }
                    for year, data in years.items()
                ]
            ),
            hide_index=True,
            width="stretch",
            column_config={
                col: st.column_config.NumberColumn(format="$%.2f")
                for col in ("Total", "Reimbursed", "Still claimable")
            },
        )
    else:
        st.info("No receipts yet. Head to **Upload** to add your first one.")

    # --- charts ------------------------------------------------------------
    chart_left, chart_right = st.columns(2)
    with chart_left:
        st.subheader("Spend by category")
        by_cat = ledger.totals_by_category(receipts)
        if by_cat:
            st.bar_chart(
                pd.DataFrame(
                    {"Amount": [float(v) for v in by_cat.values()]},
                    index=list(by_cat.keys()),
                ),
                horizontal=True,
            )
        else:
            st.caption("Nothing to chart yet.")
    with chart_right:
        st.subheader("Spend over time")
        monthly = ledger.monthly_series(receipts)
        if monthly:
            st.line_chart(
                pd.DataFrame(
                    {"Amount": [float(v) for v in monthly.values()]},
                    index=list(monthly.keys()),
                )
            )
        else:
            st.caption("Nothing to chart yet.")

    st.divider()

    # --- contributions -----------------------------------------------------
    contrib_col, projection_col = st.columns(2)
    with contrib_col:
        year = settings.irs_limit_year
        st.subheader(f"{year} contributions")
        try:
            contributed = ledger.contributions_for_year(store.contributions(), year)
        except Exception:
            contributed = Decimal("0")
        limit = settings.irs_limit_decimal
        remaining = max(limit - contributed, Decimal("0"))
        st.metric("Contributed", f"${contributed:,.2f}", f"${remaining:,.2f} room left")
        if limit > 0:
            st.progress(min(float(contributed / limit), 1.0))
        st.caption(
            f"Against a {year} limit of ${limit:,.2f} — editable in Settings, "
            "since the IRS changes it annually."
        )

    with projection_col:
        st.subheader("If you leave it invested")
        rate = settings.projection_rate
        values = ledger.projection(balance, rate, [5, 10, 20])
        cols = st.columns(3)
        for col, (years_out, value) in zip(cols, values.items()):
            col.metric(f"{years_out} yr", f"${value:,.2f}")
        st.caption(
            f"Illustration only, at an assumed {rate:.1%} annual return. "
            "Not a promise, a projection, or a recommendation."
        )

    st.divider()

    # --- warnings ----------------------------------------------------------
    st.subheader("Needs attention")
    issues = ledger.warnings(receipts, date.today())
    if not issues:
        st.success("Nothing flagged. Every receipt has a date, an amount, and a decision.")
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Date": r["receipt"].service_date,
                        "Provider": r["receipt"].provider or "—",
                        "Amount": float(r["receipt"].amount or 0),
                        "Payment": PAYMENT_LABELS.get(r["receipt"].payment_method, ""),
                        "Problems": ", ".join(r["problems"]),
                    }
                    for r in issues
                ]
            ),
            hide_index=True,
            width="stretch",
            column_config={"Amount": st.column_config.NumberColumn(format="$%.2f")},
        )

    if st.button("↻ Refresh from Google Sheets"):
        store.clear()
        store.flash("Refreshed from Google Sheets.")
        st.rerun()

    footer()
