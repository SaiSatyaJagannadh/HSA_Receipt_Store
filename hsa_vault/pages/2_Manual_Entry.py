"""Type a receipt in by hand. Same fields, same row, no vision model.

The Upload page needs a photo and an extraction call before it can show you a
form. Neither is always available: a receipt that arrived as an email total, one
whose paper is long gone, an expense you are back-filling from a statement, or
simply a day when the model is down. This page writes exactly the same
`receipts` row from typed values, so everything downstream — the balance, the
Receipts editor, withdrawals, the audit packet — treats it as an ordinary
receipt.

A document is optional here, not absent. Attach one and it is uploaded to Drive
and hashed exactly as an upload would be, so duplicate detection stays uniform
across both routes; save without one and the Export page's packet check reports
it as "no image" until you add it later from Receipts, which never changes the
receipt_id.
"""

import re
from datetime import date, datetime, timezone
from decimal import Decimal

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
    "No extraction — you type the values and they are saved verbatim. A photo is "
    "optional: attach one and it goes to Drive with the row, or leave it and add it "
    "later on **Receipts**."
)

# A file_uploader cannot be emptied by writing to its session state, so a saved
# batch re-keys it. Same trick, and same reason, as the Upload page.
_ROUND = "_hsa_manual_round"


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

    Only used when nothing is attached. With a file in hand the real hash of the
    bytes is used instead, so the same photo cannot be filed once here and once
    from Upload.
    """
    parts = ["manual", str(service_date or ""), provider.strip().lower(),
             money_str(amount), patient.strip().lower()]
    return sha256_hex("|".join(parts).encode())


_HAS_LETTER_OR_DIGIT = re.compile(r"[^\W_]", re.UNICODE)


def name_problem(label: str, value: str) -> str | None:
    """Reject a name that identifies nothing. Returns the complaint, or None.

    Checked here rather than in `Receipt.validate()` on purpose. The Sheet is
    hand-editable and years of rows already exist with a blank provider — an
    extraction that could not read one saves that way by design — so tightening
    the shared validator would make those rows unsavable from the Receipts
    editor, i.e. break editing the very receipts most in need of it. This is a
    rule about what a *person typing a new row* must supply.

    A name of "-" or "..." passes a non-empty test and is worth no more than a
    blank: the provider is what the audit packet is grouped and searched by.
    """
    text = value.strip()
    if not text:
        return f"{label} is required — it is what an audit packet is indexed by"
    if not _HAS_LETTER_OR_DIGIT.search(text):
        return f"{label} needs at least one letter or number, not just punctuation"
    return None


with st.form("manual_receipt", clear_on_submit=True):
    col1, col2 = st.columns(2)
    with col1:
        provider = st.text_input("Provider", placeholder="Dr. Ruiz Family Dental")
        service_date = st.date_input(
            "Service date (not the date you are typing this)",
            value=date.today(),
            format="YYYY-MM-DD",
        )
        # A number_input, not free text: it arrives with a value already in it,
        # rejects "about forty" at the widget instead of silently storing a blank
        # amount, and steps in cents. money() still runs on the way out so the
        # float this widget hands back never reaches the Sheet — Decimal is the
        # only thing allowed near a dollar figure.
        amount = st.number_input(
            "Amount",
            min_value=0.00,
            value=0.00,
            step=0.01,
            format="%.2f",
            help="The receipt total. 0.00 is refused — it is the untouched default, "
            "not a real expense.",
        )
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

    # Inside the form on purpose, unlike the Receipts page's attach box. There the
    # uploader has to act on its own button, so it must sit outside; here the files
    # belong to the receipt being typed and delivering them on submit is exactly
    # the wanted behaviour.
    attachments = st.file_uploader(
        "📎 Photo or PDF (optional) — several files are kept as pages of this one receipt",
        type=["jpg", "jpeg", "png", "heic", "heif", "pdf"],
        accept_multiple_files=True,
        key=f"_hsa_manual_files_{st.session_state.setdefault(_ROUND, 0)}",
    )

    submitted = st.form_submit_button("💾 Save receipt", type="primary")

# The notes sit outside the form because a form only re-runs on submit, so a
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
    files = list(attachments or [])
    # With files in hand the receipt is hashed the way an upload is, so the same
    # photo filed from either page collides. A lone file keeps its own hash, which
    # is what makes that true across pages; a set of pages gets the combined key,
    # matching the Upload page's grouping.
    page_hashes = [sha256_hex(f.getvalue()) for f in files]
    if len(page_hashes) == 1:
        file_hash = page_hashes[0]
    elif page_hashes:
        file_hash = sha256_hex("".join(sorted(page_hashes)).encode())
    else:
        file_hash = content_hash(service_date, provider, money(amount), patient)

    # Every attached page is checked, not just the group, so re-attaching one half
    # of a pair already on file is still caught.
    duplicate = next(
        (d for d in (ledger.is_duplicate(existing, h) for h in page_hashes) if d), None
    ) or ledger.is_duplicate(existing, file_hash)

    problems = [
        p
        for p in (name_problem("Provider", provider), name_problem("Patient", patient))
        if p
    ]
    # money() re-reads the widget's float as a Decimal; comparing against Decimal
    # keeps every dollar comparison off floats, same as everywhere else.
    if money(amount) == Decimal("0.00"):
        problems.append(
            "Amount is still 0.00 — enter the receipt total. A zero-dollar receipt "
            "proves nothing and would sit in your index as a real one"
        )

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
        note="entered by hand"
        + (f" — {len(files)} file(s) attached" if files else " — no document attached"),
    )

    if duplicate:
        st.error(
            "**Duplicate blocked.** This expense is already in your index — filing it "
            "twice would inflate your claimable balance. Existing record: "
            f"**{duplicate.provider or '—'}**, {duplicate.service_date or 'undated'}, "
            f"${duplicate.amount if duplicate.amount is not None else '—'} "
            f"({PAYMENT_LABELS.get(duplicate.payment_method, '')}). Change a field, or "
            "edit that receipt on **Receipts**."
        )
    elif problems or receipt.validate():
        st.error("Fix these first: " + "; ".join([*problems, *receipt.validate()]))
    else:
        try:
            with st.spinner(
                "Appending to the index…"
                if not files
                else f"Uploading {len(files)} file(s) to Drive and appending to the index…"
            ):
                if files:
                    first, *rest = files
                    store.commit_receipt(
                        receipt,
                        first.getvalue(),
                        first.name,
                        extra_pages=[(f.getvalue(), f.name) for f in rest],
                    )
                else:
                    store.save_receipt(receipt)
        except Exception as exc:  # noqa: BLE001
            st.error(
                f"Save failed: {exc}"
                + (
                    "\n\nIf the Drive upload succeeded but the Sheet write did not, the "
                    "file will be flagged as an orphan on next launch."
                    if files
                    else ""
                )
            )
        else:
            # Everything worth saying goes in the flash. An st.warning() here would
            # be drawn and then thrown away by the st.rerun() below — the same
            # disappearing-confirmation bug store.flash() exists for.
            store.flash(
                f"Saved — {receipt.provider} filed under {receipt.tax_year}. "
                + (
                    f"{len(files)} file(s) uploaded to Drive."
                    if files
                    else "No document is attached, so it will show as \"no image\" in the "
                    "audit packet check until you add one on **Receipts**."
                )
            )
            # Hand back an empty drop zone rather than leaving the saved file sitting
            # in the uploader, where the next entry would silently attach it again.
            st.session_state[_ROUND] += 1
            st.rerun()

st.divider()
st.caption("Not tax advice. Attached originals are uploaded to your Drive unmodified.")
