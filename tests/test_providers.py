import httpx

from music_crawler.providers import jamendo, openverse
from music_crawler.taxonomy import Profile


def jamendo_item(
    i,
    license_url="http://creativecommons.org/licenses/by/3.0/",
    vocal="instrumental",
    duration=200,
    allowed=True,
):
    return {
        "id": str(i),
        "name": f"Track {i}",
        "artist_name": f"Artist {i}",
        "duration": duration,
        "license_ccurl": license_url,
        "audiodownload_allowed": allowed,
        "audiodownload": f"https://example.com/dl/{i}",
        "shareurl": f"https://jamendo.test/{i}",
        "musicinfo": {
            "vocalinstrumental": vocal,
            "speed": "low",
            "tags": {"genres": ["jazz"], "instruments": ["piano"], "vartags": ["peaceful", "soft"]},
        },
    }


def test_jamendo_parse_uses_real_mood_tags_and_vocal_flag(settings):
    track, reason = jamendo.parse_track(jamendo_item(1), settings)
    assert reason is None
    assert track.genres == ["Jazz"] and track.moods == ["peaceful", "calm"]
    assert track.vocals == "instrumental" and track.license == "cc-by"


def test_jamendo_drop_reasons_are_reported(settings):
    nc = jamendo_item(2, license_url="https://creativecommons.org/licenses/by-nc-sa/3.0/")
    assert jamendo.parse_track(nc, settings)[1] == "license:cc-by-nc-sa"
    assert (
        jamendo.parse_track(jamendo_item(3, allowed=False), settings)[1] == "download-not-allowed"
    )
    assert jamendo.parse_track(jamendo_item(4, duration=5), settings)[1] == "too-short"


def test_jamendo_sync_pages_per_tag_and_stores_only_eligible(settings, db):
    settings = settings.__class__(**{**settings.__dict__, "jamendo_client_id": "x"})
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        items = [
            jamendo_item(1),
            jamendo_item(2, license_url="https://creativecommons.org/licenses/by-nc/3.0/"),
            jamendo_item(3, vocal="vocal"),
        ]
        results = (
            items if params["offset"] == "0" and params["order"] == "popularity_total_desc" else []
        )
        return httpx.Response(
            200, json={"headers": {"status": "success", "code": 0}, "results": results}
        )

    profile = Profile("t", ("Jazz",), ("calm",), vocals="strict", jamendo_tags=("jazz",))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = jamendo.sync(settings, db, profile, client=client, sleep=lambda _s: None)
    assert (
        report.kept == 1
        and report.dropped["license:cc-by-nc"] == 1
        and report.dropped["vocal"] == 1
    )
    assert calls[0]["tags"] == "jazz" and calls[0]["vocalinstrumental"] == "instrumental"
    assert db.get("jamendo:1") is not None and db.get("jamendo:2") is None


def test_jamendo_api_error_raises(settings, db):
    settings = settings.__class__(**{**settings.__dict__, "jamendo_client_id": "x"})
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={"headers": {"status": "failed", "code": 5, "error_message": "bad client"}},
            )
        )
    )
    profile = Profile("t", (), (), jamendo_tags=("jazz",))
    try:
        jamendo.sync(settings, db, profile, client=client, sleep=lambda _s: None)
    except jamendo.JamendoError as exc:
        assert "bad client" in str(exc)
    else:
        raise AssertionError("expected JamendoError")


def ov_item(i, duration_ms=200_000, license="by", category="music", title="Calm piano"):
    return {
        "id": f"id{i}",
        "title": title,
        "creator": f"C{i}",
        "url": f"https://x.test/{i}.mp3",
        "foreign_landing_url": f"https://x.test/page/{i}",
        "duration": duration_ms,
        "license": license,
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "category": category,
        "tags": [{"name": "piano"}, {"name": "relaxing"}],
    }


def test_openverse_parse_converts_ms_and_drops_clips(settings):
    track, _ = openverse.parse_track(ov_item(1), settings)
    assert track.duration == 200 and track.license == "cc-by" and "calm" in track.moods
    assert openverse.parse_track(ov_item(2, duration_ms=2500), settings)[1] == "too-short"
    assert (
        openverse.parse_track(ov_item(3, category="sound_effect"), settings)[1]
        == "category:sound_effect"
    )
    assert openverse.parse_track(ov_item(4, license="by-nc"), settings)[1] == "license:cc-by-nc"


def test_openverse_keeps_earlier_results_when_a_new_search_runs(settings, db):
    """Regression: the old code deactivated every Openverse row before each refresh."""

    def run(query, ids):
        def handler(request):
            return httpx.Response(200, json={"results": [ov_item(i) for i in ids]})

        profile = Profile("t", (), (), openverse_queries=({"q": query},))
        client = httpx.Client(transport=httpx.MockTransport(handler))
        return openverse.sync(settings, db, profile, client=client, sleep=lambda _s: None)

    run("first", [1, 2])
    run("second", [3])
    assert {t.id for t in db.all_tracks(sources=["openverse"])} == {
        "openverse:id1",
        "openverse:id2",
        "openverse:id3",
    }


def test_openverse_rate_limit_keeps_partial_results(settings, db):
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(200, json={"results": [ov_item(i) for i in range(20)]})
        return httpx.Response(429, headers={"Retry-After": "999"})

    profile = Profile("t", (), (), openverse_queries=({"q": "a"}, {"q": "b"}))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = openverse.sync(settings, db, profile, client=client, sleep=lambda _s: None)
    assert report.kept == 20 and any("429" in n for n in report.notes)


def test_jamendo_sync_resumes_from_stored_cursor(settings, db):
    """Each run continues deeper instead of re-reading from offset 0 (constant cost per run)."""
    settings = settings.__class__(**{**settings.__dict__, "jamendo_client_id": "x"})
    catalog = [jamendo_item(i) for i in range(450)]
    pops = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        off, lim = int(q["offset"]), int(q["limit"])
        if q["order"] == "popularity_total_desc":
            pops.append(off)
        rows = catalog[off : off + lim] if q["order"] == "popularity_total_desc" else []
        return httpx.Response(
            200, json={"headers": {"status": "success", "code": 0}, "results": rows}
        )

    profile = Profile("t", ("Jazz",), ("calm",), jamendo_tags=("jazz",))
    client = httpx.Client(transport=httpx.MockTransport(handler))

    def run(**kw):
        return jamendo.sync(settings, db, profile, client=client, sleep=lambda _s: None, **kw)

    assert run(per_tag_limit=200).inserted == 200 and pops == [0]
    assert run(per_tag_limit=200).inserted == 200 and pops == [0, 200]  # continued, not restarted
    third = run(per_tag_limit=200)  # 50 left -> exhausted
    assert third.inserted == 50 and pops == [0, 200, 400]
    run(per_tag_limit=200)
    assert pops == [0, 200, 400]  # exhausted: no more requests
    assert db.get_cursor("jamendo", "jazz|popularity_total_desc") == (450, True)

    run(per_tag_limit=200, reset=True)
    assert pops[-1] == 0  # --reset starts over


def test_jamendo_all_runs_until_exhausted(settings, db):
    settings = settings.__class__(**{**settings.__dict__, "jamendo_client_id": "x"})
    catalog = [jamendo_item(i) for i in range(650)]

    def handler(request):
        q = dict(request.url.params)
        rows = (
            catalog[int(q["offset"]) : int(q["offset"]) + 200]
            if q["order"] == "popularity_total_desc"
            else []
        )
        return httpx.Response(
            200, json={"headers": {"status": "success", "code": 0}, "results": rows}
        )

    profile = Profile("t", (), (), jamendo_tags=("jazz",))
    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = jamendo.sync(
        settings, db, profile, per_tag_limit=None, client=client, sleep=lambda _s: None
    )
    assert report.inserted == 650
