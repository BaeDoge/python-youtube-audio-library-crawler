from music_crawler import licenses
from music_crawler import taxonomy as t


def test_genre_mapping_across_sources():
    assert t.normalize_genres(["CREATOR_MUSIC_GENRE_JAZZ_AND_BLUES"]) == ["Jazz"]
    assert t.normalize_genres(["newage", "classical", "piano"]) == ["NewAge", "Classical", "Piano"]
    assert t.normalize_genres(["unheard-of"]) == []


def test_mood_mapping_ignores_noise_tags():
    assert t.normalize_moods(["peaceful", "soft", "neutral", "voice"]) == ["peaceful", "calm"]


def test_vocal_detection_explicit_flag_wins():
    assert t.detect_vocals("Anything", ["vocal"], "instrumental") == "instrumental"
    assert t.detect_vocals("Song (feat. Someone)") == "vocal"
    assert t.detect_vocals("Piano", ["Instrumental"]) == "instrumental"
    assert t.detect_vocals("Plain title") == "unknown"


def test_classify_text_uses_word_boundaries():
    genres, moods = t.classify_text("Upbeat funky beat")
    assert "HipHop" not in genres  # "beat" must not match hip-hop
    assert "energetic" in moods


def test_license_normalisation():
    assert licenses.from_cc_url("http://creativecommons.org/licenses/by/3.0/") == "cc-by"
    assert (
        licenses.from_cc_url("https://creativecommons.org/licenses/by-nc-sa/2.5/") == "cc-by-nc-sa"
    )
    assert licenses.from_cc_url("https://creativecommons.org/publicdomain/zero/1.0/") == "cc0"
    assert licenses.from_openverse("by") == "cc-by"
    assert licenses.from_openverse("by-nc") == "cc-by-nc"


def test_profiles_extend_from_toml(tmp_path):
    f = tmp_path / "profiles.toml"
    f.write_text('[profiles.jazzy]\nextends = "relax"\ngenres = ["Jazz"]\n', encoding="utf-8")
    profiles = t.load_profiles(f)
    assert profiles["jazzy"].genres == ("Jazz",)
    assert profiles["jazzy"].moods == t.RELAX.moods
