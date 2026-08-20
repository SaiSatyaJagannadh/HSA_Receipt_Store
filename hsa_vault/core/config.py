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

REPO_ROOT = Path(__file__).resolve().parents[2]

STATE_DIR = Path(os.getenv("HSA_STATE_DIR", Path.home() / ".hsavault"))
SETTINGS_PATH = STATE_DIR / "settings.json"
CACHE_PATH = STATE_DIR / "cache.sqlite"
AUDIT_PATH = STATE_DIR / "audit.log"

STATE_DIR.mkdir(parents=True, exist_ok=True)
set_audit_path(AUDIT_PATH)


def resolve_path(value: str) -> Path:
    """A relative key path is resolved against the CWD first, then the repo root.

    The app is launched from hsa_vault/ but the credentials file lives next to
    the README, so a bare './credentials.json' has to work from either.
    """
    path = Path(value).expanduser()
    if path.is_absolute() or path.exists():
        return path
    return REPO_ROOT / path


@dataclass
class Settings:
    google_credentials_json: str = ""
    drive_folder_id: str = ""
    sheet_id: str = ""
    # Where a phone drops receipt photos. Bulk Import scans it; nothing writes
    # here, so it can live anywhere in Drive.
    inbox_folder_id: str = ""
    nvidia_api_key: str = ""
    nvidia_model: str = "meta/llama-3.2-90b-vision-instruct"
    # Text-only, for the Ask page. Separate from nvidia_model because the vision
    # models used for extraction are slow and often unavailable on the free tier,
    # and a chat nobody waits for is a chat nobody uses.
    nvidia_chat_model: str = "meta/llama-3.1-8b-instruct"
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
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
        have_file = bool(self.google_credentials_json) and resolve_path(
            self.google_credentials_json
        ).exists()
        if not have_file and "google_token" not in _secrets():
            missing.append("Google credentials JSON path")
        if not self.drive_folder_id:
            missing.append("Drive folder ID")
        if not self.sheet_id:
            missing.append("Sheet ID")
        return missing


_ENV_MAP = {
    "google_credentials_json": "GOOGLE_CREDENTIALS_JSON",
    "drive_folder_id": "HSA_DRIVE_FOLDER_ID",
    "sheet_id": "HSA_SHEET_ID",
    "inbox_folder_id": "HSA_INBOX_FOLDER_ID",
    "nvidia_api_key": "NVIDIA_API_KEY",
    "nvidia_model": "NVIDIA_MODEL",
    "nvidia_chat_model": "NVIDIA_CHAT_MODEL",
    "nvidia_base_url": "NVIDIA_BASE_URL",
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
    """Precedence: .env  <  st.secrets[hsa]  <  settings.json.

    st.secrets is how a hosted deploy gets its folder/sheet IDs and API key,
    since there is no .env file on the server.
    """
    values = {}
    for name, env_key in _ENV_MAP.items():
        raw = os.getenv(env_key)
        if raw not in (None, ""):
            values[name] = _coerce(name, raw)
    try:
        hosted = _secrets()["hsa"]
    except Exception:
        hosted = {}
    for name in _TYPES:
        raw = hosted.get(name) if hasattr(hosted, "get") else None
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
    # Full drive scope, not drive.file: Bulk Import has to read receipts that
    # already exist in your Drive, which drive.file cannot see.
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]

TOKEN_PATH = STATE_DIR / "token.json"


def credential_kind(path: Path) -> str:
    """'service_account' | 'oauth' — decided by the file's own contents."""
    try:
        data = json.loads(path.read_text())
    except Exception:
        return "unknown"
    if data.get("type") == "service_account":
        return "service_account"
    if "installed" in data or "web" in data:
        return "oauth"
    return "unknown"


def _secrets():
    """st.secrets if we're inside a Streamlit run with secrets, else {}.

    st.secrets is lazy: constructing it never raises, but the first key lookup
    parses secrets.toml and raises StreamlitSecretNotFoundError when there is
    none. Returning it bare moved that raise to the caller's `in` check, outside
    this try — which crashed every local run that had no secrets.toml. Probe it
    here so the failure is caught where it is handled.
    """
    try:
        import streamlit as st

        secrets = st.secrets
        "google_token" in secrets  # forces the lazy parse inside the try
        return secrets
    except Exception:
        return {}


def credentials_from_secrets():
    """A hosted server has no browser, so the consent flow cannot run there.

    Instead we ship an already-authorized refresh token in st.secrets and mint
    access tokens from it. Returns None when not deployed, so local runs keep
    using the interactive flow.
    """
    secrets = _secrets()
    try:
        token = secrets["google_token"]
    except Exception:
        return None

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = Credentials.from_authorized_user_info(dict(token), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise ValueError(
                "google_token in secrets has no refresh_token. Re-run "
                "scripts/export_deploy_secrets.py to mint a fresh one."
            )
        creds.refresh(Request())  # cannot persist here; the server is read-only
    return creds


def build_credentials(credentials_json: str):
    """Google auth from whichever credential file you point at.

    OAuth is the default because a service account has no Drive storage of its
    own: on a personal (non-Workspace) account every file it creates fails with
    403 storageQuotaExceeded. Under OAuth the files are owned by you, which is
    also what the whole design wants — your records must outlive this app and
    the Cloud project it was built in.

    A service account file still works, for Workspace accounts writing to a
    Shared Drive. When deployed, a pre-authorized token in st.secrets wins,
    because a hosted server cannot open a browser for consent.
    """
    deployed = credentials_from_secrets()
    if deployed is not None:
        return deployed

    path = resolve_path(credentials_json)
    kind = credential_kind(path)

    if kind == "service_account":
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_file(
            str(path), scopes=SCOPES
        )

    if kind != "oauth":
        raise ValueError(
            f"{path} is not a Google credentials file. Expected either an OAuth "
            "client (downloaded from Credentials → OAuth client IDs) or a "
            "service account key."
        )

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if TOKEN_PATH.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        except Exception:
            creds = None  # corrupt or scope-mismatched token: re-consent below

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
            return creds
        except Exception:
            creds = None  # revoked or expired past refresh: re-consent below

    # Opens a browser once and blocks on your consent. Run a terminal command
    # (scripts/bootstrap_sheet.py) for this rather than doing it inside Streamlit.
    flow = InstalledAppFlow.from_client_secrets_file(str(path), SCOPES)
    creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json())
    TOKEN_PATH.chmod(0o600)
    return creds
