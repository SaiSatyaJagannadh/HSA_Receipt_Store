"""Google Sheets is the source of truth for the index. Everything else is a cache."""

from googleapiclient.discovery import build

from .models import TABS
from .util import audit, retry


class SheetsClient:
    def __init__(self, sheet_id: str, credentials):
        self.sheet_id = sheet_id
        self._api = build("sheets", "v4", credentials=credentials, cache_discovery=False)

    # -- bootstrap ----------------------------------------------------------

    @retry()
    def _metadata(self) -> dict:
        return self._api.spreadsheets().get(spreadsheetId=self.sheet_id).execute()

    def ensure_tabs(self) -> list[str]:
        """Create any missing tab and write its header row. Idempotent."""
        meta = self._metadata()
        existing = {s["properties"]["title"] for s in meta.get("sheets", [])}
        created = []
        requests = [
            {"addSheet": {"properties": {"title": tab}}}
            for tab in TABS
            if tab not in existing
        ]
        if requests:
            self._batch_update(requests)
            created = [r["addSheet"]["properties"]["title"] for r in requests]

        for tab, columns in TABS.items():
            header = self._values(f"{tab}!1:1")
            if not header or header[0] != columns:
                self._write(f"{tab}!A1", [columns])
        if created:
            audit("sheets.tabs_created", tabs=created)
        return created

    @retry()
    def _batch_update(self, requests: list[dict]) -> None:
        self._api.spreadsheets().batchUpdate(
            spreadsheetId=self.sheet_id, body={"requests": requests}
        ).execute()

    # -- values -------------------------------------------------------------

    @retry()
    def _values(self, rng: str) -> list[list[str]]:
        resp = (
            self._api.spreadsheets()
            .values()
            .get(spreadsheetId=self.sheet_id, range=rng)
            .execute()
        )
        return resp.get("values", [])

    @retry()
    def _write(self, rng: str, values: list[list]) -> None:
        self._api.spreadsheets().values().update(
            spreadsheetId=self.sheet_id,
            range=rng,
            valueInputOption="RAW",
            body={"values": values},
        ).execute()

    @retry()
    def _append(self, tab: str, values: list[list]) -> None:
        self._api.spreadsheets().values().append(
            spreadsheetId=self.sheet_id,
            range=f"{tab}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()

    # -- public -------------------------------------------------------------

    def read_tab(self, tab: str) -> list[dict]:
        """Returns dicts keyed by the canonical column names, header row skipped."""
        columns = TABS[tab]
        rows = self._values(f"{tab}!A1:ZZ")
        if not rows:
            return []
        out = []
        for raw in rows[1:]:
            padded = list(raw) + [""] * (len(columns) - len(raw))
            out.append(dict(zip(columns, padded)))
        return out

    def append_row(self, tab: str, row: list[str]) -> None:
        self._append(tab, [row])
        audit("sheets.append", tab=tab, key=row[0] if row else "")

    def row_number_of(self, tab: str, value: str, key_column: int = 0) -> int | None:
        """1-based sheet row number (header is row 1) for the record with this key."""
        rows = self._values(f"{tab}!A1:ZZ")
        for offset, raw in enumerate(rows[1:], start=2):
            if len(raw) > key_column and raw[key_column] == value:
                return offset
        return None

    def update_row(self, tab: str, row_number: int, row: list[str]) -> None:
        self._write(f"{tab}!A{row_number}", [row])
        audit("sheets.update", tab=tab, row=row_number, key=row[0] if row else "")

    def _tab_gid(self, tab: str) -> int:
        for sheet in self._metadata().get("sheets", []):
            if sheet["properties"]["title"] == tab:
                return sheet["properties"]["sheetId"]
        raise KeyError(f"no tab named {tab}")

    def delete_rows(self, tab: str, row_numbers: list[int]) -> int:
        """Really remove rows, rather than blanking them and leaving gaps.

        Deletes bottom-up so earlier indices stay valid as the sheet shrinks.
        """
        if not row_numbers:
            return 0
        gid = self._tab_gid(tab)
        requests = [
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": gid,
                        "dimension": "ROWS",
                        "startIndex": n - 1,  # API is 0-based; row 1 is the header
                        "endIndex": n,
                    }
                }
            }
            for n in sorted(row_numbers, reverse=True)
        ]
        self._batch_update(requests)
        audit("sheets.delete_rows", tab=tab, count=len(requests))
        return len(requests)

    def upsert_row(self, tab: str, row: list[str]) -> None:
        existing = self.row_number_of(tab, row[0])
        if existing:
            self.update_row(tab, existing, row)
        else:
            self.append_row(tab, row)
