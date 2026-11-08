from pathlib import Path

from conftest import make_track

from music_crawler import audio, cli
from music_crawler.cli import main
from music_crawler.playlist import Item, Playlist
from music_crawler.providers import local
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
    assert rows[0].endswith(",source_url,license_url")
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
    calm = make_track(10, source="local", duration=120, moods=("calm",))
    uplifting = make_track(11, source="local", duration=140, moods=("uplifting",))
    db.upsert_tracks([calm, uplifting])

    assert main([
        "--home", str(settings.home), "make", "4",
        "--genre", "jazz", "--mood", "calm",
        "--library-only", "--no-render", "--seed", "7",
    ]) == 0
    result = Playlist.load(settings, "latest")
    assert {item.track_id for item in result.items} == {calm.id, uplifting.id}
    assert result.criteria["moods"] == []



def test_desktop_menu_command_is_registered_without_starting_tkinter():
    args = cli.build_parser().parse_args(["menu"])
    assert args.func is cli.cmd_menu
    alias = cli.build_parser().parse_args(["gui"])
    assert alias.func is cli.cmd_menu



def test_pixabay_filename_metadata_extracts_creator_title_and_asset_id():
    metadata = local.pixabay_filename_metadata(
        "mellowbeansmusic-city-pop-sparks-beneath-the-overpass-349357.mp3"
    )
    assert metadata == {
        "artist": "Mellowbeansmusic",
        "title": "City Pop Sparks Beneath the Overpass",
        "pixabay_id": "349357",
    }


def test_local_category_discovery_keeps_custom_folder_names(tmp_path):
    (tmp_path / "Pixabay" / "City Pop" / "Night Drive").mkdir(parents=True)
    (tmp_path / "Pixabay" / "City Pop" / "Night Drive" / "track.mp3").write_bytes(b"audio")
    (tmp_path / "Jazz" / "Calm").mkdir(parents=True)
    (tmp_path / "Jazz" / "Calm" / "another.mp3").write_bytes(b"audio")

    genres, moods = local.discover_local_categories(tmp_path)

    assert "City Pop" in genres
    assert "Jazz" in genres
    assert "Night Drive" in moods
    assert "calm" in moods
    assert "Pixabay" not in genres


def test_local_sync_uses_custom_folders_and_pixabay_filename_metadata(
    settings, db, monkeypatch
):
    filename = "mellowbeansmusic-city-pop-sparks-beneath-the-overpass-349357.mp3"
    path = settings.inbox_dir / "Pixabay" / "City Pop" / "Night Drive" / filename
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fake audio fixture")
    digest = "a" * 64

    monkeypatch.setattr(
        local, "probe",
        lambda _settings, _path: {"duration": 181, "tags": {}},
    )
    monkeypatch.setattr(local, "file_hash", lambda _path: digest)

    report = local.sync(settings, db)

    track = db.get(f"local:{digest[:12]}")
    assert report.inserted == 1
    assert track is not None
    assert track.artist == "Mellowbeansmusic"
    assert track.title == "City Pop Sparks Beneath the Overpass"
    assert track.genres == ["City Pop"]
    assert track.moods == ["Night Drive"]
    assert track.license == "pixabay"
    assert "pixabay-id:349357" in track.tags
    assert any("349357" in note for note in report.notes)


def test_local_sidecar_overrides_pixabay_filename_display_metadata(settings, db, monkeypatch):
    filename = "mellowbeansmusic-city-pop-sparks-beneath-the-overpass-349357.mp3"
    path = settings.inbox_dir / "Pixabay" / "City Pop" / "Night" / filename
    path.parent.mkdir(parents=True)
    path.write_bytes(b"fake audio fixture")
    path.with_suffix(".json").write_text(
        (
            '{"title":"[City Pop] Sparks Beneath the Overpass",'
            '"artist":"MellowBeansMusic","genre":"City Pop","mood":"Night",'
            '"license":"pixabay","source_url":"https://pixabay.com/ko/music/example/"}'
        ),
        encoding="utf-8",
    )
    digest = "b" * 64

    monkeypatch.setattr(local, "probe", lambda _settings, _path: {"duration": 181, "tags": {}})
    monkeypatch.setattr(local, "file_hash", lambda _path: digest)
    local.sync(settings, db)

    track = db.get(f"local:{digest[:12]}")
    assert track.artist == "MellowBeansMusic"
    assert track.title == "[City Pop] Sparks Beneath the Overpass"
    assert track.source_url == "https://pixabay.com/ko/music/example/"
