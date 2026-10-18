"""Openverse audio provider (https://api.openverse.org/v1/audio/).

Fixes over the old collector:
* it used to run ``UPDATE ... SET external_eligible=0`` on *every* Openverse row before each
  refresh, so every new search erased the previous search's candidates;
* a 429 (rate limit) used to abort the run and discard everything fetched so far.
  Now each query is committed as soon as it finishes and a 429 ends the run gracefully.
* it now filters ``category=music`` and ignores sub-10-second sound-effect clips.

Anonymous access is capped at ~240 results per query, so we fan out over many specific
queries (see ``Profile.openverse_queries``). Register for free OAuth credentials and set
``OPENVERSE_CLIENT_ID`` / ``OPENVERSE_CLIENT_SECRET`` for higher rate limits.
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
from music_crawler.taxonomy import (
    Profile,
    classify_text,
    detect_vocals,
    normalize_genres,
    normalize_moods,
)

API = "https://api.openverse.org/v1/audio/"
TOKEN_URL = "https://api.openverse.org/v1/auth_tokens/token/"
PAGE_SIZE = 20
MAX_PAGES = 12  # anonymous depth limit (~240 results)


class RateLimited(RuntimeError):
    pass


def _tag_names(item: dict) -> list[str]:
    out = []
    for tag in item.get("tags") or []:
        name = tag.get("name") if isinstance(tag, dict) else tag
        if name:
            out.append(str(name).lower())
    return out


def parse_track(
    item: dict, settings: Settings, hint: dict | None = None
) -> tuple[Track | None, str | None]:
    hint = hint or {}
    raw_id = str(item.get("id") or "").strip()
    url = item.get("url") or ""
    if not raw_id or not url:
        return None, "no-url"
    code = licenses.from_openverse(item.get("license") or "", item.get("license_url") or "")
    if not licenses.is_allowed(code, settings.allowed_licenses):
        return None, f"license:{code}"
    try:
        duration = int(round(float(item.get("duration") or 0) / 1000))  # milliseconds
    except (TypeError, ValueError):
        duration = 0
    if duration < settings.min_track_seconds:
        return None, "too-short"
    if settings.max_track_seconds and duration > settings.max_track_seconds:
        return None, "too-long"
    category = (item.get("category") or "").lower()
    if category and category != "music":
        return None, f"category:{category}"

    title = (item.get("title") or "Unknown").strip()
    tags = _tag_names(item)
    genres = normalize_genres(item.get("genres") or []) or normalize_genres(tags)
    moods = normalize_moods(tags)
    text_genres, text_moods = classify_text(" ".join([title] + tags))
    genres = genres or text_genres
    moods = moods or text_moods
    # Fall back to the search query that surfaced the track (clearly an approximation).
    genres = genres or normalize_genres(hint.get("genres"))
    moods = moods or normalize_moods(hint.get("moods"))
    return Track(
        id=f"openverse:{raw_id}",
        source="openverse",
        source_id=raw_id,
        title=title,
        artist=(item.get("creator") or "Unknown").strip(),
        duration=duration,
        genres=genres or ["Other"],
        moods=moods,
        tags=sorted(set(tags)),
        vocals=detect_vocals(title, tags),
        license=code,
        license_url=item.get("license_url") or "",
        attribution=item.get("attribution") or "",
        source_url=item.get("foreign_landing_url") or item.get("detail_url") or "",
        download_url=url,
    ), None


def _token(client: httpx.Client, settings: Settings) -> str | None:
    if not (settings.openverse_client_id and settings.openverse_client_secret):
        return None
    try:
        r = client.post(
            TOKEN_URL,
            data={
                "client_id": settings.openverse_client_id,
                "client_secret": settings.openverse_client_secret,
                "grant_type": "client_credentials",
            },
        )
        r.raise_for_status()
        return r.json().get("access_token")
    except (httpx.HTTPError, ValueError):
        return None


def sync(
    settings: Settings,
    db: Database,
    profile: Profile,
    *,
    per_query_limit: int = 240,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    progress: Callable[[str], None] = lambda _msg: None,
) -> SyncReport:
    report = SyncReport("openverse")
    own_client = client is None
    client = client or httpx.Client(
        timeout=45, follow_redirects=True, headers={"User-Agent": settings.user_agent}
    )
    headers = {}
    token = _token(client, settings)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        report.notes.append(
            "익명 모드: 429(제한)가 잦으면 OPENVERSE_CLIENT_ID/SECRET을 설정하세요."
        )
    seen: set[str] = set()
    try:
        for spec in profile.openverse_queries:
            kept: list[Track] = []
            pages = min(MAX_PAGES, max(1, per_query_limit // PAGE_SIZE))
            try:
                for page in range(1, pages + 1):
                    params = {
                        "q": spec["q"],
                        "category": "music",
                        "license": "cc0,pdm,by",
                        "page": page,
                        "page_size": PAGE_SIZE,
                        "mature": "false",
                    }
                    resp = client.get(API, params=params, headers=headers)
                    if resp.status_code == 429:
                        wait = float(resp.headers.get("Retry-After", "0") or 0)
                        if 0 < wait <= 60:
                            sleep(wait)
                            resp = client.get(API, params=params, headers=headers)
                        if resp.status_code == 429:
                            raise RateLimited
                    if resp.status_code in (400, 404):  # past the last page
                        break
                    resp.raise_for_status()
                    results = resp.json().get("results") or []
                    if not results:
                        break
                    for item in results:
                        ident = str(item.get("id") or "")
                        if ident in seen:
                            continue
                        seen.add(ident)
                        report.fetched += 1
                        track, reason = parse_track(item, settings, hint=spec)
                        if track is None:
                            report.drop(reason or "unknown")
                        elif track.vocals == "vocal" and profile.vocals != "any":
                            report.drop("vocal")
                        else:
                            kept.append(track)
                    if len(results) < PAGE_SIZE:
                        break
                    sleep(0.5)
            except RateLimited:
                ins, upd = db.upsert_tracks(kept)
                report.inserted += ins
                report.updated += upd
                report.notes.append(
                    f"429 rate limit — '{spec['q']}'에서 중단했습니다. 지금까지 받은 곡은 저장했고, "
                    "잠시 후 다시 실행하면 이어서 채워집니다."
                )
                break
            ins, upd = db.upsert_tracks(kept)
            report.inserted += ins
            report.updated += upd
            progress(f"  openverse q={spec['q']!r}: {len(kept)}곡 저장")
    finally:
        if own_client:
            client.close()
    return report
