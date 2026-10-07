"""
jobs.py — the queue between the extension and the pipeline.

A transcription can take a quarter of an hour and an analysis several
minutes; Chrome keeps a native host alive only while the side panel is open.
So work lives in the db, not in a process: the host (host.py) enqueues and
reads, the worker (worker.py) claims and runs. Either can die and the other
still sees the truth.

These two tables are plumbing for the extension, not part of the log the
Takeout analysis runs on, so they are defined here rather than in
schema.sql. What a job *produced* still lands in submissions / analyses via
store.py.
"""

import sqlite3
import time
from contextlib import contextmanager

import store

HEARTBEAT_EVERY = 5     # seconds between worker heartbeats
HEARTBEAT_STALE = 20    # no heartbeat for this long = worker is gone
BUSY_TIMEOUT = 30       # seconds to wait on a locked db before giving up

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY,
    kind             TEXT NOT NULL,          -- 'transcribe' | 'analyse' | 'follow-up'
    video_id         TEXT NOT NULL,
    url              TEXT NOT NULL,
    entry_point      TEXT,                   -- 'page' | 'link'
    status           TEXT NOT NULL,          -- 'queued' | 'running' | 'done' | 'failed' | 'cancelled'
    stage            TEXT,                   -- 'metadata' | 'captions' | 'download' | 'whisper' | 'saving' | 'claude'
    stage_started_at REAL,                   -- epoch seconds; the panel counts from it
    -- known once metadata is fetched, before any videos row exists
    title            TEXT,
    channel          TEXT,
    duration         INTEGER,
    created_at       TEXT NOT NULL,          -- UTC ISO-8601, same format as store.now()
    started_at       REAL,                   -- epoch seconds
    finished_at      TEXT,
    error            TEXT,
    submission_id    INTEGER                 -- the submissions row this job produced
);

-- At most one worker. The row is the lock; the heartbeat says it is alive.
CREATE TABLE IF NOT EXISTS worker (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    pid          INTEGER,
    started_at   TEXT,
    heartbeat_at REAL                        -- epoch seconds
);
"""

# Added after the table first shipped; (column, type). Same pattern as
# store.migrate(): CREATE TABLE IF NOT EXISTS never adds columns.
COLUMNS = [
    ("prompt", "TEXT"),             # analyse: prompt name, or 'custom' for an ask
    ("question", "TEXT"),           # an ask or a follow-up
    ("session_id", "TEXT"),         # follow-up: the thread it continues
    ("analysis_id", "INTEGER"),     # the analyses row this job produced
    ("cancel_requested", "INTEGER"),
    ("notified_at", "TEXT"),        # background.js has shown its notification
    ("dismissed_at", "TEXT"),       # a failed analysis the user cleared away
]

# After the columns, for the reason store.py gives.
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_video  ON jobs(video_id)",
]

ACTIVE = ("queued", "running")

# Two lanes in the worker, so a fact-check is not stuck behind a quarter-hour
# whisper run: they compete for different things (CPU vs network).
TRANSCRIBE_KINDS = ("transcribe",)
ANALYSE_KINDS = ("analyse", "follow-up")


def connect() -> sqlite3.Connection:
    """
    Autocommit, with explicit transactions where it matters. The host reads
    while the worker writes, so wait on a lock rather than failing on it.
    """
    conn = sqlite3.connect(store.DB_PATH, timeout=BUSY_TIMEOUT, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def init(conn) -> None:
    store.init()
    conn.executescript(SCHEMA)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    for column, coltype in COLUMNS:
        if column not in cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {coltype}")
    for stmt in INDEXES:
        conn.execute(stmt)


@contextmanager
def immediate(conn):
    """A write lock taken up front, so check-then-act can't interleave."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _in(kinds) -> str:
    return "(" + ",".join("?" * len(kinds)) + ")"


# --- queue -----------------------------------------------------------------

def active_for(conn, video_id: str, kinds=TRANSCRIBE_KINDS):
    return conn.execute(
        f"SELECT * FROM jobs WHERE video_id = ? AND kind IN {_in(kinds)} "
        "AND status IN ('queued', 'running') ORDER BY id LIMIT 1",
        (video_id, *kinds),
    ).fetchone()


def enqueue(conn, video_id: str, url: str, entry_point: str) -> int:
    """A transcription. A video already queued or running is not queued twice."""
    with immediate(conn):
        existing = active_for(conn, video_id)
        if existing:
            return existing["id"]
        cur = conn.execute(
            """INSERT INTO jobs (kind, video_id, url, entry_point, status, created_at)
               VALUES ('transcribe', ?, ?, ?, 'queued', ?)""",
            (video_id, url, entry_point, store.now()),
        )
        return cur.lastrowid


def enqueue_analysis(conn, video_id: str, *, prompt: str | None = None,
                     question: str | None = None, session_id: str | None = None) -> int:
    """
    An analysis (a prompt name, or prompt='custom' with a question) or, given
    a session_id, a follow-up in that thread. The same prompt on the same
    video is not queued twice while one is pending; follow-ups always queue.
    """
    kind = "follow-up" if session_id else "analyse"
    with immediate(conn):
        if kind == "analyse":
            existing = conn.execute(
                "SELECT id FROM jobs WHERE video_id = ? AND kind = 'analyse' "
                "AND prompt = ? AND COALESCE(question, '') = ? "
                "AND status IN ('queued', 'running')",
                (video_id, prompt, question or ""),
            ).fetchone()
            if existing:
                return existing["id"]
        cur = conn.execute(
            """INSERT INTO jobs (kind, video_id, url, status, created_at,
                                 prompt, question, session_id)
               VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)""",
            (kind, video_id, f"https://www.youtube.com/watch?v={video_id}", store.now(),
             prompt, question, session_id),
        )
        return cur.lastrowid


def active(conn, kinds=None):
    if kinds is None:
        return conn.execute(
            "SELECT * FROM jobs WHERE status IN ('queued', 'running') ORDER BY id"
        ).fetchall()
    return conn.execute(
        f"SELECT * FROM jobs WHERE status IN ('queued', 'running') AND kind IN {_in(kinds)} "
        "ORDER BY id",
        kinds,
    ).fetchall()


def get(conn, job_id: int):
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def claim(conn, kinds=TRANSCRIBE_KINDS):
    """Oldest queued job of these kinds, marked running. None when there is none."""
    with immediate(conn):
        job = conn.execute(
            f"SELECT * FROM jobs WHERE status = 'queued' AND kind IN {_in(kinds)} "
            "ORDER BY id LIMIT 1",
            kinds,
        ).fetchone()
        if job is None:
            return None
        conn.execute(
            "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ?",
            (time.time(), job["id"]),
        )
    return job


def cancel(conn, job_id: int) -> None:
    """Queued: dropped now. Running: flagged, and the worker stops it."""
    with immediate(conn):
        conn.execute(
            "UPDATE jobs SET status = 'cancelled', finished_at = ? "
            "WHERE id = ? AND status = 'queued'",
            (store.now(), job_id),
        )
        conn.execute(
            "UPDATE jobs SET cancel_requested = 1 WHERE id = ? AND status = 'running'",
            (job_id,),
        )


def cancel_requested(conn, job_id: int) -> bool:
    row = conn.execute("SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return bool(row and row["cancel_requested"])


def dismiss(conn, job_id: int) -> None:
    conn.execute("UPDATE jobs SET dismissed_at = ? WHERE id = ?", (store.now(), job_id))


def set_stage(conn, job_id: int, stage: str) -> None:
    conn.execute(
        "UPDATE jobs SET stage = ?, stage_started_at = ? WHERE id = ?",
        (stage, time.time(), job_id),
    )


def set_meta(conn, job_id: int, meta: dict) -> None:
    conn.execute(
        "UPDATE jobs SET title = ?, channel = ?, duration = ? WHERE id = ?",
        (meta.get("title"), meta.get("channel"), meta.get("duration"), job_id),
    )


def finish(conn, job_id: int, submission_id: int | None = None,
           analysis_id: int | None = None) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'done', finished_at = ?, submission_id = ?, "
        "analysis_id = ? WHERE id = ?",
        (store.now(), submission_id, analysis_id, job_id),
    )


def fail(conn, job_id: int, error: str, submission_id: int | None = None) -> None:
    """The stage is left as it was, so the panel can say what failed."""
    conn.execute(
        "UPDATE jobs SET status = 'failed', finished_at = ?, error = ?, submission_id = ? "
        "WHERE id = ?",
        (store.now(), error, submission_id, job_id),
    )


def mark_cancelled(conn, job_id: int) -> None:
    conn.execute("UPDATE jobs SET status = 'cancelled', finished_at = ? WHERE id = ?",
                 (store.now(), job_id))


def last_for(conn, video_id: str, kinds=TRANSCRIBE_KINDS):
    """The newest job of these kinds for a video, finished or not."""
    return conn.execute(
        f"SELECT * FROM jobs WHERE video_id = ? AND kind IN {_in(kinds)} "
        "ORDER BY id DESC LIMIT 1",
        (video_id, *kinds),
    ).fetchone()


# --- worker lock -----------------------------------------------------------

def worker_row(conn):
    return conn.execute("SELECT * FROM worker WHERE id = 1").fetchone()


def worker_alive(conn) -> bool:
    row = worker_row(conn)
    return bool(row and row["pid"] and row["heartbeat_at"]
                and time.time() - row["heartbeat_at"] < HEARTBEAT_STALE)


def claim_worker(conn, pid: int) -> bool:
    """Take the single worker slot. False if a live worker already holds it."""
    with immediate(conn):
        if worker_alive(conn):
            return False
        conn.execute(
            """INSERT INTO worker (id, pid, started_at, heartbeat_at) VALUES (1, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET pid = excluded.pid,
                   started_at = excluded.started_at, heartbeat_at = excluded.heartbeat_at""",
            (pid, store.now(), time.time()),
        )
    return True


def heartbeat(conn, pid: int) -> None:
    conn.execute("UPDATE worker SET heartbeat_at = ? WHERE id = 1 AND pid = ?",
                 (time.time(), pid))


def release_worker(conn, pid: int) -> None:
    conn.execute("UPDATE worker SET pid = NULL, heartbeat_at = NULL WHERE id = 1 AND pid = ?",
                 (pid,))
