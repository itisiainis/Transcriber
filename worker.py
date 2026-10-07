"""
worker.py — runs queued jobs, then exits when idle.

Started by host.py whenever there is work and no live worker; never needs
starting by hand, though `python worker.py` works for debugging.

Two lanes, each one job at a time:
  transcribe — CPU-bound; whisper already uses every thread it is given
  analyse    — network-bound; claude -p, analyses and follow-ups in order,
               so two follow-ups never resume the same session at once

Transcription does what run.py does — transcribe, store, record — but
reports which stage it is in, so the panel can show progress. It does that
by wrapping the stage functions in transcribe.py from the outside:
transcribe() looks them up by module-level name at call time, so the
pipeline itself is untouched.
"""

import logging
import os
import subprocess
import sys
import threading
import time
from contextlib import closing, contextmanager

import jobs
import store

IDLE_EXIT = 60      # seconds with both lanes idle before the worker exits
POLL = 2            # seconds between queue checks while idle
CANCEL_POLL = 1     # seconds between checks for a ✕ on a running analysis
LOG_PATH = "worker.log"

log = logging.getLogger("worker")


# --- stage reporting -------------------------------------------------------

@contextmanager
def reporting_stages(tx, job_id: int, captured: dict):
    """
    Wrap transcribe.py's stage functions so each call records its stage on
    the job first. fetch_metadata's result is kept, so a job that fails
    later still knows the video's title.
    """
    stages = {
        "fetch_metadata": "metadata",
        "try_captions": "captions",
        "download_audio": "download",
        "transcribe_audio": "whisper",
    }
    originals = {name: getattr(tx, name) for name in stages}

    def wrap(name, stage):
        fn = originals[name]

        def wrapper(*args, **kwargs):
            with closing(jobs.connect()) as conn:
                jobs.set_stage(conn, job_id, stage)
            out = fn(*args, **kwargs)
            if name == "fetch_metadata":
                captured.update(out)
                with closing(jobs.connect()) as conn:
                    jobs.set_meta(conn, job_id, out)
            return out
        return wrapper

    for name, stage in stages.items():
        setattr(tx, name, wrap(name, stage))
    try:
        yield
    finally:
        for name, fn in originals.items():
            setattr(tx, name, fn)


# --- transcription ---------------------------------------------------------

def record_failed(job, error: str, meta: dict) -> None:
    with closing(store.connect()) as conn, conn:
        if meta.get("title"):
            # Give the videos row its title even though the run failed, so the
            # failure is readable in the db and not just an id.
            store.upsert_video(conn, meta)
        sub_id = store.record_failure(conn, job["video_id"], error, job["entry_point"])
    with closing(jobs.connect()) as conn:
        jobs.fail(conn, job["id"], error, sub_id)


def run_transcribe(job) -> None:
    import transcribe as tx     # heavy (yt-dlp); only once there is work

    meta: dict = {}
    try:
        with reporting_stages(tx, job["id"], meta):
            result = tx.transcribe(job["url"])

        with closing(jobs.connect()) as conn:
            jobs.set_stage(conn, job["id"], "saving")
        with closing(store.connect()) as conn, conn:
            store.upsert_video(conn, result)
            path = store.save_transcript(result)
            sub_id = store.record_submission(conn, result, job["entry_point"], path)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log.exception("job %s failed", job["id"])
        record_failed(job, error, meta)
        return

    with closing(jobs.connect()) as conn:
        jobs.finish(conn, job["id"], submission_id=sub_id)
    log.info("job %s done: %s (%s)", job["id"], result["title"], result["source"])


# --- analysis --------------------------------------------------------------

def kill_tree(proc) -> None:
    """claude.cmd is cmd.exe running node; killing cmd alone orphans node."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        proc.kill()


def watch_for_cancel(job_id: int, proc_box: dict, done: threading.Event) -> None:
    with closing(jobs.connect()) as conn:
        while not done.wait(CANCEL_POLL):
            if jobs.cancel_requested(conn, job_id) and proc_box.get("proc"):
                log.info("job %s: cancel requested, stopping claude", job_id)
                proc_box["cancelled"] = True
                kill_tree(proc_box["proc"])
                return


def run_analyse(job) -> None:
    import analyze

    with closing(jobs.connect()) as conn:
        jobs.set_stage(conn, job["id"], "claude")

    proc_box: dict = {}
    done = threading.Event()
    analyze.on_spawn = lambda proc: proc_box.__setitem__("proc", proc)
    threading.Thread(target=watch_for_cancel, args=(job["id"], proc_box, done),
                     daemon=True).start()
    try:
        if job["kind"] == "follow-up":
            analysis_id, _ = analyze.follow_up(job["video_id"], job["question"],
                                               session_id=job["session_id"])
        elif job["prompt"] == "custom":
            analysis_id, _ = analyze.analyze(job["video_id"], job["question"], "custom")
        else:
            path = analyze.list_prompts().get(job["prompt"])
            if path is None:
                raise FileNotFoundError(f"no prompt '{job['prompt']}' in prompts/")
            body = analyze.read_prompt(path)["body"]
            analysis_id, _ = analyze.analyze(job["video_id"], body, job["prompt"])
    except (Exception, SystemExit) as e:     # follow_up exits when there's no session
        with closing(jobs.connect()) as conn:
            if proc_box.get("cancelled"):
                jobs.mark_cancelled(conn, job["id"])
                log.info("job %s cancelled", job["id"])
            else:
                log.exception("job %s failed", job["id"])
                jobs.fail(conn, job["id"], f"{type(e).__name__}: {e}")
        return
    finally:
        done.set()
        analyze.on_spawn = None

    with closing(jobs.connect()) as conn:
        jobs.finish(conn, job["id"], analysis_id=analysis_id)
    log.info("job %s done: %s %s -> analysis %s",
             job["id"], job["kind"], job["prompt"] or "", analysis_id)


# --- lanes -----------------------------------------------------------------

class Lane(threading.Thread):
    def __init__(self, name: str, kinds, run, stop: threading.Event):
        super().__init__(name=name, daemon=True)
        self.kinds, self.run_job, self.stop = kinds, run, stop
        self.busy = False
        self.last_active = time.monotonic()

    def run(self) -> None:
        with closing(jobs.connect()) as conn:
            while not self.stop.is_set():
                job = jobs.claim(conn, self.kinds)
                if job is None:
                    self.stop.wait(POLL)
                    continue
                self.busy = True
                log.info("[%s] job %s: %s %s", self.name, job["id"], job["kind"], job["video_id"])
                try:
                    self.run_job(job)
                except Exception:
                    # run_* record their own failures; this is a last resort
                    # so one bad job can't take the lane down.
                    log.exception("[%s] job %s crashed the lane", self.name, job["id"])
                finally:
                    self.busy = False
                    self.last_active = time.monotonic()


def recover_orphans(conn) -> None:
    """
    Jobs left 'running' by a worker that died (killed, reboot). Only called
    after this process holds the worker slot, so nothing else is running them.
    """
    for job in conn.execute("SELECT * FROM jobs WHERE status = 'running'").fetchall():
        log.warning("job %s was orphaned in stage %s", job["id"], job["stage"])
        error = "Interrupted: the worker stopped mid-job"
        if job["kind"] in jobs.TRANSCRIBE_KINDS:
            record_failed(job, error,
                          {"video_id": job["video_id"], "title": job["title"],
                           "channel": job["channel"], "duration": job["duration"]})
        else:
            jobs.fail(conn, job["id"], error)


def beat(pid: int, stop: threading.Event) -> None:
    with closing(jobs.connect()) as conn:
        while not stop.wait(jobs.HEARTBEAT_EVERY):
            try:
                jobs.heartbeat(conn, pid)
            except Exception:
                log.exception("heartbeat failed")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, filename=LOG_PATH, encoding="utf-8",
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    pid = os.getpid()

    with closing(jobs.connect()) as conn:
        jobs.init(conn)
        if not jobs.claim_worker(conn, pid):
            log.info("another worker is alive; exiting")
            return 0
        log.info("worker %s started", pid)

        # Separate events: the heartbeat must outlive the lanes, or a job a
        # lane claimed at the last moment would look orphaned to the host.
        stop_lanes, stop_beat = threading.Event(), threading.Event()
        threading.Thread(target=beat, args=(pid, stop_beat), daemon=True).start()
        lanes = [
            Lane("transcribe", jobs.TRANSCRIBE_KINDS, run_transcribe, stop_lanes),
            Lane("analyse", jobs.ANALYSE_KINDS, run_analyse, stop_lanes),
        ]
        try:
            recover_orphans(conn)
            for lane in lanes:
                lane.start()
            while True:
                time.sleep(POLL)
                idle = all(not l.busy and time.monotonic() - l.last_active > IDLE_EXIT
                           for l in lanes)
                if idle and not jobs.active(conn):
                    break
        finally:
            stop_lanes.set()
            for lane in lanes:
                if lane.is_alive():
                    lane.join()     # finishes a job it claimed at the last moment
            stop_beat.set()
            jobs.release_worker(conn, pid)
            log.info("worker %s exiting", pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
