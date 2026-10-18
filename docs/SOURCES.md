# Sources and licensing notes

This is engineering guidance, not legal advice. Check each source's current terms before monetising.

| Source | How it is used | License handling |
|---|---|---|
| Jamendo API | per-tag queries, `vocalinstrumental`, real mood tags | only CC0 / CC BY kept (≈1–2 % of the catalog is CC BY; most is NC/ND/SA) |
| Openverse API | `category=music`, many specific queries | `license=cc0,pdm,by`; clips under `MIN_TRACK_SECONDS` dropped |
| YouTube Audio Library | optional, via logged-in Chrome (private Studio API) | CC BY 4.0 tracks (~14 %) stored as `cc-by` → credits generated |
| Local folder | `library/inbox/` | license from sidecar JSON or folder name; disallowed ones skipped |

* **Attribution:** every `cc-by` track produces a line in `description.txt` / `credits.txt`.
* **Jamendo API terms:** Jamendo's API is offered for non-commercial applications. The music's CC BY license is separate; if the tool is used for a monetised channel, read Jamendo's current API terms or licensing options.
* **Openverse anonymous limits:** results are capped per query and requests are rate-limited. Register for free OAuth keys for more headroom.
* **Pixabay:** the project intentionally has **no Pixabay scraper**. Pixabay's terms restrict automated bulk retrieval and the site blocks scripted access (HTTP 403). Download tracks manually and drop them into `library/inbox/pixabay/`.
* **Content ID:** even properly licensed tracks can trigger automatic claims; keep `credits.txt` and source URLs.
