# Architecture

```
providers/ (jamendo, openverse, local, youtube_library)
      │  parse → Track (canonical genre/mood/vocals/license)
      ▼
   db.py  (SQLite: tracks, usage, used_titles)   ◄── legacy.py (old tracks.db import)
      │
selector.py  filter → score → greedy fill → hill-climb to [target, target+tolerance]
      │
playlist.py  draft → remove/add/fill → render → commit      downloader.py (validated, atomic)
      │                                                          │
   audio.py  ffmpeg: loudnorm + acrossfade + optional exact trim ┘
```

* **Taxonomy (`taxonomy.py`)** maps every provider's labels onto one small vocabulary, so `--genre jazz --mood calm` works across sources.
* **Sync never destroys state:** re-syncing updates metadata but keeps `status` (banned/invalid) and `file_path`.
* **Failure policy:** permanent download failures (403/404/not audio) mark a track `invalid`; transient ones retry 3×.
* **Adding a source:** write `providers/<name>.py` with `parse_track()` and `sync(settings, db, profile)` returning `SyncReport`, register it in `cli.cmd_sync`.
* **Data lives outside git:** `data/`, `library/`, `output/`, `.env`.
