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


async def update_jamendo_catalog(client_id: str, limit: int = 200) -> tuple[int, int]:
    """Fetch broad Jamendo metadata, save the full catalog, and register only eligible tracks.

    Returns (metadata rows fetched, tracks eligible for playlist/download use).
    """
    if not client_id.strip():
        raise ValueError("JAMENDO_CLIENT_ID is empty. Add it to .env first.")
    if not DB_PATH.exists():
        raise FileNotFoundError(f"DB not found: {DB_PATH}. Run collect.py first.")

    limit = min(max(int(limit), 1), 200)
    base_params = {
        "client_id": client_id.strip(),
        "format": "json",
        "limit": limit,
        "include": "licenses musicinfo",
        "order": "popularity_total",
    }
    async with httpx.AsyncClient(timeout=45, follow_redirects=True) as client:
        # Prefer tracks with license conditions compatible with commercial video use.
        eligible_params = {**base_params, "ccnc": "false", "ccnd": "false", "ccsa": "false"}
        response = await client.get(JAMENDO_API, params=eligible_params)
        response.raise_for_status()
        eligible_payload = response.json()
        eligible_headers = eligible_payload.get("headers") or {}
        if str(eligible_headers.get("status", "")).lower() == "error" or eligible_headers.get("error_message"):
            raise RuntimeError(f"Jamendo API 오류: {eligible_headers.get('error_message') or eligible_headers}")
        eligible_results = eligible_payload.get("results") or []

        # Always fetch a broad list too, so the catalog remains useful even if no
        # commercially compatible tracks are returned by the license filters.
        response = await client.get(JAMENDO_API, params=base_params)
        response.raise_for_status()
        payload = response.json()
        headers = payload.get("headers") or {}
        if str(headers.get("status", "")).lower() == "error" or headers.get("error_message"):
            raise RuntimeError(f"Jamendo API 오류: {headers.get('error_message') or headers}")
        broad_results = payload.get("results") or []
        # Put license-filtered results first, then fill remaining slots with broad metadata.
        results = list(eligible_results) + list(broad_results)
        if not results:
            raise RuntimeError(
                "Jamendo API 응답은 성공했지만 결과가 0곡입니다. "
                f"results_count={headers.get('results_count', 0)}, "
                f"message={headers.get('error_message') or '없음'}. "
                "Client ID가 정확한지, Jamendo 개발자 포털에서 API 앱이 활성화됐는지 확인하세요."
            )

    # Mark the filtered API response as eligible, then merge it with broad metadata.
    eligible_ids = {str(t.get("id")) for t in eligible_results if t.get("id")}
    now = datetime.now().isoformat(timespec="seconds")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _ensure_columns(conn)
    catalog = []
    eligible_count = 0
    seen_ids = set()

    for track in results:
        if len(catalog) >= limit:
            break
        track_id_raw = track.get("id")
        if not track_id_raw:
            continue
        track_id_text = str(track_id_raw)
        if track_id_text in seen_ids:
            continue
        seen_ids.add(track_id_text)

        license_url = track.get("license_ccurl") or ""
        download_url = track.get("audiodownload") or ""
        download_allowed = track.get("audiodownload_allowed") is True and bool(download_url)
        license_ok = _is_safe_cc_license(license_url)
        # License-filter endpoint is the first choice, but validate the returned
        # license locally as well rather than trusting only query parameters.
        eligible = track_id_text in eligible_ids and license_ok and download_allowed

        musicinfo = track.get("musicinfo") or {}
        tags = musicinfo.get("tags") or {}
        genres = tags.get("genres") or ["Other"]
        moods = tags.get("vibe") or tags.get("moods") or ["Calm"]
        instruments = tags.get("instruments") or []
        if isinstance(genres, str): genres = [genres]
        if isinstance(moods, str): moods = [moods]
        if isinstance(instruments, str): instruments = [instruments]

        catalog.append({
            "track_id": f"jamendo:{track_id_text}",
            "title": track.get("name") or "Unknown",
            "artist": track.get("artist_name") or "Unknown",
            "duration": int(float(track.get("duration") or 0)),
            "genres": genres,
            "moods": moods,
            "instruments": instruments,
            "license_url": license_url,
            "license_ok_for_default_filter": license_ok,
            "download_allowed": download_allowed,
            "eligible_for_playlist": eligible,
            "source_url": track.get("shareurl") or f"https://www.jamendo.com/track/{track_id_text}",
        })

        # Keep all metadata in JSON, but only add tracks meeting the default
        # license/download checks to the playable DB candidate pool.
        if not eligible:
            continue

        raw = dict(track)
        conn.execute("""
            INSERT INTO tracks (
                track_id, title, artist, duration, genres, moods, instruments,
                license_type, external_source, external_download_url,
                external_license_url, external_source_url, raw_json, collected_at,
                external_eligible
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(track_id) DO UPDATE SET
                title=excluded.title, artist=excluded.artist,
                duration=excluded.duration, genres=excluded.genres,
                moods=excluded.moods, instruments=excluded.instruments,
                license_type=excluded.license_type,
                external_source=excluded.external_source,
                external_download_url=excluded.external_download_url,
                external_license_url=excluded.external_license_url,
                external_source_url=excluded.external_source_url,
                raw_json=excluded.raw_json, collected_at=excluded.collected_at,
                external_eligible=1
        """, (
            f"jamendo:{track_id_text}", track.get("name") or "Unknown",
            track.get("artist_name") or "Unknown", int(float(track.get("duration") or 0)),
            _safe_json(genres), _safe_json(moods), _safe_json(instruments),
            license_url, "jamendo", download_url, license_url,
            track.get("shareurl") or f"https://www.jamendo.com/track/{track_id_text}",
            _safe_json(raw), now,
        ))
        eligible_count += 1

    conn.commit()
    conn.close()
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CATALOG_PATH.write_text(json.dumps({
        "updated_at": now,
        "source": "Jamendo API",
        "requested_limit": limit,
        "metadata_count": len(catalog),
        "eligible_count": eligible_count,
        "tracks": catalog,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return len(catalog), eligible_count

