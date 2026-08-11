"""Thin app-facing layer: builds the Google clients once, reads through to Sheets,
mirrors into SQLite. Every page uses this instead of touching clients directly.
"""

from datetime import date
from decimal import Decimal

import streamlit as st

from . import cache, config, ledger
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


_FLASH = "_hsa_flash"
_OFFLINE = "_hsa_offline_reason"


def offline_reason() -> str | None:
    """Why the receipts in hand came from the local cache, or None if they're live.

    Read, never popped: the banner has to survive every rerun that serves the same
    cached list, or the app silently presents stale data as current.
    """
    return st.session_state.get(_OFFLINE)


def flash(message: str) -> None:
    """Queue a confirmation to render *after* the next st.rerun().

    st.success() called immediately before st.rerun() never reaches the browser:
    the rerun discards the page mid-render. Every save therefore looked like a
    no-op, and pressing Save a second time then reported "Nothing changed",
    which read as the edit being rejected.
    """
    st.session_state[_FLASH] = message


def show_flash() -> None:
    if message := st.session_state.pop(_FLASH, None):
        st.success(message)


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
        # Describes the data now held, so a recovered read must retract the banner.
        st.session_state.pop(_OFFLINE, None)
    except Exception as exc:  # noqa: BLE001
        audit("store.read_failed", error=str(exc)[:300])
        items = cache.load(config.CACHE_PATH)
        st.session_state[_OFFLINE] = str(exc)
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


class PartialReimbursement(RuntimeError):
    """The withdrawal was recorded but not every receipt it covers was marked.

    Carries how far it got so the caller can say so instead of guessing.
    """

    def __init__(self, applied: int, total: int, cause: Exception):
        super().__init__(str(cause))
        self.applied = applied
        self.total = total
        self.cause = cause


def record_reimbursement(
    record: Reimbursement,
    allocations: list[tuple[Receipt, Decimal, bool]],
    when: date,
) -> int:
    """Write a withdrawal and mark the receipts it covers. Returns receipts marked.

    Sheets has no transaction, so a failure part-way through is always possible and
    the write order decides which half survives.

    The withdrawal row goes first on purpose. A crash after it leaves the money on
    record with some receipts not yet marked, so the claimable balance reads too
    high and the gap is visible in the withdrawal history. The reverse — receipts
    marked with no withdrawal row — drops the balance with nothing anywhere saying
    where the money went, and a later reimbursement run would happily claim the
    same dollars again.
    """
    save_reimbursement(record)
    applied = 0
    for receipt, amount, fully in allocations:
        # apply_allocation mutates in place, so a failed write would otherwise
        # leave an in-memory receipt claiming to be reimbursed when the Sheet says
        # it is not — and the session cache would serve that until the next
        # refresh, reading the balance too LOW. Undo the mutation on failure so
        # memory never gets ahead of what was actually persisted.
        before = (
            receipt.reimbursement_amount,
            receipt.reimbursement_date,
            receipt.reimbursed,
            receipt.edit_history,
        )
        try:
            ledger.apply_allocation(receipt, amount, fully, when)
            save_receipt(receipt)
        except Exception as exc:  # noqa: BLE001
            (
                receipt.reimbursement_amount,
                receipt.reimbursement_date,
                receipt.reimbursed,
                receipt.edit_history,
            ) = before
            clear()  # force the next read to come from Sheets, not this half-applied list
            raise PartialReimbursement(applied, len(allocations), exc) from exc
        applied += 1
    return applied


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
    folder = _home_folder(drive, receipt)
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
    filename = canonical_filename(receipt, original_name)
    updated = drive.rename(drive_file_id, filename)
    drive.move(drive_file_id, _home_folder(drive, receipt))
    receipt.drive_file_id = drive_file_id
    receipt.drive_link = updated.get("webViewLink", "")
    save_receipt(receipt)
    audit("adopt.done", receipt_id=receipt.receipt_id, drive_file_id=drive_file_id)
    return receipt


def _home_folder(drive: DriveClient, receipt: Receipt) -> str:
    year = receipt.tax_year or (receipt.service_date.year if receipt.service_date else 0) or 1970
    return drive.year_folder(year)


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


def restore_receipt(receipt: Receipt, reason: str = "") -> Receipt:
    """Undo an archive: clear the flag and move the file back to its year folder.

    Archiving was always reversible in the data model — `deleted` is a flag and the
    file only moves to `_archive/` — but nothing exposed the reverse, so a mis-click
    was effectively permanent from inside the app.
    """
    _, drive = clients()
    if receipt.drive_file_id:
        drive.move(receipt.drive_file_id, _home_folder(drive, receipt))
    receipt.deleted = False
    receipt.record_edit({"deleted": False}, note=reason or "restored")
    save_receipt(receipt)
    audit("receipt.restored", receipt_id=receipt.receipt_id, reason=reason)
    return receipt


# --- integrity -------------------------------------------------------------


def find_orphans() -> list[dict]:
    """Drive files with no matching row in the Sheet — i.e. a half-finished save."""
    _, drive = clients()
    indexed = {r.drive_file_id for r in receipts() if r.drive_file_id}
    return [f for f in drive.list_all_receipt_files() if f["id"] not in indexed]


def rebuild_cache() -> int:
    return cache.rebuild(config.CACHE_PATH, receipts(refresh=True))
