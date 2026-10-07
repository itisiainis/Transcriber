"""
run.py — transcribe a video and record it.

    python run.py <url> [--entry page|link] [--prose]

This is the hand-operated version of the tool. Use it daily from now on:
every run is a real row in the db, and the data starts accumulating
before the extension exists.
"""

import argparse
import logging
import sys

import analyze as analyze_mod
import store
from transcribe import transcribe, summary, as_text, as_prose, video_id


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("url")
    ap.add_argument("--entry", default="link", choices=["page", "link"],
                    help="'link' (default) = you pasted a url you hadn't opened; "
                         "'page' = you were already watching it")
    ap.add_argument("--quiet", action="store_true", help="don't print the transcript")
    ap.add_argument("--prose", action="store_true",
                    help="print the transcript as paragraphs instead of one line per segment")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--prompt", help="analyse with a prompt from prompts/")
    g.add_argument("--ask", help="analyse with a one-off question of your own")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    log = logging.getLogger("run")

    # Without an id there's no row to hang a failure on, so bail before the db.
    try:
        vid = video_id(args.url)
    except ValueError as e:
        log.error("failed: %s", e)
        return 1

    store.init()

    with store.connect() as conn:
        expired = store.apply_retention(conn)
        if expired:
            log.info("retention: soft-deleted %d old video(s)", len(expired))

    with store.connect() as conn:
        try:
            result = transcribe(args.url)
        except Exception as e:
            store.record_failure(conn, vid, f"{type(e).__name__}: {e}", args.entry)
            log.error("failed: %s", e)
            return 1

        store.upsert_video(conn, result)
        path = store.save_transcript(result)
        sub_id = store.record_submission(conn, result, args.entry, path)

    head = summary(result)
    print(head, end="\n\n")
    if not args.quiet:
        print(as_prose(result) if args.prose else as_text(result), end="\n\n")
        print(head, end="\n\n")
    print(f"# submission {sub_id} -> {path}")

    if args.prompt or args.ask:
        if args.ask:
            prompt, name = args.ask, "custom"
        else:
            prompts = analyze_mod.list_prompts()
            if args.prompt not in prompts:
                log.error("no prompt '%s'. available: %s",
                          args.prompt, ", ".join(prompts))
                return 1
            prompt = analyze_mod.read_prompt(prompts[args.prompt])["body"]
            name = args.prompt

        log.info("analysing with '%s'...", name)
        analysis_id, text = analyze_mod.analyze(result["video_id"], prompt, name)
        print(f"\n--- analysis ({name}) ---\n")
        print(text)
        print(f"\n# analysis {analysis_id} -> analyses/{result['video_id']}.md")

    return 0


if __name__ == "__main__":
    sys.exit(main())