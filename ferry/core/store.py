"""SQLite bookkeeping: which device files are already offloaded, and run history."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    camera TEXT NOT NULL,
    path TEXT NOT NULL,
    created INTEGER NOT NULL,
    size INTEGER NOT NULL,
    dest TEXT NOT NULL,
    synced_at INTEGER NOT NULL,
    deleted_from_camera INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (camera, path, created, size)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at INTEGER NOT NULL,
    finished_at INTEGER,
    mode TEXT NOT NULL,
    camera TEXT,
    status TEXT NOT NULL,
    files INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE TABLE IF NOT EXISTS failures (
    camera TEXT NOT NULL,
    path TEXT NOT NULL,
    created INTEGER NOT NULL,
    size INTEGER NOT NULL,
    runs INTEGER NOT NULL,
    error TEXT,
    PRIMARY KEY (camera, path, created, size)
);
"""
# `camera` holds the device id. Non-GoPro providers prefix theirs ("usb:...", "bambu:...") so ids never clash.
SCHEMA_VERSION = 2


LIBRARY_COLUMNS = "camera, path, created, size, dest, synced_at, provider, dest_id, cred_id"
VIDEO_EXTENSIONS = ("mp4", "mov", "avi", "mts", "mkv", "insv", "360")
_IS_VIDEO = "(" + " OR ".join(f"lower(path) LIKE '%.{ext}'" for ext in VIDEO_EXTENSIONS) + ")"


class Store:
    def __init__(self, path: Path):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._migrate()
            # Runs left "running" by a crash or restart never finished.
            self._db.execute(
                "UPDATE runs SET status = 'interrupted', finished_at = ? WHERE status = 'running'",
                (int(time.time()),),
            )
            self._db.commit()

    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version < 2:
            # Version 2: which provider a file came from and which destination holds it
            # (NULL: the default destination, as for everything synced before destinations existed).
            files = {r[1] for r in self._db.execute("PRAGMA table_info(files)")}
            if "provider" not in files:
                self._db.execute("ALTER TABLE files ADD COLUMN provider TEXT NOT NULL DEFAULT 'gopro'")
            if "dest_id" not in files:
                self._db.execute("ALTER TABLE files ADD COLUMN dest_id TEXT")
            if "cred_id" not in files:
                # Credentials used for the upload when they override the destination's own.
                self._db.execute("ALTER TABLE files ADD COLUMN cred_id TEXT")
            runs = {r[1] for r in self._db.execute("PRAGMA table_info(runs)")}
            if "provider" not in runs:
                self._db.execute("ALTER TABLE runs ADD COLUMN provider TEXT")
            self._db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def is_synced(self, camera: str, path: str, created: int, size: int) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM files WHERE camera = ? AND path = ? AND created = ? AND size = ?",
                (camera, path, created, size),
            ).fetchone()
        return row is not None

    def dest_of(self, camera: str, path: str, created: int, size: int) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT dest FROM files WHERE camera = ? AND path = ? AND created = ? AND size = ?",
                (camera, path, created, size),
            ).fetchone()
        return row[0] if row else None

    def mark_synced(self, camera: str, path: str, created: int, size: int, dest: str,
                    provider: str = "gopro", dest_id: str | None = None, cred_id: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO files (camera, path, created, size, dest, synced_at, provider, dest_id, cred_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (camera, path, created, size, dest, int(time.time()), provider, dest_id, cred_id),
            )
            self._db.commit()

    def failed_runs(self, camera: str, path: str, created: int, size: int) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT runs FROM failures WHERE camera = ? AND path = ? AND created = ? AND size = ?",
                (camera, path, created, size),
            ).fetchone()
        return row[0] if row else 0

    def note_failure(self, camera: str, path: str, created: int, size: int, error: str) -> int:
        with self._lock:
            self._db.execute(
                "INSERT INTO failures (camera, path, created, size, runs, error) VALUES (?, ?, ?, ?, 1, ?)"
                " ON CONFLICT (camera, path, created, size) DO UPDATE SET runs = runs + 1, error = excluded.error",
                (camera, path, created, size, error),
            )
            self._db.commit()
        return self.failed_runs(camera, path, created, size)

    def clear_failure(self, camera: str, path: str, created: int, size: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM failures WHERE camera = ? AND path = ? AND created = ? AND size = ?",
                             (camera, path, created, size))
            self._db.commit()

    def is_deleted(self, camera: str, path: str, created: int, size: int) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT deleted_from_camera FROM files WHERE camera = ? AND path = ? AND created = ? AND size = ?",
                (camera, path, created, size),
            ).fetchone()
        return bool(row and row[0])

    def mark_deleted(self, camera: str, path: str, created: int, size: int) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE files SET deleted_from_camera = 1 WHERE camera = ? AND path = ? AND created = ? AND size = ?",
                (camera, path, created, size),
            )
            self._db.commit()

    def start_run(self, mode: str, camera: str | None = None, provider: str | None = None) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO runs (started_at, mode, camera, status, provider) VALUES (?, ?, ?, 'running', ?)",
                (int(time.time()), mode, camera, provider),
            )
            self._db.commit()
            return cur.lastrowid

    def finish_run(self, run_id: int, status: str, files: int, nbytes: int, error: str | None = None,
                   camera: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE runs SET finished_at = ?, status = ?, files = ?, bytes = ?, error = ?,"
                " camera = COALESCE(?, camera) WHERE id = ?",
                (int(time.time()), status, files, nbytes, error, camera, run_id),
            )
            self._db.commit()

    def recent_runs(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def library(self, offset: int = 0, limit: int = 60, provider: str | None = None,
                kind: str | None = None) -> tuple[list[dict], int]:
        """Synced files, newest capture first. `id` is the SQLite rowid, used in media URLs.

        `kind` is "video" or "photo" (anything that is not a video), judged by the extension.
        """
        clauses, args = [], []
        if provider:
            clauses.append("provider = ?")
            args.append(provider)
        if kind in ("video", "photo"):
            clauses.append(_IS_VIDEO if kind == "video" else f"NOT {_IS_VIDEO}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._db.execute(
                f"SELECT rowid AS id, {LIBRARY_COLUMNS} FROM files {where}"
                " ORDER BY created DESC, rowid DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
            total = self._db.execute(f"SELECT COUNT(*) FROM files {where}", args).fetchone()[0]
        return [dict(r) for r in rows], total

    def library_providers(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT DISTINCT provider FROM files ORDER BY provider")]

    def library_item(self, item_id: int) -> dict | None:
        with self._lock:
            row = self._db.execute(
                f"SELECT rowid AS id, {LIBRARY_COLUMNS} FROM files WHERE rowid = ?",
                (item_id,),
            ).fetchone()
        return dict(row) if row else None

    def totals(self) -> dict:
        with self._lock:
            row = self._db.execute("SELECT COUNT(*) AS files, COALESCE(SUM(size), 0) AS bytes FROM files").fetchone()
        return dict(row)
