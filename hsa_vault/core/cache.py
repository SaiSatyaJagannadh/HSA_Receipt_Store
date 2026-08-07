"""Local SQLite mirror of the Sheet. Rebuildable at any time, never authoritative.

Deleting cache.sqlite loses nothing — `rebuild()` reconstructs it from Sheets.
"""

import sqlite3
from pathlib import Path

from .models import RECEIPT_COLUMNS, Receipt
from .util import audit

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS receipts (
    {", ".join(f"{c} TEXT" for c in RECEIPT_COLUMNS)},
    PRIMARY KEY (receipt_id)
);
CREATE INDEX IF NOT EXISTS idx_hash ON receipts(file_hash);
CREATE INDEX IF NOT EXISTS idx_year ON receipts(tax_year);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def rebuild(path: Path, receipts: list[Receipt]) -> int:
    """Wipe and repopulate from whatever the Sheet just gave us."""
    conn = connect(path)
    with conn:
        conn.execute("DELETE FROM receipts")
        placeholders = ",".join("?" * len(RECEIPT_COLUMNS))
        conn.executemany(
            f"INSERT INTO receipts VALUES ({placeholders})",
            [r.to_row() for r in receipts],
        )
    conn.close()
    audit("cache.rebuilt", count=len(receipts))
    return len(receipts)


def load(path: Path) -> list[Receipt]:
    """Offline read path — used only when Sheets is unreachable."""
    if not path.exists():
        return []
    conn = connect(path)
    rows = conn.execute("SELECT * FROM receipts").fetchall()
    conn.close()
    return [Receipt.from_row(dict(r)) for r in rows]


def known_hashes(path: Path) -> set[str]:
    if not path.exists():
        return set()
    conn = connect(path)
    rows = conn.execute("SELECT file_hash FROM receipts WHERE file_hash != ''").fetchall()
    conn.close()
    return {r["file_hash"] for r in rows}
