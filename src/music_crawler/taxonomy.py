"""Controlled genre/mood vocabulary, vocal detection and search *profiles*.

Every provider uses its own labels (YouTube: ``CREATOR_MUSIC_GENRE_JAZZ_AND_BLUES``,
Jamendo: ``vartags=["peaceful", "soft"]``, Openverse: free-form tags). They are all mapped
onto the small vocabulary below so a single ``--genre jazz --mood calm`` works everywhere.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

GENRES = (
    "Jazz",
    "NewAge",
    "Classical",
    "Ambient",
    "Piano",
    "Acoustic",
    "Lounge",
    "Lofi",
    "Cinematic",
    "Folk",
    "Blues",
    "Electronic",
    "Rock",
    "Pop",
    "HipHop",
    "RnB",
    "Reggae",
    "World",
    "Kids",
    "Holiday",
    "Other",
)
MOODS = (
    "calm",
    "peaceful",
    "warm",
    "dreamy",
    "romantic",
    "melancholy",
    "uplifting",
    "dramatic",
    "dark",
    "energetic",
)

_GENRE_ALIASES: dict[str, str] = {
    # YouTube Audio Library
    "creator_music_genre_jazz_and_blues": "Jazz",
    "creator_music_genre_ambient": "Ambient",
    "creator_music_genre_classical": "Classical",
    "creator_music_genre_cinematic": "Cinematic",
    "creator_music_genre_country_and_folk": "Folk",
    "creator_music_genre_dance_and_electronics": "Electronic",
    "creator_music_genre_rock": "Rock",
    "creator_music_genre_alternative_and_punk": "Rock",
    "creator_music_genre_pop": "Pop",
    "creator_music_genre_hip_hop_and_rap": "HipHop",
    "creator_music_genre_rnb_and_soul": "RnB",
    "creator_music_genre_reggae": "Reggae",
    "creator_music_genre_childrens": "Kids",
    "creator_music_genre_holiday": "Holiday",
    # Jamendo / Openverse / folder names / free text
    "jazz": "Jazz",
    "smoothjazz": "Jazz",
    "smooth jazz": "Jazz",
    "bebop": "Jazz",
    "swing": "Jazz",
    "bossanova": "Jazz",
    "bossa nova": "Jazz",
    "jazzy": "Jazz",
    "newage": "NewAge",
    "new age": "NewAge",
    "new-age": "NewAge",
    "meditation": "NewAge",
    "relaxation": "NewAge",
    "classical": "Classical",
    "classic": "Classical",
    "orchestral": "Classical",
    "orchestra": "Classical",
    "symphony": "Classical",
    "baroque": "Classical",
    "chamber": "Classical",
    "neoclassical": "Classical",
    "opera": "Classical",
    "ambient": "Ambient",
    "drone": "Ambient",
    "soundscape": "Ambient",
    "atmospheric": "Ambient",
    "piano": "Piano",
    "solopiano": "Piano",
    "solo piano": "Piano",
    "acoustic": "Acoustic",
    "acousticguitar": "Acoustic",
    "fingerstyle": "Acoustic",
    "guitar": "Acoustic",
    "classicalguitar": "Acoustic",
    "lounge": "Lounge",
    "chillout": "Lounge",
    "chill": "Lounge",
    "downtempo": "Lounge",
    "easylistening": "Lounge",
    "easy listening": "Lounge",
    "lofi": "Lofi",
    "lo-fi": "Lofi",
    "lo fi": "Lofi",
    "chillhop": "Lofi",
    "cinematic": "Cinematic",
    "soundtrack": "Cinematic",
    "score": "Cinematic",
    "folk": "Folk",
    "country": "Folk",
    "bluegrass": "Folk",
    "blues": "Blues",
    "electronic": "Electronic",
    "electro": "Electronic",
    "techno": "Electronic",
    "house": "Electronic",
    "dance": "Electronic",
    "edm": "Electronic",
    "synthwave": "Electronic",
    "rock": "Rock",
    "metal": "Rock",
    "punk": "Rock",
    "pop": "Pop",
    "indie": "Pop",
    "synthpop": "Pop",
    "hiphop": "HipHop",
    "hip hop": "HipHop",
    "hip-hop": "HipHop",
    "rap": "HipHop",
    "rnb": "RnB",
    "soul": "RnB",
    "funk": "RnB",
    "reggae": "Reggae",
    "ska": "Reggae",
    "world": "World",
    "latin": "World",
    "african": "World",
    "ethnic": "World",
}

_MOOD_ALIASES: dict[str, str] = {
    "creator_music_mood_calm": "calm",
    "creator_music_mood_dramatic": "dramatic",
    "creator_music_mood_happy": "uplifting",
    "creator_music_mood_bright": "uplifting",
    "creator_music_mood_funky": "energetic",
    "creator_music_mood_dark": "dark",
    "creator_music_mood_inspirational": "uplifting",
    "creator_music_mood_angry": "energetic",
    "creator_music_mood_sad": "melancholy",
    "creator_music_mood_romantic": "romantic",
    "calm": "calm",
    "relaxing": "calm",
    "relaxed": "calm",
    "relax": "calm",
    "soft": "calm",
    "quiet": "calm",
    "gentle": "calm",
    "chill": "calm",
    "sleep": "calm",
    "meditative": "calm",
    "meditation": "calm",
    "serene": "calm",
    "tranquil": "calm",
    "soothing": "calm",
    "peaceful": "peaceful",
    "peace": "peaceful",
    "zen": "peaceful",
    "warm": "warm",
    "cozy": "warm",
    "nostalgic": "warm",
    "sentimental": "warm",
    "dreamy": "dreamy",
    "dream": "dreamy",
    "ethereal": "dreamy",
    "floating": "dreamy",
    "romantic": "romantic",
    "love": "romantic",
    "sad": "melancholy",
    "melancholy": "melancholy",
    "melancholic": "melancholy",
    "emotional": "melancholy",
    "happy": "uplifting",
    "uplifting": "uplifting",
    "bright": "uplifting",
    "positive": "uplifting",
    "hopeful": "uplifting",
    "inspiring": "uplifting",
    "inspirational": "uplifting",
    "dramatic": "dramatic",
    "epic": "dramatic",
    "powerful": "dramatic",
    "dark": "dark",
    "mysterious": "dark",
    "tense": "dark",
    "energetic": "energetic",
    "groovy": "energetic",
    "funky": "energetic",
    "upbeat": "energetic",
    "disco": "energetic",
    "party": "energetic",
    "club": "energetic",
    "uptempo": "energetic",
    "aggressive": "energetic",
}

_VOCAL_WORDS = {"vocal", "vocals", "voice", "singer", "lyrics", "singing", "choir", "rap"}
_TITLE_VOCAL_RE = re.compile(r"\b(feat\.?|ft\.?|featuring|vocal|vocals|lyrics|sung)\b", re.I)


def _key(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def normalize_genres(raw) -> list[str]:
    out: list[str] = []
    for item in raw or []:
        genre = _GENRE_ALIASES.get(_key(str(item)))
        if genre and genre not in out:
            out.append(genre)
    return out


def normalize_moods(raw) -> list[str]:
    out: list[str] = []
    for item in raw or []:
        mood = _MOOD_ALIASES.get(_key(str(item)))
        if mood and mood not in out:
            out.append(mood)
    return out


def classify_text(text: str) -> tuple[list[str], list[str]]:
    """Best-effort genre/mood detection from free text (title + tags). Word-boundary matched."""
    lowered = _key(text).replace("_", " ")
    genres: list[str] = []
    moods: list[str] = []
    for alias, genre in _GENRE_ALIASES.items():
        if alias.startswith("creator_music"):
            continue
        if (
            re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", lowered)
            and genre not in genres
        ):
            genres.append(genre)
    for alias, mood in _MOOD_ALIASES.items():
        if alias.startswith("creator_music"):
            continue
        if (
            re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", lowered)
            and mood not in moods
        ):
            moods.append(mood)
    return genres, moods


def detect_vocals(title: str = "", tags=(), explicit: str | None = None) -> str:
    """Return ``instrumental`` / ``vocal`` / ``unknown``.

    ``explicit`` is the provider's own flag when it has one (Jamendo does) and always wins.
    """
    if explicit:
        flag = explicit.strip().lower()
        if flag in {"instrumental", "vocal"}:
            return flag
    lowered = {_key(str(t)) for t in tags or []}
    if "instrumental" in lowered:
        return "instrumental"
    if lowered & _VOCAL_WORDS:
        return "vocal"
    if _TITLE_VOCAL_RE.search(title or ""):
        return "vocal"
    if re.search(r"\binstrumental\b", title or "", re.I):
        return "instrumental"
    return "unknown"


# --------------------------------------------------------------------------- profiles
@dataclass(frozen=True)
class Profile:
    """What to look for. Drives both *collection* (targeted queries) and default filters."""

    name: str
    genres: tuple[str, ...]
    moods: tuple[str, ...]
    vocals: str = "lenient"  # strict | lenient | any
    jamendo_tags: tuple[str, ...] = ()
    openverse_queries: tuple[dict, ...] = field(default_factory=tuple)


RELAX = Profile(
    name="relax",
    genres=("Jazz", "NewAge", "Classical", "Ambient", "Piano", "Acoustic", "Lounge"),
    moods=("calm", "peaceful", "warm", "dreamy", "romantic", "melancholy"),
    vocals="lenient",
    jamendo_tags=(
        "jazz",
        "smoothjazz",
        "newage",
        "classical",
        "ambient",
        "piano",
        "lounge",
        "chillout",
        "easylistening",
        "relaxation",
        "meditation",
        "acousticguitar",
        "downtempo",
        "bossanova",
    ),
    openverse_queries=(
        {"q": "calm piano", "genres": ["Piano"], "moods": ["calm"]},
        {"q": "relaxing jazz instrumental", "genres": ["Jazz"], "moods": ["calm"]},
        {"q": "smooth jazz", "genres": ["Jazz"]},
        {"q": "new age ambient", "genres": ["NewAge", "Ambient"], "moods": ["calm"]},
        {"q": "classical piano", "genres": ["Classical", "Piano"]},
        {"q": "chamber music", "genres": ["Classical"]},
        {"q": "meditation music", "genres": ["NewAge"], "moods": ["calm", "peaceful"]},
        {"q": "acoustic guitar instrumental", "genres": ["Acoustic"], "moods": ["calm"]},
        {"q": "lounge chillout", "genres": ["Lounge"], "moods": ["calm"]},
        {"q": "peaceful ambient", "genres": ["Ambient"], "moods": ["peaceful"]},
    ),
)


def load_profiles(path: Path | None = None) -> dict[str, Profile]:
    """Built-in ``relax`` profile plus any ``[profiles.<name>]`` tables from a TOML file."""
    profiles = {RELAX.name: RELAX}
    if path and path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        for name, table in (data.get("profiles") or {}).items():
            base = profiles.get(table.get("extends", ""), RELAX)
            profiles[name] = Profile(
                name=name,
                genres=tuple(table.get("genres", base.genres)),
                moods=tuple(table.get("moods", base.moods)),
                vocals=table.get("vocals", base.vocals),
                jamendo_tags=tuple(table.get("jamendo_tags", base.jamendo_tags)),
                openverse_queries=tuple(table.get("openverse_queries", base.openverse_queries)),
            )
    return profiles
