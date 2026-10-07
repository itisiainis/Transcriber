"""
host.py — Chrome native messaging host for the side panel.

Chrome starts this through host\\host.bat when the panel connects, and
closes stdin when the panel closes. Messages in both directions are a
4-byte little-endian length, then that many bytes of UTF-8 JSON.

stdout IS the protocol. A stray print(), or a library writing to fd 1,
corrupts the stream with no error on either side. So before anything else
is imported, fd 1 is duplicated for the protocol and then pointed at the
log file: whatever else writes to "stdout" ends up in host.log.

The host never does slow work itself — it enqueues jobs (jobs.py) and makes
sure a worker (worker.py) is running. Every request answers in milliseconds.
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)      # every path in the project is relative to here

LOG_PATH = ROOT / "host.log"
_log_file = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
PROTO_IN = os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0)
PROTO_OUT = os.fdopen(os.dup(sys.stdout.fileno()), "wb", buffering=0)
os.dup2(_log_file.fileno(), sys.stdout.fileno())
os.dup2(_log_file.fileno(), sys.stderr.fileno())
sys.stdout = sys.stderr = _log_file

if os.name == "nt":
    import msvcrt
    msvcrt.setmode(PROTO_IN.fileno(), os.O_BINARY)
    msvcrt.setmode(PROTO_OUT.fileno(), os.O_BINARY)

# --- only now is it safe to import the rest --------------------------------

import json
import logging
import re
import struct
import subprocess
import time
from contextlib import closing

import analyze
import jobs
import settings
import store
import transcribe as tx

log = logging.getLogger("host")

# Fallbacks until there is history to average over. whisper at ~0.25x
# realtime is the figure measured on this machine (see CLAUDE.md).
DEFAULT_WHISPER_RATIO = 0.25    # whisper seconds per second of video
DEFAULT_DOWNLOAD_RATIO = 0.03   # download seconds per second of video
STAGE_GUESS = {"metadata": 3, "captions": 3, "saving": 2}

SPAWN_GRACE = 15                # seconds to let a new worker write its first heartbeat
SEGMENTS_PER_MESSAGE = 800      # Chrome caps host->extension messages at 1 MB

FAILED_AT = {
    "metadata": "Couldn't fetch video info",
    "captions": "Captions check failed",
    "download": "Download failed",
    "whisper": "Whisper failed",
    "saving": "Saving failed",
}

_last_spawn = 0.0


# --- protocol --------------------------------------------------------------

def read_message():
    head = PROTO_IN.read(4)
    if len(head) < 4:
        return None     # Chrome closed the pipe: the panel is gone
    (length,) = struct.unpack("<I", head)
    body = b""
    while len(body) < length:
        chunk = PROTO_IN.read(length - len(body))
        if not chunk:
            return None
        body += chunk
    return json.loads(body.decode("utf-8"))


def send(obj: dict) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    PROTO_OUT.write(struct.pack("<I", len(data)) + data)


# --- worker ----------------------------------------------------------------

def ensure_worker(conn) -> None:
    """Start a worker if there is work and nobody alive to do it."""
    global _last_spawn
    if not jobs.active(conn) or jobs.worker_alive(conn):
        return
    if time.time() - _last_spawn < SPAWN_GRACE:
        return
    _last_spawn = time.time()

    kwargs = dict(cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=open(ROOT / "worker.log", "ab"))
    cmd = [sys.executable, str(ROOT / "worker.py")]
    if os.name != "nt":
        subprocess.Popen(cmd, start_new_session=True, **kwargs)
        return
    # CREATE_NO_WINDOW rather than DETACHED_PROCESS: the worker gets a hidden
    # console that whisper-cli and claude.cmd inherit, instead of each of
    # them popping up a window of its own.
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        # Out of Chrome's job object, if it allows that, so closing the
        # panel can never take a running transcription with it.
        subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB,
                         **kwargs)
    except OSError:
        subprocess.Popen(cmd, creationflags=flags, **kwargs)
    log.info("spawned worker")


def model_label() -> str:
    """ggml-medium-q5_0 -> medium"""
    name = Path(settings.get("whisper_model")).stem.removeprefix("ggml-")
    return re.sub(r"-q\d.*$", "", name)


# --- building the list -----------------------------------------------------

def ratios(conn) -> tuple[float, float]:
    row = conn.execute(
        """SELECT AVG(s.t_whisper / v.duration) AS w, AVG(s.t_download / v.duration) AS d
           FROM submissions s JOIN videos v USING (video_id)
           WHERE s.status = 'done' AND v.duration > 0 AND s.t_whisper IS NOT NULL"""
    ).fetchone()
    return (row["w"] or DEFAULT_WHISPER_RATIO, row["d"] or DEFAULT_DOWNLOAD_RATIO)


def progress(job, duration, whisper_ratio, download_ratio) -> dict:
    """
    Estimates, not measurements: whisper runs with its output discarded, so
    the only clock is the stage start time and past runs' speed. `after` is
    None while it is still unknown whether whisper will be needed at all.
    """
    stage = job["stage"] or "queued"
    est, after = STAGE_GUESS.get(stage), None
    if duration:
        if stage == "download":
            est, after = download_ratio * duration, whisper_ratio * duration + STAGE_GUESS["saving"]
        elif stage == "whisper":
            est, after = whisper_ratio * duration, STAGE_GUESS["saving"]
        elif stage == "saving":
            after = 0
    return {
        "stage": stage,
        "stage_started": job["stage_started_at"],
        "stage_estimate": est,
        "after_estimate": after,
        "started": job["started_at"],
    }


def short_error(error: str | None) -> str:
    """'DownloadError: ERROR: [youtube] abc: Unable to …' -> 'Unable to …'"""
    lines = (error or "").strip().splitlines()
    msg = lines[0] if lines else ""
    msg = re.sub(r"^\w+(Error|Exception): ", "", msg)
    msg = re.sub(r"^ERROR: (\[[^\]]+\] )?([\w-]{11}: )?", "", msg)
    return msg[:140]


def source_label(source: str | None) -> str | None:
    if not source:
        return None
    return "whisper" if source.startswith("whisper") else "captions"


def build_rows(conn) -> list[dict]:
    videos = conn.execute(
        """
        SELECT v.video_id, v.title, v.channel, v.duration, v.first_seen_at,
               s.status, s.error, s.source, s.entry_point,
               (SELECT COUNT(*) FROM analyses a WHERE a.video_id = v.video_id
                                               AND a.deleted_at IS NULL) AS n_analyses,
               (SELECT MIN(created_at) FROM jobs j WHERE j.video_id = v.video_id) AS first_job
        FROM videos v
        LEFT JOIN submissions s ON s.id = (
            SELECT id FROM submissions WHERE video_id = v.video_id
            ORDER BY submitted_at DESC, id DESC LIMIT 1)
        WHERE v.deleted_at IS NULL
        """
    ).fetchall()
    transcribing = jobs.active(conn, jobs.TRANSCRIBE_KINDS)
    active = {j["video_id"]: j for j in transcribing}
    queue_order = [j["id"] for j in transcribing if j["status"] == "queued"]
    whisper_ratio, download_ratio = ratios(conn)

    rows = {}
    for v in videos:
        vid = v["video_id"]
        has_transcript = (store.TRANSCRIPT_DIR / f"{vid}.json").exists()
        state = v["status"] or ("done" if has_transcript else None)
        if state is None:
            continue
        last_job = jobs.last_for(conn, vid) if state == "failed" else None
        rows[vid] = {
            "video_id": vid,
            "title": v["title"] or (last_job and last_job["title"]),
            "channel": v["channel"] or (last_job and last_job["channel"]),
            "duration": v["duration"] or (last_job and last_job["duration"]),
            "state": state,
            "source": source_label(v["source"]),
            "analysed": v["n_analyses"] > 0,
            "has_transcript": has_transcript,
            "entry_point": v["entry_point"],
            # In-progress rows keep their place: a video is ordered by when it
            # first entered the list, not by when its latest run finished.
            "order": min(filter(None, [v["first_seen_at"], v["first_job"]])),
        }
        if state == "failed":
            stage = last_job and last_job["status"] == "failed" and last_job["stage"]
            rows[vid]["error"] = f"{FAILED_AT.get(stage, 'Failed')} — {short_error(v['error'])}"
            rows[vid]["error_full"] = v["error"]

    for vid, job in active.items():
        base = rows.get(vid, {
            "video_id": vid, "analysed": False, "has_transcript": False,
            "source": None, "order": job["created_at"],
        })
        duration = job["duration"] or base.get("duration")
        base.update({
            "title": job["title"] or base.get("title"),
            "channel": job["channel"] or base.get("channel"),
            "duration": duration,
            "state": job["status"],
            "entry_point": job["entry_point"],
            "error": None,
        })
        if job["status"] == "running":
            base["progress"] = progress(job, duration, whisper_ratio, download_ratio)
        else:
            base["queue_position"] = queue_order.index(job["id"]) + 1
        rows[vid] = base

    return sorted(rows.values(), key=lambda r: r["order"], reverse=True)


# --- commands --------------------------------------------------------------

def cmd_state(conn, msg) -> dict:
    ensure_worker(conn)
    running = any(j["status"] == "running" for j in jobs.active(conn))
    threads = build_threads(conn)
    return {
        "worker": {
            "alive": jobs.worker_alive(conn),
            "busy": running,
            "model": model_label(),
            "threads": settings.get("whisper_threads"),
        },
        "rows": build_rows(conn),
        "threads": threads,
        "unread": sum(1 for t in threads if t["unread"]),
        "prompts": build_prompts(conn),
        "analysis_jobs": build_analysis_jobs(conn),
        "now": time.time(),
    }


def cmd_transcribe(conn, msg) -> dict:
    url = (msg.get("url") or "").strip()
    entry = msg.get("entry") if msg.get("entry") in ("page", "link") else "link"
    try:
        vid = tx.video_id(url)
    except ValueError:
        return {"ok": False, "error": "That isn't a YouTube link"}
    job_id = jobs.enqueue(conn, vid, f"https://www.youtube.com/watch?v={vid}", entry)
    ensure_worker(conn)
    return {"video_id": vid, "job_id": job_id}


def cmd_retry(conn, msg) -> dict:
    vid = msg.get("video_id") or ""
    last = conn.execute(
        "SELECT entry_point FROM submissions WHERE video_id = ? "
        "ORDER BY submitted_at DESC, id DESC LIMIT 1", (vid,),
    ).fetchone()
    entry = (last and last["entry_point"]) or "link"
    return cmd_transcribe(conn, {"url": vid, "entry": entry})


def cmd_transcript(conn, msg):
    """Sent in parts — a long video's segments can exceed Chrome's 1 MB cap."""
    vid = msg.get("video_id") or ""
    t = store.get_transcript(vid)
    if t is None:
        return {"ok": False, "error": "No transcript on disk for this video"}
    segs = [[round(s["start"], 1), s["text"]] for s in t["segments"]]
    parts = [segs[i:i + SEGMENTS_PER_MESSAGE]
             for i in range(0, len(segs), SEGMENTS_PER_MESSAGE)] or [[]]
    head = {k: t.get(k) for k in ("video_id", "title", "channel", "duration", "source", "language")}
    head["source_label"] = source_label(t.get("source"))
    return [{**head, "segments": p, "part": i, "parts": len(parts)}
            for i, p in enumerate(parts)]


# --- the Analyse side ------------------------------------------------------

VERDICTS = ("watched", "partial", "skipped")
PROMPT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
# Names analyses.prompt_template already uses for things that aren't files.
RESERVED_NAMES = {"custom", "follow-up", "ask"}


def build_threads(conn) -> list[dict]:
    """
    One entry per thread: an analysis that started one (parent_id NULL),
    plus the follow-ups that resumed its session. Rows whose file has gone
    are left out rather than shown as broken.
    """
    rows = conn.execute(
        """SELECT a.*, v.title, v.channel, v.duration
           FROM analyses a JOIN videos v USING (video_id)
           WHERE a.deleted_at IS NULL AND v.deleted_at IS NULL
           ORDER BY a.video_id, COALESCE(a.seq, 0), a.id"""
    ).fetchall()
    threads, by_session = {}, {}
    for r in rows:
        if r["parent_id"] is None:
            if not r["analysis_path"] or not Path(r["analysis_path"]).exists():
                continue
            t = {
                "id": r["id"], "video_id": r["video_id"], "title": r["title"],
                "channel": r["channel"], "duration": r["duration"],
                "prompt": r["prompt_template"], "question": r["question"],
                "created_at": r["created_at"], "updated_at": r["created_at"],
                "duration_s": r["duration_s"], "verdict": r["verdict"],
                "unread": r["opened_at"] is None, "followups": 0,
                "resumable": bool(r["session_id"]),
            }
            threads[r["id"]] = t
            if r["session_id"]:
                by_session[r["session_id"]] = t
    for r in rows:
        t = r["parent_id"] is not None and by_session.get(r["session_id"])
        if t:
            t["followups"] += 1
            t["updated_at"] = max(t["updated_at"], r["created_at"])
            t["unread"] = t["unread"] or r["opened_at"] is None
    return sorted(threads.values(), key=lambda t: t["updated_at"], reverse=True)


def build_prompts(conn) -> dict:
    """The library: every prompt file, with how often and how slowly it runs."""
    stats = {
        r["prompt_template"]: r
        for r in conn.execute(
            """SELECT prompt_template, COUNT(*) AS uses, AVG(duration_s) AS avg_s,
                      MAX(created_at) AS last_at
               FROM analyses WHERE parent_id IS NULL GROUP BY prompt_template"""
        )
    }
    items = []
    for name, path in analyze.list_prompts().items():
        p = analyze.read_prompt(path)
        s = stats.get(name)
        items.append({
            "name": name, "description": p["description"],
            "uses": s["uses"] if s else 0,
            "avg_s": round(s["avg_s"]) if s and s["avg_s"] else None,
            "last_at": s["last_at"] if s else None,
        })
    used = [i for i in items if i["last_at"]]
    last = max(used, key=lambda i: i["last_at"])["name"] if used else None
    return {"items": items, "last": last}


def job_view(conn, j) -> dict:
    v = conn.execute("SELECT title, channel FROM videos WHERE video_id = ?",
                     (j["video_id"],)).fetchone()
    return {
        "job_id": j["id"], "kind": j["kind"], "status": j["status"],
        "video_id": j["video_id"], "title": v and v["title"], "channel": v and v["channel"],
        "prompt": j["prompt"], "question": j["question"], "session_id": j["session_id"],
        "started": j["started_at"], "error": short_error(j["error"]) if j["error"] else None,
        "cancelling": bool(j["cancel_requested"]),
    }


def build_analysis_jobs(conn) -> list[dict]:
    """Pending analyses and follow-ups, plus failures nobody has cleared."""
    kinds = jobs.ANALYSE_KINDS
    rows = conn.execute(
        f"""SELECT * FROM jobs WHERE kind IN {jobs._in(kinds)}
            AND (status IN ('queued', 'running')
                 OR (status = 'failed' AND dismissed_at IS NULL))
            ORDER BY id""",
        kinds,
    ).fetchall()
    return [job_view(conn, j) for j in rows]


def answer_body(row) -> str:
    """The file minus the header save_analysis writes: '# name', a timestamp, the question."""
    text = Path(row["analysis_path"]).read_text(encoding="utf-8")
    lines = text.split("\n")
    if lines and lines[0].strip() == f"# {row['prompt_template']}":
        lines = lines[1:]
    if lines and re.fullmatch(r"<!--.*-->", lines[0].strip()):
        lines = lines[1:]
    body = "\n".join(lines).lstrip("\n")
    q = f"**Q:** {row['question']}" if row["question"] else None
    if q and body.startswith(q):
        body = body[len(q):].lstrip("\n")
    return body.strip()


def root_of(conn, analysis_id: int):
    row = conn.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
    if row is None:
        raise ValueError("No such analysis")
    if row["parent_id"] is not None and row["session_id"]:
        row = conn.execute(
            "SELECT * FROM analyses WHERE session_id = ? AND parent_id IS NULL",
            (row["session_id"],),
        ).fetchone() or row
    return row


def cmd_thread(conn, msg) -> dict:
    """An analysis and its follow-ups, as one document. Reading it marks it opened."""
    root = root_of(conn, int(msg["analysis_id"]))
    members = [root]
    if root["session_id"]:
        members += conn.execute(
            "SELECT * FROM analyses WHERE session_id = ? AND parent_id IS NOT NULL "
            "AND deleted_at IS NULL ORDER BY seq, id",
            (root["session_id"],),
        ).fetchall()
    video = conn.execute("SELECT * FROM videos WHERE video_id = ?",
                         (root["video_id"],)).fetchone()

    entries = []
    for m in members:
        try:
            body = answer_body(m)
        except OSError:
            continue
        entries.append({
            "id": m["id"], "prompt": m["prompt_template"], "question": m["question"],
            "created_at": m["created_at"], "duration_s": m["duration_s"], "body": body,
        })
        if msg.get("mark", True):
            # Opened means displayed: the panel only asks while the screen is open.
            store.mark_opened(conn, m["id"])

    pending = []
    if root["session_id"]:
        pending = [
            job_view(conn, j) for j in conn.execute(
                """SELECT * FROM jobs WHERE kind = 'follow-up' AND session_id = ?
                   AND (status IN ('queued', 'running')
                        OR (status = 'failed' AND dismissed_at IS NULL))
                   ORDER BY id""",
                (root["session_id"],),
            )
        ]
    return {
        "analysis_id": root["id"], "video_id": root["video_id"],
        "title": video["title"], "channel": video["channel"], "duration": video["duration"],
        "prompt": root["prompt_template"], "verdict": root["verdict"],
        "resumable": bool(root["session_id"]),
        "entries": entries, "pending": pending,
    }


def cmd_analyse(conn, msg) -> dict:
    vid = msg.get("video_id") or ""
    if store.get_transcript(vid) is None:
        return {"ok": False, "error": "Transcribe this video first"}
    question = (msg.get("question") or "").strip()
    if question:
        job_id = jobs.enqueue_analysis(conn, vid, prompt="custom", question=question)
    else:
        name = msg.get("prompt") or ""
        if name not in analyze.list_prompts():
            return {"ok": False, "error": f"No prompt called '{name}'"}
        job_id = jobs.enqueue_analysis(conn, vid, prompt=name)
    ensure_worker(conn)
    return {"job_id": job_id}


def cmd_follow_up(conn, msg) -> dict:
    root = root_of(conn, int(msg["analysis_id"]))
    question = (msg.get("question") or "").strip()
    if not question:
        return {"ok": False, "error": "Ask something first"}
    if not root["session_id"]:
        return {"ok": False, "error": "This analysis was made before follow-ups existed"}
    job_id = jobs.enqueue_analysis(conn, root["video_id"], prompt="follow-up",
                                   question=question, session_id=root["session_id"])
    ensure_worker(conn)
    return {"job_id": job_id}


def cmd_cancel(conn, msg) -> dict:
    jobs.cancel(conn, int(msg["job_id"]))
    return {}


def cmd_dismiss(conn, msg) -> dict:
    jobs.dismiss(conn, int(msg["job_id"]))
    return {}


def cmd_verdict(conn, msg) -> dict:
    verdict = msg.get("verdict")
    if verdict is not None and verdict not in VERDICTS:
        return {"ok": False, "error": f"verdict must be one of {VERDICTS}"}
    store.set_verdict(conn, root_of(conn, int(msg["analysis_id"]))["id"], verdict)
    return {}


def cmd_prompt_get(conn, msg) -> dict:
    path = analyze.list_prompts().get(msg.get("name") or "")
    if path is None:
        return {"ok": False, "error": "No such prompt"}
    return analyze.read_prompt(path)


def cmd_prompt_save(conn, msg) -> dict:
    """Writes prompts/<name>.md with its description as frontmatter."""
    name = (msg.get("name") or "").strip()
    original = msg.get("original") or None
    description = " ".join((msg.get("description") or "").split())
    body = (msg.get("body") or "").strip()
    if not PROMPT_NAME.match(name):
        return {"ok": False, "error": "Name: lowercase letters, digits and hyphens"}
    if name in RESERVED_NAMES:
        return {"ok": False, "error": f"'{name}' is reserved"}
    if not body:
        return {"ok": False, "error": "The prompt is empty"}
    path = analyze.PROMPT_DIR / f"{name}.md"
    if name != original and path.exists():
        return {"ok": False, "error": f"prompts/{name}.md already exists"}
    analyze.PROMPT_DIR.mkdir(exist_ok=True)
    head = f"---\ndescription: {description}\n---\n\n" if description else ""
    path.write_text(head + body + "\n", encoding="utf-8")
    if original and original != name:
        (analyze.PROMPT_DIR / f"{original}.md").unlink(missing_ok=True)
    return {"name": name}


def cmd_prompt_delete(conn, msg) -> dict:
    path = analyze.list_prompts().get(msg.get("name") or "")
    if path is None:
        return {"ok": False, "error": "No such prompt"}
    path.unlink()
    return {}


NOTIFY_WINDOW = 3600    # seconds; older unseen results are marked, not announced


def cmd_watch(conn, msg) -> dict:
    """
    For background.js: finished analyses it hasn't announced yet, each
    returned once. `active` tells it whether to keep polling.
    """
    kinds = jobs.ANALYSE_KINDS
    active = len(jobs.active(conn, kinds))
    rows = conn.execute(
        f"""SELECT * FROM jobs WHERE kind IN {jobs._in(kinds)}
            AND status IN ('done', 'failed') AND notified_at IS NULL""",
        kinds,
    ).fetchall()
    finished = []
    cutoff = time.time() - NOTIFY_WINDOW
    for j in rows:
        conn.execute("UPDATE jobs SET notified_at = ? WHERE id = ?", (store.now(), j["id"]))
        if (j["started_at"] or 0) < cutoff:
            continue
        view = job_view(conn, j)
        if j["analysis_id"]:
            view["root_id"] = root_of(conn, j["analysis_id"])["id"]
        finished.append(view)
    return {"active": active, "finished": finished}


# --- deletion --------------------------------------------------------------
#
# Soft deletes only (store.py explains why): files go, rows stay, and the
# panel's reads skip deleted rows. store.purge() is never reachable from here.

def cmd_delete_video(conn, msg) -> dict:
    vid = msg.get("video_id") or ""
    running = conn.execute(
        "SELECT 1 FROM jobs WHERE video_id = ? AND kind = 'transcribe' AND status = 'running'",
        (vid,),
    ).fetchone()
    if running:
        return {"ok": False, "error": "It is transcribing right now — delete it when that finishes"}
    # Nothing still queued or running should write files back after the delete.
    for j in conn.execute(
        "SELECT id FROM jobs WHERE video_id = ? AND status IN ('queued', 'running')", (vid,)
    ).fetchall():
        jobs.cancel(conn, j["id"])
    result = store.soft_delete_video(conn, vid)
    log.info("soft-deleted video %s (%d files)", vid, len(result["removed_files"]))
    return {"removed_files": len(result["removed_files"])}


def cmd_delete_analysis(conn, msg) -> dict:
    """dry_run first: the panel says "this also deletes N follow-ups" before the click."""
    root = conn.execute("SELECT * FROM analyses WHERE id = ?",
                        (int(msg["analysis_id"]),)).fetchone()
    if root is None:
        return {"ok": False, "error": "No such analysis"}
    dry = bool(msg.get("dry_run"))
    result = store.soft_delete_analysis(conn, root["id"], dry_run=dry)
    if not dry and root["session_id"] and root["parent_id"] is None:
        # A pending follow-up would otherwise land in a thread that no longer shows.
        for j in conn.execute(
            "SELECT id FROM jobs WHERE kind = 'follow-up' AND session_id = ? "
            "AND status IN ('queued', 'running')", (root["session_id"],)
        ).fetchall():
            jobs.cancel(conn, j["id"])
        log.info("soft-deleted analysis %s (+%d follow-ups)", root["id"], len(result["followups"]))
    return {"followups": len(result["followups"]), "removed_files": len(result["removed_files"])}


# --- settings --------------------------------------------------------------

VAD_MODEL_HINT = "silero"       # model files with this in the name are VAD, not whisper


def cmd_settings_get(conn, msg) -> dict:
    models = sorted(
        str(p).replace("\\", "/") for p in Path("models").glob("ggml-*.bin")
        if VAD_MODEL_HINT not in p.name
    )
    return {
        "values": settings.all(),
        "defaults": settings.defaults(),
        "stored": sorted(settings.stored()),
        "models": models,
        "vad_model_present": tx.WHISPER_VAD_MODEL.exists(),
        "vad_model": str(tx.WHISPER_VAD_MODEL).replace("\\", "/"),
    }


def cmd_settings_set(conn, msg) -> dict:
    """Validation lives in settings.py; its message goes straight to the panel."""
    key = msg.get("key") or ""
    try:
        if msg.get("reset"):
            settings.reset(key)
        else:
            settings.set(key, msg.get("value"))
    except (KeyError, ValueError) as e:
        # e.args[0], not str(e): str() of a KeyError wraps the message in quotes.
        return {"ok": False, "error": str(e.args[0]) if e.args else str(e)}
    return cmd_settings_get(conn, msg)


COMMANDS = {
    "state": cmd_state,
    "delete_video": cmd_delete_video,
    "delete_analysis": cmd_delete_analysis,
    "settings_get": cmd_settings_get,
    "settings_set": cmd_settings_set,
    "transcribe": cmd_transcribe,
    "retry": cmd_retry,
    "transcript": cmd_transcript,
    "thread": cmd_thread,
    "analyse": cmd_analyse,
    "follow_up": cmd_follow_up,
    "cancel": cmd_cancel,
    "dismiss": cmd_dismiss,
    "verdict": cmd_verdict,
    "prompt_get": cmd_prompt_get,
    "prompt_save": cmd_prompt_save,
    "prompt_delete": cmd_prompt_delete,
    "watch": cmd_watch,
}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, stream=_log_file,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log.info("host started, args %s", sys.argv[1:])
    with closing(jobs.connect()) as conn:
        jobs.init(conn)
        # Same startup step as run.py: the panel alone must honour retention too.
        expired = store.apply_retention(conn)
        if expired:
            log.info("retention: soft-deleted %s", expired)
        while (msg := read_message()) is not None:
            req_id = msg.get("id")
            handler = COMMANDS.get(msg.get("cmd"))
            try:
                if handler is None:
                    raise ValueError(f"unknown command {msg.get('cmd')!r}")
                out = handler(conn, msg)
            except Exception as e:
                log.exception("command %s failed", msg.get("cmd"))
                out = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            for reply in (out if isinstance(out, list) else [out]):
                # The envelope id goes last so no reply field can overwrite it.
                send({"ok": True, **reply, "id": req_id})
    log.info("host exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
