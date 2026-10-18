"""Local drop-in folder provider.

Put any audio you downloaded yourself into ``library/inbox/`` (sub-folders are scanned).
Folder names become hints: ``inbox/Jazz/Calm/song.mp3`` → genre Jazz, mood calm.
A folder called ``pixabay`` marks files as Pixabay-licensed. An optional ``song.json`` next to
``song.mp3`` may carry ``title, artist, genre, mood, license, license_url, source_url, vocals``.
"""

from __future__ import annotations

import json
from pathlib import Path

from music_crawler import licenses
from music_crawler.audio import AUDIO_EXTENSIONS, AudioError, file_hash, probe
from music_crawler.config import Settings
from music_crawler.db import Database
from music_crawler.models import Track
from music_crawler.providers.base import SyncReport
from music_crawler.taxonomy import (
    classify_text,
    detect_vocals,
    normalize_genres,
    normalize_moods,
)


def _as_list(value) -> list[str]:
    if not value:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _license_from_sidecar(value: str, folder_names: set[str]) -> tuple[str, str]:
    value = (value or "").strip()
    if value.startswith("http"):
        return licenses.from_cc_url(value), value
    if value:
        return value.lower().replace(" ", "-"), ""
    if "pixabay" in folder_names:
        return "pixabay", ""
    return "user", ""


def sync(settings: Settings, db: Database, root: Path | None = None) -> SyncReport:
    report = SyncReport("local")
    root = Path(root or settings.inbox_dir)
    if not root.exists():
        report.notes.append(f"폴더가 없습니다: {root}")
        return report
    known_hashes = {
        r[0]
        for r in db.conn.execute("SELECT content_hash FROM tracks WHERE content_hash IS NOT NULL")
    }
    batch: list[Track] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        report.fetched += 1
        try:
            info = probe(settings, path)
            digest = file_hash(path)
        except (AudioError, OSError):
            report.drop("unreadable")
            continue
        if digest in known_hashes and not db.get(f"local:{digest[:12]}"):
            report.drop("duplicate-file")
            continue
        known_hashes.add(digest)

        meta: dict = {}
        sidecar = path.with_suffix(".json")
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                report.notes.append(f"사이드카 JSON을 읽지 못했습니다: {sidecar.name}")
        folders = [p for p in path.relative_to(root).parts[:-1]]
        folder_names = {f.lower() for f in folders}

        code, url = _license_from_sidecar(str(meta.get("license") or ""), folder_names)
        url = str(meta.get("license_url") or url)
        if not licenses.is_allowed(code, settings.allowed_licenses):
            report.drop(f"license:{code}")
            continue
        duration = int(meta.get("duration") or info["duration"])
        if duration < settings.min_track_seconds:
            report.drop("too-short")
            continue

        title = str(meta.get("title") or info["tags"].get("title") or path.stem).strip()
        artist = str(meta.get("artist") or info["tags"].get("artist") or "").strip()
        hint_text = " ".join(folders)
        folder_genres, folder_moods = classify_text(hint_text)
        genres = (
            normalize_genres(_as_list(meta.get("genre") or meta.get("genres")))
            or normalize_genres([info["tags"].get("genre", "")])
            or folder_genres
        )
        moods = normalize_moods(_as_list(meta.get("mood") or meta.get("moods"))) or folder_moods
        batch.append(
            Track(
                id=f"local:{digest[:12]}",
                source="local",
                source_id=digest[:12],
                title=title,
                artist=artist,
                duration=duration,
                genres=genres or ["Other"],
                moods=moods,
                vocals=detect_vocals(title, [], meta.get("vocals")),
                license=code,
                license_url=url,
                source_url=str(meta.get("source_url") or ""),
                file_path=settings.to_stored(path),
                content_hash=digest,
            )
        )
    ins, upd = db.upsert_tracks(batch)
    # upsert never touches file_path of existing rows; refresh it for moved files
    for t in batch:
        db.set_file(t.id, t.file_path, t.duration, t.content_hash)
    report.inserted, report.updated = ins, upd
    return report
