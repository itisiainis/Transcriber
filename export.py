"""
export.py — transcripts out as plain text, for dropping into a claude.ai project.

Transcripts only, not analyses. Two modes:
  combined  (default) one .txt, each transcript under its header, in the order given;
            one file beats forty when the destination is a project
  separate  one .txt per video, named <upload_date>-<slug>.txt, zipped

The header is the same in both, so a file pulled out of the zip still says
what it is (format in CLAUDE.md):

    ════════════════════════════════════════════════════
    <title>
    <channel> · <platform> · <duration mm:ss>
    Опубликовано: <upload_date> · Расшифровано: <submitted_at, local>
    <url>
    ════════════════════════════════════════════════════

    python export.py --ids a,b,c --mode combined --out batch.txt
    python export.py --last 3
"""

import argparse
import io
import re
import sys
import zipfile
from contextlib import closing
from datetime import datetime

import store
import transcribe as tx

RULE = "═" * 52
PLATFORM_NAMES = {"youtube": "YouTube", "rutube": "RuTube"}
MISSING = "—"


def _mmss(seconds) -> str:
    """Total minutes, so a 1h15m video is 75:03 — the header says mm:ss."""
    if not seconds:
        return MISSING
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


def _local(iso: str | None) -> str:
    """submitted_at is stored in UTC; the header shows the owner's clock."""
    if not iso:
        return MISSING
    return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")


def _slug(title: str | None) -> str:
    """Unlike store.slug(), keeps Cyrillic: most of what gets exported is Russian."""
    s = re.sub(r"[^\w]+", "-", (title or "").lower()).strip("-_")
    return s[:60].rstrip("-_") or "video"


def _load(conn, video_id: str) -> dict:
    v = conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    t = store.get_transcript(video_id)
    if v is None or t is None:
        raise ValueError(f"no transcript for {video_id}")
    sub = conn.execute(
        "SELECT submitted_at FROM submissions WHERE video_id = ? AND status = 'done' "
        "ORDER BY submitted_at DESC, id DESC LIMIT 1", (video_id,),
    ).fetchone()
    platform = v["platform"] or "youtube"
    return {
        "video_id": video_id, "platform": platform,
        "title": v["title"] or t.get("title"), "channel": v["channel"] or t.get("channel"),
        "duration": v["duration"] or t.get("duration"),
        "upload_date": v["upload_date"], "submitted_at": sub and sub["submitted_at"],
        "url": tx.video_url(video_id, platform), "transcript": t,
    }


def header(v: dict) -> str:
    return "\n".join([
        RULE,
        v["title"] or v["video_id"],
        f"{v['channel'] or MISSING} · {PLATFORM_NAMES.get(v['platform'], v['platform'])} · {_mmss(v['duration'])}",
        f"Опубликовано: {v['upload_date'] or MISSING} · Расшифровано: {_local(v['submitted_at'])}",
        v["url"],
        RULE,
    ])


def _document(v: dict) -> str:
    return f"{header(v)}\n\n{tx.as_text(v['transcript'])}\n"


def export(video_ids: list[str], mode: str = "combined") -> tuple[str, bytes]:
    """
    -> (suggested file name, file bytes). Raises ValueError naming any video
    with no transcript, rather than leaving it out of the file unnoticed.
    """
    if mode not in ("combined", "separate"):
        raise ValueError(f"mode must be 'combined' or 'separate', not {mode!r}")
    if not video_ids:
        raise ValueError("nothing to export")
    with closing(store.connect()) as conn:
        videos, missing = [], []
        for vid in video_ids:
            try:
                videos.append(_load(conn, vid))
            except ValueError:
                missing.append(vid)
    if missing:
        raise ValueError(f"no transcript for {', '.join(missing)}")

    stamp = datetime.now().strftime("%Y-%m-%d")
    if mode == "combined":
        name = f"{_slug(videos[0]['title'])}.txt" if len(videos) == 1 \
            else f"transcripts-{stamp}-{len(videos)}.txt"
        return name, "\n\n".join(_document(v) for v in videos).encode("utf-8")

    buf, used = io.BytesIO(), set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for v in videos:
            fname = f"{v['upload_date'] or 'undated'}-{_slug(v['title'])}"
            if fname in used:           # same day, same title: keep both
                fname += f"-{v['video_id']}"
            used.add(fname)
            z.writestr(f"{fname}.txt", _document(v).encode("utf-8"))
    return f"transcripts-{stamp}-{len(videos)}.zip", buf.getvalue()


def last_ids(n: int) -> list[str]:
    """The last n videos transcribed (not deleted), oldest first — the order they were done."""
    with closing(store.connect()) as conn:
        rows = conn.execute(
            """SELECT s.video_id, MAX(s.submitted_at) AS at
               FROM submissions s JOIN videos v USING (video_id)
               WHERE s.status = 'done' AND v.deleted_at IS NULL
               GROUP BY s.video_id ORDER BY at DESC LIMIT ?""", (n,),
        ).fetchall()
    return [r["video_id"] for r in reversed(rows)]


def main() -> int:
    ap = argparse.ArgumentParser(description="Export transcripts as text.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--ids", help="comma-separated video ids, in the order wanted")
    g.add_argument("--last", type=int, metavar="N", help="the last N videos transcribed")
    ap.add_argument("--mode", choices=("combined", "separate"), default="combined")
    ap.add_argument("--out", help="file to write (default: the suggested name, here)")
    args = ap.parse_args()

    ids = [i.strip() for i in args.ids.split(",") if i.strip()] if args.ids else last_ids(args.last)
    try:
        name, data = export(ids, args.mode)
    except ValueError as e:
        print(f"export: {e}", file=sys.stderr)
        return 1
    out = args.out or name
    with open(out, "wb") as f:
        f.write(data)
    print(f"{len(ids)} transcript{'s' if len(ids) != 1 else ''} -> {out} ({len(data) // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
