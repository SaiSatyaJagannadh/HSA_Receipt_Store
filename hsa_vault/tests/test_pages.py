"""Every page must render in every realistic data state.

The Receipts page shipped a crash that only appeared with exactly one receipt:
the amount-range slider collapsed to zero width and Streamlit raised. The unit
tests all passed, because none of them render a page. These do.

The states below are the ones a real vault actually passes through — empty on
day one, one receipt after the first upload, then a mixed set.
"""

from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from core import config, store
from core.models import Contribution, Receipt, Reimbursement

APP_DIR = Path(__file__).resolve().parents[1]
PAGES = ["app.py"] + [f"pages/{p.name}" for p in sorted((APP_DIR / "pages").glob("*.py"))]


def make_receipt(**kw) -> Receipt:
    defaults = dict(
        file_hash="a" * 64,
        drive_file_id="f1",
        drive_link="https://drive.google.com/file/d/f1",
        service_date=date(2026, 3, 14),
        upload_date=datetime.now(timezone.utc),
        provider="CVS Pharmacy",
        amount=Decimal("42.18"),
        category="Prescription",
        payment_method="out_of_pocket",
        patient="self",
    )
    defaults.update(kw)
    return Receipt(**defaults)


EMPTY: list[Receipt] = []

# The exact shape that crashed production: one receipt, so min(amount) == max(amount).
SINGLE = [make_receipt(provider="Amazon", amount=Decimal("25.46"), payment_method="hsa_card")]

# Several identical amounts also collapse the slider range.
IDENTICAL = [make_receipt(file_hash=f"{i}" * 64, amount=Decimal("10.00")) for i in range(3)]

MIXED = [
    make_receipt(amount=Decimal("42.18"), payment_method="hsa_card"),
    make_receipt(file_hash="b" * 64, amount=Decimal("310.00"), provider="Dr. Ruiz",
                 category="Dental", reimbursement_amount=Decimal("50.00")),
    make_receipt(file_hash="c" * 64, amount=None, provider="", service_date=None,
                 eligibility_confidence="review"),
    make_receipt(file_hash="d" * 64, amount=Decimal("5.00"), deleted=True),
]

STATES = {
    "empty-vault": EMPTY,
    "single-receipt": SINGLE,
    "identical-amounts": IDENTICAL,
    "mixed": MIXED,
}


class FakeDrive:
    def download(self, fid):
        raise RuntimeError("no network in tests")

    def metadata(self, fid):
        return {"name": "HSA_Vault"}

    def list_folder(self, fid):
        return []

    def list_all_receipt_files(self):
        return []

    def exports_folder(self):
        return "exports"

    def year_folder(self, year):
        return str(year)

    def archive_folder(self):
        return "archive"

    def upload(self, *a, **k):
        return {"id": "new", "webViewLink": "https://drive.google.com/file/d/new"}


class FakeSheets:
    def read_tab(self, tab):
        return []

    def ensure_tabs(self):
        return []


@pytest.fixture
def wired(monkeypatch):
    """Point the store at in-memory data so no page touches Google."""

    def install(receipts):
        settings = config.Settings(
            google_credentials_json=__file__,  # any existing path
            drive_folder_id="folder",
            sheet_id="sheet",
            nvidia_api_key="",  # extraction off: no network from a test
            default_patient="Tester",
        )
        # Patch the loader, NOT store.settings(). Patching store.settings meant the
        # real one never ran, so it never wrote st.session_state — which is exactly
        # how the settings/form key collision slipped through to production.
        monkeypatch.setattr(config, "load_settings", lambda: settings)
        monkeypatch.setattr(store, "receipts", lambda refresh=False: receipts)
        monkeypatch.setattr(store, "reimbursements", lambda refresh=False: [
            Reimbursement(date=date(2026, 5, 1), amount=Decimal("50.00"), method="transfer")
        ])
        monkeypatch.setattr(store, "contributions", lambda refresh=False: [
            Contribution(date=date(2026, 1, 1), amount=Decimal("350.00"), source="payroll")
        ])
        monkeypatch.setattr(store, "clients", lambda: (FakeSheets(), FakeDrive()))
        monkeypatch.setattr(store, "find_orphans", lambda: [])

    return install


@pytest.mark.parametrize("page", PAGES)
@pytest.mark.parametrize("state", list(STATES))
def test_page_renders_without_exception(page, state, wired):
    from streamlit.testing.v1 import AppTest

    wired(STATES[state])
    at = AppTest.from_file(str(APP_DIR / page), default_timeout=90)
    at.run()
    assert not at.exception, (
        f"{page} raised in the '{state}' state: "
        + " | ".join(str(e.value) for e in at.exception)
    )


def test_ready_survives_a_missing_secrets_file(monkeypatch):
    """Settings.ready() must not raise when there is no secrets.toml.

    st.secrets is lazy: it raises on first key access, not on construction. When
    config._secrets() returned it unprobed, the raise landed on the caller's
    `in` test — crashing every page of a local run that had no secrets file.
    """
    import streamlit as st

    class Exploding:
        def __contains__(self, key):
            raise st.errors.StreamlitSecretNotFoundError("no secrets found")

    monkeypatch.setattr(st, "secrets", Exploding())
    settings = config.Settings(drive_folder_id="f", sheet_id="s")
    assert settings.ready() == ["Google credentials JSON path"]
