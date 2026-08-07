"""Thin app-facing layer: builds the Google clients once, reads through to Sheets,
mirrors into SQLite. Every page uses this instead of touching clients directly.
"""

import streamlit as st

from . import cache, config
from .drive import DriveClient
from .models import (
    Contribution,
    Receipt,
    Reimbursement,
)
from .sheets import SheetsClient
from .util import audit, short_hash, slugify


# Namespaced so a cache key can never collide with a widget key. st.form("settings")
# against a bare st.session_state["settings"] raised
# StreamlitValueAssignmentNotAllowedError and took out the whole Settings page.
_SETTINGS = "_hsa_settings"
_CACHES = ("_hsa_receipts", "_hsa_reimbursements", "_hsa_contributions")


def settings() -> config.Settings:
    if _SETTINGS not in st.session_state:
        st.session_state[_SETTINGS] = config.load_settings()
    return st.session_state[_SETTINGS]


def reload_settings() -> config.Settings:
    st.session_state[_SETTINGS] = config.load_settings()
    clear()
    return st.session_state[_SETTINGS]


@st.cache_resource(show_spinner=False)
def _clients(credentials_json: str, sheet_id: str, folder_id: str):
    creds = config.build_credentials(credentials_json)
    return SheetsClient(sheet_id, creds), DriveClient(folder_id, creds)


def clients() -> tuple[SheetsClient, DriveClient]:
    s = settings()
    missing = s.ready()
    if missing:
        raise RuntimeError("Not configured yet — missing: " + ", ".join(missing))
    return _clients(s.google_credentials_json, s.sheet_id, s.drive_folder_id)


def connected() -> bool:
    return not settings().ready()


def clear() -> None:
    for key in _CACHES:
        st.session_state.pop(key, None)


# --- reads -----------------------------------------------------------------


def receipts(refresh: bool = False) -> list[Receipt]:
    """Sheets is authoritative. On failure, fall back to the local cache and say so."""
    if refresh:
        st.session_state.pop("_hsa_receipts", None)
    if "_hsa_receipts" in st.session_state:
        return st.session_state["_hsa_receipts"]
    try:
        sheets, _ = clients()
        rows = sheets.read_tab("receipts")
        items = [Receipt.from_row(r) for r in rows]
        cache.rebuild(config.CACHE_PATH, items)
    except Exception as exc:  # noqa: BLE001
        audit("store.read_failed", error=str(exc)[:300])
        items = cache.load(config.CACHE_PATH)
        st.session_state["_hsa_offline_reason"] = str(exc)
    st.session_state["_hsa_receipts"] = items
    return items


def reimbursements(refresh: bool = False) -> list[Reimbursement]:
    if refresh:
        st.session_state.pop("_hsa_reimbursements", None)
    if "_hsa_reimbursements" not in st.session_state:
        sheets, _ = clients()
        st.session_state["_hsa_reimbursements"] = [
            Reimbursement.from_row(r) for r in sheets.read_tab("reimbursements")
        ]
    return st.session_state["_hsa_reimbursements"]


def contributions(refresh: bool = False) -> list[Contribution]:
    if refresh:
        st.session_state.pop("_hsa_contributions", None)
    if "_hsa_contributions" not in st.session_state:
        sheets, _ = clients()
        st.session_state["_hsa_contributions"] = [
            Contribution.from_row(r) for r in sheets.read_tab("contributions")
        ]
    return st.session_state["_hsa_contributions"]


# --- writes ----------------------------------------------------------------


def save_receipt(receipt: Receipt) -> None:
    sheets, _ = clients()
    sheets.upsert_row("receipts", receipt.to_row())
    clear()


def save_reimbursement(reimbursement: Reimbursement) -> None:
    sheets, _ = clients()
    sheets.upsert_row("reimbursements", reimbursement.to_row())
    st.session_state.pop("_hsa_reimbursements", None)


def save_contribution(contribution: Contribution) -> None:
    sheets, _ = clients()
    sheets.append_row("contributions", contribution.to_row())
    st.session_state.pop("_hsa_contributions", None)


def canonical_filename(receipt: Receipt, original_name: str) -> str:
    """YYYY-MM-DD__Provider__Amount__hash.ext"""
    ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else "bin"
    day = receipt.service_date.isoformat() if receipt.service_date else "undated"
    amount = f"{receipt.amount:.2f}" if receipt.amount is not None else "unknown"
    return f"{day}__{slugify(receipt.provider)}__{amount}__{short_hash(receipt.file_hash)}.{ext}"


def commit_receipt(receipt: Receipt, original_bytes: bytes, original_name: str) -> Receipt:
    """Upload original bytes to Drive, then append the index row.

    If the Sheets write fails after a successful upload, the Drive file is left in
    place and detected as an orphan on next launch (see `find_orphans`).
    """
    _, drive = clients()
    year = receipt.tax_year or (receipt.service_date.year if receipt.service_date else 0) or 1970
    folder = drive.year_folder(year)
    filename = canonical_filename(receipt, original_name)
    audit("commit.upload_start", receipt_id=receipt.receipt_id, filename=filename)
    uploaded = drive.upload(original_bytes, filename, folder)
    receipt.drive_file_id = uploaded["id"]
    receipt.drive_link = uploaded.get("webViewLink", "")
    save_receipt(receipt)
    audit("commit.done", receipt_id=receipt.receipt_id, drive_file_id=receipt.drive_file_id)
    return receipt


def adopt_receipt(receipt: Receipt, drive_file_id: str, original_name: str) -> Receipt:
    """Index a file that is already in Drive: rename it canonically, move it into
    its year folder, then append the row. Used by Bulk Import and orphan repair —
    the bytes are never re-uploaded."""
    _, drive = clients()
    year = receipt.tax_year or (receipt.service_date.year if receipt.service_date else 0) or 1970
    filename = canonical_filename(receipt, original_name)
    updated = drive.rename(drive_file_id, filename)
    drive.move(drive_file_id, drive.year_folder(year))
    receipt.drive_file_id = drive_file_id
    receipt.drive_link = updated.get("webViewLink", "")
    save_receipt(receipt)
    audit("adopt.done", receipt_id=receipt.receipt_id, drive_file_id=drive_file_id)
    return receipt


def archive_receipt(receipt: Receipt, reason: str = "") -> Receipt:
    """Soft delete: flag the row and move the file to _archive. Never hard-deleted."""
    _, drive = clients()
    if receipt.drive_file_id:
        drive.move(receipt.drive_file_id, drive.archive_folder())
    receipt.deleted = True
    receipt.record_edit({"deleted": True}, note=reason or "archived")
    save_receipt(receipt)
    audit("receipt.archived", receipt_id=receipt.receipt_id, reason=reason)
    return receipt


# --- integrity -------------------------------------------------------------


def find_orphans() -> list[dict]:
    """Drive files with no matching row in the Sheet — i.e. a half-finished save."""
    _, drive = clients()
    indexed = {r.drive_file_id for r in receipts() if r.drive_file_id}
    return [f for f in drive.list_all_receipt_files() if f["id"] not in indexed]


def rebuild_cache() -> int:
    return cache.rebuild(config.CACHE_PATH, receipts(refresh=True))
