"""YouTube Audio Library provider (optional).

Drives a Chrome you are already logged into (``--remote-debugging-port=9222``) and re-uses
the Studio session to list tracks and to resolve download URLs. Needs
``pip install "music-crawler[youtube]"``. This is a port of the original ``collect.py`` /
``music.py`` capture logic; it relies on YouTube Studio's *private* web API, so it can break
whenever YouTube changes it.

Note: ~14 % of the library is CC BY 4.0 (e.g. Kevin MacLeod) and **requires attribution**;
those tracks are stored with license ``cc-by`` so credits appear in the generated description.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import httpx

from music_crawler.config import Settings
from music_crawler.db import Database
from music_crawler.downloader import DownloadError, existing_file, finalize, target_path
from music_crawler.models import Track
from music_crawler.providers.base import SyncReport
from music_crawler.taxonomy import detect_vocals, normalize_genres, normalize_moods

LIST_URL_PART = "/youtubei/v1/creator_music/list_tracks"
GET_URL_PART = "/youtubei/v1/creator_music/get_tracks"
CCBY4 = "https://creativecommons.org/licenses/by/4.0/"
_BLOCKED_HEADERS = {
    "host",
    "content-length",
    "connection",
    "accept-encoding",
    "transfer-encoding",
    "cookie",
}


def _short(value: str, prefix: str) -> str:
    value = (value or "").lower()
    return value[len(prefix) :].replace("_", " ") if value.startswith(prefix) else value


def parse_track(raw: dict) -> Track | None:
    track_id = raw.get("trackId")
    if not track_id:
        return None
    artist = raw.get("artist") or {}
    artist_name = artist.get("name", "") if isinstance(artist, dict) else str(artist)
    try:
        duration = int(float((raw.get("duration") or {}).get("seconds") or 0))
    except (TypeError, ValueError):
        duration = 0
    attrs = raw.get("attributes") or {}
    instruments = [_short(i, "creator_music_instrument_") for i in attrs.get("instruments") or []]
    lic_type = raw.get("licenseType") or ""
    is_ccby = "CCBY" in lic_type
    title = (raw.get("title") or "Unknown").strip()
    return Track(
        id=f"youtube:{track_id}",
        source="youtube",
        source_id=track_id,
        title=title,
        artist=artist_name.strip(),
        duration=duration,
        genres=normalize_genres(attrs.get("genres")) or ["Other"],
        moods=normalize_moods(attrs.get("moods")),
        tags=sorted(set(instruments)),
        vocals=detect_vocals(title, instruments),
        license="cc-by" if is_ccby else "yt-audio-library",
        license_url=CCBY4 if is_ccby else "",
        source_url=raw.get("externalTrackUrl") or raw.get("externalArtistUrl") or "",
    )


def _clean_headers(headers: dict) -> dict:
    return {
        k: v
        for k, v in headers.items()
        if not k.startswith(":") and k.lower() not in _BLOCKED_HEADERS
    }


class _Capture:
    """Remember the first matching Studio API request so we can replay it with new payloads."""

    def __init__(self, url_part: str, payload_ok: Callable[[dict], bool]):
        self.url_part, self.payload_ok = url_part, payload_ok
        self.request: dict | None = None
        self.event = asyncio.Event()

    async def handle(self, request) -> None:
        if self.event.is_set() or request.method != "POST" or self.url_part not in request.url:
            return
        try:
            payload = json.loads(request.post_data or "")
        except (TypeError, ValueError):
            return
        if not self.payload_ok(payload):
            return
        self.request = {
            "url": request.url,
            "headers": await request.all_headers(),
            "payload": payload,
        }
        self.event.set()


async def _connect(settings: Settings, capture: _Capture, goto: str):
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise ImportError(
            "pip install 'music-crawler[youtube]' && playwright install chromium"
        ) from exc
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(settings.cdp_url)
    if not browser.contexts:
        raise RuntimeError(
            "Chrome 컨텍스트가 없습니다. scripts/chrome_debug.bat 으로 Chrome을 실행하세요."
        )
    context = browser.contexts[0]
    page = context.pages[0] if context.pages else await context.new_page()
    page.on("request", lambda r: asyncio.create_task(capture.handle(r)))
    try:
        await page.goto(goto, wait_until="domcontentloaded", timeout=60_000)
    except Exception:  # noqa: BLE001 - Studio often reports a benign navigation abort
        pass
    return pw, context


def _studio_url(settings: Settings) -> str:
    if not settings.youtube_channel_id:
        raise RuntimeError("YOUTUBE_CHANNEL_ID가 비어 있습니다 (.env 확인).")
    return f"https://studio.youtube.com/channel/{settings.youtube_channel_id}/music"


async def _sync_async(settings: Settings, db: Database, progress) -> SyncReport:
    report = SyncReport("youtube")
    capture = _Capture(LIST_URL_PART, lambda p: "channelId" in p)
    pw, context = await _connect(settings, capture, _studio_url(settings))
    try:
        progress("YouTube Studio 음악 페이지 로딩 대기 중 (최대 5분)…")
        await asyncio.wait_for(capture.event.wait(), timeout=300)
        original = capture.request
        payload = dict(original["payload"])
        payload["channelId"] = settings.youtube_channel_id
        payload["pageInfo"] = {"pageSize": 30}
        while True:
            resp = await context.request.post(
                original["url"],
                headers=_clean_headers(original["headers"]),
                data=json.dumps(payload),
                timeout=60_000,
            )
            if not resp.ok:
                raise RuntimeError(f"list_tracks HTTP {resp.status}")
            data = await resp.json()
            tracks = [t for t in map(parse_track, data.get("tracks") or []) if t]
            if not tracks:
                break
            report.fetched += len(tracks)
            ins, upd = db.upsert_tracks(tracks)
            report.inserted += ins
            report.updated += upd
            token = (data.get("pageInfo") or {}).get("nextPageToken")
            if not token:
                break
            payload = dict(original["payload"])
            payload["channelId"] = settings.youtube_channel_id
            payload["pageInfo"] = {"pageSize": 30, "pageToken": token}
            await asyncio.sleep(0.3)
    finally:
        await pw.stop()
    return report


def sync(
    settings: Settings,
    db: Database,
    profile=None,
    *,
    progress: Callable[[str], None] = lambda _m: None,
    **_ignored,
) -> SyncReport:
    return asyncio.run(_sync_async(settings, db, progress))


async def _download_async(settings: Settings, db: Database, tracks: list[Track], progress):
    ok: dict[str, Path] = {}
    failed: dict[str, str] = {}
    capture = _Capture(GET_URL_PART, lambda p: bool(p.get("trackIds")))
    pw, context = await _connect(settings, capture, _studio_url(settings))
    try:
        progress("Studio 음악 페이지에서 아무 곡의 Download 버튼을 한 번 눌러 주세요 (최대 5분)…")
        await asyncio.wait_for(capture.event.wait(), timeout=300)
        original = capture.request
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(90, connect=30),
            headers={"User-Agent": settings.user_agent},
        ) as client:
            for track in tracks:
                found = existing_file(settings, track)
                if found:
                    ok[track.id] = found
                    continue
                part = target_path(settings, track, ".part")
                try:
                    payload = dict(original["payload"])
                    payload["trackIds"] = [track.source_id]
                    payload["mask"] = {"includeDownloadUrl": True}
                    resp = await context.request.post(
                        original["url"],
                        headers=_clean_headers(original["headers"]),
                        data=json.dumps(payload),
                        timeout=30_000,
                    )
                    if not resp.ok:
                        raise DownloadError(f"get_tracks HTTP {resp.status}")
                    items = (await resp.json()).get("tracks") or []
                    url = items[0].get("downloadAudioUrl") if items else None
                    if not url:
                        raise DownloadError("downloadAudioUrl이 응답에 없습니다.")
                    part.parent.mkdir(parents=True, exist_ok=True)
                    async with client.stream("GET", url) as r:
                        r.raise_for_status()
                        with part.open("wb") as fh:
                            async for chunk in r.aiter_bytes(1 << 20):
                                fh.write(chunk)
                    ok[track.id] = finalize(settings, db, track, part, url)
                    progress(f"  ✓ {track.label}")
                except Exception as exc:  # noqa: BLE001
                    failed[track.id] = str(exc)
                    progress(f"  ✗ {track.label}: {exc}")
                finally:
                    part.unlink(missing_ok=True)
    finally:
        await pw.stop()
    return ok, failed


def download_tracks(
    settings: Settings,
    db: Database,
    tracks: list[Track],
    progress: Callable[[str], None] = lambda _m: None,
):
    return asyncio.run(_download_async(settings, db, tracks, progress))
