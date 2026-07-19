"""Local dataset persistence for Panda.

System workflow: store normalized dataset rows as CSV files and keep searchable
metadata in SQLite. ``app.py`` passes dataset IDs through the Flask session and
uses this module to save, reopen, list, and delete datasets. This layer manages
files and metadata only; pandas analysis belongs in ``app.py``.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PANDA_DATA_DIR", APP_DIR / "panda_data"))
DATASETS_DIR = DATA_DIR / "datasets"
EXPORTS_DIR = DATA_DIR / "exports"
DB_PATH = DATA_DIR / "panda.db"


def utc_now():
    """Return a compact UTC timestamp suitable for SQLite sorting."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_storage():
    """Create data directories and the idempotent SQLite metadata schema."""
    DATASETS_DIR.mkdir(parents=True, exist_ok=True)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS datasets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                original_filename TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                encoding TEXT,
                rows_count INTEGER NOT NULL,
                columns_count INTEGER NOT NULL,
                columns_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_opened_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )


def save_dataset(df, original_filename, encoding="utf-8"):
    """Persist a DataFrame as CSV and return its newly created metadata record."""
    init_storage()
    dataset_id = uuid.uuid4().hex
    safe_name = Path(original_filename).stem or "dataset"
    stored_path = DATASETS_DIR / f"{dataset_id}.csv"
    df.to_csv(stored_path, index=False)
    now = utc_now()
    columns = [str(c) for c in df.columns.tolist()]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO datasets (
                id, name, original_filename, stored_path, encoding,
                rows_count, columns_count, columns_json, created_at, last_opened_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dataset_id,
                safe_name,
                original_filename,
                str(stored_path),
                encoding,
                int(len(df)),
                int(len(df.columns)),
                json.dumps(columns),
                now,
                now,
            ),
        )
    return get_dataset(dataset_id)


def list_datasets():
    """Return saved dataset summaries ordered by most recently opened."""
    init_storage()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, name, original_filename, stored_path, encoding,
                   rows_count, columns_count, created_at, last_opened_at
            FROM datasets
            ORDER BY last_opened_at DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_dataset(dataset_id):
    """Return one dataset's metadata, or ``None`` when its ID is unknown."""
    init_storage()
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT id, name, original_filename, stored_path, encoding,
                   rows_count, columns_count, columns_json, created_at, last_opened_at
            FROM datasets
            WHERE id = ?
            """,
            (dataset_id,),
        ).fetchone()
    if not row:
        return None
    item = dict(row)
    item["columns"] = json.loads(item.pop("columns_json") or "[]")
    return item


def load_dataset(dataset_id):
    """Reload a saved CSV into pandas and update its last-opened timestamp."""
    item = get_dataset(dataset_id)
    if not item:
        return None, "Dataset not found."
    path = Path(item["stored_path"])
    if not path.exists():
        return None, "Saved dataset file is missing."
    df = pd.read_csv(path)
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "UPDATE datasets SET last_opened_at = ? WHERE id = ?",
            (utc_now(), dataset_id),
        )
    return df, None


def delete_dataset(dataset_id):
    """Delete both the stored CSV and its metadata row; report whether it existed."""
    item = get_dataset(dataset_id)
    if not item:
        return False
    path = Path(item["stored_path"])
    if path.exists():
        path.unlink()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
    return True


