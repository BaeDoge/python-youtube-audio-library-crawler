"""Core data types shared by providers, the database, selector and playlist code."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

VOCALS = ("instrumental", "vocal", "unknown")

# Licenses that legally require credit in the video description.
ATTRIBUTION_REQUIRED = {"cc-by"}


def norm(text: str) -> str:
    """Lower-case, accent-free, alphanumeric-only key used for de-duplication."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^0-9a-z\u3131-\u318e\uac00-\ud7a3]+", "", text.casefold())


def slugify(text: str, fallback: str = "untitled", max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKC", text or "").strip()
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", " ", text)
    text = re.sub(r"\s+", "-", text).strip("-. ")
    return (text[:max_len].rstrip("-. ")) or fallback


@dataclass
class Track:
    id: str  # "<source>:<source_id>"
    source: str  # jamendo | openverse | youtube | local
    source_id: str
    title: str
    artist: str = ""
    duration: int = 0  # seconds
    genres: list[str] = field(default_factory=list)  # canonical (see taxonomy)
    moods: list[str] = field(default_factory=list)  # canonical (see taxonomy)
    tags: list[str] = field(default_factory=list)  # raw provider tags, lower-case
    vocals: str = "unknown"  # instrumental | vocal | unknown
    license: str = "unknown"  # canonical code (see licenses.py)
    license_url: str = ""
    attribution: str = ""
    source_url: str = ""
    download_url: str = ""
    file_path: str | None = None
    content_hash: str | None = None
    status: str = "candidate"  # candidate | invalid | banned
    status_reason: str = ""

    @property
    def dedupe_key(self) -> str:
        return f"{norm(self.artist)}|{norm(self.title)}"

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title}" if self.artist else self.title
