import asyncio
import threading
import tempfile
import unittest
from pathlib import Path

from pixabay_music_downloader import (
    PixabayMusicDownloader, PixabayTrack, _extract_audio_url,
    _page_url, _validate_pixabay_search_url, _looks_like_audio,
)


class FakeResponse:
    def __init__(self, text="", content=b"", headers=None, status_code=200):
        self.text = text
        self.content = content or text.encode()
        self.headers = headers or {"content-type": "text/html; charset=utf-8"}
        self.status_code = status_code
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")
    def iter_bytes(self, chunk_size=65536):
        yield self.content
    def __enter__(self): return self
    def __exit__(self, *args): return False


class FakeClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []
    def get(self, url):
        self.calls.append(("get", url))
        return self.responses[url]
    def stream(self, method, url, headers=None):
        self.calls.append((method, url))
        return self.responses[url]
    def close(self): pass


class PixabayDownloaderTests(unittest.TestCase):
    def test_validates_search_url(self):
        self.assertEqual(_validate_pixabay_search_url("https://pixabay.com/music/search/new-age/"), "https://pixabay.com/music/search/new-age/")
        with self.assertRaises(ValueError):
            _validate_pixabay_search_url("https://example.com/music/search/test/")
        with self.assertRaises(ValueError):
            _validate_pixabay_search_url("https://pixabay.com/images/search/cat/")

    def test_page_query_preserves_filters_and_replaces_pagi(self):
        result = _page_url("https://pixabay.com/music/search/new-age/?genre=ambient&pagi=2", 4)
        self.assertIn("genre=ambient", result)
        self.assertIn("pagi=4", result)
        self.assertNotIn("pagi=2", result)

    def test_parses_real_detail_links_and_deduplicates(self):
        url = "https://pixabay.com/music/search/new-age/"
        html = '''<html><a href="/music/calm-night-123456/">Calm Night</a>
        <a href="/music/calm-night-123456/">Calm Night duplicate</a>
        <a href="/music/search/ambient/">Ambient search</a>
        <a href="https://example.com/music/not-pixabay-111/">Not Pixabay</a></html>'''
        client = FakeClient({url: FakeResponse(html)})
        downloader = PixabayMusicDownloader(client=client, delay_seconds=0)
        tracks = downloader.search(url)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].track_id, "123456")
        self.assertEqual(tracks[0].title, "Calm Night")

    def test_extracts_only_pixabay_audio_url(self):
        page = '<script>"src":"https://cdn.pixabay.com/audio/2024/track.mp3?x=1"</script>'
        self.assertEqual(_extract_audio_url(page, "https://pixabay.com/music/example-123/"), "https://cdn.pixabay.com/audio/2024/track.mp3?x=1")
        bad = '<script>"src":"https://evil.example/steal.mp3"</script>'
        self.assertIsNone(_extract_audio_url(bad, "https://pixabay.com/music/example-123/"))

    def test_download_validates_audio_and_writes_atomically(self):
        detail = "https://pixabay.com/music/calm-night-123456/"
        audio_url = "https://cdn.pixabay.com/audio/2024/track.mp3"
        page_html = f'<script>"src":"{audio_url}"</script>'
        payload = b"ID3" + (b"\x00" * 5000)
        client = FakeClient({
            detail: FakeResponse(page_html),
            audio_url: FakeResponse(content=payload, headers={"content-type":"audio/mpeg"}),
        })
        downloader = PixabayMusicDownloader(client=client, delay_seconds=0)
        with tempfile.TemporaryDirectory() as temp:
            result = downloader.download(PixabayTrack("Calm Night", detail, 1, "123456"), temp)
            self.assertTrue(result.exists())
            self.assertGreater(result.stat().st_size, 4096)
            self.assertFalse(result.with_suffix(".mp3.part").exists())

    def test_rejects_html_disguised_as_audio(self):
        self.assertFalse(_looks_like_audio(b"<html>blocked</html>", "audio/mpeg"))

    def test_http_403_uses_browser_fallback(self):
        url = "https://pixabay.com/music/search/new-age/"
        client = FakeClient({url: FakeResponse("blocked", status_code=403)})
        downloader = PixabayMusicDownloader(client=client, delay_seconds=0)
        expected = "<html><a href='/music/test-track-12345/'>Test Track</a></html>"
        calls = []
        def browser_fallback(requested_url):
            calls.append(requested_url)
            return expected
        downloader._get_text_via_browser = browser_fallback
        downloader._browser_mode = False
        result = downloader._get_text(url)
        self.assertEqual(result, expected)
        self.assertEqual(calls, [url])
        self.assertTrue(downloader._browser_mode)

    def test_browser_sync_api_runs_outside_asyncio_loop_thread(self):
        url = "https://pixabay.com/music/search/new-age/"
        downloader = PixabayMusicDownloader(client=FakeClient({}), delay_seconds=0)
        caller_thread = threading.get_ident()
        worker_threads = []
        expected = "<html>browser response</html>"

        def fake_sync_browser(requested_url):
            worker_threads.append(threading.get_ident())
            return expected

        downloader._get_text_via_browser_sync = fake_sync_browser

        async def scenario():
            # Reproduce the exact application shape: called while asyncio.run(main()) is active.
            return downloader._get_text_via_browser(url)

        try:
            result = asyncio.run(scenario())
            self.assertEqual(result, expected)
            self.assertEqual(len(worker_threads), 1)
            self.assertNotEqual(worker_threads[0], caller_thread)
        finally:
            downloader.close()



if __name__ == "__main__":
    unittest.main(verbosity=2)
