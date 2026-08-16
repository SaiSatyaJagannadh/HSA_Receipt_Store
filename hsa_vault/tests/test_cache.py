"""The SQLite mirror is disposable — and must fail like it.

Sheets is authoritative; this file exists only so the app degrades to read-only
instead of blank when Google is unreachable. Nothing here may ever be treated as
a source of truth, and a problem writing it must never cost us live data.
"""

from decimal import Decimal

from core import cache, models
from core.models import Receipt

def test_an_outgrown_cache_table_is_rebuilt_not_left_broken(tmp_path):
    """Adding a column must not brick the mirror.

    CREATE TABLE IF NOT EXISTS keeps the old table, so every insert failed with
    "table receipts has 21 columns but 22 values were supplied". That raised
    inside the read path, so the app declared Sheets unreachable and served the
    stale cache — a receipt that had saved correctly vanished from the list.
    """
    import sqlite3

    path = tmp_path / "cache.sqlite"
    old_columns = models.RECEIPT_COLUMNS[:-1]  # a cache written before the newest column
    conn = sqlite3.connect(path)
    conn.execute(
        f"CREATE TABLE receipts ({', '.join(f'{c} TEXT' for c in old_columns)}, "
        "PRIMARY KEY (receipt_id))"
    )
    conn.commit()
    conn.close()

    receipt = Receipt(file_hash="a" * 64, provider="Clinic", amount=Decimal("10.00"))
    assert cache.rebuild(path, [receipt]) == 1, "rebuild failed against an old cache file"
    assert [r.provider for r in cache.load(path)] == ["Clinic"]


def test_a_broken_cache_does_not_make_live_data_look_offline(monkeypatch, tmp_path):
    """The mirror is disposable; the Sheet's answer is not.

    A cache write failure used to be caught by the same handler as a Sheets
    outage, so good live rows were thrown away and replaced with the stale copy.
    """
    import streamlit as st

    from core import config, store

    st.session_state.clear()

    class Sheets:
        def read_tab(self, tab):
            return [{"receipt_id": "r1", "provider": "Live Clinic", "amount": "12.00"}]

    monkeypatch.setattr(store, "clients", lambda: (Sheets(), None))
    monkeypatch.setattr(config, "CACHE_PATH", tmp_path / "cache.sqlite")
    monkeypatch.setattr(
        cache, "rebuild", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk on fire"))
    )

    items = store.receipts()

    assert [r.provider for r in items] == ["Live Clinic"], "live rows were discarded"
    assert store.offline_reason() is None, (
        "a failed mirror write reported the app as offline while Sheets was answering"
    )
