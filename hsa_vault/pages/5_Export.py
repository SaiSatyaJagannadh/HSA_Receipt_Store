"""Audit packet export — the artifact that outlives this app."""

from datetime import date
from decimal import Decimal

import streamlit as st

from core import ledger, pdf_export, store

st.set_page_config(page_title="Export — HSAVault", page_icon="📄", layout="wide")
st.title("📄 Audit packet export")

if store.settings().ready():
    st.warning("Connect Google first — see **Settings**.")
    st.stop()

receipts = store.receipts()
years = sorted({r.tax_year for r in ledger.active(receipts) if r.tax_year}, reverse=True)

if not years:
    st.info("No receipts with a tax year yet.")
    st.stop()

col1, col2 = st.columns(2)
tax_year = col1.selectbox("Tax year", years)
owner = col2.text_input("Your name (printed on every page)", value=store.settings().default_patient)

year_receipts = [r for r in ledger.active(receipts) if r.tax_year == tax_year]
total = sum((r.amount or Decimal("0") for r in year_receipts), Decimal("0"))
reimbursed = sum((r.reimbursement_amount or Decimal("0") for r in year_receipts), Decimal("0"))

a, b, c = st.columns(3)
a.metric("Receipts", len(year_receipts))
b.metric("Total expenses", f"${total:,.2f}")
c.metric("Reimbursed", f"${reimbursed:,.2f}")

st.caption(
    "The PDF contains a cover page, a summary table, category subtotals, and one page "
    "per receipt with the full image and its metadata printed beneath it. It is designed "
    "to be legible printed in black and white."
)

st.divider()

missing_images = [r for r in year_receipts if not r.drive_file_id]
if missing_images:
    st.warning(f"{len(missing_images)} receipt(s) have no Drive file — those pages show metadata only.")

if st.button("📄 Generate audit packet", type="primary"):
    _, drive = store.clients()
    with st.spinner(f"Downloading {len(year_receipts)} receipt image(s) and building the PDF…"):
        pdf_bytes = pdf_export.build_audit_packet(receipts, tax_year, owner, drive.download)
    st.session_state[f"packet_{tax_year}"] = pdf_bytes
    st.success(f"Built a {len(pdf_bytes) / 1_000_000:.1f} MB packet.")

packet = st.session_state.get(f"packet_{tax_year}")
if packet:
    filename = f"HSA_Audit_Packet_{tax_year}.pdf"
    dl, save = st.columns(2)
    dl.download_button(
        "⬇️ Download PDF", packet, file_name=filename, mime="application/pdf",
        width="stretch",
    )
    if save.button("☁️ Save to Drive `_exports/`", width="stretch"):
        _, drive = store.clients()
        with st.spinner("Uploading…"):
            uploaded = drive.upload(
                packet, filename, drive.exports_folder(), mime="application/pdf"
            )
        st.success("Saved to Drive.")
        if uploaded.get("webViewLink"):
            st.link_button("Open in Drive ↗", uploaded["webViewLink"])

st.divider()
st.subheader("Other formats")

csv_col, zip_col = st.columns(2)
with csv_col:
    st.download_button(
        "⬇️ CSV index for this year",
        pdf_export.build_csv(receipts, tax_year),
        file_name=f"HSA_{tax_year}.csv",
        mime="text/csv",
        width="stretch",
    )
with zip_col:
    if st.button("📦 Build ZIP of raw images", width="stretch"):
        _, drive = store.clients()
        with st.spinner("Downloading originals…"):
            st.session_state[f"zip_{tax_year}"] = pdf_export.build_zip(
                receipts, tax_year, drive.download
            )
    archive = st.session_state.get(f"zip_{tax_year}")
    if archive:
        st.download_button(
            "⬇️ Download ZIP",
            archive,
            file_name=f"HSA_{tax_year}_receipts.zip",
            mime="application/zip",
            width="stretch",
        )

st.caption(f"Generated {date.today().isoformat()}. Not tax advice.")
