from pathlib import Path

from conftest import make_track

from music_crawler import audio
from music_crawler.cli import main
from music_crawler.playlist import Item, Playlist
from music_crawler.selector import Criteria


def test_final_track_always_fades_out():
    first = audio.RenderItem(Path("first.mp3"), 100.0)
    last = audio.RenderItem(Path("last.mp3"), 100.0)
    graph = audio.build_filter([first, last], 3, normalize=False)
    assert "afade=t=out:st=95.000:d=5.000" in graph
    assert "[0:a]" in graph and "afade" not in graph.split(";")[0]

    trimmed = audio.build_filter(
        [first, audio.RenderItem(Path("last.mp3"), 100.0, trim_to=40.0)],
        3,
        normalize=False,
    )
    assert "atrim=duration=40.000" in trimmed
    assert "afade=t=out:st=35.000:d=5.000" in trimmed


def test_timeline_reports_trimmed_final_track_duration(settings):
    tracks = [make_track(1, duration=100), make_track(2, duration=100)]
    p = Playlist(
        id="timeline-test",
        name="timeline",
        target_seconds=130,
        criteria=Criteria().as_dict(),
        exact=True,
        items=[Item.from_track(track) for track in tracks],
    )
    assert p.trim_for_exact(3) == 33
    p.write_text_outputs(settings, 3)
    rows = (p.folder(settings) / "timeline.csv").read_text(encoding="utf-8-sig").splitlines()
    assert rows[0].startswith("#,start,duration,end,duration_seconds")
    assert ",01:37,00:33,02:10,33," in rows[2]


def test_remove_bans_track_by_default_and_can_be_reversed(settings, db):
    track = make_track(1)
    db.upsert_tracks([track])
    p = Playlist(
        id="remove-ban-test",
        name="remove-ban",
        target_seconds=120,
        criteria=Criteria().as_dict(),
        items=[Item.from_track(track)],
    )
    p.save(settings)

    assert main([
        "--home", str(settings.home), "remove", "1",
        "--playlist", p.id, "--no-fill",
    ]) == 0
    assert db.get(track.id).status == "banned"

    assert main(["--home", str(settings.home), "lists"]) == 0
    assert main(["--home", str(settings.home), "unban", track.id]) == 0
    assert db.get(track.id).status == "candidate"


def test_make_relaxes_mood_only_after_requested_mood_is_short(settings, db):
    calm = make_track(10, source="local", duration=80, moods=("calm",))
    uplifting = make_track(11, source="local", duration=80, moods=("uplifting",))
    db.upsert_tracks([calm, uplifting])

    assert main([
        "--home", str(settings.home), "make", "2",
        "--genre", "jazz", "--mood", "calm",
        "--library-only", "--no-render", "--seed", "7",
    ]) == 0
    result = Playlist.load(settings, "latest")
    assert {item.track_id for item in result.items} == {calm.id, uplifting.id}
    assert result.criteria["moods"] == []
