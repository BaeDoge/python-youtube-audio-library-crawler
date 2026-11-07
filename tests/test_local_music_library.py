"""Regression tests for the local music library feature. Run: python -m unittest discover -s tests -v"""
import builtins
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import music


class LocalMusicLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = music.get_ffmpeg_path()
        if not cls.ffmpeg or not shutil.which("ffprobe"):
            raise unittest.SkipTest("FFmpeg and ffprobe are required for local audio tests")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="local_music_test_")
        self.root = Path(self.temp.name)
        self.library = self.root / "music_library"
        self.library.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def make_audio(self, filename, duration, artist, title, frequency=440):
        path = self.library / filename
        command = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                   f"sine=frequency={frequency}:duration={duration}", "-metadata", f"artist={artist}",
                   "-metadata", f"title={title}"]
        if path.suffix.lower() == ".mp3":
            command += ["-codec:a", "libmp3lame"]
        command += ["-y", str(path)]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return path

    def test_scan_reads_duration_and_tags_and_writes_catalog(self):
        self.make_audio("one.wav", 3, "Artist A", "Track A")
        self.make_audio("two.mp3", 4, "Artist B", "Track B", 660)
        tracks = music.scan_local_library(self.library)
        self.assertEqual(len(tracks), 2)
        self.assertEqual({row["title"] for row in tracks}, {"Track A", "Track B"})
        self.assertEqual({row["duration"] for row in tracks}, {3, 4})
        self.assertTrue((self.library / "catalog.json").is_file())

    def test_optimizer_selects_unique_tracks_to_target(self):
        self.make_audio("Artist A - Track A.wav", 3, "Artist A", "Track A")
        self.make_audio("Artist B - Track B.wav", 4, "Artist B", "Track B", 660)
        tracks = music.scan_local_library(self.library, write_catalog=False)
        original_max = music.MAX_TRACKS_PER_PLAYLIST
        try:
            music.MAX_TRACKS_PER_PLAYLIST = 10
            selected = music.find_best_playlist(tracks, 7)
        finally:
            music.MAX_TRACKS_PER_PLAYLIST = original_max
        self.assertEqual(sum(row["duration"] for row in selected), 7)
        self.assertEqual(len({row["track_id"] for row in selected}), 2)

    def test_duplicate_recordings_and_empty_optimizer_regression(self):
        self.make_audio("copy_one.wav", 3, "Artist-A", "Track A!")
        self.make_audio("copy_two.wav", 3, "artist a", "track-a", 660)
        tracks = music.scan_local_library(self.library, write_catalog=False)
        self.assertEqual(len(tracks), 1, "same artist/title with punctuation/case variants must be deduplicated")
        self.assertEqual(music.find_best_playlist([], 60), [])
        duplicate_rows = [
            {"track_id": "id-one", "title": "Blue-Sky!", "artist": "The Artist", "duration": 200, "genres": "[]", "moods": "[]"},
            {"track_id": "id-two", "title": "blue sky", "artist": "the artist", "duration": 210, "genres": "[]", "moods": "[]"},
        ]
        self.assertEqual(len(music.deduplicate_tracks(duplicate_rows)), 1)

    def test_mixed_format_merge_and_timeline_outputs(self):
        self.make_audio("Artist A - Track A.wav", 3, "Artist A", "Track A")
        self.make_audio("Artist B - Track B.mp3", 4, "Artist B", "Track B", 660)
        tracks = music.scan_local_library(self.library, write_catalog=False)
        items = []
        elapsed = 0
        for index, row in enumerate(tracks, 1):
            items.append({"position": index, "track_id": row["track_id"], "title": row["title"],
                          "artist": row["artist"], "duration": row["duration"],
                          "start_seconds": elapsed, "timestamp": music.format_time(elapsed),
                          "file_path": row["file_path"]})
            elapsed += row["duration"]
        output = self.root / "output" / "merged.mp3"
        merged = music._merge_local_tracks_with_ffmpeg(items, output)
        self.assertIsNotNone(merged)
        self.assertGreater(merged.stat().st_size, 1000)
        out_dir = self.root / "output" / "playlist"
        music._write_local_playlist_outputs(items, 7, out_dir, merged)
        for name in ("playlist.txt", "timeline.txt", "timeline.csv", "playlist.json"):
            self.assertTrue((out_dir / name).is_file(), name)

    def test_end_to_end_updates_used_and_existing_database_tables(self):
        self.make_audio("Artist A - Track A.wav", 3, "Artist A", "Track A")
        self.make_audio("Artist B - Track B.mp3", 4, "Artist B", "Track B", 660)
        data_dir = self.root / "data"
        data_dir.mkdir()
        db_path = data_dir / "tracks.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE tracks (track_id TEXT PRIMARY KEY, external_source TEXT)")
        conn.commit()
        conn.close()

        original = {
            "LOCAL_MUSIC_DIR": music.LOCAL_MUSIC_DIR,
            "LOCAL_CATALOG_PATH": music.LOCAL_CATALOG_PATH,
            "DB_PATH": music.DB_PATH,
            "USED_FILE": music.USED_FILE,
            "PLAYLIST_DIR": music.PLAYLIST_DIR,
        }
        music.LOCAL_MUSIC_DIR = self.library
        music.LOCAL_CATALOG_PATH = self.library / "catalog.json"
        music.DB_PATH = db_path
        music.USED_FILE = data_dir / "used.txt"
        music.PLAYLIST_DIR = self.root / "output" / "playlists"
        try:
            music.init_db()
            with patch("builtins.input", return_value="00:07"):
                music.create_local_library_playlist()
            used = music.USED_FILE.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(used), 2)
            self.assertEqual(len(list(music.PLAYLIST_DIR.glob("local_*/merged_playlist.mp3"))), 1)
            conn = sqlite3.connect(db_path)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM track_usage").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM playlist_history").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM playlist_items").fetchone()[0], 2)
            conn.close()
            with patch("builtins.input", return_value="00:07"):
                music.create_local_library_playlist()
            conn = sqlite3.connect(db_path)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM playlist_history").fetchone()[0], 1)
            conn.close()
        finally:
            for name, value in original.items():
                setattr(music, name, value)


if __name__ == "__main__":
    unittest.main()
