"""Playlist model: build → edit (add/remove/fill) → render (download + merge) → commit."""

from __future__ import annotations

import csv
import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from music_crawler import audio, licenses
from music_crawler.config import Settings
from music_crawler.db import Database
from music_crawler.downloader import download_many
from music_crawler.models import Track, slugify
from music_crawler.selector import Criteria, filter_candidates, select

MIN_TRIM_SECONDS = 30


def fmt_time(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


@dataclass
class Item:
    track_id: str
    title: str
    artist: str
    source: str
    duration: int
    license: str = "unknown"
    license_url: str = ""
    source_url: str = ""
    locked: bool = False

    @classmethod
    def from_track(cls, t: Track, locked: bool = False) -> Item:
        return cls(
            t.id,
            t.title,
            t.artist,
            t.source,
            t.duration,
            t.license,
            t.license_url,
            t.source_url,
            locked,
        )


@dataclass
class Playlist:
    id: str
    name: str
    target_seconds: int
    criteria: dict
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    status: str = "draft"  # draft | rendered | committed
    exact: bool = False  # trim the final track so the mix is exactly the target
    items: list[Item] = field(default_factory=list)
    outputs: dict = field(default_factory=dict)
    dismissed: list[str] = field(default_factory=list)  # removed by the user: never auto-refilled

    # ---- paths / persistence ------------------------------------------------
    def folder(self, settings: Settings) -> Path:
        return settings.playlists_dir / self.id

    def save(self, settings: Settings) -> Path:
        folder = self.folder(settings)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "playlist.json"
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, settings: Settings, ref: str = "latest") -> Playlist:
        base = settings.playlists_dir
        folders = sorted((p for p in base.glob("*/playlist.json")), key=lambda p: p.parent.name)
        if not folders:
            raise FileNotFoundError("저장된 플레이리스트가 없습니다. 먼저 `make`로 만드세요.")
        if ref in ("latest", ""):
            chosen = folders[-1]
        else:
            hits = [p for p in folders if p.parent.name == ref] or [
                p for p in folders if ref in p.parent.name
            ]
            if not hits:
                raise FileNotFoundError(f"플레이리스트를 찾을 수 없습니다: {ref}")
            if len(hits) > 1:
                names = ", ".join(p.parent.name for p in hits)
                raise FileNotFoundError(f"여러 개가 일치합니다: {names}")
            chosen = hits[0]
        data = json.loads(chosen.read_text(encoding="utf-8"))
        data["items"] = [Item(**i) for i in data.get("items", [])]
        return cls(**data)

    # ---- metrics ---------------------------------------------------------------
    def length(self, crossfade: float) -> float:
        if not self.items:
            return 0.0
        total = sum(i.duration for i in self.items) - crossfade * (len(self.items) - 1)
        return total

    def timeline(self, crossfade: float) -> list[tuple[float, Item]]:
        out, start = [], 0.0
        for item in self.items:
            out.append((start, item))
            start += item.duration - crossfade
        return out

    def trim_for_exact(self, crossfade: float) -> float | None:
        """Seconds to keep of the last track so the whole mix equals ``target_seconds``."""
        if not (self.exact and self.items):
            return None
        start_last = self.timeline(crossfade)[-1][0]
        keep = self.target_seconds - start_last
        if MIN_TRIM_SECONDS <= keep < self.items[-1].duration:
            return keep
        return None

    # ---- editing -----------------------------------------------------------------
    def remove(self, positions: list[int]) -> list[Item]:
        """Remove by 1-based position. Returns the removed items."""
        bad = [p for p in positions if not 1 <= p <= len(self.items)]
        if bad:
            raise IndexError(f"없는 번호: {bad} (1~{len(self.items)})")
        removed = [self.items[p - 1] for p in sorted(set(positions))]
        self.dismissed += [i.track_id for i in removed if i.track_id not in self.dismissed]
        keep = set(positions)
        self.items = [it for n, it in enumerate(self.items, 1) if n not in keep]
        self._invalidate()
        return removed

    def add(self, track: Track, position: int | None = None) -> None:
        if any(i.track_id == track.id for i in self.items):
            raise ValueError("이미 플레이리스트에 있는 곡입니다.")
        item = Item.from_track(track, locked=True)
        if position is None or position > len(self.items):
            self.items.append(item)
        else:
            self.items.insert(max(0, position - 1), item)
        self._invalidate()

    def move(self, src: int, dst: int) -> None:
        item = self.items.pop(src - 1)
        self.items.insert(dst - 1, item)
        self._invalidate()

    def _invalidate(self) -> None:
        if self.status != "committed":
            self.status = "draft"
        self.outputs = {}

    def fill(
        self, settings: Settings, db: Database, *, seed: int | None = None, tolerance: int = 120
    ) -> list[str]:
        """Top the playlist back up to the target length after removals/additions."""
        criteria = criteria_from_dict(self.criteria, settings)
        criteria.exclude_ids = frozenset(self.dismissed)
        kept = [t for t in (db.get(i.track_id) for i in self.items) if t]
        pool = filter_candidates(db, criteria)
        result = select(
            pool,
            self.target_seconds,
            settings,
            criteria,
            locked=kept,
            tolerance=tolerance,
            seed=seed,
        )
        old_ids = {i.track_id for i in self.items}
        self.items = [Item.from_track(t, locked=t.id in old_ids) for t in result.tracks]
        self._invalidate()
        return result.notes

    # ---- text outputs -------------------------------------------------------------
    def description(self, crossfade: float) -> str:
        lines = [self.name, "", "Tracklist"]
        for start, item in self.timeline(crossfade):
            lines.append(
                f"{fmt_time(start)} {item.artist + ' - ' if item.artist else ''}{item.title}"
            )
        credits = self.credits()
        if credits:
            lines += ["", "Music credits"] + credits
        return "\n".join(lines) + "\n"

    def credits(self) -> list[str]:
        """Credit lines for every licence that legally requires attribution."""
        out = []
        for item in self.items:
            if licenses.needs_attribution(item.license):
                out.append(
                    licenses.credit_line(
                        item.title, item.artist, item.license, item.license_url, item.source_url
                    )
                )
        return out

    def write_text_outputs(self, settings: Settings, crossfade: float) -> None:
        folder = self.folder(settings)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "description.txt").write_text(self.description(crossfade), encoding="utf-8")
        with (folder / "timeline.csv").open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow(["#", "start", "duration", "artist", "title", "source", "license"])
            for n, (start, item) in enumerate(self.timeline(crossfade), 1):
                writer.writerow(
                    [
                        n,
                        fmt_time(start),
                        fmt_time(item.duration),
                        item.artist,
                        item.title,
                        item.source,
                        item.license,
                    ]
                )


# ------------------------------------------------------------------------------- builders
def criteria_from_dict(data: dict, settings: Settings) -> Criteria:
    return Criteria(
        genres=tuple(data.get("genres", ())),
        moods=tuple(data.get("moods", ())),
        vocals=data.get("vocals", "lenient"),
        sources=tuple(data.get("sources", ())),
        strict_mood=bool(data.get("strict_mood", False)),
        exclude_used=bool(data.get("exclude_used", True)),
        min_seconds=int(data.get("min_seconds", settings.min_track_seconds)),
        max_seconds=int(data.get("max_seconds", settings.max_track_seconds)),
    )


def create(
    settings: Settings,
    db: Database,
    *,
    name: str,
    target_seconds: int,
    criteria: Criteria,
    exact: bool = False,
    seed: int | None = None,
    tolerance: int = 120,
) -> tuple[Playlist, list[str]]:
    pool = filter_candidates(db, criteria)
    result = select(pool, target_seconds, settings, criteria, tolerance=tolerance, seed=seed)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    playlist = Playlist(
        id=f"{stamp}_{slugify(name, 'playlist')}",
        name=name,
        target_seconds=target_seconds,
        criteria=criteria.as_dict(),
        exact=exact,
        items=[Item.from_track(t) for t in result.tracks],
    )
    notes = list(result.notes)
    if not pool:
        notes.append("조건에 맞는 후보가 0곡입니다. `stats`로 풀을 확인하고 조건을 넓히세요.")
    return playlist, notes


def render(
    settings: Settings, db: Database, playlist: Playlist, *, progress=lambda _m: None
) -> Path:
    """Download missing files, merge to ``mix.mp3`` and write description/timeline/JSON."""
    tracks = [db.get(i.track_id) for i in playlist.items]
    tracks = [t for t in tracks if t]
    ok, failed = download_many(settings, db, tracks, progress=progress)
    if failed:
        # permanently-bad tracks were marked invalid; drop them and let the user refill
        bad_ids = set(failed)
        playlist.items = [i for i in playlist.items if i.track_id not in bad_ids]
        playlist.save(settings)
        names = "; ".join(f"{db.get(t).label}: {why}" for t, why in failed.items())
        raise RuntimeError(
            f"{len(failed)}곡 다운로드 실패 → 플레이리스트에서 제외했습니다 ({names}). "
            "`fill`로 길이를 다시 채운 뒤 `render`를 다시 실행하세요."
        )
    # refresh durations with the real (probed) values
    for item in playlist.items:
        fresh = db.get(item.track_id)
        if fresh:
            item.duration = fresh.duration
    xf = settings.crossfade_seconds
    trim = playlist.trim_for_exact(xf)
    render_items = []
    for n, item in enumerate(playlist.items):
        is_last = n == len(playlist.items) - 1
        render_items.append(
            audio.RenderItem(
                ok[item.track_id], float(item.duration), trim if (is_last and trim) else None
            )
        )
    folder = playlist.folder(settings)
    out = audio.render(settings, render_items, folder / "mix.mp3", title=playlist.name)
    playlist.write_text_outputs(settings, xf)
    if settings.copy_tracks:
        export_tracks(settings, playlist, ok)
    (folder / "credits.txt").write_text("\n".join(playlist.credits()) + "\n", encoding="utf-8")
    playlist.status = "rendered"
    playlist.outputs = {
        "mix": out.name,
        "seconds": round(playlist.target_seconds if trim else playlist.length(xf)),
    }
    playlist.save(settings)
    return out


def export_tracks(settings: Settings, playlist: Playlist, files: dict[str, Path]) -> Path:
    """Write every song as its own numbered file (numbers match ``remove N``) + an .m3u8."""
    folder = playlist.folder(settings) / "tracks"
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    lines = ["#EXTM3U"]
    for n, item in enumerate(playlist.items, 1):
        src = files[item.track_id]
        label = f"{item.artist} - {item.title}" if item.artist else item.title
        name = f"{n:02d} - {slugify(label, 'track', 80)}{src.suffix}"
        shutil.copy2(src, folder / name)
        lines += [f"#EXTINF:{item.duration},{label}", f"tracks/{name}"]
    (playlist.folder(settings) / "playlist.m3u8").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return folder


def commit(db: Database, playlist: Playlist, settings: Settings) -> None:
    """Mark tracks as used so they are not suggested for the next video."""
    db.record_usage([i.track_id for i in playlist.items], playlist.id)
    playlist.status = "committed"
    playlist.save(settings)
