import dataclasses
import shutil
import subprocess
from pathlib import Path

import pytest

from music_crawler.config import Settings
from music_crawler.db import Database
from music_crawler.models import Track

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed")


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = dataclasses.replace(
        Settings(home=tmp_path),
        min_track_seconds=10,
        crossfade_seconds=2,
        max_per_artist=2,
        normalize_loudness=False,
    )
    s.ensure_dirs()
    return s


@pytest.fixture
def db(settings) -> Database:
    with Database(settings.db_path) as database:
        yield database


def make_tone(path: Path, seconds: int, freq: int = 440) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={seconds}",
            "-b:a",
            "128k",
            str(path),
        ],
        check=True,
    )
    return path


def make_track(
    i: int,
    *,
    duration=200,
    artist=None,
    genres=("Jazz",),
    moods=("calm",),
    vocals="instrumental",
    source="jamendo",
    license="cc-by",
    **extra,
) -> Track:
    return Track(
        id=f"{source}:{i}",
        source=source,
        source_id=str(i),
        title=f"Song {i}",
        artist=artist or f"Artist {i}",
        duration=duration,
        genres=list(genres),
        moods=list(moods),
        vocals=vocals,
        license=license,
        license_url="https://creativecommons.org/licenses/by/4.0/",
        source_url=f"https://example.com/{i}",
        **extra,
    )
