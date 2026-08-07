"""Contribution log, tracked against the (editable) IRS annual limit."""

from datetime import date
from decimal import Decimal

import pandas as pd
import streamlit as st

from core import auth, ledger, store
from core.models import Contribution, money

st.set_page_config(page_title="Contributions — HSAVault", page_icon="🏦", layout="wide")

auth.require_login()
st.title("🏦 Contributions")

if store.settings().ready():
    st.warning("Connect Google first — see **Settings**.")
    st.stop()

settings = store.settings()

try:
    contributions = store.contributions()
except Exception as exc:  # noqa: BLE001
    st.error(f"Could not read the contributions tab: {exc}")
    st.stop()

year = st.selectbox(
    "Tax year",
    sorted({c.tax_year for c in contributions if c.tax_year} | {settings.irs_limit_year}, reverse=True),
)

contributed = ledger.contributions_for_year(contributions, year)
limit = settings.irs_limit_decimal if year == settings.irs_limit_year else Decimal("0")

a, b, c = st.columns(3)
a.metric(f"{year} contributions", f"${contributed:,.2f}")
if limit > 0:
    a.progress(min(float(contributed / limit), 1.0))
    b.metric("Annual limit", f"${limit:,.2f}")
    c.metric("Room remaining", f"${max(limit - contributed, Decimal('0')):,.2f}")
else:
    b.info(f"No limit configured for {year}. Set it in **Settings** for the current year.")

st.divider()
st.subheader("Log a contribution")

with st.form("add_contribution"):
    c1, c2, c3 = st.columns(3)
    when = c1.date_input("Date", value=date.today(), format="YYYY-MM-DD")
    amount_text = c2.text_input("Amount")
    source = c3.selectbox("Source", ["payroll", "personal"])
    submitted = st.form_submit_button("Add contribution", type="primary")

if submitted:
    amount = money(amount_text)
    if amount is None or amount <= 0:
        st.error("Enter a positive amount.")
    else:
        store.save_contribution(Contribution(date=when, amount=amount, source=source))
        st.success(f"Logged ${amount:,.2f} for {when.year}.")
        st.rerun()

st.divider()
st.subheader("History")
if not contributions:
    st.caption("Nothing logged yet.")
else:
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Date": c.date,
                    "Amount": float(c.amount or 0),
                    "Source": c.source,
                    "Tax year": c.tax_year,
                }
                for c in sorted(contributions, key=lambda x: (x.date is None, x.date), reverse=True)
            ]
        ),
        hide_index=True,
        width="stretch",
        column_config={
            "Amount": st.column_config.NumberColumn(format="$%.2f"),
            "Tax year": st.column_config.NumberColumn(format="%d"),
        },
    )

st.caption(
    "The IRS contribution limit changes annually and varies by coverage type and age. "
    "Set your own figure in Settings. Not tax advice."
)
