"""
bench_vad.py — does VAD actually make whisper faster here, and at what cost?

VAD drops non-speech before the encoder sees it. Encode is ~70% of runtime on
this machine, so the saving should scale with how much of the audio is not
speech: a talking-head video may gain nothing, one with music, gaps or long
ad breaks could gain a lot. The risk is that it clips speech at the edges —
so this measures words kept as well as seconds spent.

    python bench_vad.py audio/test2.wav
    python bench_vad.py audio/test2.wav --thresholds 0.3 0.5 0.7

Needs the VAD model once:
    huggingface.co/ggml-org/whisper-vad -> models/ggml-silero-v6.2.0.bin
"""

import argparse
import json
import subprocess
import sys
import time
import wave
from pathlib import Path

import settings
import transcribe as T


def audio_seconds(wav: Path) -> float:
    with wave.open(str(wav)) as w:
        return w.getnframes() / w.getframerate()


def run(wav: Path, vad: float | None, keep: Path) -> dict:
    """One run. vad=None means the current baseline settings."""
    # In-process only: benchmarking must not change the saved settings.
    with settings.override(vad_enabled=vad is not None, vad_threshold=vad):
        t0 = time.perf_counter()
        result = T.transcribe_audio(wav)
        elapsed = time.perf_counter() - t0

    keep.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    text = " ".join(s["text"] for s in result["segments"])
    return {
        "seconds": round(elapsed, 1),
        "words": len(text.split()),
        "segments": len(result["segments"]),
        "confidence": result.get("confidence"),
        "text": text,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", type=Path)
    ap.add_argument("--thresholds", nargs="*", type=float, default=[0.5],
                    help="VAD thresholds to try; higher = more aggressive trimming")
    ap.add_argument("--no-recheck", action="store_true",
                    help="skip the closing baseline re-run (see drift note below)")
    args = ap.parse_args()

    if not args.wav.exists():
        print(f"no such file: {args.wav}", file=sys.stderr)
        return 1
    if not T.whisper_supports("--vad"):
        print("this whisper build has no --vad; nothing to compare", file=sys.stderr)
        return 1
    if not T.WHISPER_VAD_MODEL.exists():
        print(f"missing {T.WHISPER_VAD_MODEL} — get it from "
              "huggingface.co/ggml-org/whisper-vad", file=sys.stderr)
        return 1

    out = Path("bench"); out.mkdir(exist_ok=True)
    length = audio_seconds(args.wav)
    print(f"{args.wav.name} — {length:.0f}s of audio\n")

    rows = []
    base = run(args.wav, None, out / "baseline.json")
    rows.append(("off", base))
    for th in args.thresholds:
        rows.append((f"{th:g}", run(args.wav, th, out / f"vad-{th:g}.json")))

    # The baseline runs on a cold CPU and every VAD run after it on a hot one.
    # On a laptop that throttles, that alone can look like VAD being slower.
    # Re-run the baseline last and measure the drift between the two.
    drift = None
    if not args.no_recheck:
        base2 = run(args.wav, None, out / "baseline-again.json")
        drift = (base2["seconds"] - base["seconds"]) / base["seconds"] * 100
        rows.append(("off*", base2))

    head = f"{'vad':>6} {'time':>8} {'xrt':>7} {'vs base':>9} {'words':>7} {'kept':>7} {'segs':>6} {'conf':>6}"
    print(head)
    print("-" * len(head))
    for name, r in rows:
        xrt = r["seconds"] / length
        faster = "" if name == "off" else f"{(1 - r['seconds'] / base['seconds']) * 100:+.0f}%"
        kept = "" if name == "off" else f"{r['words'] / base['words'] * 100:.0f}%"
        conf = f"{r['confidence']:.2f}" if r["confidence"] is not None else "-"
        print(f"{name:>6} {r['seconds']:>7.1f}s {xrt:>6.2f}x {faster:>9} "
              f"{r['words']:>7} {kept:>7} {r['segments']:>6} {conf:>6}")

    if drift is not None:
        print(f"\noff* is the baseline re-run at the end: {drift:+.0f}% against the first.")
        print("That is your noise floor — a VAD saving smaller than it means nothing.")

    print("\nWords kept is the number that matters: a big speedup with 90% of the")
    print("words is VAD eating speech, not saving time. Transcripts are in bench/")
    print("if you want to diff them:")
    print("  python -c \"import json,difflib,sys;"
          "a=json.load(open('bench/baseline.json'));b=json.load(open('bench/vad-0.5.json'));"
          "print('\\n'.join(difflib.unified_diff("
          "[s['text'] for s in a['segments']],[s['text'] for s in b['segments']],"
          "'baseline','vad',n=1)))\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())