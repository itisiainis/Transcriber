"""
batch.py — several transcriptions queued together, run one by one.

A batch is not a second queue. It is ordinary transcribe jobs (jobs.py) with
entry_point 'batch' (here) or 'channel' (picked from a channel listing in
the panel), run by the same worker, one at a time, in the order queued. So
the panel shows a CLI batch and the CLI can run a panel one; either way, one
whisper at a time is all this CPU can take.

Crash-safe through the worker: a batch job left 'running' by a worker that
died is queued again on the next start (once — see worker.recover_orphans).

    python batch.py add <video_or_channel_url>... [--limit N]
    python batch.py list         # queued and running, with estimates and a total
    python batch.py run          # work through the queue in this terminal
    python batch.py clear        # drop everything still waiting
"""

import argparse
import statistics
import sys
import threading
import time
from contextlib import closing

import jobs
import store

FALLBACK_FACTOR = 0.25      # whisper on this CPU (CLAUDE.md), until there is history


def factors(conn) -> dict:
    """
    Median realtime_factor (total time / video length) from completed
    submissions — self-calibrating as the db fills. Two of them: a non-YouTube
    video always goes through whisper, so it is measured against whisper runs
    only; a YouTube one against every run, captions included, because that
    is the mix a YouTube video actually gets.
    """
    rows = conn.execute(
        "SELECT realtime_factor AS f, source FROM submissions "
        "WHERE status = 'done' AND realtime_factor > 0"
    ).fetchall()
    every = [r["f"] for r in rows]
    whisper = [r["f"] for r in rows if (r["source"] or "").startswith("whisper")]
    return {
        "whisper": statistics.median(whisper) if whisper else FALLBACK_FACTOR,
        "any": statistics.median(every) if every else FALLBACK_FACTOR,
    }


def estimate_seconds(duration: int | None, platform: str = "youtube",
                     f: dict | None = None) -> float | None:
    """Expected wall-clock seconds to transcribe a video this long. None if its length isn't known."""
    if not duration:
        return None
    if f is None:
        with closing(store.connect()) as conn:
            f = factors(conn)
    return duration * (f["any"] if platform == "youtube" else f["whisper"])


def fmt_estimate(seconds: float | None) -> str:
    """'~4h 20m', '~12m', '<1m'; '?' when unknown."""
    if seconds is None:
        return "?"
    m = round(seconds / 60)
    if m < 1:
        return "<1m"
    return f"~{m // 60}h {m % 60}m" if m >= 60 else f"~{m}m"


def queued(conn) -> list:
    """Transcriptions waiting or running, in the order the worker takes them."""
    return conn.execute(
        "SELECT j.*, COALESCE(v.platform, 'youtube') AS platform, "
        "       COALESCE(j.duration, v.duration) AS dur, COALESCE(j.title, v.title) AS name "
        "FROM jobs j LEFT JOIN videos v USING (video_id) "
        "WHERE j.kind = 'transcribe' AND j.status IN ('queued', 'running') "
        "ORDER BY j.status = 'running' DESC, j.id"
    ).fetchall()


# --- CLI -------------------------------------------------------------------

def cmd_add(conn, urls: list[str], limit: int | None) -> None:
    import channel
    import transcribe as tx

    f = factors(conn)
    added = []
    for url in urls:
        try:
            listing = channel.enumerate_channel(url)
        except ValueError:
            listing = None
        if listing:
            # Newest first, skipping what is already in the panel.
            picks = [v for v in listing["videos"] if not v["present"]][:limit]
            print(f"{listing['channel'] or listing['url']}: {len(listing['videos'])} listed, "
                  f"queueing {len(picks)} not yet transcribed")
            for v in picks:
                v["channel"] = listing["channel"]
        else:
            platform, vid = tx.parse_url(url)
            # One metadata request, so the row has a name and an estimate.
            meta = tx.fetch_metadata(vid, platform)
            picks = [meta]
        for v in picks:
            jobs.enqueue(conn, v["video_id"], tx.video_url(v["video_id"], v["platform"]),
                         "batch", v)
            added.append(v)

    total = 0.0
    for v in added:
        est = estimate_seconds(v.get("duration"), v["platform"], f)
        total += est or 0
        print(f"  + {v['video_id']}  {fmt_estimate(est):>8}  {v.get('title') or ''}")
    print(f"{len(added)} queued · {fmt_estimate(total)}")


def cmd_list(conn) -> None:
    f = factors(conn)
    rows = queued(conn)
    if not rows:
        print("nothing queued")
        return
    total = 0.0
    for r in rows:
        est = estimate_seconds(r["dur"], r["platform"], f)
        total += est or 0
        state = f"running · {r['stage'] or 'starting'}" if r["status"] == "running" else "waiting"
        print(f"{r['id']:>5}  {state:20}  {fmt_estimate(est):>8}  {r['name'] or r['video_id']}")
    print(f"{len(rows)} video{'s' if len(rows) != 1 else ''} · {fmt_estimate(total)}"
          f"  (median realtime factor: whisper {f['whisper']:.3f}, all {f['any']:.3f})")


def cmd_run(conn) -> int:
    """
    Runs the worker in this process and prints each job as it finishes. If
    the panel's worker is already alive, that one does the work and this
    just watches — there is only ever one.
    """
    import worker

    if not queued(conn):
        print("nothing queued")
        return 0
    if jobs.worker_alive(conn):
        print("a worker is already running; watching it")
    else:
        threading.Thread(target=worker.main, daemon=True).start()

    seen = {r["id"]: r["status"] for r in queued(conn)}
    started = {}
    while True:
        for job_id, before in list(seen.items()):
            j = jobs.get(conn, job_id)
            if j["status"] == "running" and job_id not in started:
                started[job_id] = time.monotonic()
                print(f"→ {j['title'] or j['video_id']}", flush=True)
            if j["status"] not in jobs.ACTIVE:
                took = time.monotonic() - started.get(job_id, time.monotonic())
                note = j["status"] if j["status"] != "failed" else f"failed: {(j['error'] or '')[:120]}"
                print(f"  {note} ({took / 60:.1f} min)", flush=True)
                del seen[job_id]
        for r in queued(conn):          # anything added while running
            seen.setdefault(r["id"], r["status"])
        if not seen:
            print("queue empty")
            return 0
        time.sleep(2)


def cmd_clear(conn) -> None:
    waiting = [r for r in queued(conn) if r["status"] == "queued"]
    for r in waiting:
        jobs.cancel(conn, r["id"])
    running = len(queued(conn))
    print(f"cleared {len(waiting)} waiting" + (f"; {running} running, left to finish" if running else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add", help="queue videos, or a channel's not-yet-transcribed ones")
    a.add_argument("urls", nargs="+")
    a.add_argument("--limit", type=int, help="from a channel, at most this many (newest first)")
    sub.add_parser("list")
    sub.add_parser("run")
    sub.add_parser("clear")
    args = ap.parse_args()

    with closing(jobs.connect()) as conn:
        jobs.init(conn)
        if args.cmd == "add":
            cmd_add(conn, args.urls, args.limit)
        elif args.cmd == "list":
            cmd_list(conn)
        elif args.cmd == "run":
            return cmd_run(conn)
        elif args.cmd == "clear":
            cmd_clear(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
