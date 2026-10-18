import json
import sqlite3

import pytest
from conftest import make_track

from music_crawler import cli, legacy


def build_legacy_project(root):
    (root / "data").mkdir(parents=True)
    con = sqlite3.connect(root / "data" / "tracks.db")
    con.executescript("""
        CREATE TABLE tracks (track_id TEXT PRIMARY KEY, raw_json TEXT, external_source TEXT);
        CREATE TABLE downloaded_tracks (track_id TEXT, file_path TEXT);
        CREATE TABLE track_usage (track_id TEXT, use_count INTEGER);
    """)
    yt = {
        "trackId": "abc123",
        "title": "Quiet Piece",
        "artist": {"name": "Someone"},
        "duration": {"seconds": "200"},
        "attributes": {
            "genres": ["CREATOR_MUSIC_GENRE_CLASSICAL"],
            "moods": ["CREATOR_MUSIC_MOOD_CALM"],
            "instruments": ["CREATOR_MUSIC_INSTRUMENT_PIANO"],
        },
        "licenseType": "CREATOR_MUSIC_LICENSE_TYPE_CCBY_4",
        "externalTrackUrl": "http://example.com/t",
    }
    con.execute("INSERT INTO tracks VALUES (?,?,NULL)", ("abc123", json.dumps(yt)))
    con.execute("INSERT INTO track_usage VALUES ('abc123', 1)")
    con.execute("INSERT INTO downloaded_tracks VALUES ('abc123', 'C:\\\\Users\\\\x\\\\gone.mp3')")
    con.commit()
    con.close()
    (root / "data" / "used.txt").write_text("# comment\nOld Song\nA - B\n", encoding="utf-8")


def test_migrate_legacy_maps_ids_licenses_usage_and_used_txt(settings, db, tmp_path):
    old = tmp_path / "old"
    build_legacy_project(old)
    report = legacy.migrate(settings, db, old, copy_audio=False)
    assert report.tracks == 1 and report.usage_rows == 1 and report.used_titles == 2
    track = db.get("youtube:abc123")
    assert track.license == "cc-by" and track.genres == ["Classical"] and track.moods == ["calm"]
    assert track.source_url == "http://example.com/t"
    assert "youtube:abc123" in db.used_track_ids()
    assert report.files_missing == 1  # original download not present on this machine


def test_migrate_requires_legacy_db(settings, db, tmp_path):
    with pytest.raises(FileNotFoundError):
        legacy.migrate(settings, db, tmp_path / "nothing")


@pytest.mark.parametrize(
    "raw,seconds", [("60", 3600), ("45:00", 2700), ("1:15:00", 4500), ("90", 5400)]
)
def test_parse_length(raw, seconds):
    assert cli.parse_length(raw) == seconds


def test_parse_length_rejects_garbage():
    import argparse

    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_length("abc")


def test_cli_stats_make_show_remove_ban(settings, db, capsys):
    db.upsert_tracks([make_track(i, duration=200 + i) for i in range(40)])
    home = str(settings.home)
    assert cli.main(["--home", home, "stats", "--genre", "jazz", "--mood", "calm"]) == 0
    assert "조건에 맞는 곡 40곡" in capsys.readouterr().out

    assert cli.main(["--home", home, "make", "30", "--no-render", "--seed", "1"]) == 0
    assert cli.main(["--home", home, "show"]) == 0
    assert cli.main(["--home", home, "remove", "1", "--ban", "--seed", "2"]) == 0
    out = capsys.readouterr().out
    assert "제거:" in out
    assert db.conn.execute("SELECT COUNT(*) FROM tracks WHERE status='banned'").fetchone()[0] == 1


def test_cli_unknown_genre_is_a_clear_error(settings, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--home", str(settings.home), "stats", "--genre", "polka-metal"])
    assert "알 수 없는 장르" in str(exc.value)


def test_cli_add_by_search_and_ambiguity(settings, db, capsys):
    db.upsert_tracks([make_track(i) for i in range(30)])
    home = str(settings.home)
    cli.main(["--home", home, "make", "20", "--no-render", "--seed", "1"])
    capsys.readouterr()
    assert cli.main(["--home", home, "add", "Song"]) == 1  # ambiguous
    assert "여러 곡이 일치" in capsys.readouterr().out


def test_cli_make_random_genre_picks_a_genre_with_enough_music(settings, db, capsys):
    db.upsert_tracks(
        [make_track(i, genres=("Rock",), duration=200) for i in range(30)]
        + [make_track(100 + i, genres=("Jazz",), duration=200) for i in range(2)]
    )
    home = str(settings.home)
    assert (
        cli.main(
            [
                "--home",
                home,
                "make",
                "30",
                "--genre",
                "random",
                "--mood",
                "all",
                "--no-render",
                "--seed",
                "4",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "랜덤 장르 선택: Rock" in out  # Jazz has only ~13 min: not eligible for 30 min
