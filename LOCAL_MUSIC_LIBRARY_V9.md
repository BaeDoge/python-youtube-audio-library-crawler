# Local Music Library v9

## Added menu items

- `11. 로컬 음원 라이브러리 스캔/카탈로그 갱신`
- `12. 로컬 음원으로 플레이리스트 + 타임라인 + 통합 MP3 생성`

## How it works

1. Place audio you are authorized to use in `music_library/` (subfolders are scanned too).
2. Menu 11 scans supported audio files, extracts duration with ffprobe/ffmpeg, reads artist/title tags where available, hashes each file, removes duplicate content/recordings, and writes `music_library/catalog.json`.
3. Menu 12 accepts a target duration such as `45`, `45:00`, or `01:15:00`. A bare integer means minutes.
4. The selector excludes entries already recorded in the configured shared `used.txt`, selects unique tracks, and tries to fit the target duration.
5. It creates `output/playlists/local_<timestamp>/merged_playlist.mp3`, `playlist.txt`, `timeline.txt`, `timeline.csv`, and `playlist.json`.
6. Only after the MP3 is successfully generated, selected tracks are appended to `used.txt` and recorded in the existing SQLite `track_usage`, `playlist_history`, and `playlist_items` tables. If MP3 generation fails, usage history is not committed.

## Compatibility and preservation

- Menu 1's YouTube Audio Library path was not changed; local library functions are separate and available through menu 11/12.
- The code uses the existing `DB_PATH`, `USED_FILE`, `PLAYLIST_DIR`, and `FFMPEG_BIN` configuration, including `.env` overrides.
- The distributable ZIP intentionally excludes a real `.env`, `data/tracks.db`, `data/used.txt`, downloaded audio, and generated output. Extract the code into your existing project folder and merge files without deleting or replacing your private configuration/data.
- MP3 generation requires FFmpeg. Duration probing uses ffprobe where available and falls back to ffmpeg.

## Pixabay note

Pixabay's official API documentation describes image and video search APIs; it does not document a music-search/download API. Pixabay's terms prohibit unauthorized automated data extraction and bulk/systematic copying. This feature therefore does not scrape Pixabay or download from it. Download only via a method Pixabay expressly permits, then place the audio file in `music_library/`.

Official references:
- https://pixabay.com/api/docs/
- https://pixabay.com/service/terms/
- https://pixabay.com/service/license-summary/
- https://pixabay.com/service/faq/
