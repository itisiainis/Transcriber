"""
recording.py — audio recorded in the browser, as a source the pipeline can
transcribe like any video.

The panel's Record screen records a player on any site (MediaRecorder,
opus) and the extension sends a chunk every 10 s. The host appends each one
to recordings/<rec_id>/seg-NNN.webm the moment it arrives, so what was
recorded is on disk within 10 s: a crashed tab, or all of Chrome, loses at
most the last chunk.

A segment is one MediaRecorder run. A new one starts when the player
rebuilds its <video> (quality switch, reconnect) or the audio track ends.
Each carries its own WebM header, so segments are kept apart and joined by
ffmpeg at the end, never appended into one file — that would be a broken
WebM after the first header.

The join is also a remux, and that matters on its own: MediaRecorder never
writes a duration into its WebM, so players guess the length (the 8-second
"10-second" test file). ffmpeg's output has a real one.

Ids are 'r' + 15 hex: never 11 characters (YouTube) or 32 hex (RuTube), so
video_id stays one join key across every source.
"""

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import store

RECORD_DIR = Path("recordings")
ID_RE = re.compile(r"r[0-9a-f]{15}")

BITRATE = 32000            # opus bits/s: plenty for speech; two hours ≈ 29 MB
SILENCE_STOP = 15          # [setting] record_silence_stop: seconds of playing silence that end a recording
SILENCE_STOP_LIVE = 300    # [setting] record_silence_stop_live: the same for a live stream


def folder(rec_id: str) -> Path:
    # rec_id arrives from the extension: never let it name a path elsewhere.
    if not ID_RE.fullmatch(rec_id or ""):
        raise ValueError(f"not a recording id: {rec_id!r}")
    return RECORD_DIR / rec_id


def _meta_path(rec_id: str) -> Path:
    return folder(rec_id) / "meta.json"


def meta(rec_id: str) -> dict:
    return json.loads(_meta_path(rec_id).read_text(encoding="utf-8"))


def _save_meta(rec_id: str, m: dict) -> None:
    _meta_path(rec_id).write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")


def start(title: str | None, page_url: str | None, source: str | None, live: bool) -> str:
    rec_id = "r" + uuid.uuid4().hex[:15]
    folder(rec_id).mkdir(parents=True)
    site = re.sub(r"^www\.", "", re.sub(r"^\w+://([^/]+).*$", r"\1", page_url or "")) or None
    _save_meta(rec_id, {
        "rec_id": rec_id, "title": title, "page_url": page_url, "site": site,
        "source": source, "live": live, "started_at": store.now(),
    })
    return rec_id


def append(rec_id: str, seg: int, data: bytes) -> int:
    """One chunk onto its segment. Returns the bytes recorded so far, all segments."""
    d = folder(rec_id)
    if not d.is_dir():
        raise ValueError(f"no recording {rec_id}")
    with open(d / f"seg-{int(seg):03d}.webm", "ab") as f:
        f.write(data)
    return sum(p.stat().st_size for p in d.glob("seg-*.webm"))


def audio_path(rec_id: str) -> Path:
    return folder(rec_id) / "audio.webm"


TRANSFER_CHUNK = 384_000  # raw bytes; base64 stays below native messaging limits


def export_chunk(rec_id: str, offset: int) -> dict:
    path = audio_path(rec_id)
    total = path.stat().st_size
    if offset < 0 or offset > total:
        raise ValueError("invalid audio offset")
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(TRANSFER_CHUNK)
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", meta(rec_id).get("title") or rec_id).strip()
    return {"filename": f"{title[:120] or rec_id}.webm", "total": total, "data": data}


def begin_import(filename: str, size: int) -> str:
    name = re.split(r"[\\/]", filename or "")[-1]
    if not name.lower().endswith(".webm"):
        raise ValueError("Choose a .webm recording exported from Transcriber")
    if size <= 0:
        raise ValueError("The recording is empty")
    rec_id = start(name[:-5], None, "Imported recording", False)
    m = meta(rec_id)
    m.update({"import_size": size, "import_filename": name})
    _save_meta(rec_id, m)
    (folder(rec_id) / "import.webm").touch()
    return rec_id


def import_chunk(rec_id: str, offset: int, data: bytes) -> int:
    m = meta(rec_id)
    path = folder(rec_id) / "import.webm"
    if m.get("finished_at") or not path.is_file():
        raise ValueError("No pending import")
    current = path.stat().st_size
    if offset != current or not data or len(data) > TRANSFER_CHUNK:
        raise ValueError("Invalid recording chunk or offset; import the file again")
    if current + len(data) > m["import_size"]:
        raise ValueError("Recording exceeds the declared file size")
    with path.open("ab") as stream:
        stream.write(data)
    return current + len(data)


def finish_import(rec_id: str) -> dict:
    m = meta(rec_id)
    path = folder(rec_id) / "import.webm"
    if m.get("finished_at") or not path.is_file() or path.stat().st_size != m.get("import_size"):
        raise ValueError("Recording import is incomplete")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
         "stream=codec_type", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=30,
    )
    if probe.returncode or not json.loads(probe.stdout).get("streams"):
        raise ValueError("This file does not contain readable recording audio")
    path.replace(audio_path(rec_id))
    m.update({"finished_at": store.now(), "reason": "imported", "segments": 1,
              "duration": duration(audio_path(rec_id)), "bytes": m["import_size"]})
    _save_meta(rec_id, m)
    return m


def duration(path: Path) -> float | None:
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return round(float(p.stdout.strip()), 1)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def finish(rec_id: str, reason: str | None = None) -> dict:
    """Join the segments into audio.webm. Returns the updated meta."""
    d = folder(rec_id)
    segs = sorted(p for p in d.glob("seg-*.webm") if p.stat().st_size > 0)
    if not segs:
        raise ValueError("nothing was recorded")
    out = audio_path(rec_id)
    listing = d / "segments.txt"
    listing.write_text("".join(f"file '{p.name}'\n" for p in segs), encoding="utf-8")
    base = ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing)]
    # Copy first (instant); re-encode only if a segment's header disagrees.
    for codec in (["-c", "copy"], ["-c:a", "libopus", "-b:a", str(BITRATE)]):
        p = subprocess.run(base + codec + [str(out)], capture_output=True, text=True)
        if p.returncode == 0 and out.exists() and out.stat().st_size > 0:
            break
    else:
        raise RuntimeError(f"ffmpeg could not join the segments: {p.stderr.strip()[-300:]}")
    m = meta(rec_id)
    m.update({"finished_at": store.now(), "reason": reason, "segments": len(segs),
              "duration": duration(out), "bytes": out.stat().st_size})
    _save_meta(rec_id, m)
    return m


# --- as a pipeline source (transcribe.py) ------------------------------------

def metadata(rec_id: str) -> dict:
    """What fetch_metadata returns for a video: title, 'channel' = the site."""
    m = meta(rec_id)
    return {
        "video_id": rec_id, "platform": "recording",
        "title": m.get("title") or f"Recording {rec_id}",
        "channel": m.get("site"),
        "duration": int(m["duration"]) if m.get("duration") else None,
        "language": None, "upload_date": None,
    }


def page_url(rec_id: str) -> str | None:
    try:
        return meta(rec_id).get("page_url")
    except (OSError, ValueError):
        return None


def to_wav(rec_id: str, dest: Path) -> Path:
    """16 kHz mono, what whisper.cpp wants — the download step of a recording."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    wav = dest.with_suffix(".wav")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(audio_path(rec_id)),
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)],
        check=True, capture_output=True,
    )
    return wav


def remove(rec_id: str) -> bool:
    """With the video's other files, when it is deleted (store.soft_delete_video)."""
    d = folder(rec_id)
    if d.is_dir():
        shutil.rmtree(d)
        return True
    return False
