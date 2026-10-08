"""SQLite-backed sync state.

A row exists per known Nextcloud file (keyed by the stable `oc:fileid`).
`status` is one of:
- pending:      seen in a scan, not yet ingested (or a previous attempt failed)
- synced:       Onyx accepted this version
- skipped:      nothing to ingest (scanned PDF without text, oversized, empty)
Deletion from Onyx removes the row.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StateRow:
    nc_fileid: str
    path: str
    etag: str
    size: int
    modified_utc: str
    doc_id: str
    status: str
    synced_utc: str | None
    note: str | None


class SyncState:
    _lock = threading.Lock()

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS documents (
                nc_fileid TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                etag TEXT NOT NULL,
                size INTEGER NOT NULL,
                modified_utc TEXT NOT NULL,
                doc_id TEXT NOT NULL,
                status TEXT NOT NULL,
                synced_utc TEXT,
                note TEXT
            )
            """
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )

    def get(self, nc_fileid: str) -> StateRow | None:
        row = self._conn.execute(
            "SELECT nc_fileid, path, etag, size, modified_utc, doc_id, status, synced_utc, note"
            " FROM documents WHERE nc_fileid = ?",
            (nc_fileid,),
        ).fetchone()
        return StateRow(*row) if row else None

    def upsert(
        self,
        nc_fileid: str,
        path: str,
        etag: str,
        size: int,
        modified_utc: str,
        doc_id: str,
        status: str,
        synced_utc: str | None,
        note: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO documents
                    (nc_fileid, path, etag, size, modified_utc, doc_id, status, synced_utc, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(nc_fileid) DO UPDATE SET
                    path = excluded.path,
                    etag = excluded.etag,
                    size = excluded.size,
                    modified_utc = excluded.modified_utc,
                    doc_id = excluded.doc_id,
                    status = excluded.status,
                    synced_utc = excluded.synced_utc,
                    note = excluded.note
                """,
                (nc_fileid, path, etag, size, modified_utc, doc_id, status, synced_utc, note),
            )

    def forget(self, nc_fileid: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM documents WHERE nc_fileid = ?", (nc_fileid,))

    def all_rows(self) -> list[StateRow]:
        rows = self._conn.execute(
            "SELECT nc_fileid, path, etag, size, modified_utc, doc_id, status, synced_utc, note"
            " FROM documents ORDER BY path"
        ).fetchall()
        return [StateRow(*r) for r in rows]

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def close(self) -> None:
        self._conn.close()
