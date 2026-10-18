"""Candidate filtering and fixed-length playlist selection."""

from __future__ import annotations

import random
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from music_crawler.config import Settings
from music_crawler.db import Database
from music_crawler.models import Track, norm


@dataclass
class Criteria:  # not frozen: playlist.fill() adds exclude_ids
    genres: tuple[str, ...] = ()
    moods: tuple[str, ...] = ()
    vocals: str = "lenient"  # strict: instrumental only | lenient: not-vocal | any
    sources: tuple[str, ...] = ()
    strict_mood: bool = False  # True: drop tracks that have no mood label at all
    exclude_used: bool = True
    exclude_ids: frozenset[str] = frozenset()
    min_seconds: int = 120
    max_seconds: int = 0

    def as_dict(self) -> dict:
        return {
            "genres": list(self.genres),
            "moods": list(self.moods),
            "vocals": self.vocals,
            "sources": list(self.sources),
            "strict_mood": self.strict_mood,
            "exclude_used": self.exclude_used,
            "min_seconds": self.min_seconds,
            "max_seconds": self.max_seconds,
        }


def _ci(values: Iterable[str]) -> set[str]:
    return {v.lower() for v in values}


def matches(track: Track, c: Criteria, used_ids: set[str], used_keys: set[str]) -> bool:
    if track.id in c.exclude_ids:
        return False
    if c.sources and track.source not in c.sources:
        return False
    if track.duration < c.min_seconds or (c.max_seconds and track.duration > c.max_seconds):
        return False
    if c.vocals == "strict" and track.vocals != "instrumental":
        return False
    if c.vocals == "lenient" and track.vocals == "vocal":
        return False
    if c.genres and not (_ci(track.genres) & _ci(c.genres)):
        return False
    if c.moods:
        if track.moods:
            if not (_ci(track.moods) & _ci(c.moods)):
                return False
        elif c.strict_mood:
            return False
    if c.exclude_used:
        if track.id in used_ids:
            return False
        if (
            norm(track.title) in used_keys
            or norm(f"{track.artist} {track.title}") in used_keys
            or norm(f"{track.artist} - {track.title}") in used_keys
        ):
            return False
    return True


def filter_candidates(db: Database, c: Criteria) -> list[Track]:
    used_ids = db.used_track_ids() if c.exclude_used else set()
    used_keys = db.used_title_keys() if c.exclude_used else set()
    seen: set[str] = set()
    out: list[Track] = []
    for t in db.all_tracks(sources=c.sources or None):
        if not matches(t, c, used_ids, used_keys):
            continue
        if t.dedupe_key in seen:  # same recording listed twice (e.g. Jamendo via Openverse)
            continue
        seen.add(t.dedupe_key)
        out.append(t)
    return out


def playlist_length(tracks: Sequence[Track], crossfade: float) -> float:
    """Total seconds once the tracks are joined with ``crossfade`` overlap."""
    if not tracks:
        return 0.0
    return sum(t.duration for t in tracks) - crossfade * (len(tracks) - 1)


@dataclass
class SelectionResult:
    tracks: list[Track]
    length: float
    target: float
    pool_size: int
    notes: list[str] = field(default_factory=list)


def _score(t: Track, c: Criteria, s: Settings, rng: random.Random) -> float:
    closeness = max(
        0.0, 1.0 - abs(t.duration - s.preferred_track_seconds) / s.preferred_track_seconds
    )
    score = closeness * 10 + rng.random() * 6
    if t.vocals == "instrumental":
        score += 6
    if c.genres and _ci(t.genres) & _ci(c.genres):
        score += 2
    if c.moods:
        if _ci(t.moods) & _ci(c.moods):
            score += 3
        elif not t.moods:
            score -= 8  # unlabeled mood: only used when labeled tracks run out
    if t.file_path:
        score += 1.5  # already on disk → no download needed
    return score


def _cost(total: float, target: float, tolerance: float) -> float:
    """0 inside [target, target+tolerance]; music that is too short is penalised harder."""
    if total < target:
        return (target - total) * 2.0
    if total > target + tolerance:
        return total - (target + tolerance)
    return 0.0


def select(
    candidates: list[Track],
    target_seconds: int,
    settings: Settings,
    criteria: Criteria,
    *,
    locked: Sequence[Track] = (),
    tolerance: int = 120,
    seed: int | None = None,
) -> SelectionResult:
    """Pick tracks whose joined length lands in ``[target, target + tolerance]``.

    1. score + randomise candidates, then greedily fill respecting a per-artist cap;
    2. hill-climb with swap / drop / add moves to minimise the length error;
    3. order tracks so the same artist never plays back-to-back.
    """
    rng = random.Random(seed)
    xf = settings.crossfade_seconds
    locked_ids = {t.id for t in locked}
    locked_keys = {t.dedupe_key for t in locked}
    pool = [t for t in candidates if t.id not in locked_ids and t.dedupe_key not in locked_keys]
    ranked = sorted(pool, key=lambda t: _score(t, criteria, settings, rng), reverse=True)

    chosen: list[Track] = list(locked)
    artists = Counter(norm(t.artist) for t in chosen)
    for t in ranked:
        if playlist_length(chosen, xf) >= target_seconds:
            break
        key = norm(t.artist)
        if key and artists[key] >= settings.max_per_artist:
            continue
        chosen.append(t)
        artists[key] += 1

    chosen_ids = {t.id for t in chosen}
    unused = [t for t in ranked if t.id not in chosen_ids]
    fixed = len(locked)

    def total_of(items):
        return playlist_length(items, xf)

    for _ in range(60):
        base = _cost(total_of(chosen), target_seconds, tolerance)
        if base == 0:
            break
        best_cost, best_move = base, None
        n = len(chosen)
        for i in range(fixed, n):  # drop
            c = _cost(total_of(chosen[:i] + chosen[i + 1 :]), target_seconds, tolerance)
            if c < best_cost:
                best_cost, best_move = c, ("drop", i, None)
        sample = unused if len(unused) <= 600 else rng.sample(unused, 600)
        for cand in sample:
            key = norm(cand.artist)
            if key and artists[key] >= settings.max_per_artist:
                continue
            c = _cost(total_of(chosen + [cand]), target_seconds, tolerance)  # add
            if c < best_cost:
                best_cost, best_move = c, ("add", None, cand)
            for i in range(fixed, n):  # swap
                swapped = chosen[:i] + [cand] + chosen[i + 1 :]
                c = _cost(total_of(swapped), target_seconds, tolerance)
                if c < best_cost:
                    best_cost, best_move = c, ("swap", i, cand)
        if best_move is None:
            break
        kind, i, cand = best_move
        if kind == "drop":
            gone = chosen.pop(i)
            unused.append(gone)
            artists[norm(gone.artist)] -= 1
        elif kind == "add":
            chosen.append(cand)
            unused.remove(cand)
            artists[norm(cand.artist)] += 1
        else:
            gone = chosen[i]
            chosen[i] = cand
            unused.remove(cand)
            unused.append(gone)
            artists[norm(gone.artist)] -= 1
            artists[norm(cand.artist)] += 1

    ordered = locked_first_spread(chosen, fixed, rng)
    length = total_of(ordered)
    notes = []
    if length < target_seconds:
        notes.append(
            f"후보가 부족해 목표보다 {int(target_seconds - length)}초 짧습니다. "
            "조건을 넓히거나 `sync`로 후보를 더 모으세요."
        )
    return SelectionResult(ordered, length, float(target_seconds), len(candidates), notes)


def locked_first_spread(tracks: list[Track], fixed: int, rng: random.Random) -> list[Track]:
    """Keep locked tracks in front (their order is the user's); shuffle the rest while
    avoiding the same artist twice in a row."""
    head, tail = tracks[:fixed], tracks[fixed:]
    rng.shuffle(tail)
    out: list[Track] = []
    pending = list(tail)
    prev = norm(head[-1].artist) if head else None
    while pending:
        pick = next((t for t in pending if norm(t.artist) != prev or not prev), pending[0])
        pending.remove(pick)
        out.append(pick)
        prev = norm(pick.artist)
    return head + out
