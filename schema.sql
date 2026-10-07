-- transcriber schema
--
-- Three tables, each holding a different kind of thing:
--   videos       — facts about a video, true regardless of what you do
--   submissions  — one row per time you sent a video to the tool
--   analyses     — one row per Claude run over a transcript, including
--                  follow-ups, which continue the same session
--
-- Transcript and analysis text live as files; the db holds the path.
-- Timestamps are UTC ISO-8601 with offset, so the local hour can be
-- derived later for time-of-day metrics.

CREATE TABLE IF NOT EXISTS videos (
    video_id      TEXT PRIMARY KEY,          -- canonical 11-char id, the join key
    title         TEXT,
    channel       TEXT,
    duration      INTEGER,                   -- seconds
    first_seen_at TEXT NOT NULL,
    deleted_at    TEXT                       -- soft delete: files gone, row kept (see store.py)
);

CREATE TABLE IF NOT EXISTS submissions (
    id              INTEGER PRIMARY KEY,
    video_id        TEXT NOT NULL REFERENCES videos(video_id),
    submitted_at    TEXT NOT NULL,           -- UTC; direction in time makes the Takeout join valid
    entry_point     TEXT,                    -- 'page' | 'link'
    status          TEXT NOT NULL,           -- 'done' | 'failed'
    error           TEXT,

    -- what the transcript ended up being
    source          TEXT,                    -- 'captions-manual' | 'captions-auto' | 'whisper-<model>'
    language        TEXT,
    transcript_path TEXT,

    -- the caption attempt, recorded even when it was rejected
    caption_track   TEXT,                    -- 'manual' | 'auto' | NULL
    coverage        REAL,
    density         REAL,
    words_per_min   REAL,
    caption_reasons TEXT,                    -- JSON array; empty when accepted

    -- whisper only; NULL whenever captions were used
    confidence      REAL,                    -- mean token probability
    low_conf_count  INTEGER,                 -- segments under LOW_CONF
    lang_p          REAL,                    -- language-detection probability
    fallbacks       INTEGER,                 -- temperature fallbacks; >0 = it struggled

    -- timings, per stage
    t_metadata      REAL,
    t_captions      REAL,
    t_download      REAL,
    t_whisper       REAL,
    t_total         REAL,
    realtime_factor REAL
);

CREATE TABLE IF NOT EXISTS analyses (
    id              INTEGER PRIMARY KEY,
    video_id        TEXT NOT NULL REFERENCES videos(video_id),
    created_at      TEXT NOT NULL,
    prompt_template TEXT,                    -- 'factcheck' | 'custom' | 'follow-up' | …
    analysis_path   TEXT,                    -- analyses/<video_id>/<NNN>-<name>.md
    seq             INTEGER,                 -- position in this video's thread, from 1
    duration_s      REAL,                    -- how long Claude took to answer
    session_id      TEXT,                    -- the Claude Code session, resumable
    parent_id       INTEGER REFERENCES analyses(id),  -- NULL = started the thread
    question        TEXT,                    -- what was asked, for --ask and follow-ups
    opened_at       TEXT,                    -- NULL until actually read; the filter-vs-hoard signal
    verdict         TEXT,                    -- 'watched' | 'skipped' | 'partial' | NULL
    deleted_at      TEXT                     -- soft delete: the .md is gone, the row is kept
);

-- Tunable settings (settings.py). A key absent here means "use the module
-- constant" — an empty table behaves exactly like the old hard-coded values.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,                         -- JSON, so numbers stay numbers
    updated_at TEXT
);


-- Indexes deliberately live in store.migrate(), not here: this script runs
-- BEFORE the columns migrate() adds exist, so an index on a newly added
-- column (parent_id) fails on any database created earlier.