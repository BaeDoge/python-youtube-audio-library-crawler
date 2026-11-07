# Architecture

```
library/inbox + providers (Jamendo / Openverse / YouTube Audio Library)
                    │
                    ▼
        provider adapters → canonical Track
                    │
                    ▼
       SQLite (tracks / usage / sync cursors)
                    │
                    ▼
      selector (genre / mood / license / usage)
                    │
        insufficient length?
          ├─ selected online providers → sync → refill
          └─ still short → relax mood → refill
                    │
                    ▼
     playlist draft → remove / add / fill
                    │
                    ▼
       validated downloads → ffmpeg render
                    │
                    ▼
     mix.mp3 + timeline + credits + track files
                    │
                 commit
                    ▼
             persistent USED list
```

- **Providers** normalize metadata into one `Track` model. Local scans use folder names as classification hints; sidecar JSON can override descriptive and license metadata.
- **Selection** excludes banned, invalid, already-used, and duplicate recordings. Mood is relaxed only after the requested pool and explicitly selected online providers cannot fill the target duration.
- **Downloads** are validated before atomic placement in `library/<provider>/`. Invalid or permanently unavailable downloads are marked invalid.
- **Editing** keeps removed IDs out of the current draft. By default, removing a track also records a persistent BAN status; `--keep-eligible` opts out of the global ban.
- **Usage** is committed only after the user approves the generated mix. The usage table is the persistent USED list.
- **Rendering** uses loudness normalization, short crossfades between tracks, optional exact final-track trim, and a final fade-out of approximately five seconds.
- **Runtime data** (`.env`, `data/`, `library/`, `output/`) is excluded from git.
