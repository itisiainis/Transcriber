"""
transcribe_file.py — a local media file in, a readable .txt out.

Standalone: nothing from the Transcriber project is imported, but it expects
whisper/ and models/ in the same folder, so drop it next to them.

    python transcribe_file.py interview.mkv
    python transcribe_file.py interview.webm --lang ru --diarize
    python transcribe_file.py talk.mp4 --model models/ggml-large-v3-turbo-q5_0.bin

Takes anything ffmpeg can read (.webm, .mkv, .mp4, .m4a, .opus …). Expect
roughly a quarter of the running time on a CPU build, so an hour of audio is
about 15 minutes.

Speaker labels need pyannote, which is a separate install — see --diarize
below. Without it you still get timestamps and paragraphs.
"""

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

WHISPER_EXE = Path("whisper/whisper-cli.exe")
WHISPER_MODEL = Path("models/ggml-medium-q5_0.bin")
THREADS = 8

PARA_GAP = 2.0      # a silence this long starts a new paragraph
PARA_MAX = 700      # characters, then break at the next sentence end

# pyannote's pipeline name; check huggingface.co/pyannote if this 404s,
# the version moves from time to time.
DIARIZE_MODEL = "pyannote/speaker-diarization-3.1"


def to_wav(src: Path, dest: Path) -> Path:
    """16 kHz mono — what whisper.cpp wants. -map picks the first audio
    track, since an .mkv often carries several."""
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(src), "-map", "0:a:0",
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(dest)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return dest


def run_whisper(wav: Path, model: Path, lang: str, on_progress=None,
                max_len: int | None = None) -> dict:
    # Build the base by name, never with with_suffix(): a name like
    # "clip.16k.wav" has two suffixes and pathlib has not always agreed on
    # what stripping them means.
    out_base = wav.with_name(wav.stem)
    started = time.time()
    cmd = [
        str(WHISPER_EXE), "-m", str(model), "-f", str(wav),
        "-oj", "-of", str(out_base),
        "-l", lang, "-t", str(THREADS), "-bs", "1", "-bo", "1",
        "--print-progress",
    ]
    if max_len:
        # Finer segments. Speakers are assigned per segment, so a segment
        # that straddles a speaker change can only get one label — cutting
        # shorter is what buys accurate turn boundaries. --split-on-word
        # keeps the cut off the middle of a word.
        cmd += ["-ml", str(max_len), "--split-on-word"]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace", bufsize=1)
    pct_re = re.compile(r"progress\s*=\s*(\d+)\s*%")
    last = -1
    for line in proc.stderr:
        m = pct_re.search(line)
        if m and on_progress:
            pct = int(m.group(1))
            if pct != last:
                last = pct
                on_progress(pct)
    if proc.wait() != 0:
        raise RuntimeError(f"whisper exited {proc.returncode}")

    expected = Path(str(out_base) + ".json")
    if not expected.exists():
        # Builds differ on what they append to -of. Take any json this run
        # just produced rather than guessing again.
        fresh = [p for p in wav.parent.glob("*.json")
                 if p.stat().st_mtime >= started - 1]
        if not fresh:
            raise FileNotFoundError(
                f"whisper finished but wrote no json next to {wav.name}.\n"
                f"Looked for {expected.name}. Files there: "
                + ", ".join(sorted(p.name for p in wav.parent.iterdir())[:20])
            )
        expected = max(fresh, key=lambda p: p.stat().st_mtime)

    return load_whisper_json(expected)


def load_whisper_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "language": data["result"]["language"],
        "segments": [
            {
                "start": s["offsets"]["from"] / 1000,
                "end": s["offsets"]["to"] / 1000,
                "text": s["text"].strip(),
            }
            for s in data["transcription"] if s["text"].strip()
        ],
    }


def diarize(wav: Path, token: str) -> list:
    """
    Returns [(start, end, "SPEAKER_00"), …]. pyannote works on the waveform,
    not the words, so the language is irrelevant to it.
    """
    from pyannote.audio import Pipeline
    pipeline = Pipeline.from_pretrained(DIARIZE_MODEL, use_auth_token=token)
    turns = pipeline(str(wav))
    return [(t.start, t.end, spk) for t, _, spk in turns.itertracks(yield_label=True)]


def assign_speakers(segments: list, turns: list) -> None:
    """
    Whisper's segments and pyannote's turns are cut on different boundaries,
    so give each segment the speaker it overlaps with most.
    """
    for seg in segments:
        best, best_overlap = None, 0.0
        for start, end, spk in turns:
            overlap = min(seg["end"], end) - max(seg["start"], start)
            if overlap > best_overlap:
                best, best_overlap = spk, overlap
        seg["speaker"] = best


def label_map(segments: list) -> dict:
    """SPEAKER_00 → «Спикер 1», numbered by who talks first."""
    order, n = {}, 0
    for seg in segments:
        spk = seg.get("speaker")
        if spk and spk not in order:
            n += 1
            order[spk] = f"Спикер {n}"
    return order


def paragraphs(segments: list, names: dict,
               gap: float = PARA_GAP, max_chars: int = PARA_MAX) -> list:
    """
    Merge segments into paragraphs. A paragraph ends on a real pause, at a
    sentence end once it gets long, or whenever the speaker changes — a
    change of speaker is always a new paragraph in an interview.
    """
    out, cur, started, speaker = [], [], None, None

    def flush():
        if cur:
            m, s = divmod(int(started), 60)
            who = f"{names[speaker]}: " if speaker in names else ""
            out.append(f"[{m:02d}:{s:02d}] {who}" + " ".join(cur))

    for i, seg in enumerate(segments):
        if started is None:
            started, speaker = seg["start"], seg.get("speaker")
        cur.append(seg["text"])

        nxt = segments[i + 1] if i + 1 < len(segments) else None
        pause = (nxt["start"] - seg["end"]) if nxt else 0
        changed = bool(nxt) and nxt.get("speaker") != speaker
        size = sum(len(t) for t in cur)
        sentence_end = seg["text"].endswith((".", "!", "?", "…", '."', '?"', '!"'))
        # With short segments a sentence end is rare, so without the hard cap
        # a paragraph could run on indefinitely waiting for a full stop.
        too_long = size > max_chars * 2

        if (nxt is None or changed or pause >= gap
                or (size > max_chars and sentence_end) or too_long):
            flush()
            cur, started, speaker = [], None, None

    flush()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("media", type=Path)
    ap.add_argument("--lang", default="ru",
                    help="forced language code, or 'auto' (default: ru)")
    ap.add_argument("--model", type=Path, default=WHISPER_MODEL)
    ap.add_argument("--out", type=Path, help="default: <media>.txt")
    ap.add_argument("--diarize", action="store_true", help="label speakers (needs pyannote)")
    ap.add_argument("--hf-token", help="Hugging Face token for pyannote")
    ap.add_argument("--keep-wav", action="store_true")
    ap.add_argument("--json", type=Path,
                    help="skip whisper and use an existing whisper json")
    ap.add_argument("--max-len", type=int, metavar="CHARS",
                    help="cut whisper's own segments at this length "
                         "(needs a re-run; 40-80 suits diarization)")
    ap.add_argument("--gap", type=float, default=PARA_GAP, metavar="SEC",
                    help=f"silence that starts a new paragraph (default {PARA_GAP})")
    ap.add_argument("--max-chars", type=int, default=PARA_MAX,
                    help=f"paragraph size before breaking (default {PARA_MAX})")
    args = ap.parse_args()

    if not args.media.exists():
        print(f"no such file: {args.media}", file=sys.stderr)
        return 1
    if not args.model.exists():
        print(f"no such model: {args.model}", file=sys.stderr)
        return 1

    wav = args.media.with_name(args.media.stem + "_16k.wav")
    out = args.out or args.media.with_name(args.media.stem + ".txt")

    t0 = time.perf_counter()
    ok = False
    if args.json:
        print(f"reusing {args.json.name}", file=sys.stderr)
    else:
        print("extracting audio…", file=sys.stderr)
        to_wav(args.media, wav)

    try:
        def show(pct):
            print(f"\r  whisper {pct:3d}%", end="", file=sys.stderr, flush=True)

        if args.json:
            result = load_whisper_json(args.json)
        else:
            result = run_whisper(wav, args.model, args.lang, show, args.max_len)
            print(file=sys.stderr)
        ok = True

        names = {}
        if args.diarize:
            token = args.hf_token
            if not token:
                import os
                token = os.environ.get("HF_TOKEN")
            if not token:
                print("--diarize needs --hf-token or HF_TOKEN in the environment",
                      file=sys.stderr)
                return 1
            print("finding speakers…", file=sys.stderr)
            try:
                turns = diarize(wav, token)
            except ImportError:
                print("pyannote.audio is not installed — see the note at the top",
                      file=sys.stderr)
                return 1
            assign_speakers(result["segments"], turns)
            names = label_map(result["segments"])
            print(f"  {len(names)} speaker(s)", file=sys.stderr)
    finally:
        # Keep the wav when something failed — redoing an hour of ffmpeg and
        # whisper to chase a bug is not a good trade.
        if ok and not args.keep_wav:
            wav.unlink(missing_ok=True)
        elif not ok and wav.exists():
            print(f"kept {wav.name} so you can retry without re-extracting",
                  file=sys.stderr)

    last = result["segments"][-1]["end"] if result["segments"] else 0
    took = time.perf_counter() - t0
    header = [
        f"# {args.media.name}",
        f"# {int(last // 60)}:{int(last % 60):02d}"
        f" · {result['language']} · {args.model.stem}"
        + (f" · {len(names)} speakers" if names else ""),
        f"# transcribed in {took / 60:.1f} min ({took / last:.2f}x realtime)" if last else "",
        "",
    ]
    body = paragraphs(result["segments"], names, args.gap, args.max_chars)
    out.write_text("\n".join(h for h in header if h) + "\n" + "\n\n".join(body) + "\n",
                   encoding="utf-8")

    print(f"{out}  —  {len(body)} paragraphs, {took / 60:.1f} min", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())