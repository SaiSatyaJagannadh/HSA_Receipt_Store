"""Configuration: .env supplies defaults, settings.json (written by the Settings
page) overrides them. Secrets stay in .env; everything else is editable in the UI."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

from .util import set_audit_path

load_dotenv()

STATE_DIR = Path(os.getenv("HSA_STATE_DIR", Path.home() / ".hsavault"))
SETTINGS_PATH = STATE_DIR / "settings.json"
CACHE_PATH = STATE_DIR / "cache.sqlite"
AUDIT_PATH = STATE_DIR / "audit.log"

STATE_DIR.mkdir(parents=True, exist_ok=True)
set_audit_path(AUDIT_PATH)


@dataclass
class Settings:
    service_account_json: str = ""
    drive_folder_id: str = ""
    sheet_id: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    default_payment_method: str = "out_of_pocket"
    default_patient: str = "self"
    projection_rate: float = 0.07
    irs_limit: str = "4400"
    irs_limit_year: int = 2026

    @property
    def irs_limit_decimal(self) -> Decimal:
        try:
            return Decimal(str(self.irs_limit))
        except Exception:
            return Decimal("0")

    def ready(self) -> list[str]:
        """Returns the list of missing things that block Google access."""
        missing = []
        if not self.service_account_json or not Path(self.service_account_json).exists():
            missing.append("service account JSON path")
        if not self.drive_folder_id:
            missing.append("Drive folder ID")
        if not self.sheet_id:
            missing.append("Sheet ID")
        return missing


_ENV_MAP = {
    "service_account_json": "GOOGLE_SERVICE_ACCOUNT_JSON",
    "drive_folder_id": "HSA_DRIVE_FOLDER_ID",
    "sheet_id": "HSA_SHEET_ID",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "anthropic_model": "ANTHROPIC_MODEL",
    "default_payment_method": "HSA_DEFAULT_PAYMENT_METHOD",
    "default_patient": "HSA_DEFAULT_PATIENT",
    "projection_rate": "HSA_PROJECTION_RATE",
    "irs_limit": "HSA_IRS_LIMIT",
    "irs_limit_year": "HSA_IRS_LIMIT_YEAR",
}

_TYPES = {f.name: f.type for f in fields(Settings)}


def _coerce(name: str, value):
    target = _TYPES.get(name)
    if target == "float":
        return float(value)
    if target == "int":
        return int(value)
    return str(value)


def load_settings() -> Settings:
    values = {}
    for name, env_key in _ENV_MAP.items():
        raw = os.getenv(env_key)
        if raw not in (None, ""):
            values[name] = _coerce(name, raw)
    if SETTINGS_PATH.exists():
        stored = json.loads(SETTINGS_PATH.read_text())
        for name, raw in stored.items():
            if name in _TYPES and raw not in (None, ""):
                values[name] = _coerce(name, raw)
    return Settings(**values)


def save_settings(settings: Settings) -> None:
    SETTINGS_PATH.write_text(json.dumps(asdict(settings), indent=2))


SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]


def build_credentials(service_account_json: str):
    """Service account auth — no OAuth consent screen, no refresh tokens to babysit."""
    from google.oauth2 import service_account

    return service_account.Credentials.from_service_account_file(
        service_account_json, scopes=SCOPES
    )
