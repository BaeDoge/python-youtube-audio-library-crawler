import random

from conftest import make_track

from music_crawler.selector import Criteria, filter_candidates, playlist_length, select


def test_filter_respects_genre_mood_vocals_and_used(settings, db):
    db.upsert_tracks(
        [
            make_track(1),
            make_track(2, genres=("Rock",)),
            make_track(3, moods=("energetic",)),
            make_track(4, vocals="vocal"),
            make_track(5, vocals="unknown"),
            make_track(6, moods=()),
        ]
    )
    db.record_usage(["jamendo:1"], "p1")
    c = Criteria(genres=("Jazz",), moods=("calm",), vocals="lenient")
    ids = {t.id for t in filter_candidates(db, c)}
    assert ids == {"jamendo:5", "jamendo:6"}  # 1 used, 2 genre, 3 mood, 4 vocal
    strict = Criteria(
        genres=("Jazz",), moods=("calm",), vocals="strict", exclude_used=False, strict_mood=True
    )
    assert {t.id for t in filter_candidates(db, strict)} == {"jamendo:1"}


def test_banned_and_invalid_tracks_are_never_candidates(settings, db):
    db.upsert_tracks([make_track(1), make_track(2), make_track(3)])
    db.set_status("jamendo:2", "banned")
    db.set_status("jamendo:3", "invalid", "not audio")
    assert {t.id for t in filter_candidates(db, Criteria())} == {"jamendo:1"}


def test_legacy_used_titles_exclude_matching_tracks(settings, db):
    db.upsert_tracks([make_track(1, artist="Kevin MacLeod"), make_track(2)])
    db.add_used_titles(["Kevin MacLeod - Song 1"])
    assert {t.id for t in filter_candidates(db, Criteria())} == {"jamendo:2"}


def test_duplicate_recordings_collapse_to_one(settings, db):
    a = make_track(1, artist="Same", source="jamendo")
    b = make_track(1, artist="Same", source="openverse")
    b.title = a.title
    db.upsert_tracks([a, b])
    assert len(filter_candidates(db, Criteria())) == 1


def test_select_hits_target_within_tolerance(settings):
    rng = random.Random(0)
    pool = [make_track(i, duration=rng.randint(150, 330)) for i in range(200)]
    for target in (1800, 3600, 7200):
        res = select(pool, target, settings, Criteria(), tolerance=120, seed=target)
        assert target <= res.length <= target + 120
        assert len({t.id for t in res.tracks}) == len(res.tracks)


def test_select_caps_tracks_per_artist_and_avoids_back_to_back(settings):
    pool = [make_track(i, artist=f"A{i % 5}") for i in range(60)]
    res = select(pool, 3600, settings, Criteria(), seed=1)
    names = [t.artist for t in res.tracks]
    assert max(names.count(a) for a in set(names)) <= settings.max_per_artist
    assert all(a != b for a, b in zip(names, names[1:], strict=False))


def test_select_keeps_locked_tracks_first_and_reports_shortage(settings):
    pool = [make_track(i) for i in range(3)]
    res = select(pool, 3600, settings, Criteria(), locked=[pool[2]], seed=1)
    assert res.tracks[0].id == pool[2].id
    assert res.notes and res.length < 3600


def test_playlist_length_accounts_for_crossfade():
    tracks = [make_track(i, duration=100) for i in range(3)]
    assert playlist_length(tracks, 3) == 294
