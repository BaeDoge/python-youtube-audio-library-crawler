import asyncio
import json
import random
import re
import sqlite3
from datetime import datetime
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

CDP_URL = "http://127.0.0.1:9222"
CHANNEL_ID = "UCzLBIavNT7yYjN1sl1cCnrg"
STUDIO_URL = f"https://studio.youtube.com/channel/{CHANNEL_ID}/music"
DB_PATH = Path(__file__).resolve().parent / "tracks.db"
DOWNLOAD_DIR = Path(__file__).resolve().parent / "downloads"
DOWNLOAD_COUNT = 30

GENRE_MAP = {
    "CREATOR_MUSIC_GENRE_ALTERNATIVE": "Alternative",
    "CREATOR_MUSIC_GENRE_AMBIENT": "Ambient",
    "CREATOR_MUSIC_GENRE_BLUES": "Blues",
    "CREATOR_MUSIC_GENRE_CLASSICAL": "Classical",
    "CREATOR_MUSIC_GENRE_COUNTRY": "Country",
    "CREATOR_MUSIC_GENRE_DANCE_ELECTRONIC": "Dance & Electronic",
    "CREATOR_MUSIC_GENRE_HIP_HOP_RAP": "Hip Hop & Rap",
    "CREATOR_MUSIC_GENRE_HOLIDAY": "Holiday",
    "CREATOR_MUSIC_GENRE_JAZZ": "Jazz",
    "CREATOR_MUSIC_GENRE_LATIN": "Latin",
    "CREATOR_MUSIC_GENRE_POP": "Pop",
    "CREATOR_MUSIC_GENRE_RNB_SOUL": "R&B & Soul",
    "CREATOR_MUSIC_GENRE_REGGAE": "Reggae",
    "CREATOR_MUSIC_GENRE_ROCK": "Rock",
    "CREATOR_MUSIC_GENRE_FILM": "Film",
    "CREATOR_MUSIC_GENRE_FOLK": "Folk",
    "CREATOR_MUSIC_GENRE_SINGER_SONGWRITER": "Singer-Songwriter",
}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS downloaded_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_id TEXT NOT NULL UNIQUE,
            title TEXT,
            artist TEXT,
            file_path TEXT,
            downloaded_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def display_genre(genre):
    return GENRE_MAP.get(genre, genre)


def get_genres():
    conn = db()
    rows = conn.execute(
        "SELECT genres FROM tracks WHERE genres IS NOT NULL AND genres != ''"
    ).fetchall()
    conn.close()

    genres = set()
    for row in rows:
        try:
            value = json.loads(row["genres"])
            if isinstance(value, list):
                genres.update(value)
        except (TypeError, json.JSONDecodeError):
            genres.update(x.strip() for x in str(row["genres"]).split(",") if x.strip())

    return sorted(genres, key=lambda x: display_genre(x).lower())


def get_tracks_by_genre(genre):
    conn = db()
    rows = conn.execute("""
        SELECT *
        FROM tracks
        WHERE genres LIKE ?
          AND track_id NOT IN (SELECT track_id FROM downloaded_tracks)
    """, (f'%"{genre}"%',)).fetchall()
    conn.close()
    return rows


def get_history():
    conn = db()
    rows = conn.execute("""
        SELECT track_id, title, artist, file_path, downloaded_at
        FROM downloaded_tracks
        ORDER BY downloaded_at DESC
    """).fetchall()
    conn.close()
    return rows


def safe_filename(value):
    value = re.sub(r'[<>:"/\\|?*]', "_", str(value or "").strip())
    value = re.sub(r"\s+", " ", value).rstrip(". ")
    return value or "unknown"


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


class GetTracksCapture:
    def __init__(self):
        self.request = None
        self.event = asyncio.Event()

    async def handle_request(self, request):
        if self.event.is_set() or request.method != "POST":
            return
        if "/youtubei/v1/creator_music/get_tracks" not in request.url:
            return

        try:
            payload = json.loads(request.post_data or "")
        except Exception:
            return
        if not payload.get("trackIds"):
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
        print("✅ get_tracks 요청 캡처 완료")


async def get_download_url(client, captured, track_id):
    payload = dict(captured["payload"])
    payload["trackIds"] = [track_id]
    payload["mask"] = {"includeDownloadUrl": True}

    response = await client.post(
        captured["url"],
        headers=prepare_headers(captured["headers"]),
        data=json.dumps(payload),
        timeout=30_000,
    )
    if not response.ok:
        print(f"❌ get_tracks 실패: HTTP {response.status}")
        try:
            print(json.dumps(await response.json(), ensure_ascii=False, indent=2))
        except Exception:
            print((await response.text())[:1000])
        response.raise_for_status()

    data = await response.json()
    tracks = data.get("tracks") or []
    if not tracks:
        raise RuntimeError("get_tracks 응답에 tracks가 없습니다.")
    url = tracks[0].get("downloadAudioUrl")
    if not url:
        raise RuntimeError("downloadAudioUrl이 응답에 없습니다.")
    return url


async def download_file(url, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(90.0, connect=30.0),
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with path.open("wb") as f:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    f.write(chunk)


def save_history(track_id, title, artist, path):
    conn = db()
    conn.execute("""
        INSERT OR REPLACE INTO downloaded_tracks
        (track_id, title, artist, file_path, downloaded_at)
        VALUES (?, ?, ?, ?, ?)
    """, (
        track_id, title, artist, str(path),
        datetime.now().isoformat(timespec="seconds"),
    ))
    conn.commit()
    conn.close()


async def download_one(context, captured, row, index, total, genre):
    track_id = row["track_id"]
    title = row["title"] or "Unknown"
    artist = row["artist"] or "Unknown"
    print(f"[{index}/{total}] {artist} - {title}")

    try:
        url = await get_download_url(context.request, captured, track_id)
        base = f"{safe_filename(artist)} - {safe_filename(title)}"
        DOWNLOAD_FULL_DIR = DOWNLOAD_DIR / display_genre(genre)
        DOWNLOAD_FULL_DIR.mkdir(parents=True, exist_ok=True)
        path = DOWNLOAD_FULL_DIR / f"{base}.mp3"
        if path.exists():
            path = DOWNLOAD_FULL_DIR / f"{base} [{track_id}].mp3"

        await download_file(url, path)
        save_history(track_id, title, artist, path)
        print(f"    ✅ 저장: {path}")
        return True
    except Exception as exc:
        print(f"    ❌ 실패: {type(exc).__name__}: {exc}")
        return False


def show_genres(genres):
    print("\n사용 가능한 장르")
    print("-" * 58)
    for i, genre in enumerate(genres, 1):
        count = len(get_tracks_by_genre(genre))
        print(f"{i:2}. {display_genre(genre):<25} ({count}곡 미다운로드)")
    print()


def select_genre(genres):
    show_genres(genres)
    while True:
        value = input("장르 번호 (q: 취소): ").strip()
        if value.lower() == "q":
            return None
        try:
            index = int(value)
            if 1 <= index <= len(genres):
                return genres[index - 1]
        except ValueError:
            pass
        print("올바른 번호를 입력하세요.")


def show_history():
    rows = get_history()
    if not rows:
        print("\n다운로드 이력이 없습니다.\n")
        return
    print("\n다운로드 이력")
    print("-" * 70)
    for i, row in enumerate(rows, 1):
        print(f"{i:3}. {row['artist']} - {row['title']}")
        print(f"     {row['downloaded_at']} | {row['file_path']}")


def show_status():
    conn = db()
    total = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM downloaded_tracks").fetchone()[0]
    conn.close()
    print(f"\n전체 메타데이터: {total:,}곡 | 다운로드 이력: {done:,}곡\n")


async def download_by_genre(context, captured):
    genres = get_genres()
    if not genres:
        print("DB에 장르가 없습니다. 먼저 collect.py를 실행하세요.")
        return

    genre = select_genre(genres)
    if genre is None:
        return

    available = get_tracks_by_genre(genre)
    if not available:
        print("해당 장르에 미다운로드 곡이 없습니다.")
        return

    count = min(DOWNLOAD_COUNT, len(available))
    selected = random.sample(available, count)
    print(f"\n{display_genre(genre)} 장르에서 {count}곡을 다운로드합니다.\n")

    success = 0
    for i, row in enumerate(selected, 1):
        if await download_one(context, captured, row, i, count,genre):
            success += 1
        await asyncio.sleep(random.uniform(0.3, 0.8))

    print(f"\n완료: 성공 {success}곡 / 실패 {count - success}곡")
    print(f"저장 폴더: {DOWNLOAD_DIR}")


async def main():
    if not DB_PATH.exists():
        raise FileNotFoundError(f"DB가 없습니다: {DB_PATH}\n먼저 uv run collect.py를 실행하세요.")

    init_db()
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        if not browser.contexts:
            raise RuntimeError("Chrome BrowserContext를 찾을 수 없습니다.")
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else await context.new_page()

        capture = GetTracksCapture()

        def listener(request):
            asyncio.create_task(capture.handle_request(request))

        page.on("request", listener)
        try:
            await page.goto(STUDIO_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            print(f"⚠️ Studio 이동 중 알림: {exc}")

        print("\nChrome의 YouTube Studio에서 음악 목록으로 이동한 뒤")
        print("아무 곡의 Download 버튼을 한 번 눌러 요청을 캡처하세요.")
        print("요청을 캡처하면 메뉴가 표시됩니다.\n")

        try:
            await asyncio.wait_for(capture.event.wait(), timeout=300)
        except asyncio.TimeoutError:
            raise RuntimeError(
                "5분 내 get_tracks 요청을 캡처하지 못했습니다. "
                "Studio에서 음악 목록을 열고 Download를 눌러주세요."
            )

        while True:
            print("\n" + "=" * 54)
            print("YouTube Music Downloader")
            print("=" * 54)
            show_status()
            print("1. 장르 목록")
            print("2. 장르 선택 → 랜덤 30곡 다운로드")
            print("3. 다운로드 이력")
            print("0. 종료")
            choice = input("\n선택: ").strip()

            if choice == "1":
                genres = get_genres()
                show_genres(genres)
            elif choice == "2":
                await download_by_genre(context, capture.request)
            elif choice == "3":
                show_history()
            elif choice == "0":
                break
            else:
                print("올바른 메뉴를 선택하세요.")

        page.remove_listener("request", listener)


if __name__ == "__main__":
    asyncio.run(main())
