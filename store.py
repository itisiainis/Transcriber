"""
store.py — SQLite wrapper for the transcriber.

The db holds structured rows you query; transcripts and analyses are
files on disk with their path recorded. Keeps the tables small and the
text openable in any editor.

An analysis is no longer one file per video: it is a folder per video,
holding the original answer and every follow-up in order, because a
follow-up belongs to the analysis it came from.

transcribe.py knows nothing about storage — the caller wires them together.
"""

import json
import re
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path("transcriber.db")
SCHEMA_PATH = Path("schema.sql")
TRANSCRIPT_DIR = Path("transcripts")
ANALYSIS_DIR = Path("analyses")

# Default for the retention_days setting (settings.py): soft-delete anything
# older than this many days. 0 = never.
RETENTION_DAYS = 0

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_submissions_video ON submissions(video_id)",
    "CREATE INDEX IF NOT EXISTS idx_submissions_time  ON submissions(submitted_at)",
    "CREATE INDEX IF NOT EXISTS idx_analyses_video    ON analyses(video_id)",
    "CREATE INDEX IF NOT EXISTS idx_analyses_parent   ON analyses(parent_id)",
]


def now() -> str:
    """UTC, with offset, so the local hour can be derived later."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(path: Path = DB_PATH) -> None:
    """Create the tables if they aren't there. Safe to call every run."""
    with connect(path) as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        migrate(conn)


def migrate(conn) -> None:
    """
    CREATE TABLE IF NOT EXISTS won't add columns to a table that already
    exists, so new columns are added here. Each entry is (table, column, type).
    """
    wanted = [
        ("analyses", "duration_s", "REAL"),
        # The Claude Code session this answer came from. A follow-up resumes
        # it instead of re-sending the transcript.
        ("analyses", "session_id", "TEXT"),
        # Which analysis this one continues. NULL = it started the thread.
        ("analyses", "parent_id", "INTEGER"),
        # The question asked, for a follow-up or an --ask run.
        ("analyses", "question", "TEXT"),
        ("analyses", "seq", "INTEGER"),
        # whisper-only; NULL on the captions path
        ("submissions", "confidence", "REAL"),
        ("submissions", "low_conf_count", "INTEGER"),
        ("submissions", "lang_p", "REAL"),
        ("submissions", "fallbacks", "INTEGER"),
        # Soft delete: the UI hides the row and its files are removed, but the
        # row stays for the before/after comparison. See soft_delete_video().
        ("videos", "deleted_at", "TEXT"),
        ("analyses", "deleted_at", "TEXT"),
    ]
    for table, column, coltype in wanted:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

    # Only now — an index on a column added just above cannot be created by
    # schema.sql, which runs before any of this.
    for stmt in INDEXES:
        conn.execute(stmt)

    _relocate_flat_analyses(conn)


def _relocate_flat_analyses(conn) -> None:
    """
    Analyses used to live at analyses/<video_id>.md, one per video. Move any
    that are still there into the per-video folder so nothing is orphaned.
    """
    rows = conn.execute(
        "SELECT id, video_id, prompt_template, analysis_path FROM analyses "
        "WHERE analysis_path IS NOT NULL"
    ).fetchall()
    for row in rows:
        old = Path(row["analysis_path"])
        if old.parent != ANALYSIS_DIR or not old.exists():
            continue    # already in a folder, or the file is gone
        new = analysis_path(row["video_id"], 1, row["prompt_template"] or "analysis")
        new.parent.mkdir(parents=True, exist_ok=True)
        old.replace(new)
        conn.execute(
            "UPDATE analyses SET analysis_path = ?, seq = COALESCE(seq, 1) WHERE id = ?",
            (str(new), row["id"]),
        )


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "analysis").lower()).strip("-")[:40] or "analysis"


def analysis_path(video_id: str, seq: int, name: str) -> Path:
    return ANALYSIS_DIR / video_id / f"{seq:03d}-{slug(name)}.md"


# --- writes ----------------------------------------------------------------

def upsert_video(conn, result: dict) -> None:
    """
    One row per video, ever. Titles can change, so refresh them; first_seen_at
    is set once and left alone. Submitting a soft-deleted video again brings
    it back: you asked for it, so it should show.
    """
    conn.execute(
        """
        INSERT INTO videos (video_id, title, channel, duration, first_seen_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(video_id) DO UPDATE SET
            title = excluded.title,
            channel = excluded.channel,
            duration = excluded.duration,
            deleted_at = NULL
        """,
        (result["video_id"], result["title"], result["channel"],
         result["duration"], now()),
    )


def save_transcript(result: dict) -> Path:
    """Overwrites any previous transcript for this video."""
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    path = TRANSCRIPT_DIR / f"{result['video_id']}.json"
    payload = {
        "video_id": result["video_id"],
        "title": result["title"],
        "channel": result["channel"],
        "duration": result["duration"],
        "source": result["source"],
        "language": result["language"],
        "segments": result["segments"],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def record_submission(conn, result: dict, entry_point: str = "link",
                      transcript_path: Path | None = None) -> int:
    """One row per submission event. Returns its id."""
    t = result.get("timings", {})
    ca = result.get("caption_attempt", {}) or {}
    q = ca.get("quality") or {}

    cur = conn.execute(
        """
        INSERT INTO submissions (
            video_id, submitted_at, entry_point, status, error,
            source, language, transcript_path,
            caption_track, coverage, density, words_per_min, caption_reasons,
            confidence, low_conf_count, lang_p, fallbacks,
            t_metadata, t_captions, t_download, t_whisper, t_total, realtime_factor
        ) VALUES (?,?,?,?,?, ?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?,?,?,?,?)
        """,
        (
            result["video_id"], now(), entry_point, "done", None,
            result.get("source"), result.get("language"),
            str(transcript_path) if transcript_path else None,
            ca.get("track"), q.get("coverage"), q.get("density"),
            q.get("words_per_min"), json.dumps(ca.get("reasons", []), ensure_ascii=False),
            result.get("confidence"), result.get("low_conf_count"),
            result.get("lang_p"), result.get("fallbacks"),
            t.get("metadata"), t.get("captions"), t.get("download"),
            t.get("whisper"), t.get("total"), t.get("realtime_factor"),
        ),
    )
    return cur.lastrowid


def record_failure(conn, video_id: str, error: str, entry_point: str = "link") -> int:
    """Failures are data too — they tell you how often YouTube blocks you."""
    conn.execute(
        "INSERT OR IGNORE INTO videos (video_id, first_seen_at) VALUES (?, ?)",
        (video_id, now()),
    )
    # A retry of a deleted video is a new submission: show it again.
    conn.execute("UPDATE videos SET deleted_at = NULL WHERE video_id = ?", (video_id,))
    cur = conn.execute(
        """INSERT INTO submissions (video_id, submitted_at, entry_point, status, error)
           VALUES (?, ?, ?, 'failed', ?)""",
        (video_id, now(), entry_point, error),
    )
    return cur.lastrowid


def next_seq(conn, video_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS n FROM analyses WHERE video_id = ?",
        (video_id,),
    ).fetchone()
    return row["n"] + 1


def save_analysis(conn, video_id: str, text: str, prompt_template: str,
                  duration_s: float | None = None, session_id: str | None = None,
                  parent_id: int | None = None, question: str | None = None) -> tuple[int, Path]:
    """
    Writes analyses/<video_id>/<NNN>-<name>.md and records the row.
    Returns (analysis_id, path).
    """
    seq = next_seq(conn, video_id)
    path = analysis_path(video_id, seq, prompt_template)
    path.parent.mkdir(parents=True, exist_ok=True)

    header = [f"# {prompt_template}", f"<!-- {now()} -->"]
    if question:
        header.append(f"\n**Q:** {question}\n")
    path.write_text("\n".join(header) + "\n\n" + text, encoding="utf-8")

    cur = conn.execute(
        """INSERT INTO analyses (video_id, created_at, prompt_template, analysis_path,
                                 duration_s, session_id, parent_id, question, seq)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (video_id, now(), prompt_template, str(path), duration_s,
         session_id, parent_id, question, seq),
    )
    return cur.lastrowid, path


def mark_opened(conn, analysis_id: int) -> None:
    """Called when you actually read the analysis — not when it's produced."""
    conn.execute(
        "UPDATE analyses SET opened_at = ? WHERE id = ? AND opened_at IS NULL",
        (now(), analysis_id),
    )


def set_verdict(conn, analysis_id: int, verdict: str) -> None:
    """'watched' | 'skipped' | 'partial' — the one thing Takeout can't infer."""
    conn.execute("UPDATE analyses SET verdict = ? WHERE id = ?", (verdict, analysis_id))


# --- deletion --------------------------------------------------------------
#
# Delete files, not rows. submissions and analyses are the behavioural record
# this project exists to build; a deleted row punches a hole in the
# before/after comparison, and the videos you delete are mostly the ones you
# skipped — the interesting half of the data. So a delete removes the
# transcript/analysis files and sets deleted_at; the UI's reads skip those
# rows, and every metric query still sees them. purge() is the one exception.

def soft_delete_video(conn, video_id: str) -> dict:
    """
    Hide a video: set deleted_at, remove transcripts/<id>.json and the whole
    analyses/<id>/ folder. Its submissions and analyses rows stay as they are.
    """
    conn.execute(
        "UPDATE videos SET deleted_at = COALESCE(deleted_at, ?) WHERE video_id = ?",
        (now(), video_id),
    )
    removed = []
    transcript = TRANSCRIPT_DIR / f"{video_id}.json"
    if transcript.exists():
        transcript.unlink()
        removed.append(str(transcript))
    folder = ANALYSIS_DIR / video_id
    if folder.is_dir():
        removed += [str(p) for p in sorted(folder.rglob("*")) if p.is_file()]
        shutil.rmtree(folder)
    return {"video_id": video_id, "removed_files": removed}


def _descendants(conn, analysis_id: int) -> list[int]:
    """Follow-ups hanging off this analysis, however deep the chain."""
    return [r["id"] for r in conn.execute(
        """WITH RECURSIVE d(id) AS (
               SELECT id FROM analyses WHERE parent_id = ?
               UNION SELECT a.id FROM analyses a JOIN d ON a.parent_id = d.id)
           SELECT id FROM d ORDER BY id""",
        (analysis_id,),
    )]


def soft_delete_analysis(conn, analysis_id: int, dry_run: bool = False) -> dict:
    """
    Hide one analysis: set deleted_at and remove its .md. Its follow-ups
    would be left answering a question nobody can see, so they go too, and
    the return value lists them — call with dry_run=True first so the UI
    can warn before the click ("this also deletes 2 follow-ups").
    """
    followups = _descendants(conn, analysis_id)
    result = {"analysis_id": analysis_id, "followups": followups, "removed_files": []}
    if dry_run:
        return result
    ids = [analysis_id, *followups]
    marks = ",".join("?" * len(ids))
    for r in conn.execute(f"SELECT analysis_path FROM analyses WHERE id IN ({marks})", ids):
        p = Path(r["analysis_path"]) if r["analysis_path"] else None
        if p and p.exists():
            p.unlink()
            result["removed_files"].append(str(p))
    conn.execute(
        f"UPDATE analyses SET deleted_at = COALESCE(deleted_at, ?) WHERE id IN ({marks})",
        (now(), *ids),
    )
    return result


def purge(conn, video_id: str) -> dict:
    """
    Hard delete: files and every row, as if the video never existed. Not wired
    to anything on purpose — it destroys data the metrics depend on. Only for
    the rare "forget this entirely" case, run by hand.
    """
    result = soft_delete_video(conn, video_id)
    counts = {}
    for table in ("analyses", "submissions", "jobs", "videos"):
        try:
            counts[table] = conn.execute(
                f"DELETE FROM {table} WHERE video_id = ?", (video_id,)).rowcount
        except sqlite3.OperationalError:
            counts[table] = 0       # jobs only exists once the panel has run
    result["deleted_rows"] = counts
    return result


def apply_retention(conn) -> list[str]:
    """
    Soft-delete videos whose last activity is older than the retention_days
    setting. 0 (the default) means never. Activity is the latest submission,
    so a video you re-ran last week is not "old" because you first saw it
    a year ago. Returns the ids it deleted.
    """
    import settings     # settings imports store, so not at the top
    days = settings.get("retention_days")
    if not days:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    old = [r["video_id"] for r in conn.execute(
        """SELECT v.video_id FROM videos v
           WHERE v.deleted_at IS NULL
             AND COALESCE((SELECT MAX(submitted_at) FROM submissions s
                           WHERE s.video_id = v.video_id), v.first_seen_at) < ?""",
        (cutoff,),
    )]
    for vid in old:
        soft_delete_video(conn, vid)
    return old


# --- reads -----------------------------------------------------------------
#
# The reads below serve the UI and the CLI, so they skip soft-deleted rows.
# Metric queries (the notebook, the Takeout join) read the tables directly
# and must not filter: the deleted videos are data too.

def get_transcript(video_id: str) -> dict | None:
    path = TRANSCRIPT_DIR / f"{video_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def last_video_id(conn) -> str | None:
    """The most recently submitted video — so you can say 'last' instead of an id."""
    row = conn.execute(
        "SELECT s.video_id FROM submissions s JOIN videos v USING (video_id) "
        "WHERE s.status = 'done' AND v.deleted_at IS NULL "
        "ORDER BY s.submitted_at DESC LIMIT 1"
    ).fetchone()
    return row["video_id"] if row else None


def latest_analysis(conn, video_id: str):
    """The newest analysis for this video that carries a resumable session."""
    return conn.execute(
        "SELECT a.* FROM analyses a JOIN videos v USING (video_id) "
        "WHERE a.video_id = ? AND a.session_id IS NOT NULL "
        "AND a.deleted_at IS NULL AND v.deleted_at IS NULL "
        "ORDER BY a.seq DESC LIMIT 1",
        (video_id,),
    ).fetchone()


def thread(conn, video_id: str):
    """Every analysis for a video, oldest first — the thread as stored."""
    return conn.execute(
        "SELECT a.* FROM analyses a JOIN videos v USING (video_id) "
        "WHERE a.video_id = ? AND a.deleted_at IS NULL AND v.deleted_at IS NULL "
        "ORDER BY a.seq",
        (video_id,),
    ).fetchall()


def recent(conn, limit: int = 20):
    return conn.execute(
        """
        SELECT s.submitted_at, s.entry_point, s.status, s.source,
               s.t_total, v.title, v.channel, v.duration
        FROM submissions s JOIN videos v USING (video_id)
        WHERE v.deleted_at IS NULL
        ORDER BY s.submitted_at DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()


if __name__ == "__main__":
    init()
    print(f"initialised {DB_PATH}")