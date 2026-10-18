"""SQLite storage. One ``tracks`` table for every source plus usage bookkeeping."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from music_crawler.models import Track, norm

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    duration INTEGER NOT NULL DEFAULT 0,
    genres TEXT NOT NULL DEFAULT '[]',
    moods TEXT NOT NULL DEFAULT '[]',
    tags TEXT NOT NULL DEFAULT '[]',
    vocals TEXT NOT NULL DEFAULT 'unknown',
    license TEXT NOT NULL DEFAULT 'unknown',
    license_url TEXT NOT NULL DEFAULT '',
    attribution TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    download_url TEXT NOT NULL DEFAULT '',
    file_path TEXT,
    content_hash TEXT,
    status TEXT NOT NULL DEFAULT 'candidate',
    status_reason TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tracks_source ON tracks(source);
CREATE INDEX IF NOT EXISTS idx_tracks_status ON tracks(status);
CREATE TABLE IF NOT EXISTS usage (
    track_id TEXT NOT NULL,
    playlist_id TEXT NOT NULL,
    used_at TEXT NOT NULL,
    PRIMARY KEY (track_id, playlist_id)
);
CREATE TABLE IF NOT EXISTS sync_cursors (
    source TEXT NOT NULL,
    key TEXT NOT NULL,
    offset INTEGER NOT NULL DEFAULT 0,
    exhausted INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source, key)
);
CREATE TABLE IF NOT EXISTS used_titles (
    key TEXT PRIMARY KEY,
    raw TEXT NOT NULL
);
"""

_COLUMNS = (
    "id",
    "source",
    "source_id",
    "title",
    "artist",
    "duration",
    "genres",
    "moods",
    "tags",
    "vocals",
    "license",
    "license_url",
    "attribution",
    "source_url",
    "download_url",
    "file_path",
    "content_hash",
    "status",
    "status_reason",
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def row_to_track(row: sqlite3.Row) -> Track:
    return Track(
        id=row["id"],
        source=row["source"],
        source_id=row["source_id"],
        title=row["title"],
        artist=row["artist"],
        duration=row["duration"],
        genres=json.loads(row["genres"]),
        moods=json.loads(row["moods"]),
        tags=json.loads(row["tags"]),
        vocals=row["vocals"],
        license=row["license"],
        license_url=row["license_url"],
        attribution=row["attribution"],
        source_url=row["source_url"],
        download_url=row["download_url"],
        file_path=row["file_path"],
        content_hash=row["content_hash"],
        status=row["status"],
        status_reason=row["status_reason"],
    )


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- writes ----------------------------------------------------------
    def upsert_tracks(self, tracks: Iterable[Track]) -> tuple[int, int]:
        """Insert or refresh metadata. Never touches status/file_path of existing rows,
        so a banned or already-downloaded track keeps its state across re-syncs.
        Returns ``(inserted, updated)``."""
        inserted = updated = 0
        now = _now()
        for t in tracks:
            exists = self.conn.execute("SELECT 1 FROM tracks WHERE id=?", (t.id,)).fetchone()
            if exists:
                self.conn.execute(
                    """UPDATE tracks SET title=?, artist=?, duration=?, genres=?, moods=?, tags=?,
                       vocals=?, license=?, license_url=?, attribution=?, source_url=?,
                       download_url=?, updated_at=? WHERE id=?""",
                    (
                        t.title,
                        t.artist,
                        t.duration,
                        json.dumps(t.genres),
                        json.dumps(t.moods),
                        json.dumps(t.tags),
                        t.vocals,
                        t.license,
                        t.license_url,
                        t.attribution,
                        t.source_url,
                        t.download_url,
                        now,
                        t.id,
                    ),
                )
                updated += 1
            else:
                self.conn.execute(
                    f"INSERT INTO tracks ({','.join(_COLUMNS)}, added_at, updated_at) "
                    f"VALUES ({','.join('?' * len(_COLUMNS))}, ?, ?)",
                    (
                        t.id,
                        t.source,
                        t.source_id,
                        t.title,
                        t.artist,
                        t.duration,
                        json.dumps(t.genres),
                        json.dumps(t.moods),
                        json.dumps(t.tags),
                        t.vocals,
                        t.license,
                        t.license_url,
                        t.attribution,
                        t.source_url,
                        t.download_url,
                        t.file_path,
                        t.content_hash,
                        t.status,
                        t.status_reason,
                        now,
                        now,
                    ),
                )
                inserted += 1
        self.conn.commit()
        return inserted, updated

    def set_file(
        self,
        track_id: str,
        path: str | None,
        duration: int | None = None,
        content_hash: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE tracks SET file_path=?, content_hash=COALESCE(?, content_hash), "
            "duration=COALESCE(?, duration), updated_at=? WHERE id=?",
            (path, content_hash, duration, _now(), track_id),
        )
        self.conn.commit()

    def set_status(self, track_id: str, status: str, reason: str = "") -> None:
        self.conn.execute(
            "UPDATE tracks SET status=?, status_reason=?, updated_at=? WHERE id=?",
            (status, reason, _now(), track_id),
        )
        self.conn.commit()

    def record_usage(self, track_ids: Iterable[str], playlist_id: str) -> None:
        now = _now()
        self.conn.executemany(
            "INSERT OR IGNORE INTO usage (track_id, playlist_id, used_at) VALUES (?, ?, ?)",
            [(tid, playlist_id, now) for tid in track_ids],
        )
        self.conn.commit()

    def get_cursor(self, source: str, key: str) -> tuple[int, bool]:
        row = self.conn.execute(
            "SELECT offset, exhausted FROM sync_cursors WHERE source=? AND key=?", (source, key)
        ).fetchone()
        return (row["offset"], bool(row["exhausted"])) if row else (0, False)

    def set_cursor(self, source: str, key: str, offset: int, exhausted: bool) -> None:
        self.conn.execute(
            "INSERT INTO sync_cursors (source, key, offset, exhausted, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(source, key) DO UPDATE SET "
            "offset=excluded.offset, exhausted=excluded.exhausted, updated_at=excluded.updated_at",
            (source, key, offset, int(exhausted), _now()),
        )
        self.conn.commit()

    def reset_cursors(self, source: str) -> None:
        self.conn.execute("DELETE FROM sync_cursors WHERE source=?", (source,))
        self.conn.commit()

    def add_used_titles(self, raws: Iterable[str]) -> int:
        n = 0
        for raw in raws:
            key = norm(raw)
            if not key:
                continue
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO used_titles (key, raw) VALUES (?, ?)", (key, raw.strip())
            )
            n += cur.rowcount
        self.conn.commit()
        return n

    # ---- reads -----------------------------------------------------------
    def get(self, track_id: str) -> Track | None:
        row = self.conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
        return row_to_track(row) if row else None

    def find(self, text: str, limit: int = 10) -> list[Track]:
        like = f"%{text}%"
        rows = self.conn.execute(
            "SELECT * FROM tracks WHERE id=? OR title LIKE ? OR artist LIKE ? LIMIT ?",
            (text, like, like, limit),
        ).fetchall()
        return [row_to_track(r) for r in rows]

    def all_tracks(
        self, *, sources: Iterable[str] | None = None, status: str | None = "candidate"
    ) -> list[Track]:
        sql, params = "SELECT * FROM tracks WHERE 1=1", []
        if status:
            sql += " AND status=?"
            params.append(status)
        if sources:
            sources = list(sources)
            sql += f" AND source IN ({','.join('?' * len(sources))})"
            params.extend(sources)
        return [row_to_track(r) for r in self.conn.execute(sql, params)]

    def used_track_ids(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT DISTINCT track_id FROM usage")}

    def used_title_keys(self) -> set[str]:
        return {r[0] for r in self.conn.execute("SELECT key FROM used_titles")}

    def count_by_source(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT source, COUNT(*) FROM tracks WHERE status='candidate' GROUP BY source"
        )
        return {r[0]: r[1] for r in rows}
