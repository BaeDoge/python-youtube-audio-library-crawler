"""Jamendo provider (https://developer.jamendo.com/v3.0/tracks).

Why this differs from the old collector: instead of downloading ~10,000 tracks of *every*
genre and throwing 99 % away on the license filter, we query **per tag** with
``vocalinstrumental=instrumental`` so every page is already on-target, and we read the real
``musicinfo.tags.vartags`` for mood (the old code looked for a non-existent ``vibe`` key and
labelled every track "Calm").
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from music_crawler import licenses
from music_crawler.config import Settings
from music_crawler.db import Database
from music_crawler.models import Track
from music_crawler.providers.base import SyncReport
from music_crawler.taxonomy import Profile, detect_vocals, normalize_genres, normalize_moods

API = "https://api.jamendo.com/v3.0/tracks/"
PAGE_SIZE = 200
ORDERS = ("popularity_total_desc", "releasedate_desc", "id_asc")


class JamendoError(RuntimeError):
    pass


def parse_track(
    item: dict, settings: Settings, hint_genre: str | None = None
) -> tuple[Track | None, str | None]:
    """Convert one API item to a Track, or return ``(None, reason)`` when it must be dropped."""
    raw_id = str(item.get("id") or "").strip()
    if not raw_id:
        return None, "no-id"
    code = licenses.from_cc_url(item.get("license_ccurl") or "")
    if not licenses.is_allowed(code, settings.allowed_licenses):
        return None, f"license:{code}"
    url = item.get("audiodownload") or ""
    if item.get("audiodownload_allowed") is not True or not url:
        return None, "download-not-allowed"
    try:
        duration = int(float(item.get("duration") or 0))
    except (TypeError, ValueError):
        duration = 0
    if duration < settings.min_track_seconds:
        return None, "too-short"
    if settings.max_track_seconds and duration > settings.max_track_seconds:
        return None, "too-long"

    info = item.get("musicinfo") or {}
    tag_block = info.get("tags") or {}
    genres = normalize_genres(tag_block.get("genres"))
    if not genres and hint_genre:
        genres = normalize_genres([hint_genre])
    var_tags = [str(t).lower() for t in tag_block.get("vartags") or []]
    instruments = [str(t).lower() for t in tag_block.get("instruments") or []]
    vocals = detect_vocals(item.get("name") or "", var_tags, info.get("vocalinstrumental"))

    return Track(
        id=f"jamendo:{raw_id}",
        source="jamendo",
        source_id=raw_id,
        title=(item.get("name") or "Unknown").strip(),
        artist=(item.get("artist_name") or "Unknown").strip(),
        duration=duration,
        genres=genres or ["Other"],
        moods=normalize_moods(var_tags),
        tags=sorted(set(var_tags + instruments)),
        vocals=vocals,
        license=code,
        license_url=item.get("license_ccurl") or "",
        source_url=item.get("shareurl") or f"https://www.jamendo.com/track/{raw_id}",
        download_url=url,
    ), None


def _get(client: httpx.Client, params: dict, sleep: Callable[[float], None]) -> dict:
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            response = client.get(API, params=params)
            if response.status_code == 429:
                sleep(2.0 * attempt)
                continue
            response.raise_for_status()
            payload = response.json()
            headers = payload.get("headers") or {}
            if str(headers.get("status", "success")).lower() in {"failed", "error"} or headers.get(
                "code"
            ) not in (None, 0, "0"):
                raise JamendoError(headers.get("error_message") or str(headers))
            return payload
        except JamendoError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
            sleep(0.75 * attempt)
    raise JamendoError(f"요청 실패(3회 재시도): {last}")


def sync(
    settings: Settings,
    db: Database,
    profile: Profile,
    *,
    per_tag_limit: int | None = 400,
    new_pages: int = 2,
    reset: bool = False,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] = lambda _msg: None,
) -> SyncReport:
    """Resumable crawl.

    For every tag the position in Jamendo's popularity ranking is stored (``sync_cursors``),
    so each run continues where the last one stopped and fetches *per_tag_limit more*
    tracks - the cost of a run is constant instead of growing with the catalog.
    ``per_tag_limit=None`` keeps going until every tag is exhausted (safe to Ctrl+C: the
    cursor is saved after every page). Each run also re-reads the ``new_pages`` newest pages
    per tag so freshly released tracks are picked up without a restart.
    """
    if not settings.jamendo_client_id:
        raise JamendoError("JAMENDO_CLIENT_ID가 비어 있습니다 (.env 확인).")
    report = SyncReport("jamendo")
    own_client = client is None
    client = client or httpx.Client(
        timeout=45, follow_redirects=True, headers={"User-Agent": settings.user_agent}
    )
    if reset:
        db.reset_cursors("jamendo")
        report.notes.append("진행 위치를 초기화하고 처음부터 다시 훑었습니다.")
    seen: set[str] = set()

    def fetch_page(tag: str, order: str, offset: int) -> list[dict]:
        params = {
            "client_id": settings.jamendo_client_id,
            "format": "json",
            "limit": PAGE_SIZE,
            "offset": offset,
            "order": order,
            "tags": tag,
            "include": "licenses musicinfo",
            "audiodlformat": "mp32",
            "type": "albumtrack single",
        }
        if profile.vocals == "strict":
            params["vocalinstrumental"] = "instrumental"
        return _get(client, params, sleep).get("results") or []

    def store(batch: list[dict], tag: str) -> int:
        kept: list[Track] = []
        fresh = 0
        for item in batch:
            raw_id = str(item.get("id") or "")
            if raw_id in seen:
                continue
            seen.add(raw_id)
            fresh += 1
            report.fetched += 1
            track, reason = parse_track(item, settings, hint_genre=tag)
            if track is None:
                report.drop(reason or "unknown")
            elif track.vocals == "vocal" and profile.vocals != "any":
                report.drop("vocal")
            else:
                kept.append(track)
        ins, upd = db.upsert_tracks(kept)
        report.inserted += ins
        report.updated += upd
        return fresh

    try:
        for tag in profile.jamendo_tags:
            before = report.inserted
            key = f"{tag}|popularity_total_desc"
            offset, exhausted = db.get_cursor("jamendo", key)
            start_offset, got = offset, 0
            while not exhausted and (per_tag_limit is None or got < per_tag_limit):
                batch = fetch_page(tag, "popularity_total_desc", offset)
                got += store(batch, tag)
                offset += len(batch)
                exhausted = len(batch) < PAGE_SIZE
                db.set_cursor("jamendo", key, offset, exhausted)  # after the upsert: crash-safe
                if not exhausted:
                    sleep(0.2)
            for page in range(new_pages):  # newest releases, constant cost
                batch = fetch_page(tag, "releasedate_desc", page * PAGE_SIZE)
                store(batch, tag)
                if len(batch) < PAGE_SIZE:
                    break
                sleep(0.2)
            state = "끝까지 수집됨" if exhausted else f"offset {offset}까지"
            progress(
                f"  jamendo tag={tag}: {start_offset}→{offset} 구간 {got}곡 확인, "
                f"신규 저장 {report.inserted - before}곡 ({state})"
            )
    finally:
        if own_client:
            client.close()
    return report
