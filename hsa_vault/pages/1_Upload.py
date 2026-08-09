"""Upload → extract → confirm → save. Nothing is written until you click Save."""

from datetime import date, datetime, timezone

import streamlit as st

from core import auth, extraction, ledger, store
from core.models import (
    CATEGORIES,
    CONFIDENCE_LEVELS,
    NARROW_ELIGIBILITY,
    PAYMENT_LABELS,
    PAYMENT_METHODS,
    Receipt,
    money,
    parse_date,
)
from core.util import sha256_hex

st.set_page_config(page_title="Upload — HSAVault", page_icon="⬆️", layout="wide")

auth.require_login()
st.title("⬆️ Upload receipts")
store.show_flash()

if store.settings().ready():
    st.warning("Connect Google first — see **Settings**.")
    st.stop()

settings = store.settings()
existing = store.receipts()

# Streamlit cannot drop a single file from a file_uploader's value, and the widget
# cannot be emptied by writing to its session state. Re-keying it is the only way
# to hand back an empty drop zone, so the key carries a round number that a
# finished batch bumps.
_ROUND = "_hsa_upload_round"
_SAVED = "_hsa_saved_hashes"

uploads = st.file_uploader(
    "Receipt images or PDFs",
    type=["jpg", "jpeg", "png", "heic", "heif", "pdf"],
    accept_multiple_files=True,
    key=f"_hsa_uploads_{st.session_state.setdefault(_ROUND, 0)}",
)

if not uploads:
    st.info(
        "Drop in one or more receipts. Each is hashed for duplicate detection, sent to "
        "the vision model for extraction, and shown to you for confirmation before "
        "anything is saved."
    )
    st.stop()


def confirmation_form(key: str, upload, data: dict, uncertain: set[str], raw: str):
    """Pre-filled, editable, and explicitly opt-in. Uncertain fields are marked."""

    def label(text: str, field: str) -> str:
        return f"{text} ⚠️" if field in uncertain else text

    with st.form(key=f"form_{key}"):
        col1, col2 = st.columns(2)
        with col1:
            provider = st.text_input(label("Provider", "provider"), value=data.get("provider") or "")
            service_date = st.date_input(
                label("Service date (not upload date)", "service_date"),
                value=parse_date(data.get("service_date")) or date.today(),
                format="YYYY-MM-DD",
            )
            amount = st.text_input(
                label("Amount", "total_amount"), value=data.get("total_amount") or ""
            )
            category_value = data.get("category") if data.get("category") in CATEGORIES else "Other"
            category = st.selectbox(
                label("Category", "category"),
                CATEGORIES,
                index=CATEGORIES.index(category_value),
            )
            if category in NARROW_ELIGIBILITY:
                st.warning(
                    "Eligibility for insurance premiums is narrow — only specific premium "
                    "types qualify. Check IRS Publication 969 for your situation."
                )
        with col2:
            default_pm = settings.default_payment_method
            payment_method = st.radio(
                "**How did you pay?** (this is the field that matters most)",
                PAYMENT_METHODS,
                index=PAYMENT_METHODS.index(default_pm) if default_pm in PAYMENT_METHODS else 0,
                format_func=lambda m: PAYMENT_LABELS[m],
            )
            if payment_method == "hsa_card":
                st.info(
                    "HSA card: already paid from the HSA. This receipt is audit "
                    "documentation and will **not** count toward your claimable balance."
                )
            else:
                st.success(
                    "Out of pocket: this **adds to your claimable balance** and can be "
                    "reimbursed to yourself later, even years from now."
                )
            patient = st.text_input(
                "Patient",
                value=data.get("patient_name_if_visible") or settings.default_patient,
            )
            confidence_value = data.get("eligibility_confidence")
            confidence = st.selectbox(
                "Eligibility confidence",
                CONFIDENCE_LEVELS,
                index=CONFIDENCE_LEVELS.index(confidence_value)
                if confidence_value in CONFIDENCE_LEVELS
                else 2,
            )

        description = st.text_area(
            label("What is this for, in plain English?", "description"),
            value=data.get("description") or "",
            height=70,
        )
        notes = st.text_area("Notes", value="", height=70)

        saved = st.form_submit_button("💾 Save receipt", type="primary")

    if not saved:
        return

    receipt = Receipt(
        file_hash=key,
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
        extraction_raw=raw,
    )
    errors = receipt.validate()
    if errors:
        st.error("Fix these first: " + "; ".join(errors))
        return
    if receipt.amount is None:
        st.warning("Saved without an amount — it will show in the dashboard warnings panel.")

    with st.spinner("Uploading original to Drive and appending to the index…"):
        try:
            store.commit_receipt(receipt, upload.getvalue(), upload.name)
        except Exception as exc:  # noqa: BLE001
            st.error(
                f"Save failed: {exc}\n\nIf the Drive upload succeeded but the Sheet write "
                "did not, the file will be flagged as an orphan on next launch."
            )
            return
    store.flash(f"Saved — {receipt.provider or 'receipt'} filed under {receipt.tax_year}.")
    saved = st.session_state.setdefault(_SAVED, set())
    saved.add(key)
    # Last one in the batch: hand back an empty drop zone instead of asking the
    # user to remove the files themselves. Bumping the round is what makes the
    # widget forget. Duplicates never enter `saved`, so a batch still holding an
    # unresolved one keeps its card on screen rather than hiding the problem.
    if all(sha256_hex(u.getvalue()) in saved for u in uploads):
        st.session_state[_ROUND] += 1
        st.session_state.pop(_SAVED, None)
    st.rerun()


# A saved receipt drops off the list on the next render. Its confirmation is
# already at the top of the page, so a spent card is nothing but clutter.
_saved_hashes = st.session_state.get(_SAVED, set())
pending = [u for u in uploads if sha256_hex(u.getvalue()) not in _saved_hashes]

for upload in pending:
    raw_bytes = upload.getvalue()
    file_hash = sha256_hex(raw_bytes)

    with st.container(border=True):
        st.markdown(f"### {upload.name}")

        duplicate = ledger.is_duplicate(existing, file_hash)
        if duplicate:
            st.error("**Duplicate blocked.** This exact file is already in your index.")
            st.write(
                f"Existing record: **{duplicate.provider or '—'}**, "
                f"{duplicate.service_date or 'undated'}, "
                f"${duplicate.amount if duplicate.amount is not None else '—'} "
                f"({PAYMENT_LABELS.get(duplicate.payment_method, '')})"
            )
            if duplicate.drive_link:
                st.link_button("Open the existing file in Drive", duplicate.drive_link)
            continue

        preview, form_area = st.columns([1, 2])
        with preview:
            if not upload.name.lower().endswith(".pdf"):
                st.image(raw_bytes, caption=upload.name, width="stretch")
            else:
                st.caption("PDF — no inline preview.")
            st.caption(f"SHA-256 `{file_hash[:16]}…`")

        cache_key = f"_hsa_extraction_{file_hash}"
        if cache_key not in st.session_state:
            with st.spinner("Reading the receipt…"):
                pages = extraction.normalize(raw_bytes, upload.name)
                st.session_state[cache_key] = extraction.extract(
                    pages, settings.nvidia_api_key, settings.nvidia_model, settings.nvidia_base_url
                )
        result: extraction.Extraction = st.session_state[cache_key]

        with form_area:
            if result.error:
                st.warning(
                    f"Automatic extraction unavailable ({result.error}). "
                    "Fill the fields in by hand — saving still works."
                )
            if result.ambiguities:
                with st.expander(f"Model flagged {len(result.ambiguities)} thing(s) as unclear"):
                    for item in result.ambiguities:
                        st.write(f"- {item}")
            if result.data.get("is_medical_expense") is False:
                st.warning("The model does not think this is a medical expense. Double-check it.")
            if result.data.get("line_items"):
                with st.expander("Line items read from the receipt"):
                    st.table(result.data["line_items"])
            if result.uncertain_fields:
                st.caption("⚠️ marks a field the model could not read — check those first.")

            confirmation_form(file_hash, upload, result.data, result.uncertain_fields, result.raw)

st.divider()
st.caption("Not tax advice. Originals are uploaded to your Drive unmodified.")
