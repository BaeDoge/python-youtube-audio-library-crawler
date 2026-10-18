"""Download + validate audio files into ``library/<source>/``.

A file only counts as downloaded once it passed: size check, magic-byte check, ffprobe, and
(atomic) rename from ``.part``. Permanent failures (HTTP 403/404/410, not audio) mark the
track ``invalid`` so it is never suggested again; transient failures are retried.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import httpx

from music_crawler.audio import AudioError, file_hash, looks_like_audio, probe
from music_crawler.config import Settings
from music_crawler.db import Database
from music_crawler.models import Track, slugify

MIN_BYTES = 50_000
PERMANENT_HTTP = {400, 401, 403, 404, 410}


class DownloadError(RuntimeError):
    def __init__(self, message: str, permanent: bool = False):
        super().__init__(message)
        self.permanent = permanent


def _extension(url: str, content_type: str = "") -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".mp3", ".ogg", ".wav", ".flac", ".m4a", ".opus"}:
        return suffix
    if "ogg" in content_type:
        return ".ogg"
    if "flac" in content_type:
        return ".flac"
    if "wav" in content_type:
        return ".wav"
    return ".mp3"


def target_path(settings: Settings, track: Track, ext: str = ".mp3") -> Path:
    return settings.library_dir / track.source / f"{slugify(track.source_id, 'track')}{ext}"


def existing_file(settings: Settings, track: Track) -> Path | None:
    if track.file_path:
        path = settings.from_stored(track.file_path)
        if path.exists():
            return path
    return None


def _fetch(client: httpx.Client, url: str, dest: Path, sleep: Callable[[float], None]) -> str:
    """Stream ``url`` to ``dest``; returns the content-type. Retries transient errors."""
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            with client.stream("GET", url) as response:
                if response.status_code == 429:
                    sleep(min(30.0, float(response.headers.get("Retry-After", "2") or 2)))
                    raise DownloadError("HTTP 429")
                if response.status_code in PERMANENT_HTTP:
                    raise DownloadError(f"HTTP {response.status_code}", permanent=True)
                response.raise_for_status()
                with dest.open("wb") as fh:
                    for chunk in response.iter_bytes(1 << 20):
                        fh.write(chunk)
                return response.headers.get("content-type", "")
        except DownloadError as exc:
            if exc.permanent:
                raise
            last = exc
        except httpx.HTTPError as exc:
            last = exc
        sleep(1.0 * attempt)
    raise DownloadError(f"다운로드 실패(3회 재시도): {last}")


def finalize(
    settings: Settings, db: Database, track: Track, part: Path, url: str, content_type: str = ""
) -> Path:
    """Validate a finished ``.part`` file and move it into the library (raises DownloadError)."""
    final = target_path(settings, track, _extension(url, content_type))
    try:
        if part.stat().st_size < MIN_BYTES or not looks_like_audio(part):
            raise DownloadError("오디오 파일이 아닙니다(오류 페이지/빈 파일).", permanent=True)
        final.parent.mkdir(parents=True, exist_ok=True)
        part.replace(final)
        info = probe(settings, final)
        if (
            info["duration"] < settings.min_track_seconds
            and track.duration >= info["duration"] + 15
        ):
            # metadata said 4:00 but the file is 0:30 — don't build playlists on wrong lengths
            final.unlink(missing_ok=True)
            raise DownloadError(
                f"실제 길이 {info['duration']}s (메타데이터 {track.duration}s)", permanent=True
            )
        db.set_file(track.id, settings.to_stored(final), info["duration"], file_hash(final))
        return final
    except DownloadError as exc:
        if exc.permanent:
            db.set_status(track.id, "invalid", str(exc))
        raise
    except AudioError as exc:
        db.set_status(track.id, "invalid", str(exc))
        raise DownloadError(str(exc), permanent=True) from exc


def download_one(
    settings: Settings,
    db: Database,
    track: Track,
    client: httpx.Client,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    found = existing_file(settings, track)
    if found:
        return found
    if not track.download_url:
        raise DownloadError("다운로드 URL이 없습니다 (이 출처는 전용 다운로더가 필요합니다).")
    part = target_path(settings, track, ".part")
    part.parent.mkdir(parents=True, exist_ok=True)
    try:
        content_type = _fetch(client, track.download_url, part, sleep)
        return finalize(settings, db, track, part, track.download_url, content_type)
    except DownloadError as exc:
        if exc.permanent:
            db.set_status(track.id, "invalid", str(exc))
        raise
    finally:
        part.unlink(missing_ok=True)


def download_many(
    settings: Settings,
    db: Database,
    tracks: list[Track],
    *,
    jobs: int = 4,
    progress: Callable[[str], None] = lambda _m: None,
) -> tuple[dict[str, Path], dict[str, str]]:
    """Download in parallel. Returns ``(ok: id→path, failed: id→reason)``."""
    ok: dict[str, Path] = {}
    failed: dict[str, str] = {}
    todo = []
    for t in tracks:
        found = existing_file(settings, t)
        if found:
            ok[t.id] = found
        else:
            todo.append(t)
    yt = [t for t in todo if t.source == "youtube"]
    todo = [t for t in todo if t.source != "youtube"]
    if yt:
        try:
            from music_crawler.providers import youtube_library

            y_ok, y_failed = youtube_library.download_tracks(settings, db, yt, progress)
            ok.update(y_ok)
            failed.update(y_failed)
        except ImportError as exc:
            for t in yt:
                failed[t.id] = f"YouTube 다운로드에는 playwright가 필요합니다: {exc}"
    if not todo:
        return ok, failed

    def job(track: Track):
        # SQLite connections are not shared across threads → open one per worker call.
        with (
            Database(db.path) as local_db,
            httpx.Client(
                timeout=httpx.Timeout(90.0, connect=30.0),
                follow_redirects=True,
                headers={"User-Agent": settings.user_agent},
            ) as client,
        ):
            return download_one(settings, local_db, track, client)

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = {pool.submit(job, t): t for t in todo}
        for future, track in futures.items():
            try:
                ok[track.id] = future.result()
                progress(f"  ✓ {track.label}")
            except Exception as exc:  # noqa: BLE001 - report every failure, keep going
                failed[track.id] = str(exc)
                progress(f"  ✗ {track.label}: {exc}")
    return ok, failed
