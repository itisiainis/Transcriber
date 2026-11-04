import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import analyze
import jobs
import settings
import store


def events(session="codex-thread", text="The answer"):
    return "\n".join(json.dumps(e) for e in [
        {"type": "thread.started", "thread_id": session},
        {"type": "item.completed", "item": {"type": "web_search", "text": "not the answer"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
        {"type": "turn.completed"},
    ])


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.cwd = Path.cwd()
        os.chdir(self.directory.name)
        self.schema = patch.object(store, "SCHEMA_PATH", ROOT / "schema.sql")
        self.schema.start()
        # sqlite's context manager commits but does not close connections.
        # Track the production helpers' connections so Windows can remove the DB.
        self.connections = []
        connect = store.connect
        def tracked_connect(*args, **kwargs):
            conn = connect(*args, **kwargs)
            self.connections.append(conn)
            return conn
        self.connect_patch = patch.object(store, "connect", side_effect=tracked_connect)
        self.connect_patch.start()
        store.init()
        self.conn = jobs.connect()
        jobs.init(self.conn)
        self.conn.execute("INSERT INTO videos (video_id, first_seen_at) VALUES ('video', ?)", (store.now(),))
        self.transcript = {"title": "Title", "channel": "Channel", "duration": 20,
                           "source": "whisper", "segments": [{"start": 0, "text": "Transcript text"}]}

    def tearDown(self):
        self.conn.close()
        for conn in self.connections:
            conn.close()
        self.connect_patch.stop()
        self.schema.stop()
        os.chdir(self.cwd)
        self.directory.cleanup()

    def test_setting_validates_persists_and_resets(self):
        self.assertEqual(settings.get("analysis_provider"), "claude")
        settings.set("analysis_provider", "chatgpt")
        self.assertEqual(settings.get("analysis_provider"), "chatgpt")
        with self.assertRaises(ValueError):
            settings.set("analysis_provider", "unknown")
        self.assertEqual(settings.get("analysis_provider"), "chatgpt")
        settings.reset("analysis_provider")
        self.assertEqual(settings.get("analysis_provider"), "claude")

    def test_migration_preserves_legacy_claude_rows(self):
        old = store.connect(Path("old.db"))
        try:
            old.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
            old.execute("ALTER TABLE analyses DROP COLUMN provider")
            old.execute("INSERT INTO videos (video_id, first_seen_at) VALUES ('legacy', 'date')")
            old.execute("INSERT INTO analyses (video_id, created_at, session_id) VALUES ('legacy', 'date', 'claude-session')")
            store.migrate(old)
            store.migrate(old)  # idempotent on subsequent host connections
            row = old.execute("SELECT * FROM analyses").fetchone()
            self.assertEqual(row["provider"], "claude")
            self.assertEqual(row["session_id"], "claude-session")
        finally:
            old.close()

    @patch.object(analyze, "codex_path", return_value="codex.exe")
    def test_codex_json_parsing_stdin_and_resume(self, _path):
        with patch.object(analyze, "_communicate", return_value=events()) as run:
            self.assertEqual(analyze.run_chatgpt("input"), ("The answer", "codex-thread"))
            cmd, payload = run.call_args.args
            self.assertEqual(payload, "input")
            self.assertEqual(cmd[-1], "-")
            self.assertIn('forced_login_method="chatgpt"', cmd)
            self.assertIn('sandbox_mode="read-only"', cmd)
            self.assertIn('features.shell_tool=false', cmd)
            self.assertNotIn("--ephemeral", cmd)
            analyze.run_chatgpt("question", "existing-thread")
            self.assertEqual(run.call_args.args[0][-3:], ["resume", "existing-thread", "-"])

    @patch.object(analyze, "codex_path", return_value="codex.exe")
    def test_codex_failure_or_incomplete_output_is_not_saved_as_answer(self, _path):
        cases = ["", events().replace('"turn.completed"', '"turn.started"'),
                 json.dumps({"type": "turn.failed", "error": {"message": "Usage limit reached"}})]
        for output in cases:
            with self.subTest(output=output), patch.object(analyze, "_communicate", return_value=output):
                with self.assertRaises(RuntimeError):
                    analyze.run_chatgpt("input")

    def test_new_chatgpt_analysis_and_followup_keep_provider_after_switch(self):
        settings.set("analysis_provider", "chatgpt")
        with patch.object(store, "get_transcript", return_value=self.transcript), \
             patch.object(analyze, "run_chatgpt", return_value=("First answer", "chatgpt-session")) as run:
            root_id, _ = analyze.analyze("video", "Summarize", "summary")
            self.assertIn("Transcript text", run.call_args.args[0])
        settings.set("analysis_provider", "claude")
        with patch.object(analyze, "run_chatgpt", return_value=("Follow-up answer", "chatgpt-session")) as run, \
             patch.object(analyze, "resume_claude") as claude:
            child_id, _ = analyze.follow_up("video", "Explain", "chatgpt-session")
            run.assert_called_once_with("Explain", "chatgpt-session")
            claude.assert_not_called()
        child = self.conn.execute("SELECT * FROM analyses WHERE id = ?", (child_id,)).fetchone()
        self.assertEqual(child["provider"], "chatgpt")
        self.assertEqual(child["parent_id"], root_id)

    def test_claude_followup_stays_claude_when_chatgpt_selected(self):
        with patch.object(store, "get_transcript", return_value=self.transcript), \
             patch.object(analyze, "run_claude", return_value="Claude answer"):
            root_id, _ = analyze.analyze("video", "Summarize", "summary", provider="claude")
        root = self.conn.execute("SELECT * FROM analyses WHERE id = ?", (root_id,)).fetchone()
        settings.set("analysis_provider", "chatgpt")
        with patch.object(analyze, "resume_claude", return_value="Claude follow-up") as run, \
             patch.object(analyze, "run_chatgpt") as chatgpt:
            analyze.follow_up("video", "Explain", root["session_id"])
            run.assert_called_once_with(root["session_id"], "Explain")
            chatgpt.assert_not_called()

    def test_queue_snapshots_provider_and_distinguishes_identical_prompts(self):
        first = jobs.enqueue_analysis(self.conn, "video", prompt="summary")
        settings.set("analysis_provider", "chatgpt")
        second = jobs.enqueue_analysis(self.conn, "video", prompt="summary")
        self.assertNotEqual(first, second)
        self.assertEqual(jobs.get(self.conn, first)["provider"], "claude")
        self.assertEqual(jobs.get(self.conn, second)["provider"], "chatgpt")
        self.assertEqual(jobs.enqueue_analysis(self.conn, "video", prompt="summary"), second)
        root, _ = store.save_analysis(self.conn, "video", "answer", "summary",
                                      session_id="claude-thread", provider="claude")
        follow = jobs.enqueue_analysis(self.conn, "video", prompt="follow-up", question="Explain",
                                        session_id="claude-thread")
        self.assertEqual(jobs.get(self.conn, follow)["provider"], "claude")


if __name__ == "__main__":
    unittest.main()
