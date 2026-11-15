# Music Library

A Python desktop and command-line tool for organising music and creating background-music mixes that fit a target video duration.

The application can use a local music collection and selected external catalogs, then prepare an editable playlist and render a combined audio file.

## Features

- Import and manage local audio files
- Collect catalog metadata from Jamendo and Openverse
- Optional YouTube Audio Library integration
- Organise tracks by genre, mood, instruments, vocals, and license metadata
- Select tracks to fit a target duration
- Edit playlists by adding, removing, and reordering tracks
- Apply crossfades and optionally trim the final track to a target length
- Export a merged audio file, track timeline, and attribution information
- Use either the desktop menu or CLI

## Technology

- Python 3.11+
- uv
- SQLite
- HTTPX
- FFmpeg and ffprobe
- Playwright (optional, for YouTube Audio Library integration)

## Requirements

Install Python 3.11 or later, uv, FFmpeg, and ffprobe. Make sure the FFmpeg executables are available on PATH.

## Install

    git clone https://github.com/BaeDoge/python-youtube-audio-library-crawler.git
    cd python-youtube-audio-library-crawler
    uv sync

To enable the optional YouTube Audio Library provider, install the optional dependency group and configure an authenticated Chrome session as described in the project configuration.

## Start the desktop menu

    uv run music-crawler menu

The menu provides access to mix creation, the music library, playlist review and editing, include/exclude lists, and application settings.

## CLI

    uv run music-crawler --help

The CLI supports library initialisation and synchronisation, playlist creation, review, editing, and rendering. Use the help output for the available commands and options.

## Output

Rendered mixes and their associated metadata are stored under the configured playlists output directory. A typical output includes the combined audio file, a track timeline, and credits for tracks that require attribution.

## Licensing

License metadata and automatic filters help organise music sources, but they do not guarantee usage rights. Before publishing or monetising a video, review the current terms of the source platform and the licence of every selected track. Include attribution where required.

The YouTube Audio Library provider relies on an authenticated YouTube Studio browser session and internal web endpoints. Those endpoints are not a stable public API and may change without notice.
