"""Create the index Sheet (or repair an existing one) with all three tabs.

    python -m scripts.bootstrap_sheet          # uses HSA_SHEET_ID if set
    python -m scripts.bootstrap_sheet --create # creates a new sheet in the Drive folder

Run from the hsa_vault/ directory.
"""

import argparse
import sys

from googleapiclient.discovery import build

from core import config
from core.models import TABS
from core.sheets import SheetsClient


def create_spreadsheet(credentials, folder_id: str, title: str) -> str:
    """Create the sheet inside the shared Drive folder so the service account owns
    it and you can still see it (the folder is yours)."""
    drive = build("drive", "v3", credentials=credentials, cache_discovery=False)
    created = drive.files().create(
        body={
            "name": title,
            "mimeType": "application/vnd.google-apps.spreadsheet",
            "parents": [folder_id],
        },
        fields="id,webViewLink",
        supportsAllDrives=True,
    ).execute()
    print(f"Created spreadsheet: {created['id']}")
    print(f"Open it: {created.get('webViewLink')}")
    return created["id"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--create", action="store_true", help="create a new spreadsheet")
    parser.add_argument("--title", default="HSA Vault Index")
    args = parser.parse_args()

    settings = config.load_settings()
    if not settings.service_account_json:
        print("GOOGLE_SERVICE_ACCOUNT_JSON is not set. Copy .env.example to .env first.")
        return 1

    credentials = config.build_credentials(settings.service_account_json)

    sheet_id = settings.sheet_id
    if args.create or not sheet_id:
        if not settings.drive_folder_id:
            print("HSA_DRIVE_FOLDER_ID is required to create a new sheet.")
            return 1
        sheet_id = create_spreadsheet(credentials, settings.drive_folder_id, args.title)
        print("\nAdd this to your .env:")
        print(f"HSA_SHEET_ID={sheet_id}")

    client = SheetsClient(sheet_id, credentials)
    created = client.ensure_tabs()
    print(f"\nTabs present: {', '.join(TABS)}")
    print(f"Tabs created this run: {created or 'none'}")
    print("Headers verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
