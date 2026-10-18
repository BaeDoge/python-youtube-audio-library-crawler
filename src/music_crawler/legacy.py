"""Import data from the old flat-layout project (``tracks.db``, ``used.txt``, audio files)."""

from __future__ import annotations

import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from music_crawler import audio
from music_crawler.audio import AUDIO_EXTENSIONS
from music_crawler.config import Settings
from music_crawler.db import Database
from music_crawler.downloader import target_path
from music_crawler.models import Track
from music_crawler.providers import jamendo, openverse, youtube_library

_AUDIO_DIRS = (
    "output/downloads",
    "downloads",
    "external_music",
    "assets/external_music",
    "music_library",
)


@dataclass
class MigrationReport:
    tracks: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    files_copied: int = 0
    files_missing: int = 0
    usage_rows: int = 0
    used_titles: int = 0
    inbox_copied: int = 0

    def lines(self) -> list[str]:
        out = [f"곡 메타데이터 {self.tracks:,}곡 가져옴"]
        if self.skipped:
            out.append(
                "    제외: " + ", ".join(f"{k} {v}" for k, v in sorted(self.skipped.items()))
            )
        out.append(
            f"다운로드 파일 복사 {self.files_copied}개 (원본을 찾지 못함 {self.files_missing}개)"
        )
        out.append(f"사용 이력 {self.usage_rows}곡, used.txt 제목 {self.used_titles}개")
        if self.inbox_copied:
            out.append(
                f"직접 받은 음원 {self.inbox_copied}개를 library/inbox/legacy 로 복사 "
                "(`sync local` 로 등록)"
            )
        return out


def new_id(old_id: str) -> str:
    return old_id if ":" in old_id else f"youtube:{old_id}"


def _find_legacy_db(root: Path) -> Path | None:
    for candidate in (root / "data" / "tracks.db", root / "tracks.db"):
        if candidate.exists():
            return candidate
    return None


def _convert(row: sqlite3.Row, settings: Settings) -> tuple[Track | None, str | None]:
    source = row["external_source"] or "youtube"
    try:
        raw = json.loads(row["raw_json"] or "{}")
    except json.JSONDecodeError:
        raw = {}
    if source == "youtube":
        track = youtube_library.parse_track(raw) if raw else None
        return (track, None) if track else (None, "youtube:no-raw")
    if source == "jamendo":
        return jamendo.parse_track(raw, settings)
    if source == "openverse":
        return openverse.parse_track(raw, settings)
    return None, f"source:{source}"  # local files are re-scanned from the inbox instead


def _locate(old_path: str, legacy_root: Path, index: dict[str, Path]) -> Path | None:
    candidate = Path(old_path)
    if candidate.exists():
        return candidate
    rel = legacy_root / old_path.replace("\\", "/")
    if rel.exists():
        return rel
    return index.get(Path(old_path.replace("\\", "/")).name.lower())


def migrate(
    settings: Settings, db: Database, legacy_root: Path, *, copy_audio: bool = True
) -> MigrationReport:
    legacy_root = Path(legacy_root)
    report = MigrationReport()
    legacy_db = _find_legacy_db(legacy_root)
    if legacy_db is None:
        raise FileNotFoundError(f"이전 DB(data/tracks.db)를 찾을 수 없습니다: {legacy_root}")
    old = sqlite3.connect(f"file:{legacy_db}?mode=ro", uri=True)
    old.row_factory = sqlite3.Row

    batch: list[Track] = []
    for row in old.execute("SELECT * FROM tracks"):
        track, reason = _convert(row, settings)
        if track is None:
            report.skipped[reason or "unknown"] = report.skipped.get(reason or "unknown", 0) + 1
            continue
        batch.append(track)
        if len(batch) >= 1000:
            db.upsert_tracks(batch)
            report.tracks += len(batch)
            batch = []
    db.upsert_tracks(batch)
    report.tracks += len(batch)

    index: dict[str, Path] = {}
    for rel in _AUDIO_DIRS:
        base = legacy_root / rel
        if base.exists():
            for p in base.rglob("*"):
                if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS:
                    index.setdefault(p.name.lower(), p)

    tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "downloaded_tracks" in tables:
        for row in old.execute("SELECT track_id, file_path FROM downloaded_tracks"):
            tid = new_id(row["track_id"])
            track = db.get(tid)
            if track is None:
                continue
            src = _locate(row["file_path"] or "", legacy_root, index)
            if src is None:
                report.files_missing += 1
                continue
            if not copy_audio:
                continue
            dest = target_path(settings, track, src.suffix.lower() or ".mp3")
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copy2(src, dest)
            try:
                info = audio.probe(settings, dest)
                db.set_file(tid, settings.to_stored(dest), info["duration"], audio.file_hash(dest))
            except audio.AudioError:
                db.set_file(tid, settings.to_stored(dest))
            report.files_copied += 1

    if "track_usage" in tables:
        used = [
            new_id(r["track_id"])
            for r in old.execute("SELECT track_id FROM track_usage WHERE use_count > 0")
        ]
        db.record_usage(used, "legacy")
        report.usage_rows = len(used)
    old.close()

    used_file = legacy_db.parent / "used.txt"
    if not used_file.exists():
        used_file = legacy_root / "used.txt"
    if used_file.exists():
        lines = [ln.strip() for ln in used_file.read_text(encoding="utf-8").splitlines()]
        report.used_titles = db.add_used_titles(ln for ln in lines if ln and not ln.startswith("#"))

    if copy_audio:
        for rel in ("music_library", "external_music", "assets/external_music"):
            base = legacy_root / rel
            if not base.exists():
                continue
            for p in base.rglob("*"):
                if p.is_file() and (p.suffix.lower() in AUDIO_EXTENSIONS or p.suffix == ".json"):
                    dest = settings.inbox_dir / "legacy" / p.relative_to(base)
                    if not dest.exists():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(p, dest)
                        report.inbox_copied += p.suffix.lower() in AUDIO_EXTENSIONS
    return report
