import asyncio
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from external_music import scan_local_music, update_jamendo_catalog, update_openverse_catalog
from project_paths import (
    BASE_DIR, DB_PATH, DOWNLOAD_DIR, PLAYLIST_DIR, DRAFT_DIR,
    USED_FILE, CATALOG_PATH, EXTERNAL_MUSIC_DIR, migrate_legacy_paths,
)


# ============================================================
# PATH / ENV
# ============================================================

load_dotenv(BASE_DIR / ".env")
migrate_legacy_paths()

# Paths may be overridden in .env, but default to the organized output tree.
_download_setting = os.getenv("DOWNLOAD_DIR", "output/downloads").strip().replace("\\", "/")
_playlist_setting = os.getenv("PLAYLIST_DIR", "output/playlists").strip().replace("\\", "/")
_draft_setting = os.getenv("DRAFT_DIR", "output/playlist_drafts").strip().replace("\\", "/")
_used_setting = os.getenv("USED_FILE", "data/used.txt").strip().replace("\\", "/")
DOWNLOAD_DIR = BASE_DIR / ("output/downloads" if _download_setting in {"downloads", "download"} else _download_setting)
PLAYLIST_DIR = BASE_DIR / ("output/playlists" if _playlist_setting in {"playlists", "playlist"} else _playlist_setting)
DRAFT_DIR = BASE_DIR / ("output/playlist_drafts" if _draft_setting in {"playlist_drafts", "playlist_draft"} else _draft_setting)
USED_FILE = BASE_DIR / ("data/used.txt" if _used_setting == "used.txt" else _used_setting)

CDP_URL = os.getenv(
    "CDP_URL",
    "http://127.0.0.1:9222",
)

CHANNEL_ID = os.getenv(
    "CHANNEL_ID",
    "UCzLBIavNT7yYjN1sl1cCnrg",
)

STUDIO_URL = (
    f"https://studio.youtube.com/"
    f"channel/{CHANNEL_ID}/music"
)

FFMPEG_BIN = os.getenv(
    "FFMPEG_BIN",
    "ffmpeg",
)

JAMENDO_CLIENT_ID = os.getenv("JAMENDO_CLIENT_ID", "").strip()


# ============================================================
# PLAYLIST FILTER / SCORING CONFIG
# ============================================================

def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


MIN_TRACK_DURATION = env_int(
    "MIN_TRACK_DURATION_SECONDS",
    120,
)

MAX_TRACK_DURATION = env_int(
    "MAX_TRACK_DURATION_SECONDS",
    0,
)

MAX_TRACKS_PER_PLAYLIST = env_int(
    "MAX_TRACKS_PER_PLAYLIST",
    12,
)

PREFERRED_TRACK_DURATION = env_int(
    "PREFERRED_TRACK_DURATION_SECONDS",
    240,
)

DURATION_SCORE_WEIGHT = env_float(
    "SCORING_DURATION_WEIGHT",
    10.0,
)

MOOD_SCORE_WEIGHT = env_float(
    "SCORING_MOOD_WEIGHT",
    5.0,
)

GENRE_SCORE_WEIGHT = env_float(
    "SCORING_GENRE_WEIGHT",
    3.0,
)

ARTIST_REPEAT_PENALTY = env_float(
    "SCORING_ARTIST_REPEAT_PENALTY",
    8.0,
)

RANDOMNESS_WEIGHT = env_float(
    "SCORING_RANDOMNESS_WEIGHT",
    0.5,
)

BEAM_WIDTH_PER_DURATION = env_int(
    "BEAM_WIDTH_PER_DURATION",
    4,
)

COPY_TRACKS_TO_PLAYLIST = (
    os.getenv(
        "COPY_TRACKS_TO_PLAYLIST",
        "true",
    ).strip().lower()
    in {"1", "true", "yes", "y"}
)


# ============================================================
# DISPLAY
# ============================================================

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


def display_genre(value):
    return GENRE_MAP.get(
        value,
        str(value),
    )


def display_mood(value):
    value = str(value or "").strip()

    if not value:
        return "Unknown"

    prefix = "CREATOR_MUSIC_MOOD_"

    if value.startswith(prefix):
        value = value[len(prefix):]

    return value.replace(
        "_",
        " ",
    ).title()


def normalize_text(value):
    return " ".join(
        str(value or "").strip().split()
    )


def safe_filename(value):
    value = re.sub(
        r'[<>:"/\\|?*]',
        "_",
        str(value or "").strip(),
    )

    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    value = value.rstrip(". ")

    return value or "unknown"


def format_time(seconds):
    seconds = int(seconds)

    hours = seconds // 3600

    minutes = (
        seconds % 3600
    ) // 60

    secs = seconds % 60

    if hours:
        return (
            f"{hours:02d}:"
            f"{minutes:02d}:"
            f"{secs:02d}"
        )

    return (
        f"{minutes:02d}:"
        f"{secs:02d}"
    )


def parse_duration(value):
    value = str(value).strip()

    if value.isdigit():
        return int(value) * 60

    parts = value.split(":")

    if len(parts) == 2:
        minutes, seconds = map(
            int,
            parts,
        )

        return (
            minutes * 60
            + seconds
        )

    if len(parts) == 3:
        hours, minutes, seconds = map(
            int,
            parts,
        )

        return (
            hours * 3600
            + minutes * 60
            + seconds
        )

    raise ValueError(
        "영상 길이는 "
        "15 / 15:30 / 01:15:30 "
        "형식으로 입력하세요."
    )


# ============================================================
# DATABASE
# ============================================================

def db():
    conn = sqlite3.connect(
        DB_PATH
    )

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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS track_usage (
            track_id TEXT PRIMARY KEY,
            use_count INTEGER NOT NULL DEFAULT 0,
            first_used_at TEXT,
            last_used_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS playlist_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_title TEXT,
            video_duration INTEGER NOT NULL,
            music_duration INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS playlist_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id INTEGER NOT NULL,
            track_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            start_seconds INTEGER NOT NULL,
            duration INTEGER NOT NULL,
            title TEXT,
            artist TEXT,
            file_path TEXT
        )
    """)

    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(tracks)")}
    for column, definition in {
        "external_source": "TEXT",
        "external_download_url": "TEXT",
        "external_license_url": "TEXT",
        "external_source_url": "TEXT",
        "local_file_path": "TEXT",
        "external_eligible": "INTEGER NOT NULL DEFAULT 1",
    }.items():
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE tracks ADD COLUMN {column} {definition}")

    conn.commit()
    conn.close()


# ============================================================
# JSON HELPERS
# ============================================================

def parse_json_list(value):
    if not value:
        return []

    try:
        result = json.loads(value)

        if isinstance(result, list):
            return [
                str(x).strip()
                for x in result
                if str(x).strip()
            ]

    except (
        TypeError,
        json.JSONDecodeError,
    ):
        pass

    return [
        x.strip()
        for x in str(value).split(",")
        if x.strip()
    ]


def row_genres(row):
    return parse_json_list(
        row["genres"]
    )


def row_moods(row):
    return parse_json_list(
        row["moods"]
    )


def primary_genre(row):
    values = row_genres(row)

    if values:
        return display_genre(
            values[0]
        )

    return "Unknown"


def primary_mood(row):
    values = row_moods(row)

    if values:
        return display_mood(
            values[0]
        )

    return "Unknown"


# ============================================================
# GENRES / MOODS
# ============================================================

def get_all_genres():
    conn = db()

    rows = conn.execute("""
        SELECT genres
        FROM tracks
        WHERE genres IS NOT NULL
          AND genres != ''
    """).fetchall()

    conn.close()

    values = set()

    for row in rows:
        values.update(
            parse_json_list(
                row["genres"]
            )
        )

    return sorted(
        values,
        key=lambda x:
            display_genre(x).lower(),
    )


def get_all_moods():
    conn = db()

    rows = conn.execute("""
        SELECT moods
        FROM tracks
        WHERE moods IS NOT NULL
          AND moods != ''
    """).fetchall()

    conn.close()

    values = set()

    for row in rows:
        values.update(
            parse_json_list(
                row["moods"]
            )
        )

    return sorted(
        values,
        key=lambda x:
            display_mood(x).lower(),
    )


def show_genres():
    genres = get_all_genres()

    print()
    print("=" * 70)
    print("🎵 GENRES")
    print("=" * 70)

    conn = db()

    for index, genre in enumerate(
        genres,
        1,
    ):
        rows = conn.execute(
            """
            SELECT genres
            FROM tracks
            WHERE genres IS NOT NULL
            AND genres != ''
            """
        ).fetchall()

        count = 0

        for row in rows:
            if genre in parse_json_list(
                row["genres"]
            ):
                count += 1

        print(
            f"{index:2}. "
            f"{display_genre(genre):<25} "
            f"{count:,}곡"
        )

    conn.close()


def show_moods():
    moods = get_all_moods()

    print()
    print("=" * 70)
    print("🎨 MOODS")
    print("=" * 70)

    conn = db()

    rows = conn.execute(
        """
        SELECT moods
        FROM tracks
        WHERE moods IS NOT NULL
        AND moods != ''
        """
    ).fetchall()

    conn.close()

    for index, mood in enumerate(
        moods,
        1,
    ):
        count = sum(
            mood in parse_json_list(
                row["moods"]
            )
            for row in rows
        )

        print(
            f"{index:2}. "
            f"{display_mood(mood):<25} "
            f"{count:,}곡"
        )


def choose_from_list(
    values,
    display_function,
    label,
):
    print(
        "0. 전체"
    )

    for index, value in enumerate(
        values,
        1,
    ):
        print(
            f"{index:2}. "
            f"{display_function(value)}"
        )

    while True:
        value = input(
            f"\n{label}: "
        ).strip()

        if value == "0":
            return None

        try:
            index = int(value)

            if 1 <= index <= len(values):
                return values[
                    index - 1
                ]

        except ValueError:
            pass

        print(
            "올바른 번호를 입력하세요."
        )


# ============================================================
# TRACK QUERY
# ============================================================

def get_candidate_tracks(
    genre=None,
    mood=None,
    source_filter=None,
):
    conn = db()

    rows = conn.execute("""
        SELECT
            t.*,
            COALESCE(
                tu.use_count,
                0
            ) AS use_count,
            dt.file_path AS downloaded_file,
            t.external_source AS external_source,
            t.local_file_path AS local_file_path
        FROM tracks t
        LEFT JOIN track_usage tu
            ON tu.track_id = t.track_id
        LEFT JOIN downloaded_tracks dt
            ON dt.track_id = t.track_id
        WHERE t.duration IS NOT NULL
          AND t.duration > 0
          AND (t.external_source IS NULL OR t.external_source = 'local' OR COALESCE(t.external_eligible, 0) = 1)
        ORDER BY t.duration ASC
    """).fetchall()

    conn.close()

    result = []

    for row in rows:
        duration = int(
            row["duration"]
        )

        if int(row["use_count"] or 0) > 0:
            continue

        if duration < MIN_TRACK_DURATION:
            continue

        if (
            MAX_TRACK_DURATION > 0
            and duration > MAX_TRACK_DURATION
        ):
            continue

        is_local = (
            ("external_source" in row.keys() and row["external_source"] == "local")
            or ("local_file_path" in row.keys() and row["local_file_path"] and Path(row["local_file_path"]).exists())
        )

        # 외부 전용 플레이리스트에서는 YouTube 트랙과 직접 보유한 로컬 파일을 제외합니다.
        if source_filter:
            row_source = (row["external_source"] or "") if "external_source" in row.keys() else ""
            if source_filter == "external":
                if not row_source or row_source == "local":
                    continue
            elif row_source != source_filter:
                continue

        # 사용자가 직접 받은 로컬 음원은 선택한 장르/Mood 필터와 무관하게 우선 후보로 포함합니다.
        if not is_local and genre is not None and genre not in row_genres(row):
            continue

        if not is_local and mood is not None and mood not in row_moods(row):
            continue

        result.append(row)

    return result


# ============================================================
# SCORING
# ============================================================

def track_base_score(row):
    duration = int(
        row["duration"]
    )

    preferred = max(
        1,
        PREFERRED_TRACK_DURATION,
    )

    distance = abs(
        duration - preferred
    )

    duration_quality = max(
        0.0,
        1.0 - (
            distance
            / preferred
        ),
    )

    local_bonus = 100.0 if (
        ("external_source" in row.keys() and row["external_source"] == "local")
        or ("local_file_path" in row.keys() and row["local_file_path"] and Path(row["local_file_path"]).exists())
    ) else 0.0

    return (
        local_bonus
        + duration_quality
        * DURATION_SCORE_WEIGHT
        + random.random()
        * RANDOMNESS_WEIGHT
    )


def shared_attribute_score(
    row_a,
    row_b,
):
    genres_a = set(
        row_genres(row_a)
    )

    genres_b = set(
        row_genres(row_b)
    )

    moods_a = set(
        row_moods(row_a)
    )

    moods_b = set(
        row_moods(row_b)
    )

    genre_match = bool(
        genres_a & genres_b
    )

    mood_match = bool(
        moods_a & moods_b
    )

    score = 0.0

    if genre_match:
        score += GENRE_SCORE_WEIGHT

    if mood_match:
        score += MOOD_SCORE_WEIGHT

    return score


def artist_repeat_penalty(
    rows,
    candidate,
):
    artist = normalize_text(
        candidate["artist"]
    ).casefold()

    if not artist:
        return 0.0

    repeat_count = sum(
        normalize_text(
            row["artist"]
        ).casefold()
        == artist
        for row in rows
    )

    return (
        repeat_count
        * ARTIST_REPEAT_PENALTY
    )


def state_score(
    rows,
    candidate,
):
    score = track_base_score(
        candidate
    )

    score += sum(
        shared_attribute_score(
            row,
            candidate,
        )
        for row in rows
    )

    score -= artist_repeat_penalty(
        rows,
        candidate,
    )

    return score


# ============================================================
# PLAYLIST OPTIMIZATION
# ============================================================

def find_best_playlist(
    tracks,
    target_seconds,
):
    valid = [
        row
        for row in tracks
        if int(row["duration"])
        <= target_seconds
    ]

    if not valid:
        return []

    valid = sorted(
        valid,
        key=lambda row:
            (
                int(row["duration"]),
                -track_base_score(row),
            ),
    )

    states = {
        0: [
            (
                0.0,
                (),
            )
        ]
    }

    for candidate_index, candidate in enumerate(
        valid
    ):
        duration = int(
            candidate["duration"]
        )

        current_states = list(
            states.items()
        )

        for current_sum, candidates in current_states:

            new_sum = (
                current_sum
                + duration
            )

            if new_sum > target_seconds:
                continue

            destination = states.setdefault(
                new_sum,
                [],
            )

            for base_score, indexes in candidates:

                if (
                    len(indexes)
                    >= MAX_TRACKS_PER_PLAYLIST
                ):
                    continue

                selected_rows = [
                    valid[i]
                    for i in indexes
                ]

                score = (
                    base_score
                    + state_score(
                        selected_rows,
                        candidate,
                    )
                )

                destination.append(
                    (
                        score,
                        indexes
                        + (
                            candidate_index,
                        ),
                    )
                )

            destination.sort(
                key=lambda x: x[0],
                reverse=True,
            )

            unique = []

            seen = set()

            for item in destination:
                indexes = item[1]

                if indexes in seen:
                    continue

                seen.add(indexes)

                unique.append(item)

                if len(unique) >= (
                    BEAM_WIDTH_PER_DURATION
                ):
                    break

            states[new_sum] = unique

    if not states:
        return []

    best_sum = max(
        states.keys()
    )

    best_state = max(
        states[best_sum],
        key=lambda x: x[0],
    )

    indexes = best_state[1]

    selected = [
        valid[i]
        for i in indexes
    ]

    selected = arrange_tracks(
        selected
    )

    return selected


def arrange_tracks(rows):
    if len(rows) <= 1:
        return rows

    remaining = list(rows)
    result = []

    while remaining:
        previous_artist = (
            normalize_text(
                result[-1]["artist"]
            ).casefold()
            if result
            else ""
        )

        candidates = sorted(
            remaining,
            key=lambda row: (
                normalize_text(
                    row["artist"]
                ).casefold()
                == previous_artist,
                -track_base_score(row),
            ),
        )

        selected = candidates[0]

        result.append(
            selected
        )

        remaining.remove(
            selected
        )

    return result


# ============================================================
# PLAYLIST ITEMS
# ============================================================

def build_playlist_items(
    rows,
):
    current = 0

    items = []

    for position, row in enumerate(
        rows,
        1,
    ):
        duration = int(
            row["duration"]
        )

        items.append({
            "position": position,
            "track_id": row["track_id"],
            "title": row["title"] or "Unknown",
            "artist": row["artist"] or "Unknown",
            "duration": duration,
            "start_seconds": current,
            "timestamp": format_time(current),
            "file_path": row["downloaded_file"] or "",
            "genre": primary_genre(row),
            "mood": primary_mood(row),
            "source": (row["external_source"] if "external_source" in row.keys() and row["external_source"] else "YouTube Audio Library"),
            "source_url": (row["external_source_url"] if "external_source_url" in row.keys() else "") or "",
            "license_url": (row["external_license_url"] if "external_license_url" in row.keys() else "") or "",
        })

        current += duration

    return items


def make_youtube_description(
    items,
):
    return "\n".join(
        (
            f"{item['timestamp']} "
            f"{item['title']}"
        )
        for item in items
    )


# ============================================================
# DISPLAY PLAYLIST
# ============================================================

def print_playlist(
    items,
    target_seconds,
):
    total = sum(
        item["duration"]
        for item in items
    )

    print()
    print("=" * 80)
    print("🎵 GENERATED PLAYLIST")
    print("=" * 80)

    print(
        f"영상 길이 : "
        f"{format_time(target_seconds)}"
    )

    print(
        f"음악 길이 : "
        f"{format_time(total)}"
    )

    print(
        f"남는 시간 : "
        f"{format_time(target_seconds - total)}"
    )

    print(
        f"곡 수     : "
        f"{len(items)}"
    )

    print()

    for item in items:
        print(
            f"{item['position']:2}. "
            f"{item['timestamp']}  "
            f"{item['artist']} - "
            f"{item['title']} "
            f"[{item['genre']} / "
            f"{item['mood']}]"
        )

    print()
    print("-" * 80)
    print("📋 YOUTUBE DESCRIPTION")
    print("-" * 80)
    print(
        make_youtube_description(
            items
        )
    )
    print("-" * 80)


# ============================================================
# LEGACY USED.TXT
# ============================================================

def import_legacy_used_file():
    if not USED_FILE.exists():
        print(
            f"\n{USED_FILE}가 없습니다."
        )
        return

    titles = [
        normalize_text(line)
        for line in USED_FILE.read_text(
            encoding="utf-8-sig"
        ).splitlines()
        if normalize_text(line)
        and not normalize_text(
            line
        ).startswith("#")
    ]

    conn = db()

    tracks = conn.execute("""
        SELECT
            track_id,
            title,
            artist
        FROM tracks
    """).fetchall()

    now = datetime.now().isoformat(
        timespec="seconds"
    )

    imported = 0
    unmatched = []

    for legacy in titles:

        matches = []

        if " - " in legacy:
            artist_part, title_part = (
                legacy.split(
                    " - ",
                    1,
                )
            )

            artist_part = (
                normalize_text(
                    artist_part
                ).casefold()
            )

            title_part = (
                normalize_text(
                    title_part
                ).casefold()
            )

            for row in tracks:
                if (
                    normalize_text(
                        row["artist"]
                    ).casefold()
                    == artist_part
                    and
                    normalize_text(
                        row["title"]
                    ).casefold()
                    == title_part
                ):
                    matches.append(
                        row
                    )

        else:
            title_part = (
                normalize_text(
                    legacy
                ).casefold()
            )

            matches = [
                row
                for row in tracks
                if normalize_text(
                    row["title"]
                ).casefold()
                == title_part
            ]

        if not matches:
            unmatched.append(
                legacy
            )
            continue

        for row in matches:
            conn.execute("""
                INSERT INTO track_usage (
                    track_id,
                    use_count,
                    first_used_at,
                    last_used_at
                )
                VALUES (?, 1, ?, ?)

                ON CONFLICT(track_id)
                DO UPDATE SET
                    use_count =
                        CASE
                            WHEN track_usage.use_count = 0
                            THEN 1
                            ELSE track_usage.use_count
                        END,
                    first_used_at =
                        COALESCE(
                            track_usage.first_used_at,
                            excluded.first_used_at
                        ),
                    last_used_at =
                        excluded.last_used_at
            """, (
                row["track_id"],
                now,
                now,
            ))

            imported += 1

    conn.commit()
    conn.close()

    print()
    print(
        f"읽은 used.txt 항목: "
        f"{len(titles):,}"
    )

    print(
        f"사용 처리된 곡: "
        f"{imported:,}"
    )

    if unmatched:
        print(
            f"\n⚠️ 매칭되지 않은 항목: "
            f"{len(unmatched)}"
        )

        for item in unmatched[:20]:
            print(
                f"  - {item}"
            )


# ============================================================
# DOWNLOAD API (오리지널 깃허브 코드 방식 복원)
# ============================================================

def prepare_headers(headers):
    blocked = {
        "host",
        "content-length",
        "connection",
        "accept-encoding",
        "transfer-encoding",
        "cookie",
    }

    return {
        key: value
        for key, value in headers.items()
        if not key.startswith(":")
        and key.lower() not in blocked
    }


class GetTracksCapture:

    def __init__(self):
        self.request = None
        self.event = asyncio.Event()

    async def handle_request(
        self,
        request,
    ):
        if (
            self.event.is_set()
            or request.method != "POST"
        ):
            return

        if (
            "/youtubei/v1/"
            "creator_music/get_tracks"
            not in request.url
        ):
            return

        try:
            payload = json.loads(
                request.post_data or ""
            )
        except Exception:
            return

        if not payload.get(
            "trackIds"
        ):
            return

        try:
            headers = await request.all_headers()
        except Exception as exc:
            print(
                f"⚠️ 요청 헤더 캡처 실패: {exc}"
            )
            return

        self.request = {
            "url": request.url,
            "headers": headers,
            "payload": payload,
        }

        self.event.set()

        print(
            "✅ get_tracks 요청 캡처 완료"
        )


async def get_download_url(
    client,
    captured,
    track_id,
):
    payload = dict(
        captured["payload"]
    )

    payload["trackIds"] = [
        track_id
    ]

    payload["mask"] = {
        "includeDownloadUrl": True
    }

    api_client = captured.get(
        "api_request"
    )

    if api_client is None:
        raise RuntimeError(
            "브라우저 인증 세션을 공유하는 "
            "API client가 없습니다."
        )

    response = await api_client.post(
        captured["url"],
        headers=prepare_headers(
            captured["headers"]
        ),
        data=json.dumps(payload),
        timeout=30_000,
    )

    if not response.ok:
        print(
            f"❌ get_tracks 실패: HTTP {response.status}"
        )

        try:
            print(
                (await response.text())[:1000]
            )
        except Exception:
            pass

        raise RuntimeError(
            f"get_tracks HTTP {response.status}: "
            f"{await response.text()}"
        )

    data = await response.json()

    tracks = (
        data.get("tracks")
        or []
    )

    if not tracks:
        raise RuntimeError(
            "get_tracks 응답에 "
            "tracks가 없습니다."
        )

    url = tracks[0].get(
        "downloadAudioUrl"
    )

    if not url:
        raise RuntimeError(
            "downloadAudioUrl이 응답에 없습니다."
        )

    return url


async def download_file(
    client,
    url,
    path,
):
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    async with client.stream(
        "GET",
        url,
    ) as response:

        response.raise_for_status()

        with path.open(
            "wb"
        ) as file:

            async for chunk in response.aiter_bytes(
                1024 * 1024
            ):
                file.write(chunk)


def _probe_audio_duration_seconds(path: Path):
    """Return decoded media duration, or None when ffprobe cannot inspect it."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        ffmpeg_path = shutil.which(FFMPEG_BIN)
        if ffmpeg_path:
            candidate = Path(ffmpeg_path).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
            if candidate.exists():
                ffprobe = str(candidate)
    if not ffprobe:
        return None
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=20, check=False,
        )
        if result.returncode != 0:
            return None
        return float(result.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _validate_external_audio(path: Path, source: str, expected_duration: int = 0):
    """Reject Openverse preview/snippet URLs that are much shorter than catalog metadata."""
    if source != "openverse":
        return

    actual_duration = _probe_audio_duration_seconds(path)
    # Small audio samples and preview clips are not useful as background music.
    too_short = actual_duration is not None and actual_duration < 10
    duration_mismatch = (
        actual_duration is not None and expected_duration > 0
        and actual_duration < max(10, expected_duration * 0.5)
    )
    suspiciously_small = path.stat().st_size < 32 * 1024
    if too_short or duration_mismatch or suspiciously_small:
        details = (
            f"실제 길이 {actual_duration:.2f}초" if actual_duration is not None
            else f"파일 크기 {path.stat().st_size:,} bytes"
        )
        if expected_duration > 0:
            details += f", Openverse 메타데이터 길이 {expected_duration}초"
        raise RuntimeError(
            f"Openverse 음원이 미리듣기/일부 데이터일 수 있어 제외했습니다: {path.name} ({details}). "
            "이 트랙은 다운로드 후보에서 제외하고 다른 곡을 선택하세요."
        )


def _remove_download_history(track_id):
    conn = db()
    conn.execute("DELETE FROM downloaded_tracks WHERE track_id = ?", (track_id,))
    conn.commit()
    conn.close()


def find_downloaded_file(
    track_id,
):
    conn = db()

    row = conn.execute("""
        SELECT file_path
        FROM downloaded_tracks
        WHERE track_id = ?
    """, (
        track_id,
    )).fetchone()

    conn.close()

    if not row:
        return None

    path = Path(
        row["file_path"]
    )

    if path.exists():
        return path

    return None


def save_download_history(
    track_id,
    title,
    artist,
    path,
):
    conn = db()

    conn.execute("""
        INSERT OR REPLACE INTO downloaded_tracks (
            track_id,
            title,
            artist,
            file_path,
            downloaded_at
        )
        VALUES (?, ?, ?, ?, ?)
    """, (
        track_id,
        title,
        artist,
        str(path),
        datetime.now().isoformat(
            timespec="seconds"
        ),
    ))

    conn.commit()
    conn.close()


async def ensure_downloaded(
    client,
    captured,
    item,
):
    existing = find_downloaded_file(
        item["track_id"]
    )

    conn = db()

    row = conn.execute("""
        SELECT *
        FROM tracks
        WHERE track_id = ?
    """, (
        item["track_id"],
    )).fetchone()

    conn.close()

    if not row:
        raise RuntimeError(
            f"트랙을 찾을 수 없습니다: "
            f"{item['track_id']}"
        )

    external_source = row["external_source"] if "external_source" in row.keys() else None
    expected_duration = int(row["duration"] or 0)
    if existing:
        try:
            _validate_external_audio(existing, external_source or "", expected_duration)
        except RuntimeError as exc:
            print(f"⚠️ 기존 다운로드 파일이 비정상이라 다시 받습니다: {exc}")
            try:
                existing.unlink(missing_ok=True)
            except OSError:
                pass
            _remove_download_history(item["track_id"])
        else:
            item["file_path"] = str(existing)
            return existing

    print(
        f"⬇ "
        f"{item['artist']} - "
        f"{item['title']}"
    )

    if row["track_id"].startswith("local:") and row["local_file_path"]:
        local_path = Path(row["local_file_path"])
        if local_path.exists():
            item["file_path"] = str(local_path)
            save_download_history(item["track_id"], item["title"], item["artist"], local_path)
            return local_path

    if external_source not in (None, "", "local"):
        # Any provider with a direct, permitted download URL can use the same downloader.
        url = row["external_download_url"]
        if not url:
            raise RuntimeError(
                f"{external_source} 음원에 다운로드 URL이 없습니다: {item['title']}"
            )
    else:
        url = await get_download_url(
            client,
            captured,
            item["track_id"],
        )

    genre = primary_genre(row)
    mood = primary_mood(row)

    if external_source not in (None, "", "local"):
        folder = DOWNLOAD_DIR / "external" / safe_filename(external_source) / safe_filename(genre) / safe_filename(mood)
    else:
        folder = DOWNLOAD_DIR / safe_filename(genre) / safe_filename(mood)

    folder.mkdir(
        parents=True,
        exist_ok=True,
    )

    base = (
        f"{safe_filename(item['artist'])}"
        f" - "
        f"{safe_filename(item['title'])}"
    )

    path = folder / (
        f"{base}.mp3"
    )

    if path.exists():
        path = folder / (
            f"{base} "
            f"[{item['track_id']}].mp3"
        )

    await download_file(
        client,
        url,
        path,
    )

    try:
        _validate_external_audio(path, external_source or "", expected_duration)
    except RuntimeError:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    save_download_history(
        item["track_id"],
        item["title"],
        item["artist"],
        path,
    )

    item["file_path"] = str(
        path
    )

    print(
        f"   ✅ {path}"
    )

    return path


# ============================================================
# PLAYLIST FILES
# ============================================================

def copy_playlist_tracks(
    items,
    playlist_dir,
):
    if not COPY_TRACKS_TO_PLAYLIST:
        return

    tracks_dir = (
        playlist_dir
        / "tracks"
    )

    tracks_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    for index, item in enumerate(
        items,
        1,
    ):
        source = Path(
            item["file_path"]
        )

        if not source.exists():
            continue

        destination = (
            tracks_dir
            / (
                f"{index:02d} - "
                f"{source.name}"
            )
        )

        shutil.copy2(
            source,
            destination,
        )

        item[
            "playlist_file_path"
        ] = str(
            destination
        )


def write_manifest(
    playlist_dir,
    items,
    target_seconds,
    video_title,
    ffmpeg_output,
):
    manifest = {
        "video_title": video_title,
        "video_duration": target_seconds,
        "music_duration": sum(
            item["duration"]
            for item in items
        ),
        "created_at":
            datetime.now().isoformat(
                timespec="seconds"
            ),
        "integrated_file":
            str(ffmpeg_output)
            if ffmpeg_output
            else None,
        "items": items,
    }

    path = (
        playlist_dir
        / "playlist.json"
    )

    path.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return path


def write_description(
    playlist_dir,
    items,
):
    path = (
        playlist_dir
        / "youtube_description.txt"
    )

    description = make_youtube_description(items)
    external_items = [item for item in items if item.get("source") and item.get("source") != "YouTube Audio Library"]
    if external_items:
        description += "\n\nMusic credits / 라이선스 정보\n"
        for item in external_items:
            description += f"- {item.get('title', 'Unknown')} - {item.get('artist', 'Unknown')}\n"
            description += f"  Source: {item.get('source_url') or item.get('source', '')}\n"
            if item.get("license_url"):
                description += f"  License: {item['license_url']}\n"
    path.write_text(description + "\n", encoding="utf-8")

    return path


# ============================================================
# FFMPEG
# ============================================================

def get_ffmpeg_path():
    configured = Path(
        FFMPEG_BIN
    )

    if configured.is_file():
        return str(
            configured
        )

    found = shutil.which(
        FFMPEG_BIN
    )

    if found:
        return found

    return None


def ffconcat_escape(path):
    value = str(
        Path(path).resolve()
    ).replace(
        "\\",
        "/",
    )

    value = value.replace(
        "'",
        "'\\''",
    )

    return (
        "'"
        + value
        + "'"
    )


def create_concat_file(
    items,
    path,
):
    with path.open(
        "w",
        encoding="utf-8",
        newline="\n",
    ) as file:

        file.write(
            "ffconcat version 1.0\n"
        )

        for item in items:
            file.write(
                "file "
                + ffconcat_escape(
                    item["file_path"]
                )
                + "\n"
            )


def merge_with_ffmpeg(
    items,
    output_path,
):
    ffmpeg = get_ffmpeg_path()

    if not ffmpeg:
        print()
        print(
            "⚠️ FFmpeg를 찾지 못했습니다."
        )
        print(
            "통합 MP3는 만들지 않고 "
            "개별 음원만 보존합니다."
        )
        return None

    concat_path = (
        output_path.parent
        / "concat.ffconcat"
    )

    create_concat_file(
        items,
        concat_path,
    )

    command = [
        ffmpeg,

        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",

        "-f",
        "concat",

        "-safe",
        "0",

        "-i",
        str(concat_path),

        "-vn",

        "-c:a",
        "libmp3lame",

        "-b:a",
        "192k",

        "-id3v2_version",
        "3",

        "-metadata",
        "title=YouTube Background Music",

        "-y",

        str(output_path),
    ]

    print()
    print(
        "🎧 통합 MP3 생성 중..."
    )

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=600,
        )

    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(
            f"⚠️ FFmpeg 실행 실패: {exc}"
        )
        return None

    finally:
        concat_path.unlink(
            missing_ok=True
        )

    if result.returncode != 0:
        print(
            "⚠️ FFmpeg concat 실패."
        )

        if result.stderr:
            print(
                result.stderr
            )

        print(
            "개별 음원은 그대로 보존합니다."
        )

        return None

    print(
        f"✅ 통합 MP3: "
        f"{output_path}"
    )

    return output_path


# ============================================================
# DRAFT
# ============================================================

def save_draft(
    items,
    target_seconds,
    video_title,
    playlist_dir,
    integrated_file,
    genre=None,
    mood=None,
):
    DRAFT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    draft = {
        "video_title": video_title,
        "video_duration": target_seconds,
        "playlist_dir":
            str(playlist_dir),
        "integrated_file":
            str(integrated_file)
            if integrated_file
            else None,
        "created_at":
            datetime.now().isoformat(
                timespec="seconds"
            ),
        "genre": genre,
        "mood": mood,
        "items": items,
    }

    latest_path = (
        DRAFT_DIR
        / "latest.json"
    )

    latest_path.write_text(
        json.dumps(
            draft,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return latest_path


def load_latest_draft():
    path = (
        DRAFT_DIR
        / "latest.json"
    )

    if not path.exists():
        return None

    return json.loads(
        path.read_text(
            encoding="utf-8"
        )
    )


def renumber_playlist_items(items):
    current = 0

    for position, item in enumerate(
        items,
        1,
    ):
        item["position"] = position
        item["start_seconds"] = current
        item["timestamp"] = format_time(
            current
        )

        current += int(
            item["duration"]
        )

    return items


def refresh_draft_files(draft):
    playlist_dir = Path(
        draft["playlist_dir"]
    )

    items = draft["items"]

    tracks_dir = (
        playlist_dir
        / "tracks"
    )

    # OneDrive 환경에서는 tracks 폴더 또는 내부 파일이 동기화/미리보기
    # 등의 이유로 잠겨 있어 shutil.rmtree()가 WinError 5를 발생시킬 수 있습니다.
    # 폴더 자체는 유지하고, 삭제 가능한 기존 파일만 정리한 뒤 남은 곡을 다시 복사합니다.
    tracks_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    expected_names = set()

    if COPY_TRACKS_TO_PLAYLIST:
        for index, item in enumerate(items, 1):
            source = Path(item["file_path"])
            if source.exists():
                expected_names.add(
                    f"{index:02d} - {source.name}"
                )

    for existing in tracks_dir.iterdir():
        if existing.is_file() and existing.name not in expected_names:
            try:
                existing.unlink()
            except PermissionError:
                print(
                    f"⚠️ 잠긴 파일은 삭제하지 않고 유지합니다: {existing.name}"
                )

    copy_playlist_tracks(
        items,
        playlist_dir,
    )

    integrated_path = (
        playlist_dir
        / "background_music.mp3"
    )

    integrated_file = merge_with_ffmpeg(
        items,
        integrated_path,
    )

    draft["integrated_file"] = (
        str(integrated_file)
        if integrated_file
        else None
    )

    write_description(
        playlist_dir,
        items,
    )

    write_manifest(
        playlist_dir,
        items,
        int(draft["video_duration"]),
        draft.get("video_title") or "",
        integrated_file,
    )

    DRAFT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    latest_path = (
        DRAFT_DIR
        / "latest.json"
    )

    latest_path.write_text(
        json.dumps(
            draft,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return latest_path


async def remove_tracks_from_last_draft(
    client,
    captured,
):
    draft = load_latest_draft()

    if not draft:
        print(
            "\n제거할 마지막 플레이리스트 draft가 없습니다."
        )
        return

    items = draft.get("items") or []

    if not items:
        print(
            "\n현재 플레이리스트에 곡이 없습니다."
        )
        return

    print()
    print("=" * 80)
    print("🎧 마지막 플레이리스트에서 곡 제거")
    print("=" * 80)

    for item in items:
        print(
            f"{item['position']:2}. "
            f"{item['timestamp']}  "
            f"{item['artist']} - "
            f"{item['title']}"
        )

    print()
    value = input(
        "제거할 곡 번호 (예: 3,7,10 / q: 취소): "
    ).strip()

    if value.lower() == "q":
        print("❌ 제거 취소")
        return

    try:
        positions = sorted({
            int(part.strip())
            for part in value.split(",")
            if part.strip()
        })
    except ValueError:
        print(
            "❌ 곡 번호는 쉼표로 구분된 숫자로 입력하세요. "
            "예: 3,7,10"
        )
        return

    if not positions:
        print("❌ 제거할 곡이 없습니다.")
        return

    invalid = [
        position
        for position in positions
        if position < 1
        or position > len(items)
    ]

    if invalid:
        print(
            "❌ 존재하지 않는 곡 번호: "
            + ", ".join(
                map(str, invalid)
            )
        )
        return

    removed = [
        item
        for item in items
        if item["position"] in positions
    ]

    remaining = [
        item
        for item in items
        if item["position"] not in positions
    ]

    if not remaining:
        print(
            "\n⚠️ 모든 곡을 제거할 수는 없습니다. "
            "최소 1곡은 남겨주세요."
        )
        return

    removed_seconds = sum(
        int(item["duration"])
        for item in removed
    )

    print()
    print("제거할 곡:")

    for item in removed:
        print(
            f"  ❌ {item['artist']} - "
            f"{item['title']} "
            f"[{format_time(int(item['duration']))}]"
        )

    print(
        f"\n빈자리: {format_time(removed_seconds)}"
    )

    # --------------------------------------------------------
    # 처음 선택했던 장르 / Mood를 유지합니다.
    # 이전 draft에 이 정보가 없으면 첫 곡의 값을 fallback으로 사용합니다.
    # --------------------------------------------------------
    genre = draft.get("genre")
    mood = draft.get("mood")

    if genre is None and items:
        genre = items[0].get("genre")

    if mood is None and items:
        mood = items[0].get("mood")

    # 현재 플레이리스트에 이미 들어있는 곡은 대체 후보에서 제외합니다.
    existing_track_ids = {
        item["track_id"]
        for item in items
    }

    candidates = get_candidate_tracks(
        genre,
        mood,
    )

    candidates = [
        row
        for row in candidates
        if row["track_id"] not in existing_track_ids
    ]

    replacements = find_best_playlist(
        candidates,
        removed_seconds,
    )

    if not replacements:
        print(
            "\n⚠️ 같은 장르 / Mood에서 빈자리를 채울 "
            "미사용 곡을 찾지 못했습니다."
        )
        print(
            "제거 작업을 취소하고 기존 플레이리스트를 유지합니다."
        )
        return

    replacement_items = build_playlist_items(
        replacements
    )

    print()
    print("대체할 곡:")
    replacement_seconds = sum(
        int(item["duration"])
        for item in replacement_items
    )

    for item in replacement_items:
        print(
            f"  ➕ {item['artist']} - "
            f"{item['title']} "
            f"[{format_time(int(item['duration']))}]"
        )

    print(
        f"대체 음악 길이: {format_time(replacement_seconds)} "
        f"/ 빈자리: {format_time(removed_seconds)}"
    )
    print(
        f"남는 시간: {format_time(removed_seconds - replacement_seconds)}"
    )

    # 대체곡을 실제로 다운로드합니다.
    print()
    print("=" * 80)
    print("⬇️ 대체 음악 다운로드")
    print("=" * 80)

    for index, item in enumerate(
        replacement_items,
        1,
    ):
        print(
            f"\n[{index}/{len(replacement_items)}]"
        )

        await ensure_downloaded(
            client,
            captured,
            item,
        )

        await asyncio.sleep(
            random.uniform(0.3, 0.8)
        )

    # 제거한 곡은 다시 추천되지 않도록 used.txt에 기록하고
    # DB의 사용 이력에도 반영합니다.
    existing_used = set()

    if USED_FILE.exists():
        existing_used = {
            normalize_text(line).casefold()
            for line in USED_FILE.read_text(
                encoding="utf-8-sig"
            ).splitlines()
            if normalize_text(line)
            and not normalize_text(line).startswith("#")
        }

    used_to_add = []

    for item in removed:
        used_entry = (
            f"{item['artist']} - {item['title']}"
        )

        if used_entry.casefold() not in existing_used:
            used_to_add.append(used_entry)
            existing_used.add(used_entry.casefold())

    if used_to_add:
        USED_FILE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with USED_FILE.open(
            "a",
            encoding="utf-8",
            newline=""
        ) as file:
            for entry in used_to_add:
                file.write(entry + "\n")

    conn = db()
    now = datetime.now().isoformat(
        timespec="seconds"
    )

    for item in removed:
        conn.execute("""
            INSERT INTO track_usage (
                track_id,
                use_count,
                first_used_at,
                last_used_at
            )
            VALUES (?, 1, ?, ?)

            ON CONFLICT(track_id)
            DO UPDATE SET
                use_count =
                    CASE
                        WHEN track_usage.use_count = 0
                        THEN 1
                        ELSE track_usage.use_count
                    END,
                first_used_at =
                    COALESCE(
                        track_usage.first_used_at,
                        excluded.first_used_at
                    ),
                last_used_at =
                    excluded.last_used_at
        """, (
            item["track_id"],
            now,
            now,
        ))

    conn.commit()
    conn.close()

    # 기존 곡 + 대체곡을 하나의 플레이리스트로 구성합니다.
    remaining.extend(
        replacement_items
    )

    renumber_playlist_items(
        remaining
    )

    draft["genre"] = genre
    draft["mood"] = mood
    draft["items"] = remaining

    refresh_draft_files(
        draft
    )

    print()
    print("=" * 80)
    print("✅ 플레이리스트 수정 완료")
    print("=" * 80)
    print(
        f"제거: {len(removed)}곡 / "
        f"{format_time(removed_seconds)}"
    )
    print(
        f"대체: {len(replacement_items)}곡 / "
        f"{format_time(replacement_seconds)}"
    )
    print(
        f"남는 음악 시간: "
        f"{format_time(removed_seconds - replacement_seconds)}"
    )
    print(
        f"전체 음악 길이: "
        f"{format_time(sum(int(item['duration']) for item in remaining))}"
    )
    print(
        f"통합 MP3: {draft.get('integrated_file') or '생성되지 않음'}"
    )
    print(
        f"Draft: {DRAFT_DIR / 'latest.json'}"
    )
    print(
        "\n※ 제거한 곡은 다운로드 폴더에서 삭제하지 않습니다. "
        "used.txt 및 사용 이력에 기록되어 다음 플레이리스트에서는 제외됩니다."
    )


# ============================================================
# COMMIT
# ============================================================

def commit_playlist(
    draft,
):
    items = draft["items"]

    if not items:
        return

    conn = db()

    try:
        conn.execute(
            "BEGIN"
        )

        now = datetime.now().isoformat(
            timespec="seconds"
        )

        music_duration = sum(
            int(item["duration"])
            for item in items
        )

        cursor = conn.execute("""
            INSERT INTO playlist_history (
                video_title,
                video_duration,
                music_duration,
                created_at
            )
            VALUES (?, ?, ?, ?)
        """, (
            draft.get(
                "video_title"
            ),
            int(
                draft[
                    "video_duration"
                ]
            ),
            music_duration,
            now,
        ))

        playlist_id = (
            cursor.lastrowid
        )

        for position, item in enumerate(
            items,
            1,
        ):
            conn.execute("""
                INSERT INTO track_usage (
                    track_id,
                    use_count,
                    first_used_at,
                    last_used_at
                )
                VALUES (?, 1, ?, ?)

                ON CONFLICT(track_id)
                DO UPDATE SET
                    use_count =
                        track_usage.use_count + 1,
                    last_used_at =
                        excluded.last_used_at
            """, (
                item["track_id"],
                now,
                now,
            ))

            conn.execute("""
                INSERT INTO playlist_items (
                    playlist_id,
                    track_id,
                    position,
                    start_seconds,
                    duration,
                    title,
                    artist,
                    file_path
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                playlist_id,
                item["track_id"],
                position,
                item["start_seconds"],
                item["duration"],
                item["title"],
                item["artist"],
                item["file_path"],
            ))

        conn.commit()

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()

    print()
    print("=" * 70)
    print(
        "✅ COMMIT 완료"
    )
    print(
        f"Playlist ID: {playlist_id}"
    )
    print(
        f"사용 처리: {len(items)}곡"
    )
    # Commit된 곡은 기존 DB 사용 이력과 함께 used.txt에도 기록합니다.
    existing_used = set()
    if USED_FILE.exists():
        existing_used = {
            normalize_text(line).casefold()
            for line in USED_FILE.read_text(encoding="utf-8-sig").splitlines()
            if normalize_text(line) and not normalize_text(line).startswith("#")
        }
    used_to_add = []
    for item in items:
        entry = f"{item['artist']} - {item['title']}"
        if entry.casefold() not in existing_used:
            used_to_add.append(entry)
            existing_used.add(entry.casefold())
    if used_to_add:
        USED_FILE.parent.mkdir(parents=True, exist_ok=True)
        with USED_FILE.open("a", encoding="utf-8", newline="") as file:
            for entry in used_to_add:
                file.write(entry + "\n")

    print(
        "used.txt 및 DB 사용 이력에 기록되어 다음 플레이리스트 후보에서 제외됩니다."
    )
    print("=" * 70)


def commit_last_draft():
    draft = load_latest_draft()

    if not draft:
        print(
            "\nCommit할 draft가 없습니다."
        )
        return

    print()
    print("=" * 80)
    print("🎧 COMMIT 확인")
    print("=" * 80)

    print(
        f"영상: "
        f"{draft.get('video_title') or '(제목 없음)'}"
    )

    print(
        f"영상 길이: "
        f"{format_time(draft['video_duration'])}"
    )

    print()

    for item in draft["items"]:
        print(
            f"{item['timestamp']} "
            f"{item['artist']} - "
            f"{item['title']}"
        )

    print()

    answer = input(
        "이 음악들을 실제 사용 처리할까요? "
        "(y/N): "
    ).strip().lower()

    if answer != "y":
        print(
            "❌ Commit 취소"
        )
        return

    commit_playlist(
        draft
    )


# ============================================================
# HISTORY / STATUS
# ============================================================

def show_usage():
    conn = db()

    rows = conn.execute("""
        SELECT
            t.title,
            t.artist,
            tu.use_count,
            tu.first_used_at,
            tu.last_used_at
        FROM track_usage tu
        INNER JOIN tracks t
            ON t.track_id = tu.track_id
        ORDER BY tu.last_used_at DESC
    """).fetchall()

    conn.close()

    print()
    print("=" * 80)
    print("🎵 MUSIC USAGE")
    print("=" * 80)

    if not rows:
        print(
            "사용 처리된 음악이 없습니다."
        )
        return

    for index, row in enumerate(
        rows,
        1,
    ):
        print(
            f"{index:3}. "
            f"{row['artist']} - "
            f"{row['title']}"
        )

        print(
            f"     사용 횟수: "
            f"{row['use_count']}"
        )

        print(
            f"     최근 사용: "
            f"{row['last_used_at']}"
        )


def show_history():
    conn = db()

    rows = conn.execute("""
        SELECT
            id,
            video_title,
            video_duration,
            music_duration,
            created_at
        FROM playlist_history
        ORDER BY created_at DESC
    """).fetchall()

    conn.close()

    print()
    print("=" * 80)
    print("📋 PLAYLIST HISTORY")
    print("=" * 80)

    if not rows:
        print(
            "플레이리스트 이력이 없습니다."
        )
        return

    for row in rows:
        print(
            f"[{row['id']}] "
            f"{row['video_title'] or '(제목 없음)'}"
        )

        print(
            f"    영상: "
            f"{format_time(row['video_duration'])}"
        )

        print(
            f"    음악: "
            f"{format_time(row['music_duration'])}"
        )

        print(
            f"    생성: "
            f"{row['created_at']}"
        )


def show_status():
    conn = db()

    total = conn.execute(
        "SELECT COUNT(*) FROM tracks"
    ).fetchone()[0]

    downloaded = conn.execute(
        "SELECT COUNT(*) FROM downloaded_tracks"
    ).fetchone()[0]

    used = conn.execute(
        "SELECT COUNT(*) FROM track_usage"
    ).fetchone()[0]

    conn.close()

    print()
    print(
        f"메타데이터: {total:,}곡"
    )

    print(
        f"다운로드됨: {downloaded:,}곡"
    )

    print(
        f"사용 처리됨: {used:,}곡"
    )

    print()
    print(
        "현재 필터:"
    )

    print(
        f"  최소 곡 길이: "
        f"{format_time(MIN_TRACK_DURATION)}"
    )

    if MAX_TRACK_DURATION > 0:
        print(
            f"  최대 곡 길이: "
            f"{format_time(MAX_TRACK_DURATION)}"
        )
    else:
        print(
            "  최대 곡 길이: 제한 없음"
        )

    print(
        f"  최대 곡 수: "
        f"{MAX_TRACKS_PER_PLAYLIST}"
    )

    print(
        f"  선호 곡 길이: "
        f"{format_time(PREFERRED_TRACK_DURATION)}"
    )


# ============================================================
# CREATE PLAYLIST
# ============================================================

async def create_playlist(
    client,
    captured,
    source_filter=None,
):
    if source_filter:
        source_candidates = get_candidate_tracks(None, None, source_filter=source_filter)
        genres = sorted({g for row in source_candidates for g in row_genres(row)}, key=lambda x: display_genre(x).lower())
        moods = sorted({m for row in source_candidates for m in row_moods(row)}, key=lambda x: display_mood(x).lower())
    else:
        source_candidates = None
        genres = get_all_genres()
        moods = get_all_moods()

    if not genres:
        print(
            "장르 데이터가 없습니다."
        )
        return

    duration_input = input(
        "\n영상 길이 "
        "(예: 15:30): "
    ).strip()

    try:
        target_seconds = parse_duration(
            duration_input
        )

    except ValueError as exc:
        print(
            f"\n❌ {exc}"
        )
        return

    if target_seconds <= 0:
        return

    print()
    print("=" * 70)
    print("장르 선택")
    print("=" * 70)

    genre = choose_from_list(
        genres,
        display_genre,
        "장르",
    )

    print()
    print("=" * 70)
    print("Mood 선택")
    print("=" * 70)

    mood = choose_from_list(
        moods,
        display_mood,
        "Mood",
    )

    if source_filter:
        candidates = [
            row for row in source_candidates
            if (genre is None or genre in row_genres(row))
            and (mood is None or mood in row_moods(row))
        ]
    else:
        candidates = get_candidate_tracks(
            genre,
            mood,
        )

    print()
    print(
        f"필터 통과 미사용 곡: "
        f"{len(candidates):,}곡"
    )

    if not candidates:
        print(
            "조건에 맞는 곡이 없습니다."
        )
        return

    selected = find_best_playlist(
        candidates,
        target_seconds,
    )

    if not selected:
        print(
            "적합한 플레이리스트를 "
            "만들지 못했습니다."
        )
        return

    items = build_playlist_items(
        selected
    )

    print_playlist(
        items,
        target_seconds,
    )

    video_title = input(
        "\n영상 제목 "
        "(Enter = 없음): "
    ).strip()

    timestamp = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    folder_name = (
        f"{timestamp}_"
        f"{'external_' if source_filter else ''}{safe_filename(video_title or 'background_music')}"
    )

    playlist_dir = (
        PLAYLIST_DIR
        / folder_name
    )

    playlist_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # Download only selected tracks
    # --------------------------------------------------------

    print()
    print("=" * 80)
    print("⬇️ 선택된 음악만 다운로드")
    print("=" * 80)

    if source_filter:
        # External providers are downloaded concurrently with a small cap to avoid
        # overloading a source or creating too many simultaneous connections.
        semaphore = asyncio.Semaphore(3)

        async def download_one(index, item):
            async with semaphore:
                print(f"\n[{index}/{len(items)}] {item['artist']} - {item['title']}")
                return await ensure_downloaded(client, captured, item)

        results = await asyncio.gather(
            *(download_one(index, item) for index, item in enumerate(items, 1)),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            for error in failures:
                print(f"❌ 외부 음원 다운로드 실패: {error}")
            print("다운로드 실패가 있어 합본 생성을 중단했습니다. 다시 시도할 수 있습니다.")
            return
    else:
        for index, item in enumerate(items, 1):
            print(f"\n[{index}/{len(items)}]")
            await ensure_downloaded(client, captured, item)
            await asyncio.sleep(random.uniform(0.3, 0.8))

    # --------------------------------------------------------
    # Copy individual tracks into playlist package
    # --------------------------------------------------------

    copy_playlist_tracks(
        items,
        playlist_dir,
    )

    # --------------------------------------------------------
    # FFmpeg
    # --------------------------------------------------------

    integrated_path = (
        playlist_dir
        / "background_music.mp3"
    )

    integrated_file = (
        merge_with_ffmpeg(
            items,
            integrated_path,
        )
    )

    # --------------------------------------------------------
    # Metadata / YouTube description
    # --------------------------------------------------------

    description_path = (
        write_description(
            playlist_dir,
            items,
        )
    )

    manifest_path = (
        write_manifest(
            playlist_dir,
            items,
            target_seconds,
            video_title,
            integrated_file,
        )
    )

    draft_path = save_draft(
        items,
        target_seconds,
        video_title,
        playlist_dir,
        integrated_file,
        genre,
        mood,
    )

    # --------------------------------------------------------
    # Review
    # --------------------------------------------------------

    print()
    print("=" * 80)
    print("🎧 REVIEW READY")
    print("=" * 80)

    print(
        f"플레이리스트 폴더:\n"
        f"{playlist_dir}"
    )

    if integrated_file:
        print()
        print(
            f"통합 MP3:\n"
            f"{integrated_file}"
        )
    else:
        print()
        print(
            "⚠️ 통합 MP3는 생성되지 않았습니다."
        )

    print()
    print(
        f"개별 음원 폴더:\n"
        f"{playlist_dir / 'tracks'}"
    )

    print()
    print(
        f"YouTube 설명란:\n"
        f"{description_path}"
    )

    print()
    print(
        f"Playlist metadata:\n"
        f"{manifest_path}"
    )

    print()
    print(
        "⚠️ 아직 사용 처리하지 않았습니다."
    )

    print(
        "음원을 들어보고 괜찮으면"
    )

    print(
        "메뉴에서 "
        "'마지막 플레이리스트 Commit'을 선택하세요."
    )

    print()
    print(
        f"Draft:\n{draft_path}"
    )


# ============================================================
# EXTERNAL SOURCE HELPERS
# ============================================================

def get_external_sources():
    """List external providers currently registered as playable tracks."""
    conn = db()
    try:
        rows = conn.execute("""
            SELECT DISTINCT external_source
            FROM tracks
            WHERE external_source IS NOT NULL
              AND external_source NOT IN ('', 'local')
              AND COALESCE(external_eligible, 0) = 1
            ORDER BY external_source
        """).fetchall()
        return [row["external_source"] for row in rows]
    finally:
        conn.close()


def choose_external_source():
    sources = get_external_sources()
    if not sources:
        print("외부 플레이리스트 후보가 없습니다. 메뉴 9에서 카탈로그를 업데이트하세요.")
        return None
    options = ["모든 외부 사이트"] + sources
    print("\n외부 음원 소스 선택")
    for index, value in enumerate(options, 1):
        label = "모든 외부 사이트" if value == "모든 외부 사이트" else value.capitalize()
        print(f"{index:2}. {label}")
    while True:
        choice = input("\n소스 번호: ").strip()
        try:
            index = int(choice)
            if 1 <= index <= len(options):
                return "external" if index == 1 else options[index - 1]
        except ValueError:
            pass
        print("올바른 번호를 입력하세요.")


# ============================================================
# MAIN
# ============================================================

async def main():
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"DB가 없습니다:\n"
            f"{DB_PATH}\n\n"
            "먼저 collect.py를 실행하세요."
        )

    init_db()

    DOWNLOAD_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    PLAYLIST_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    DRAFT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    async with async_playwright() as p:
        # YouTube browser authentication is initialized lazily. External-only
        # playlist generation must work even when Chrome/YouTube Studio is closed.
        browser = None
        context = None
        page = None
        capture = GetTracksCapture()
        listener = None

        async def ensure_youtube_capture():
            nonlocal browser, context, page, listener

            if context is None:
                browser = await p.chromium.connect_over_cdp(CDP_URL)
                if not browser.contexts:
                    raise RuntimeError(
                        "Chrome BrowserContext를 찾을 수 없습니다. Chrome을 디버깅 포트로 실행했는지 확인하세요."
                    )
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else await context.new_page()

                def request_listener(request):
                    asyncio.create_task(capture.handle_request(request))

                listener = request_listener
                page.on("request", listener)

            if capture.request is not None:
                capture.request["api_request"] = context.request
                return capture.request

            try:
                await page.goto(
                    STUDIO_URL,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
            except Exception as exc:
                print(f"⚠️ Studio 이동 알림: {exc}")

            print()
            print("YouTube Audio Library 다운로드 인증이 필요합니다.")
            print("Studio 음악 페이지에서 아무 곡의 Download 버튼을 한 번 눌러주세요.")
            try:
                await asyncio.wait_for(capture.event.wait(), timeout=300)
            except asyncio.TimeoutError:
                raise RuntimeError("5분 내 get_tracks 요청을 캡처하지 못했습니다.")

            capture.request["api_request"] = context.request
            return capture.request

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(
                90.0,
                connect=30.0,
            ),
        ) as client:

            while True:

                print()
                print("=" * 70)
                print(
                    "YouTube Music Manager"
                )
                print("=" * 70)

                show_status()

                print()
                print(
                    "1. 새 플레이리스트 만들기"
                )

                print(
                    "2. 마지막 플레이리스트 Commit"
                )

                print(
                    "3. 장르 목록"
                )

                print(
                    "4. Mood 목록"
                )

                print(
                    "5. 음악 사용 이력"
                )

                print(
                    "6. 플레이리스트 이력"
                )

                print(
                    "7. 기존 used.txt 가져오기"
                )

                print(
                    "8. 마지막 플레이리스트에서 싫은 곡 제거"
                )

                print(
                    "9. 외부 음원 메타데이터 업데이트 + 내 폴더 음원 등록"
                )

                print(
                    "10. 외부 음원 플레이리스트 만들기 + 다운로드 (사이트별 장르/Mood)"
                )

                print(
                    "0. 종료"
                )

                choice = input(
                    "\n선택: "
                ).strip()

                if choice == "1":

                    captured = await ensure_youtube_capture()
                    await create_playlist(
                        client,
                        captured,
                    )

                elif choice == "2":

                    commit_last_draft()

                elif choice == "3":

                    show_genres()

                elif choice == "4":

                    show_moods()

                elif choice == "5":

                    show_usage()

                elif choice == "6":

                    show_history()

                elif choice == "7":

                    import_legacy_used_file()

                elif choice == "8":

                    captured = await ensure_youtube_capture()
                    await remove_tracks_from_last_draft(
                        client,
                        captured,
                    )

                elif choice == "9":

                    print("\n[1/2] 직접 받은 음원 폴더 검색")
                    registered, skipped = scan_local_music()
                    print(f"내 폴더 음원 등록: {registered}곡 / 길이 확인 불가로 건너뜀: {skipped}곡")
                    print("\n메타데이터 수집량은 MP3 다운로드량이 아닙니다. 음원 파일은 메뉴 10에서 선택한 곡만 받습니다.")
                    raw_limit = input("Jamendo에서 수집할 총 메타데이터 수 (기본 2000, 최대 20000): ").strip()
                    try:
                        jamendo_limit = int(raw_limit) if raw_limit else 2000
                        if jamendo_limit < 1 or jamendo_limit > 20000:
                            raise ValueError
                    except ValueError:
                        print("잘못된 수집량입니다. 기본값 2000을 사용합니다.")
                        jamendo_limit = 2000
                    jamendo_selected = False
                    if not JAMENDO_CLIENT_ID:
                        print("Jamendo는 .env의 JAMENDO_CLIENT_ID가 없어 건너뜁니다.")
                    else:
                        jamendo_selected = input("\nJamendo 메타데이터를 페이지 단위로 수집할까요? (y/N): ").strip().lower() == "y"
                    open_limit_raw = input("Openverse에서 수집할 메타데이터 수 (기본 500, 최대 5000, 0=건너뛰기): ").strip()
                    try:
                        open_limit = int(open_limit_raw) if open_limit_raw else 500
                        if open_limit < 0 or open_limit > 5000:
                            raise ValueError
                    except ValueError:
                        print("잘못된 수집량입니다. 기본값 500을 사용합니다.")
                        open_limit = 500
                    open_query = "instrumental music"
                    if open_limit:
                        open_query = input("Openverse 검색어 (Enter=instrumental music): ").strip() or "instrumental music"

                    # Query the selected providers concurrently; each adapter writes
                    # only its own provider section in the shared catalog.
                    jobs = []
                    job_names = []
                    if jamendo_selected:
                        jobs.append(update_jamendo_catalog(JAMENDO_CLIENT_ID, limit=jamendo_limit))
                        job_names.append("Jamendo")
                    if open_limit:
                        jobs.append(update_openverse_catalog(limit=open_limit, query=open_query))
                        job_names.append("Openverse")
                    if jobs:
                        results = await asyncio.gather(*jobs, return_exceptions=True)
                        for provider_name, result in zip(job_names, results):
                            if isinstance(result, Exception):
                                print(f"❌ {provider_name} 업데이트 실패: {result}")
                            else:
                                metadata_count, eligible_count = result
                                print(f"{provider_name} 메타데이터 저장: {metadata_count}곡")
                                print(f"{provider_name} 플레이리스트 후보 통과: {eligible_count}곡")
                    print(f"\n통합 카탈로그: {CATALOG_PATH.relative_to(BASE_DIR)}")
                    print("\n주의: 자동 필터는 라이선스 위험을 줄일 뿐, 수익화 권리를 보증하지 않습니다. 게시 전 원본 페이지와 라이선스를 확인하세요.")

                elif choice == "10":

                    source_filter = choose_external_source()
                    if source_filter:
                        await create_playlist(
                            client,
                            None,
                            source_filter=source_filter,
                        )

                elif choice == "0":

                    break

                else:

                    print(
                        "올바른 메뉴를 선택하세요."
                    )

        if page is not None and listener is not None:
            page.remove_listener("request", listener)


if __name__ == "__main__":
    asyncio.run(
        main()
    )