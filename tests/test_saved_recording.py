"""Exercise host commands without importing its stdout/native-pipe setup."""
import ast
import logging
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import jobs
import recording
import store
import transcribe


class SavedRecordingTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        store.migrate(self.conn)
        self.conn.executescript(jobs.SCHEMA)
        for column, kind in jobs.COLUMNS:
            self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {kind}")
        tree = ast.parse((ROOT / "host.py").read_text(encoding="utf-8"))
        names = {"cmd_rec_stop", "cmd_transcribe", "build_rows", "cmd_rec_import_finish", "cmd_rec_import_abort"}
        module = ast.Module(body=[node for node in tree.body if isinstance(node, ast.FunctionDef)
                                  and node.name in names], type_ignores=[])
        self.env = {"recording": recording, "store": store, "jobs": jobs, "tx": transcribe,
                    "log": logging.getLogger(__name__), "ensure_worker": Mock(),
                    "ratios": lambda conn: (0.25, 0.01), "batch": Mock(),
                    "source_label": lambda value: value}
        exec(compile(module, "host_commands", "exec"), self.env)
        self.rec_id = "r0123456789abcde"
        self.metadata = {"video_id": self.rec_id, "platform": "recording", "title": "Lesson",
                         "channel": "example.com", "duration": 60}

    def tearDown(self):
        self.conn.close()

    def test_stop_saves_metadata_without_enqueuing_or_starting_worker(self):
        with patch.object(recording, "finish", return_value={"duration": 60, "bytes": 123}), \
             patch.object(recording, "metadata", return_value=self.metadata):
            result = self.env["cmd_rec_stop"](self.conn, {"rec_id": self.rec_id})
        self.assertNotIn("job_id", result)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM submissions").fetchone()[0], 0)
        self.env["ensure_worker"].assert_not_called()
        self.assertEqual(self.conn.execute("SELECT title FROM videos").fetchone()[0], "Lesson")

    def test_saved_audio_is_listed_without_a_transcript(self):
        store.upsert_video(self.conn, self.metadata)
        audio = Mock()
        audio.is_file.return_value = True
        with patch.object(recording, "audio_path", return_value=audio), \
             patch.object(store, "TRANSCRIPT_DIR", ROOT / "tests" / "no-transcripts"):
            rows = self.env["build_rows"](self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], "saved")
        self.assertFalse(rows[0]["has_transcript"])

    def test_manual_transcribe_uses_saved_audio_not_original_site_url(self):
        store.upsert_video(self.conn, self.metadata)
        with patch.object(transcribe, "video_url", return_value="https://example.com/lesson"):
            result = self.env["cmd_transcribe"](self.conn, {"url": self.rec_id})
        job = jobs.get(self.conn, result["job_id"])
        self.assertEqual(job["url"], f"recording:{self.rec_id}")
        self.assertEqual(job["entry_point"], "record")
        self.env["ensure_worker"].assert_called_once()

    def test_finishing_import_registers_saved_recording_without_starting_whisper(self):
        with patch.object(recording, "finish_import"), \
             patch.object(recording, "metadata", return_value=self.metadata):
            result = self.env["cmd_rec_import_finish"](self.conn, {"rec_id": self.rec_id})
        self.assertEqual(result["video_id"], self.rec_id)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        self.env["ensure_worker"].assert_not_called()

    def test_lost_import_success_reply_cannot_delete_finished_audio(self):
        with patch.object(recording, "meta", return_value={"finished_at": "now"}), \
             patch.object(recording, "remove") as remove:
            self.env["cmd_rec_import_abort"](self.conn, {"rec_id": self.rec_id})
            remove.assert_not_called()


if __name__ == "__main__":
    unittest.main()
