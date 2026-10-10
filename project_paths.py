"""Centralized paths and one-time migration from the old flat project layout."""
from __future__ import annotations

import shutil
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
ASSETS_DIR = BASE_DIR / "assets"
OUTPUT_DIR = BASE_DIR / "output"
DB_PATH = DATA_DIR / "tracks.db"
USED_FILE = DATA_DIR / "used.txt"
CATALOG_PATH = DATA_DIR / "external_music_catalog.json"
EXTERNAL_MUSIC_DIR = ASSETS_DIR / "external_music"
DOWNLOAD_DIR = OUTPUT_DIR / "downloads"
PLAYLIST_DIR = OUTPUT_DIR / "playlists"
DRAFT_DIR = OUTPUT_DIR / "playlist_drafts"


def _move_file_if_needed(source: Path, target: Path) -> None:
    if source.exists() and source.is_file() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))


def _merge_directory(source: Path, target: Path) -> None:
    if not source.exists() or not source.is_dir():
        return
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        destination = target / item.name
        if item.is_dir():
            _merge_directory(item, destination)
        elif not destination.exists():
            shutil.move(str(item), str(destination))
    try:
        source.rmdir()
    except OSError:
        # Preserve any conflicts instead of overwriting the user's files.
        pass


def migrate_legacy_paths() -> None:
    """Move old root-level files/folders into data/assets/output without overwriting."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    _move_file_if_needed(BASE_DIR / "tracks.db", DB_PATH)
    _move_file_if_needed(BASE_DIR / "used.txt", USED_FILE)
    _move_file_if_needed(BASE_DIR / "external_music_catalog.json", CATALOG_PATH)

    for old_name, new_path in (
        ("external_music", EXTERNAL_MUSIC_DIR),
        ("downloads", DOWNLOAD_DIR),
        ("playlists", PLAYLIST_DIR),
        ("playlist", PLAYLIST_DIR),
        ("playlist_drafts", DRAFT_DIR),
        ("playlist_draft", DRAFT_DIR),
        ("download", DOWNLOAD_DIR),
    ):
        _merge_directory(BASE_DIR / old_name, new_path)

    # Also create the stable working directories on a fresh install.
    EXTERNAL_MUSIC_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    PLAYLIST_DIR.mkdir(parents=True, exist_ok=True)
    DRAFT_DIR.mkdir(parents=True, exist_ok=True)
