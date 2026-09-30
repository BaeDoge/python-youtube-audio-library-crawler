import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

CDP_URL = "http://127.0.0.1:9222"
CHANNEL_ID = "UCzLBIavNT7yYjN1sl1cCnrg"
STUDIO_URL = f"https://studio.youtube.com/channel/{CHANNEL_ID}/music"
DB_PATH = Path(__file__).resolve().parent / "tracks.db"
PAGE_SIZE = 30


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tracks (
            track_id TEXT PRIMARY KEY,
            title TEXT,
            artist TEXT,
            artist_channel_id TEXT,
            duration INTEGER,
            genres TEXT,
            moods TEXT,
            instruments TEXT,
            publish_time INTEGER,
            viper_id TEXT,
            license_type TEXT,
            external_artist_url TEXT,
            raw_json TEXT,
            collected_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def json_string(value):
    return json.dumps(value, ensure_ascii=False) if value is not None else None


def save_track(track):
    track_id = track.get("trackId")
    if not track_id:
        return

    artist = track.get("artist") or {}
    if isinstance(artist, dict):
        artist_name = artist.get("name")
        artist_channel_id = artist.get("channelId")
    else:
        artist_name = str(artist)
        artist_channel_id = None

    duration = (track.get("duration") or {}).get("seconds")
    publish_time = (track.get("publishTime") or {}).get("seconds")

    try:
        duration = int(duration)
    except (TypeError, ValueError):
        duration = None

    try:
        publish_time = int(publish_time)
    except (TypeError, ValueError):
        publish_time = None

    attributes = track.get("attributes") or {}
    conn = get_connection()
    conn.execute("""
        INSERT OR REPLACE INTO tracks (
            track_id, title, artist, artist_channel_id, duration,
            genres, moods, instruments, publish_time, viper_id,
            license_type, external_artist_url, raw_json, collected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        track_id,
        track.get("title"),
        artist_name,
        artist_channel_id,
        duration,
        json_string(attributes.get("genres") or []),
        json_string(attributes.get("moods") or []),
        json_string(attributes.get("instruments") or []),
        publish_time,
        track.get("viperId"),
        track.get("licenseType"),
        track.get("externalArtistUrl"),
        json.dumps(track, ensure_ascii=False),
        datetime.now().isoformat(timespec="seconds"),
    ))
    conn.commit()
    conn.close()


def prepare_headers(headers):
    blocked = {
        "host", "content-length", "connection",
        "accept-encoding", "transfer-encoding", "cookie",
    }
    return {
        key: value
        for key, value in headers.items()
        if not key.startswith(":") and key.lower() not in blocked
    }


class ListTracksCapture:
    def __init__(self):
        self.request = None
        self.event = asyncio.Event()

    async def handle_request(self, request):
        if self.event.is_set() or request.method != "POST":
            return
        if "/youtubei/v1/creator_music/list_tracks" not in request.url:
            return

        try:
            payload = json.loads(request.post_data or "")
        except Exception:
            return
        if "channelId" not in payload:
            return

        try:
            headers = await request.all_headers()
        except Exception as exc:
            print(f"⚠️ 요청 헤더 캡처 실패: {exc}")
            return

        self.request = {
            "url": request.url,
            "headers": headers,
            "payload": payload,
        }
        self.event.set()
        print("✅ list_tracks 요청 캡처 완료")


async def request_list_tracks(client, captured, payload):
    response = await client.post(
        captured["url"],
        headers=prepare_headers(captured["headers"]),
        data=json.dumps(payload),
        timeout=60_000,
    )
    if not response.ok:
        print(f"❌ list_tracks 실패: HTTP {response.status}")
        try:
            print(json.dumps(await response.json(), ensure_ascii=False, indent=2))
        except Exception:
            print((await response.text())[:1500])
        response.raise_for_status()
    return await response.json()


async def collect_all(context, captured):
    original = captured["payload"]
    payload = dict(original)
    payload["channelId"] = CHANNEL_ID
    payload["pageInfo"] = {"pageSize": PAGE_SIZE}

    total_saved = 0
    page_number = 0

    while True:
        page_number += 1
        print(f"\n📄 페이지 {page_number} 수집 중...")
        data = await request_list_tracks(context.request, captured, payload)
        tracks = data.get("tracks", [])
        print(f"   응답 곡 수: {len(tracks)}")

        if not tracks:
            break

        for track in tracks:
            try:
                save_track(track)
                total_saved += 1
            except Exception as exc:
                print(f"   ❌ DB 저장 실패: {exc}")

        page_info = data.get("pageInfo") or {}
        token = page_info.get("nextPageToken")
        if not token:
            break

        payload = dict(original)
        payload["channelId"] = CHANNEL_ID
        payload["pageInfo"] = {
            "pageSize": PAGE_SIZE,
            "pageToken": token,
        }
        await asyncio.sleep(0.3)

    conn = get_connection()
    count = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    conn.close()
    print(f"\n✅ 수집 완료: 이번 실행 {total_saved:,}곡 처리")
    print(f"DB 누적 곡 수: {count:,}")
    print(f"DB 경로: {DB_PATH}")


async def main():
    init_db()
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        if not browser.contexts:
            raise RuntimeError("Chrome BrowserContext를 찾을 수 없습니다.")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        capture = ListTracksCapture()

        def listener(request):
            asyncio.create_task(capture.handle_request(request))

        page.on("request", listener)
        try:
            await page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            print(f"⚠️ Studio 이동 중 알림: {exc}")

        print("YouTube Studio에서 음악 목록이 로드될 때까지 기다립니다.")
        try:
            await asyncio.wait_for(capture.event.wait(), timeout=300)
        except asyncio.TimeoutError:
            raise RuntimeError(
                "5분 내 list_tracks 요청을 캡처하지 못했습니다. "
                "Studio 음악 페이지를 새로고침해 보세요."
            )

        await collect_all(context, capture.request)
        page.remove_listener("request", listener)


if __name__ == "__main__":
    asyncio.run(main())
