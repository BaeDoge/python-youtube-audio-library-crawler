"""External music catalog helpers for music.py.

Remote catalog: Jamendo API (requires a developer client_id).
Local files: external_music/ (recursive scan; these are prioritized).
Only CC BY and CC0 remote tracks are accepted by default. Check each license
before publishing; API commercial-use terms may require separate permission.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import httpx

from project_paths import (
    BASE_DIR, DB_PATH, EXTERNAL_MUSIC_DIR as LOCAL_DIR,
    CATALOG_PATH, migrate_legacy_paths,
)

migrate_legacy_paths()
JAMENDO_API = "https://api.jamendo.com/v3.0/tracks/"
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"}


def _safe_json(value):
    return json.dumps(value, ensure_ascii=False)


def _duration_with_ffprobe(path: Path) -> int:
    import shutil
    import subprocess

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return max(0, int(float(result.stdout.strip()))) if result.returncode == 0 else 0
    except Exception:
        return 0


def _metadata_for_file(path: Path) -> dict:
    """Read optional same-name .json sidecar; infer tags from parent folders."""
    sidecar = path.with_suffix(".json")
    data = {}
    if sidecar.exists():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    title = data.get("title") or path.stem
    artist = data.get("artist") or "Local file"
    # Folder convention: external_music/<genre>/<mood>/track.mp3
    relative = path.relative_to(LOCAL_DIR)
    parts = relative.parts[:-1]
    genre = data.get("genre") or (parts[0] if len(parts) >= 1 else "Other")
    mood = data.get("mood") or (parts[1] if len(parts) >= 2 else "Calm")
    duration = int(data.get("duration") or _duration_with_ffprobe(path) or 0)
    return {
        "title": str(title), "artist": str(artist), "duration": duration,
        "genre": str(genre), "mood": str(mood),
        "license_type": str(data.get("license_type") or "USER_PROVIDED_VERIFY_LICENSE"),
        "license_url": str(data.get("license_url") or ""),
        "source_url": str(data.get("source_url") or ""),
    }


def _ensure_columns(conn):
    existing = {r[1] for r in conn.execute("PRAGMA table_info(tracks)")}
    additions = {
        "external_source": "TEXT",
        "external_download_url": "TEXT",
        "external_license_url": "TEXT",
        "external_source_url": "TEXT",
        "local_file_path": "TEXT",
        "external_eligible": "INTEGER NOT NULL DEFAULT 1",
    }
    for name, definition in additions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE tracks ADD COLUMN {name} {definition}")


def scan_local_music() -> tuple[int, int]:
    """Register manually downloaded files. Returns (registered, skipped)."""
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    if not DB_PATH.exists():
        raise FileNotFoundError(f"DB not found: {DB_PATH}. Run collect.py first.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_columns(conn)
    registered = skipped = 0
    now = datetime.now().isoformat(timespec="seconds")
    for path in LOCAL_DIR.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue
        meta = _metadata_for_file(path)
        if meta["duration"] <= 0:
            skipped += 1
            continue
        digest = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
        track_id = f"local:{digest}"
        conn.execute("""
            INSERT INTO tracks (
                track_id, title, artist, duration, genres, moods, instruments,
                license_type, external_source, external_license_url,
                external_source_url, local_file_path, raw_json, collected_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(track_id) DO UPDATE SET
                title=excluded.title, artist=excluded.artist,
                duration=excluded.duration, genres=excluded.genres,
                moods=excluded.moods, license_type=excluded.license_type,
                external_license_url=excluded.external_license_url,
                external_source_url=excluded.external_source_url,
                local_file_path=excluded.local_file_path,
                raw_json=excluded.raw_json, collected_at=excluded.collected_at
        """, (
            track_id, meta["title"], meta["artist"], meta["duration"],
            _safe_json([meta["genre"]]), _safe_json([meta["mood"]]), _safe_json([]),
            meta["license_type"], "local", meta["license_url"], meta["source_url"],
            str(path.resolve()), _safe_json(meta), now,
        ))
        conn.execute("""
            INSERT INTO downloaded_tracks (track_id, title, artist, file_path, downloaded_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(track_id) DO UPDATE SET
                title=excluded.title, artist=excluded.artist,
                file_path=excluded.file_path, downloaded_at=excluded.downloaded_at
        """, (track_id, meta["title"], meta["artist"], str(path.resolve()), now))
        registered += 1
    conn.commit()
    conn.close()
    return registered, skipped


def _is_safe_cc_license(url: str) -> bool:
    """Allow only CC0 and CC BY; exclude NC, ND, and ShareAlike licenses."""
    normalized = (url or "").lower().rstrip("/")
    return normalized.endswith("/zero/1.0") or bool(
        re.search(r"creativecommons\.org/licenses/by/(?:1\.0|2\.0|2\.5|3\.0|4\.0)$", normalized)
    )


def _load_catalog_sources() -> dict:
    """Load existing multi-provider catalog without discarding other sources."""
    existing_doc = {}
    if CATALOG_PATH.exists():
        try:
            existing_doc = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        except Exception:
            existing_doc = {}
    sources = existing_doc.get("sources") or {}
    if not sources and existing_doc.get("tracks"):
        old_source = str(existing_doc.get("source") or "unknown").lower()
        old_key = "jamendo" if "jamendo" in old_source else old_source.replace(" api", "").replace(" ", "_")
        sources[old_key] = {
            "updated_at": existing_doc.get("updated_at", ""),
            "source": existing_doc.get("source", old_source),
            "metadata_count": existing_doc.get("metadata_count", len(existing_doc.get("tracks", []))),
            "eligible_count": existing_doc.get("eligible_count", 0),
            "tracks": existing_doc.get("tracks", []),
        }
    return sources


def _save_catalog_source(source_key: str, source_name: str, tracks: list[dict], eligible_count: int, extra=None) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    sources = _load_catalog_sources()
    source_doc = {
        "updated_at": now,
        "source": source_name,
        "metadata_count": len(tracks),
        "eligible_count": int(eligible_count),
        "tracks": tracks,
    }
    if extra:
        source_doc.update(extra)
    sources[source_key] = source_doc
    combined_tracks = [track for data in sources.values() for track in (data.get("tracks") or [])]
    combined_eligible = sum(int(data.get("eligible_count") or 0) for data in sources.values())
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(json.dumps({
        "updated_at": now,
        "source": "multiple",
        "metadata_count": len(combined_tracks),
        "eligible_count": combined_eligible,
        "sources": sources,
        "tracks": combined_tracks,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


async def update_jamendo_catalog(client_id: str, limit: int = 2000) -> tuple[int, int]:
    """Paginate Jamendo metadata in batches of <=200.

    `limit` is the desired total metadata count across all API pages, not the
    per-request limit. Returns (new catalog metadata count, eligible count).
    Only CC0/CC BY tracks with explicit audio-download permission enter DB.
    """
    if not client_id.strip():
        raise ValueError("JAMENDO_CLIENT_ID is empty. Add it to .env first.")
    if not DB_PATH.exists():
        raise FileNotFoundError(f"DB not found: {DB_PATH}. Run collect.py first.")

    total_limit = max(1, min(int(limit), 20000))
    page_size = 200  # Jamendo API maximum per request
    fetched: list[dict] = []
    seen: set[str] = set()
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        offset = 0
        while len(fetched) < total_limit:
            request_limit = min(page_size, total_limit - len(fetched))
            params = {
                "client_id": client_id.strip(), "format": "json",
                "limit": request_limit, "offset": offset,
                "include": "licenses musicinfo", "order": "popularity_total",
                "type": "albumtrack single", "audiodlformat": "mp32",
            }
            response = await client.get(JAMENDO_API, params=params)
            response.raise_for_status()
            payload = response.json()
            headers = payload.get("headers") or {}
            if str(headers.get("status", "")).lower() == "error" or headers.get("error_message"):
                raise RuntimeError(f"Jamendo API 오류 (offset={offset}): {headers.get('error_message') or headers}")
            batch = payload.get("results") or []
            if not batch:
                break
            new_in_batch = 0
            for track in batch:
                track_id = str(track.get("id") or "")
                if not track_id or track_id in seen:
                    continue
                seen.add(track_id)
                fetched.append(track)
                new_in_batch += 1
                if len(fetched) >= total_limit:
                    break
            print(f"Jamendo 수집 진행: {len(fetched):,}/{total_limit:,}곡 (offset={offset})")
            if len(batch) < request_limit or new_in_batch == 0:
                break
            offset += len(batch)

    if not fetched:
        raise RuntimeError("Jamendo API 응답 결과가 0곡입니다. Client ID와 API 앱 상태를 확인하세요.")

    now = datetime.now().isoformat(timespec="seconds")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_columns(conn)
    catalog: list[dict] = []
    eligible_count = 0
    for track in fetched:
        raw_id = str(track.get("id") or "")
        license_url = track.get("license_ccurl") or ""
        download_url = track.get("audiodownload") or ""
        duration = int(float(track.get("duration") or 0))
        download_allowed = track.get("audiodownload_allowed") is True and bool(download_url)
        license_ok = _is_safe_cc_license(license_url)
        eligible = license_ok and download_allowed and duration > 0
        musicinfo = track.get("musicinfo") or {}
        tags = musicinfo.get("tags") or {}
        genres = tags.get("genres") or ["Other"]
        moods = tags.get("vibe") or tags.get("moods") or ["Calm"]
        instruments = tags.get("instruments") or []
        if isinstance(genres, str): genres = [genres]
        if isinstance(moods, str): moods = [moods]
        if isinstance(instruments, str): instruments = [instruments]
        track_id = f"jamendo:{raw_id}"
        catalog.append({
            "track_id": track_id, "title": track.get("name") or "Unknown",
            "artist": track.get("artist_name") or "Unknown", "duration": duration,
            "genres": genres, "moods": moods, "instruments": instruments,
            "license_url": license_url, "license_ok_for_default_filter": license_ok,
            "download_allowed": download_allowed, "eligible_for_playlist": eligible,
            "source_url": track.get("shareurl") or f"https://www.jamendo.com/track/{raw_id}",
        })
        if not eligible:
            continue
        conn.execute("""
            INSERT INTO tracks (
                track_id, title, artist, duration, genres, moods, instruments,
                license_type, external_source, external_download_url,
                external_license_url, external_source_url, raw_json, collected_at,
                external_eligible
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(track_id) DO UPDATE SET
                title=excluded.title, artist=excluded.artist, duration=excluded.duration,
                genres=excluded.genres, moods=excluded.moods, instruments=excluded.instruments,
                license_type=excluded.license_type, external_source=excluded.external_source,
                external_download_url=excluded.external_download_url,
                external_license_url=excluded.external_license_url,
                external_source_url=excluded.external_source_url, raw_json=excluded.raw_json,
                collected_at=excluded.collected_at, external_eligible=1
        """, (track_id, track.get("name") or "Unknown", track.get("artist_name") or "Unknown",
              duration, _safe_json(genres), _safe_json(moods), _safe_json(instruments),
              license_url, "jamendo", download_url, license_url,
              track.get("shareurl") or f"https://www.jamendo.com/track/{raw_id}",
              _safe_json(track), now))
        eligible_count += 1
    conn.commit()
    conn.close()
    _save_catalog_source("jamendo", "Jamendo API", catalog, eligible_count, {
        "requested_limit": total_limit, "page_size": page_size,
        "pages_fetched": (len(fetched) + page_size - 1) // page_size,
    })
    return len(catalog), eligible_count


async def update_openverse_catalog(limit: int = 1000, query: str = "instrumental music") -> tuple[int, int]:
    """Collect openly licensed audio metadata from Openverse.

    Only CC0/CC BY tracks with a direct URL and positive duration are inserted
    as playlist candidates. Metadata records remain in the JSON catalog even
    when their duration/download URL is unavailable.
    """
    if not DB_PATH.exists():
        raise FileNotFoundError(f"DB not found: {DB_PATH}. Run collect.py first.")
    total_limit = max(1, min(int(limit), 5000))
    page_size = 20
    endpoint = "https://api.openverse.org/v1/audio/"
    fetched: list[dict] = []
    seen: set[str] = set()
    async with httpx.AsyncClient(timeout=45, follow_redirects=True, headers={"User-Agent": "MusicManager/1.0 (open-license audio catalog)"}) as client:
        page = 1
        while len(fetched) < total_limit:
            params = {
                "q": query.strip() or "instrumental music",
                "license": "cc0,by", "license_type": "commercial",
                "page": page, "page_size": min(page_size, total_limit - len(fetched)),
                "filter_dead": "true", "mature": "false",
            }
            response = await client.get(endpoint, params=params)
            if response.status_code == 429:
                raise RuntimeError("Openverse API rate limit(429)에 도달했습니다. 잠시 후 다시 시도하세요.")
            response.raise_for_status()
            payload = response.json()
            results = payload.get("results") or []
            if not results:
                break
            new_count = 0
            for item in results:
                identifier = str(item.get("id") or item.get("foreign_landing_url") or item.get("url") or "")
                if not identifier or identifier in seen:
                    continue
                seen.add(identifier)
                fetched.append(item)
                new_count += 1
                if len(fetched) >= total_limit:
                    break
            print(f"Openverse 수집 진행: {len(fetched):,}/{total_limit:,}곡 (page={page})")
            page_count = int(payload.get("page_count") or 0)
            has_next = bool(payload.get("next")) or (page_count > 0 and page < page_count)
            if new_count == 0 or not has_next:
                break
            page += 1

    if not fetched:
        raise RuntimeError("Openverse 검색 결과가 0곡입니다. 검색어를 바꾸거나 잠시 후 다시 시도하세요.")

    now = datetime.now().isoformat(timespec="seconds")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_columns(conn)
    catalog: list[dict] = []
    eligible_count = 0
    for item in fetched:
        raw_id = str(item.get("id") or hashlib.sha1(str(item.get("url") or item.get("foreign_landing_url") or "").encode()).hexdigest()[:16])
        title = item.get("title") or "Unknown"
        artist = item.get("creator") or "Unknown"
        duration_value = item.get("duration")
        try:
            duration = int(float(duration_value or 0))
        except (TypeError, ValueError):
            duration = 0
        license_code = str(item.get("license") or "").lower()
        license_url = item.get("license_url") or (f"https://creativecommons.org/licenses/{license_code}/4.0/" if license_code == "by" else "https://creativecommons.org/publicdomain/zero/1.0/" if license_code == "cc0" else "")
        download_url = item.get("url") or ""
        source_url = item.get("foreign_landing_url") or item.get("detail_url") or ""
        # Be conservative: allow CC0 and CC BY only, and require a usable URL/duration.
        license_ok = license_code in {"cc0", "by"}
        eligible = license_ok and bool(download_url) and bool(source_url) and duration > 0
        tags = item.get("tags") or []
        if isinstance(tags, list):
            tag_names = [str(tag.get("name") if isinstance(tag, dict) else tag) for tag in tags]
        else:
            tag_names = [str(tags)]
        genres = item.get("genres") or tag_names or ["Other"]
        if isinstance(genres, str): genres = [genres]
        moods = ["Calm"]
        track_id = f"openverse:{raw_id}"
        catalog.append({
            "track_id": track_id, "title": title, "artist": artist,
            "duration": duration, "genres": genres, "moods": moods,
            "license": license_code, "license_url": license_url,
            "license_ok_for_default_filter": license_ok,
            "download_allowed": bool(download_url), "eligible_for_playlist": eligible,
            "source_url": source_url, "download_url": download_url,
            "provider": item.get("provider") or "Openverse",
            "attribution": item.get("attribution") or "",
        })
        if not eligible:
            continue
        conn.execute("""
            INSERT INTO tracks (
                track_id, title, artist, duration, genres, moods, instruments,
                license_type, external_source, external_download_url,
                external_license_url, external_source_url, raw_json, collected_at,
                external_eligible
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(track_id) DO UPDATE SET
                title=excluded.title, artist=excluded.artist, duration=excluded.duration,
                genres=excluded.genres, moods=excluded.moods, license_type=excluded.license_type,
                external_source=excluded.external_source, external_download_url=excluded.external_download_url,
                external_license_url=excluded.external_license_url, external_source_url=excluded.external_source_url,
                raw_json=excluded.raw_json, collected_at=excluded.collected_at, external_eligible=1
        """, (track_id, title, artist, duration, _safe_json(genres), _safe_json(moods), _safe_json([]),
              license_url, "openverse", download_url, license_url, source_url,
              _safe_json(item), now))
        eligible_count += 1
    conn.commit()
    conn.close()
    _save_catalog_source("openverse", "Openverse API", catalog, eligible_count, {
        "requested_limit": total_limit, "query": query.strip() or "instrumental music",
        "license_filter": "cc0,by", "license_type_filter": "commercial",
    })
    return len(catalog), eligible_count

