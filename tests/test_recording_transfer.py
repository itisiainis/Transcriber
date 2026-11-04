import base64
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import recording


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe required")
class RecordingTransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_dir = tempfile.TemporaryDirectory()
        fixture = Path(cls.fixture_dir.name) / "source.webm"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=1", "-c:a", "libopus", str(fixture)],
                       check=True, capture_output=True)
        cls.audio = fixture.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.fixture_dir.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = patch.object(recording, "RECORD_DIR", Path(self.directory.name) / "recordings")
        self.root.start()

    def tearDown(self):
        self.root.stop()
        self.directory.cleanup()

    def upload(self, data, filename="Lesson.webm"):
        rec_id = recording.begin_import(filename, len(data))
        for offset in range(0, len(data), recording.TRANSFER_CHUNK):
            recording.import_chunk(rec_id, offset, data[offset:offset + recording.TRANSFER_CHUNK])
        return rec_id

    def test_real_webm_export_import_round_trip_preserves_audio_bytes(self):
        # Small chunks force multiple requests even with a short audio fixture.
        with patch.object(recording, "TRANSFER_CHUNK", 1000):
            source = self.upload(self.audio, "Lesson.webm")
            recording.finish_import(source)
            chunks, offset = [], 0
            while offset < len(self.audio):
                result = recording.export_chunk(source, offset)
                self.assertEqual(result["filename"], "Lesson.webm")
                self.assertLess(len(base64.b64encode(result["data"])), 1_000_000)
                chunks.append(result["data"])
                offset += len(result["data"])
            downloaded = b"".join(chunks)
            self.assertEqual(downloaded, self.audio)
            destination = self.upload(downloaded, result["filename"])
            recording.finish_import(destination)
        self.assertNotEqual(source, destination)
        self.assertEqual(recording.audio_path(destination).read_bytes(), self.audio)
        self.assertEqual(recording.metadata(destination)["title"], "Lesson")
        self.assertGreater(recording.metadata(destination)["duration"], 0)

    def test_incomplete_import_is_not_published(self):
        rec_id = recording.begin_import("Lesson.webm", len(self.audio))
        recording.import_chunk(rec_id, 0, self.audio[:100])
        with self.assertRaisesRegex(ValueError, "incomplete"):
            recording.finish_import(rec_id)
        self.assertFalse(recording.audio_path(rec_id).exists())

    def test_invalid_audio_is_rejected(self):
        rec_id = self.upload(b"not an audio recording")
        with self.assertRaisesRegex(ValueError, "readable recording audio"):
            recording.finish_import(rec_id)
        self.assertFalse(recording.audio_path(rec_id).exists())

    def test_wrong_offsets_and_sizes_do_not_corrupt_import(self):
        rec_id = recording.begin_import("Lesson.webm", 5)
        with self.assertRaises(ValueError):
            recording.import_chunk(rec_id, 1, b"abc")
        with self.assertRaises(ValueError):
            recording.import_chunk(rec_id, 0, b"abcdef")
        self.assertEqual(recording.import_chunk(rec_id, 0, b"abc"), 3)
        with self.assertRaises(ValueError):
            recording.import_chunk(rec_id, 0, b"abc")
        self.assertEqual((recording.folder(rec_id) / "import.webm").read_bytes(), b"abc")

    def test_file_names_cannot_choose_storage_paths(self):
        rec_id = recording.begin_import("../../Lesson.webm", 10)
        self.assertEqual(recording.metadata(rec_id)["title"], "Lesson")
        self.assertEqual(recording.folder(rec_id).parent, recording.RECORD_DIR)
        with self.assertRaises(ValueError):
            recording.begin_import("Lesson.exe", 10)
        with self.assertRaises(ValueError):
            recording.begin_import("Lesson.webm", 0)
        with self.assertRaises(ValueError):
            recording.export_chunk("../../outside", 0)
