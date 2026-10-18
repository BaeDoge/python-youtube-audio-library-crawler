"""End-to-end tests with real ffmpeg + synthetic sine-wave "songs"."""

import subprocess

import pytest
from conftest import make_tone, needs_ffmpeg

from music_crawler import audio
from music_crawler import playlist as pl
from music_crawler.playlist import Playlist
from music_crawler.providers import local
from music_crawler.selector import Criteria

pytestmark = needs_ffmpeg


def mix_seconds(settings, path) -> float:
    out = subprocess.run(
        [
            settings.ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return float(out)


@pytest.fixture
def library(settings, db):
    for i in range(6):
        make_tone(settings.inbox_dir / "Jazz" / "Calm" / f"tone{i}.mp3", 60, 300 + i * 40)
    report = local.sync(settings, db)
    assert report.kept == 6
    return db


def test_local_scan_infers_genre_mood_from_folders_and_dedupes(settings, library):
    track = library.all_tracks()[0]
    assert track.genres == ["Jazz"] and track.moods == ["calm"] and track.license == "user"
    again = local.sync(settings, library)
    assert again.inserted == 0  # same files → no duplicates


def test_local_scan_rejects_disallowed_license(settings, db):
    path = make_tone(settings.inbox_dir / "nc.mp3", 30)
    path.with_suffix(".json").write_text('{"license": "CC BY-NC 4.0"}', encoding="utf-8")
    report = local.sync(settings, db)
    assert report.kept == 0 and sum(report.dropped.values()) == 1


def test_make_edit_render_commit_flow(settings, library):
    db = library
    criteria = Criteria(genres=("Jazz",), moods=("calm",), min_seconds=10)
    playlist, notes = pl.create(
        settings, db, name="flow", target_seconds=150, criteria=criteria, exact=True, seed=1
    )
    assert not notes and 3 <= len(playlist.items) <= 4

    out = pl.render(settings, db, playlist)
    assert out.exists() and playlist.status == "rendered"
    assert abs(mix_seconds(settings, out) - 150) < 1.5  # exact trim + crossfade maths
    folder = playlist.folder(settings)
    assert (folder / "description.txt").exists() and (folder / "timeline.csv").exists()
    exported = sorted((folder / "tracks").iterdir())
    assert len(exported) == len(playlist.items) and exported[0].name.startswith("01 - ")
    assert "tracks/01 - " in (folder / "playlist.m3u8").read_text(encoding="utf-8")

    # edit: remove first track, auto-refill to target, re-render
    removed_id = playlist.items[0].track_id
    playlist.remove([1])
    assert playlist.status == "draft" and playlist.outputs == {}
    playlist.fill(settings, db, seed=2)
    assert removed_id not in [i.track_id for i in playlist.items]  # never re-added after removal
    assert 3 <= len(playlist.items) <= 4
    out = pl.render(settings, db, playlist)
    assert abs(mix_seconds(settings, out) - 150) < 1.5
    assert len(list((folder / "tracks").iterdir())) == len(playlist.items)  # rebuilt after edit

    pl.commit(db, playlist, settings)
    assert db.used_track_ids() == {i.track_id for i in playlist.items}
    reloaded = Playlist.load(settings, "latest")
    assert reloaded.status == "committed" and len(reloaded.items) == len(playlist.items)

    # committed tracks are excluded from the next playlist
    nxt, notes = pl.create(settings, db, name="next", target_seconds=150, criteria=criteria)
    assert not ({i.track_id for i in nxt.items} & db.used_track_ids()) or notes


def test_render_with_loudness_normalisation_and_no_crossfade(settings, library):
    tracks = library.all_tracks()[:2]
    items = [audio.RenderItem(settings.from_stored(t.file_path), float(t.duration)) for t in tracks]
    out = settings.home / "norm.mp3"
    audio.render(settings, items, out, crossfade=0, normalize=True)
    assert abs(mix_seconds(settings, out) - 120) < 1.5
    out2 = settings.home / "xf.mp3"
    audio.render(settings, items, out2, crossfade=4, normalize=True)
    assert abs(mix_seconds(settings, out2) - 116) < 1.5


def test_filter_graph_shapes():
    items = [audio.RenderItem(__import__("pathlib").Path(f"{i}.mp3"), 100.0) for i in range(3)]
    graph = audio.build_filter(items, 3, normalize=False)
    assert graph.count("acrossfade") == 2 and graph.endswith("[out]")
    assert "concat=n=3" in audio.build_filter(items, 0, normalize=False)
    trimmed = [items[0], audio.RenderItem(items[1].path, 100.0, trim_to=40.0)]
    assert "atrim=duration=40.000" in audio.build_filter(trimmed, 2, normalize=False)
