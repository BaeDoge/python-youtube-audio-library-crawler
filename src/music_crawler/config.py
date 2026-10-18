"""Runtime settings, loaded from environment variables (and a local ``.env``)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LICENSES = ("cc0", "pdm", "cc-by", "yt-audio-library", "pixabay", "user")


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    home: Path
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    jamendo_client_id: str = ""
    openverse_client_id: str = ""
    openverse_client_secret: str = ""
    youtube_channel_id: str = ""
    cdp_url: str = "http://127.0.0.1:9222"
    min_track_seconds: int = 120
    max_track_seconds: int = 0  # 0 = unlimited
    preferred_track_seconds: int = 240
    max_per_artist: int = 2
    crossfade_seconds: int = 3
    normalize_loudness: bool = True
    copy_tracks: bool = True  # also write each song into <playlist>/tracks/
    mp3_bitrate: str = "192k"
    allowed_licenses: tuple[str, ...] = DEFAULT_LICENSES
    user_agent: str = "music-crawler/0.2 (personal background-music tool)"

    # ---- derived paths -------------------------------------------------
    @property
    def data_dir(self) -> Path:
        return self.home / "data"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "music.db"

    @property
    def library_dir(self) -> Path:
        return self.home / "library"

    @property
    def inbox_dir(self) -> Path:
        """Drop manually downloaded audio here (``inbox/<genre>/<mood>/file.mp3``)."""
        return self.library_dir / "inbox"

    @property
    def playlists_dir(self) -> Path:
        return self.home / "output" / "playlists"

    @property
    def profiles_file(self) -> Path:
        return self.home / "config" / "profiles.toml"

    def to_stored(self, path: Path) -> str:
        """Path as stored in the DB: relative to the project home when possible (portable)."""
        path = Path(path).resolve()
        try:
            return path.relative_to(self.home).as_posix()
        except ValueError:
            return str(path)

    def from_stored(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.home / path

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.library_dir, self.inbox_dir, self.playlists_dir):
            path.mkdir(parents=True, exist_ok=True)

    # ---- loading -------------------------------------------------------
    @classmethod
    def load(cls, home: str | Path | None = None) -> Settings:
        from dotenv import load_dotenv

        base = Path(home or os.getenv("MUSIC_CRAWLER_HOME") or Path.cwd()).resolve()
        load_dotenv(base / ".env", override=False)
        licenses = tuple(
            part.strip().lower()
            for part in os.getenv("ALLOWED_LICENSES", ",".join(DEFAULT_LICENSES)).split(",")
            if part.strip()
        )
        return cls(
            home=base,
            ffmpeg_bin=os.getenv("FFMPEG_BIN", "ffmpeg").strip() or "ffmpeg",
            ffprobe_bin=os.getenv("FFPROBE_BIN", "ffprobe").strip() or "ffprobe",
            jamendo_client_id=os.getenv("JAMENDO_CLIENT_ID", "").strip(),
            openverse_client_id=os.getenv("OPENVERSE_CLIENT_ID", "").strip(),
            openverse_client_secret=os.getenv("OPENVERSE_CLIENT_SECRET", "").strip(),
            youtube_channel_id=os.getenv("YOUTUBE_CHANNEL_ID", "").strip(),
            cdp_url=os.getenv("CDP_URL", "http://127.0.0.1:9222").strip(),
            min_track_seconds=_int("MIN_TRACK_SECONDS", 120),
            max_track_seconds=_int("MAX_TRACK_SECONDS", 0),
            preferred_track_seconds=_int("PREFERRED_TRACK_SECONDS", 240),
            max_per_artist=max(1, _int("MAX_PER_ARTIST", 2)),
            crossfade_seconds=max(0, _int("CROSSFADE_SECONDS", 3)),
            normalize_loudness=_bool("NORMALIZE_LOUDNESS", True),
            copy_tracks=_bool("COPY_TRACKS", True),
            mp3_bitrate=os.getenv("MP3_BITRATE", "192k").strip() or "192k",
            allowed_licenses=licenses or DEFAULT_LICENSES,
        )
