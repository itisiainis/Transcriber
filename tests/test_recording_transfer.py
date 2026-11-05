import base64
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

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
        rec_id = self.upload(b"not an audio recording", "Lesson.mp3")
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
        self.assertEqual((recording.folder(rec_id) / "import.media").read_bytes(), b"abc")

    def test_file_names_cannot_choose_storage_paths(self):
        rec_id = recording.begin_import("../../Lesson.webm", 10)
        self.assertEqual(recording.metadata(rec_id)["title"], "Lesson")
        self.assertEqual(recording.folder(rec_id).parent, recording.RECORD_DIR)
        with self.assertRaises(ValueError):
            recording.begin_import("", 10)
        with self.assertRaises(ValueError):
            recording.begin_import("Lesson.webm", 0)
        with self.assertRaises(ValueError):
            recording.export_chunk("../../outside", 0)

    def test_common_audio_formats_are_converted_and_exported_as_audio_webm(self):
        for extension, codec in [('mp3', 'libmp3lame'), ('ogg', 'libvorbis'),
                                 ('wav', 'pcm_s16le'), ('flac', 'flac'),
                                 ('m4a', 'aac'), ('aac', 'aac'), ('opus', 'libopus')]:
            with self.subTest(extension=extension):
                source = Path(self.directory.name) / f'Lecture.part1.{extension}'
                subprocess.run(['ffmpeg', '-v', 'error', '-i',
                                str(Path(self.fixture_dir.name) / 'source.webm'),
                                '-c:a', codec, str(source)], check=True, capture_output=True)
                rec_id = self.upload(source.read_bytes(), source.name)
                meta = recording.finish_import(rec_id)
                self.assertEqual(recording.metadata(rec_id)['title'], 'Lecture.part1')
                self.assertGreater(meta['duration'], 0)
                self.assertEqual(meta['bytes'], recording.audio_path(rec_id).stat().st_size)
                self.assertEqual(meta['import_size'], source.stat().st_size)
                self.assertFalse((recording.folder(rec_id) / 'import.media').exists())
                probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                                        'stream=codec_type,codec_name', '-of', 'json',
                                        str(recording.audio_path(rec_id))],
                                       capture_output=True, text=True, check=True)
                streams = json.loads(probe.stdout)['streams']
                self.assertEqual(len(streams), 1)
                self.assertEqual(streams[0]['codec_type'], 'audio')
                self.assertIn(streams[0]['codec_name'], ('opus', 'vorbis'))
                exported = recording.export_chunk(rec_id, 0)
                self.assertEqual(exported['filename'], 'Lecture.part1.webm')
                # The same conversion the transcription pipeline will use.
                self.assertTrue(recording.to_wav(rec_id, Path(self.directory.name) / extension).is_file())

    def test_mp4_video_import_extracts_only_audio(self):
        source = Path(self.directory.name) / 'Lecture.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'color=size=16x16:rate=1:duration=1', '-i',
                        str(Path(self.fixture_dir.name) / 'source.webm'),
                        '-c:v', 'mpeg4', '-c:a', 'aac', '-shortest', str(source)],
                       check=True, capture_output=True)
        rec_id = self.upload(source.read_bytes(), source.name)
        recording.finish_import(rec_id)
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type',
                                '-of', 'json', str(recording.audio_path(rec_id))],
                               capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(probe.stdout)['streams'], [{'codec_type': 'audio'}])

    def test_video_without_audio_is_rejected(self):
        source = Path(self.directory.name) / 'Silent.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'color=size=16x16:rate=1:duration=1', '-c:v', 'mpeg4', str(source)],
                       check=True, capture_output=True)
        rec_id = self.upload(source.read_bytes(), source.name)
        with self.assertRaisesRegex(ValueError, 'readable recording audio'):
            recording.finish_import(rec_id)
        self.assertFalse(recording.audio_path(rec_id).exists())

    def test_conversion_failure_does_not_publish_partial_audio(self):
        rec_id = self.upload(self.audio, 'Lecture.mp3')
        # Force conversion even though the fixture is already WebM.
        probe = Mock(returncode=0, stdout=json.dumps({
            'streams': [{'codec_type': 'audio', 'codec_name': 'mp3'}],
            'format': {'format_name': 'mp3'}}))
        prepared = recording.folder(rec_id) / 'prepared.webm'
        prepared.write_bytes(b'partial output')
        with patch.object(recording.subprocess, 'run', side_effect=[
                probe, Mock(returncode=1, stderr='decoder failed')]):
            with self.assertRaisesRegex(ValueError, 'Could not import this audio'):
                recording.finish_import(rec_id)
        self.assertFalse(recording.audio_path(rec_id).exists())
        self.assertFalse(prepared.exists())
        self.assertNotIn('finished_at', recording.meta(rec_id))
