"""Backlog import: scan a Drive folder of loose receipts, extract in batch, then
work a review queue. Also repairs orphans (Drive files with no index row).

Files are renamed and moved into their year folder — never re-uploaded.
"""

from datetime import date, datetime, timezone

import streamlit as st

from core import auth, extraction, ledger, store
from core.models import (
    CATEGORIES,
    CONFIDENCE_LEVELS,
    PAYMENT_LABELS,
    PAYMENT_METHODS,
    Receipt,
    money,
    parse_date,
)
from core.util import sha256_hex

st.set_page_config(page_title="Bulk Import — HSAVault", page_icon="📥", layout="wide")

auth.require_login()
st.title("📥 Bulk import")

if store.settings().ready():
    st.warning("Connect Google first — see **Settings**.")
    st.stop()

settings = store.settings()
receipts = store.receipts()
indexed_ids = {r.drive_file_id for r in receipts if r.drive_file_id}

tab_scan, tab_orphans = st.tabs(["Scan a folder", "Repair orphans"])

# --- orphan repair ---------------------------------------------------------

with tab_orphans:
    st.caption(
        "Files sitting in your year folders that have no row in the Sheet. These "
        "appear when a Drive upload succeeded but the Sheets write failed."
    )
    if st.button("Scan for orphans"):
        with st.spinner("Listing Drive…"):
            st.session_state.orphans = store.find_orphans()
    orphans = st.session_state.get("orphans", [])
    if orphans:
        st.error(f"{len(orphans)} orphaned file(s).")
        for f in orphans:
            st.write(f"- `{f['name']}` in `{f.get('folder', '?')}`")
        st.info(
            "Queue them below by choosing **Orphaned files** as the source, then confirm "
            "each one as usual."
        )
    else:
        st.success("No orphans. Every Drive file has an index row.")

# --- folder scan -----------------------------------------------------------

with tab_scan:
    source = st.radio(
        "Source",
        ["A Drive folder ID", "Orphaned files"],
        horizontal=True,
    )
    folder_id = ""
    if source == "A Drive folder ID":
        folder_id = st.text_input(
            "Folder ID (from the folder's URL)",
            help="The loose folder holding your backlog. It does not need to be inside HSA_Vault.",
        ).strip()

    limit = st.number_input("Process at most", 1, 200, 25, help="Keeps API cost predictable.")

    if st.button("Scan and extract", type="primary", disabled=source == "A Drive folder ID" and not folder_id):
        _, drive = store.clients()
        with st.spinner("Listing files…"):
            if source == "Orphaned files":
                files = store.find_orphans()
            else:
                files = drive.list_folder(folder_id)
        files = [f for f in files if f["id"] not in indexed_ids][: int(limit)]

        queue = []
        progress = st.progress(0.0, text="Starting…")
        for index, meta in enumerate(files, start=1):
            progress.progress(index / max(len(files), 1), text=f"{meta['name']} ({index}/{len(files)})")
            try:
                data = drive.download(meta["id"])
            except Exception as exc:  # noqa: BLE001
                queue.append({"meta": meta, "error": f"download failed: {exc}"})
                continue
            file_hash = sha256_hex(data)
            duplicate = ledger.is_duplicate(receipts, file_hash)
            if duplicate:
                queue.append({"meta": meta, "duplicate": duplicate, "hash": file_hash})
                continue
            pages = extraction.normalize(data, meta["name"])
            result = extraction.extract(pages, settings.nvidia_api_key, settings.nvidia_model, settings.nvidia_base_url)
            queue.append({"meta": meta, "hash": file_hash, "result": result})
        progress.empty()
        st.session_state.import_queue = queue
        st.success(f"Queued {len(queue)} file(s) for review.")

# --- review queue ----------------------------------------------------------

queue = st.session_state.get("import_queue", [])
if queue:
    st.divider()
    pending = [
        item
        for item in queue
        if item["meta"]["id"] not in st.session_state.get("imported_ids", set())
    ]
    st.subheader(f"Review queue — {len(pending)} remaining")
    st.caption("Nothing is indexed until you confirm it. Skipping leaves the file untouched.")

    for item in pending:
        meta = item["meta"]
        with st.expander(f"📄 {meta['name']}", expanded=False):
            if item.get("error"):
                st.error(item["error"])
                continue
            if item.get("duplicate"):
                dup = item["duplicate"]
                st.error(
                    "Duplicate of an existing record: "
                    f"{dup.provider or '—'} / {dup.service_date or 'undated'} / "
                    f"${dup.amount if dup.amount is not None else '—'}"
                )
                continue

            result: extraction.Extraction = item["result"]
            data = result.data
            if result.error:
                st.warning(f"Extraction unavailable ({result.error}) — enter by hand.")
            if result.ambiguities:
                st.caption("Unclear: " + "; ".join(result.ambiguities))

            with st.form(f"import_{meta['id']}"):
                c1, c2 = st.columns(2)
                with c1:
                    provider = st.text_input("Provider", value=data.get("provider") or "")
                    service_date = st.date_input(
                        "Service date",
                        value=parse_date(data.get("service_date")) or date.today(),
                        format="YYYY-MM-DD",
                    )
                    amount = st.text_input("Amount", value=data.get("total_amount") or "")
                with c2:
                    category_value = (
                        data.get("category") if data.get("category") in CATEGORIES else "Other"
                    )
                    category = st.selectbox(
                        "Category", CATEGORIES, index=CATEGORIES.index(category_value)
                    )
                    payment_method = st.radio(
                        "Payment method",
                        PAYMENT_METHODS,
                        index=PAYMENT_METHODS.index(settings.default_payment_method)
                        if settings.default_payment_method in PAYMENT_METHODS
                        else 0,
                        format_func=lambda m: PAYMENT_LABELS[m],
                        horizontal=True,
                    )
                    patient = st.text_input(
                        "Patient",
                        value=data.get("patient_name_if_visible") or settings.default_patient,
                    )
                description = st.text_input("Description", value=data.get("description") or "")
                confidence_value = data.get("eligibility_confidence")
                confidence = st.selectbox(
                    "Eligibility confidence",
                    CONFIDENCE_LEVELS,
                    index=CONFIDENCE_LEVELS.index(confidence_value)
                    if confidence_value in CONFIDENCE_LEVELS
                    else 2,
                )
                save, skip = st.columns(2)
                confirmed = save.form_submit_button("💾 Index this receipt", type="primary")
                skipped = skip.form_submit_button("Skip")

            if skipped:
                st.session_state.setdefault("imported_ids", set()).add(meta["id"])
                st.rerun()

            if confirmed:
                receipt = Receipt(
                    file_hash=item["hash"],
                    service_date=service_date,
                    upload_date=datetime.now(timezone.utc),
                    provider=provider.strip(),
                    amount=money(amount),
                    category=category,
                    description=description.strip(),
                    payment_method=payment_method,
                    patient=patient.strip(),
                    eligibility_confidence=confidence,
                    notes="Imported from existing Drive file",
                    extraction_raw=result.raw,
                )
                errors = receipt.validate()
                if errors:
                    st.error("; ".join(errors))
                else:
                    try:
                        store.adopt_receipt(receipt, meta["id"], meta["name"])
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"Failed: {exc}")
                    else:
                        st.session_state.setdefault("imported_ids", set()).add(meta["id"])
                        st.success("Indexed.")
                        st.rerun()

    if st.button("Clear the queue"):
        st.session_state.pop("import_queue", None)
        st.session_state.pop("imported_ids", None)
        st.rerun()

st.caption("Not tax advice.")
