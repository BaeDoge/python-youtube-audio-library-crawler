"""License normalisation and the default "safe for a monetised video" policy."""

from __future__ import annotations

import re

from music_crawler.models import ATTRIBUTION_REQUIRED


def from_cc_url(url: str) -> str:
    """Map a Creative Commons URL to a canonical code.

    ``cc0`` / ``pdm`` / ``cc-by`` are usable commercially; every other CC variant is
    returned verbatim (``cc-by-nc-sa`` ...) so the caller can report *why* it was dropped.
    """
    value = (url or "").lower().strip().rstrip("/")
    if not value:
        return "unknown"
    if "publicdomain/zero" in value:
        return "cc0"
    if "publicdomain/mark" in value:
        return "pdm"
    match = re.search(r"/licenses/([a-z\-]+)(?:/|$)", value)
    if match:
        return "cc-" + match.group(1)
    return "other"


def from_openverse(code: str, url: str = "") -> str:
    code = (code or "").lower().strip()
    if code in {"cc0", "pdm"}:
        return code
    if code:
        return "cc-" + code
    return from_cc_url(url)


def is_allowed(code: str, allowed: tuple[str, ...]) -> bool:
    return code in allowed


def needs_attribution(code: str) -> bool:
    return code in ATTRIBUTION_REQUIRED


def credit_line(title: str, artist: str, code: str, license_url: str, source_url: str) -> str:
    """One human-readable credit line for a video description."""
    pretty = {"cc-by": "CC BY", "cc0": "CC0", "pdm": "Public Domain"}.get(code, code.upper())
    parts = [f'"{title}"' + (f" by {artist}" if artist else ""), f"({pretty})"]
    if license_url:
        parts.append(license_url)
    if source_url:
        parts.append(f"Source: {source_url}")
    return " ".join(parts)
