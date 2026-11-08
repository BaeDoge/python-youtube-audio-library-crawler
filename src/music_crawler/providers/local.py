"""Local drop-in folder provider.

Put audio into library/inbox/<genre>/<mood>/. An optional source folder is supported:
library/inbox/Pixabay/Jazz/Calm/song.mp3. Custom folder labels are retained as metadata
when they are not part of the built-in taxonomy.
"""

from __future__ import annotations

import json
import re
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

_SOURCE_FOLDER_MARKERS = {
    "pixabay", "jamendo", "openverse", "youtube", "youtube-audio-library",
    "local", "manual", "user", "inbox", "download",
}
_MINOR_TITLE_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "for", "in", "nor",
    "of", "on", "or", "the", "to", "via",
}


def _as_list(value) -> list[str]:
    if not value:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _normalize_or_keep(values, normalizer) -> list[str]:
    """Normalize known taxonomy labels but preserve custom user folder/sidecar values."""
    out: list[str] = []
    for raw in _as_list(values):
        value = raw.strip()
        if not value:
            continue
        mapped = normalizer([value]) or [value]
        for item in mapped:
            if item.casefold() not in {existing.casefold() for existing in out}:
                out.append(item)
    return out


def _classification_folders(root: Path, path: Path) -> list[str]:
    folders = path.relative_to(root).parts[:-1]
    return [name for name in folders if name.strip().casefold() not in _SOURCE_FOLDER_MARKERS]


def _humanize_title_slug(value: str) -> str:
    words = [word.replace("_", " ").strip() for word in value.split("-") if word]
    result = []
    for index, word in enumerate(words):
        lowered = word.casefold()
        if index and lowered in _MINOR_TITLE_WORDS:
            result.append(lowered)
        else:
            result.append(word[:1].upper() + word[1:])
    return " ".join(result)


def pixabay_filename_metadata(filename: str) -> dict[str, str] | None:
    """Best-effort parse of Pixabay's common creator-title-numeric-ID filenames.

    The creator handle's original camel-case cannot be recovered from a lowercase filename;
    users can correct it through a same-name JSON sidecar.
    """
    stem = Path(filename).stem
    parts = stem.split("-")
    if len(parts) < 3 or not re.fullmatch(r"\d{5,8}", parts[-1]):
        return None
    creator_slug = parts[0].strip()
    title_slug = "-".join(parts[1:-1]).strip()
    if not creator_slug or not title_slug:
        return None
    creator = creator_slug.replace("_", " ")
    creator = " ".join(word[:1].upper() + word[1:] for word in creator.split())
    return {
        "artist": creator,
        "title": _humanize_title_slug(title_slug),
        "pixabay_id": parts[-1],
    }


def discover_local_categories(root: Path) -> tuple[list[str], list[str]]:
    """Return real genre/mood folder labels and sidecar labels for the GUI selectors."""
    root = Path(root)
    genres: dict[str, str] = {}
    moods: dict[str, str] = {}
    if not root.exists():
        return [], []

    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        folders = _classification_folders(root, path)
        if folders:
            for value in _normalize_or_keep([folders[0]], normalize_genres):
                genres.setdefault(value.casefold(), value)
        if len(folders) > 1:
            for value in _normalize_or_keep([folders[1]], normalize_moods):
                moods.setdefault(value.casefold(), value)

        sidecar = path.with_suffix(".json")
        if sidecar.exists():
            try:
                meta = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for value in _normalize_or_keep(meta.get("genre") or meta.get("genres"), normalize_genres):
                genres.setdefault(value.casefold(), value)
            for value in _normalize_or_keep(meta.get("mood") or meta.get("moods"), normalize_moods):
                moods.setdefault(value.casefold(), value)

    return (
        sorted(genres.values(), key=str.casefold),
        sorted(moods.values(), key=str.casefold),
    )


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

        folders = _classification_folders(root, path)
        folder_names = {f.lower() for f in path.relative_to(root).parts[:-1]}
        filename_meta = pixabay_filename_metadata(path.name)

        code, url = _license_from_sidecar(str(meta.get("license") or ""), folder_names)
        if code == "user" and filename_meta:
            # The creator-title-numeric-ID naming convention is a useful Pixabay hint.
            code = "pixabay"
        url = str(meta.get("license_url") or url)
        if not licenses.is_allowed(code, settings.allowed_licenses):
            report.drop(f"license:{code}")
            continue

        duration = int(meta.get("duration") or info["duration"])
        if duration < settings.min_track_seconds:
            report.drop("too-short")
            continue

        title = str(
            meta.get("title")
            or info["tags"].get("title")
            or (filename_meta or {}).get("title")
            or path.stem
        ).strip()
        artist = str(
            meta.get("artist")
            or info["tags"].get("artist")
            or (filename_meta or {}).get("artist")
            or ""
        ).strip()

        genre_from_folder = (
            _normalize_or_keep([folders[0]], normalize_genres) if folders else []
        )
        mood_from_folder = (
            _normalize_or_keep([folders[1]], normalize_moods) if len(folders) > 1 else []
        )
        inferred_genres, inferred_moods = classify_text(" ".join(folders))
        genres = (
            _normalize_or_keep(meta.get("genre") or meta.get("genres"), normalize_genres)
            or _normalize_or_keep([info["tags"].get("genre", "")], normalize_genres)
            or genre_from_folder
            or inferred_genres
            or ["Other"]
        )
        moods = (
            _normalize_or_keep(meta.get("mood") or meta.get("moods"), normalize_moods)
            or _normalize_or_keep([info["tags"].get("mood", "")], normalize_moods)
            or mood_from_folder
            or inferred_moods
        )
        tags = _as_list(meta.get("tags"))
        if filename_meta:
            tags.extend([
                "pixabay",
                f"pixabay-id:{filename_meta['pixabay_id']}",
            ])
            report.notes.append(
                f"Pixabay 파일명 인식: {path.name} → {artist} - {title} "
                f"(ID {filename_meta['pixabay_id']}; 표기 대소문자는 sidecar로 수정 가능)"
            )

        batch.append(
            Track(
                id=f"local:{digest[:12]}",
                source="local",
                source_id=digest[:12],
                title=title,
                artist=artist,
                duration=duration,
                genres=genres,
                moods=moods,
                tags=list(dict.fromkeys(tags)),
                vocals=detect_vocals(title, tags, meta.get("vocals")),
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
