# Transcriber

Personal tool: YouTube or RuTube video → transcript → Claude analysis, with a SQLite
log of everything, so the owner can later measure whether it changed his
watching habits.

## Why it exists

The owner had a habit of leaving videos running as background noise. The tool
is meant to turn watching into reading: get the text, ask sharp questions of
it, and decide from the answer whether the video is worth the time. The
logging exists to check whether that actually worked, or whether the tool just
became another way to consume.

## Layout

```
transcribe.py   url -> transcript. Captions if usable, whisper.cpp otherwise.
                Knows nothing about storage.
store.py        SQLite wrapper. Tables in schema.sql; migrate() adds columns
                to an existing db and relocates old flat analysis files.
settings.py     Tunable settings: `settings` table, module constants as
                defaults. get / set / reset / all / override.
analyze.py      Runs a transcript past Claude Code (`claude -p`) with a prompt,
                and resumes that session for follow-ups.
run.py          Wires the two together: transcribe, store, optionally analyse.
channel.py      A channel's video list (yt-dlp flat-playlist), for picking from.
batch.py        Several transcriptions queued as ordinary jobs, with estimates.
export.py       Transcripts out as .txt (combined) or a zip (separate).
jobs.py         Job queue + single-worker lock (own tables: jobs, worker).
worker.py       Runs queued jobs in two lanes (transcribe / analyse), exits
                when idle. Started by host.py, never by hand.
host.py         Native messaging host for the panel. Enqueues and reads only.
host/           host.bat (Chrome's entry point), install.ps1 (registry key).
extension/      MV3 side panel. sidepanel.js = shell + Transcribe tab,
                analyse.js = Analyse tab and its screens, markdown.js =
                renderer for answers, settings.js = settings screen,
                channel.js = a channel's videos to pick from,
                background.js = notifications.
prompts/*.md    Analysis prompts. Filename stem = the name shown when choosing;
                optional frontmatter `description:` is the line under it.
transcripts/    {video_id}.json — overwritten on re-run, no history kept.
analyses/       {video_id}/001-factcheck.md, 002-follow-up.md, …
design/         Screens for the extension (see "The UI" below)
whisper/        whisper.cpp binaries (BLAS CPU build)
models/         ggml-*.bin
```

## Usage

```
python run.py <url> [--entry page|link] [--prose] [--prompt <name> | --ask "..."]
python settings.py                       # every setting and its current value
python analyze.py <url|video_id|last> --prompt factcheck
python analyze.py last --more "why is the hh index a weak measure?"
python analyze.py last --thread
python analyze.py --list
python channel.py <channel url>          # list it; ✓ = already in the panel
python batch.py add <video or channel url>... [--limit N]
python batch.py list | run | clear
python export.py --ids a,b,c [--mode combined|separate] [--out batch.txt]
python export.py --last 3
```

## Decisions already made — don't relitigate these

**Captions first, whisper as fallback.** Captions take ~2s, whisper runs at
about 0.25x realtime on this machine (~15 min per hour of video). The gap is
the whole reason the caption path exists.

**Caption quality is checked with three numbers**, all logged on every
submission even when captions are rejected, so thresholds can later be set
from the actual distribution rather than guesswork:
- `coverage` — do captions reach the end of the video
- `density` — fraction of runtime actually covered, computed by merging
  overlapping intervals (auto-captions scroll and overlap, so a plain sum
  double-counts and yields ~2.0)
- `words_per_min` — volume check against total runtime, catches tracks that
  are well-formed but nearly empty

**Track preference: manual > auto > nothing.** Translated tracks are ignored
entirely — a translation of a machine transcription is two layers of error,
and whisper on the real audio beats it.

**Caption languages are matched on their base code.** Auto-dubbed videos
carry one ASR track per dub language (21 on one test video, Arabic first).
yt-dlp reports the original as `en-US` while the track is `en`, so an exact
match failed and the fallback took the first track — Arabic captions of an
AI dub. `pick_track()` now matches `en-US` to `en`. The `caption_language`
setting ("auto" = the original audio) is tried first; if the video has no
captions in that language, the original audio's are used. It is separate
from `language`, which is only what whisper listens for.

**Hardware: AMD GPU, so no CUDA.** whisper.cpp runs on the BLAS CPU build.
Settings `-t 8 -bo 1 -bs 1` took a test clip from 56s to 26.6s. large-v3-turbo
is *slower* than medium on CPU (bigger encoder, and encode is ~70% of runtime)
despite being the better model. A Vulkan build would flip that, but it has to
be compiled by hand — worth doing later, not now.

**yt-dlp needs `-f "bestaudio[protocol^=http]"`.** The default picks a
fragmented HLS stream that dies under the owner's connection. YouTube media
hosts are unreachable without a VPN from his location.

**Platforms: captions are YouTube-only; everything else goes straight to
whisper.** `transcribe.parse_url(url) -> (platform, video_id)` is the only
place a URL is read; an unknown site raises with the host named. Sites live in
`transcribe.PLATFORMS` (id pattern, the URL yt-dlp gets, download format).
Video ids don't collide across sites (YouTube 11 chars, RuTube 32 hex), so
`video_id` stays the one join key and a bare id says which site it is. A
non-YouTube video records the caption_reason `"platform has no captions"`
with no track and no quality numbers: nothing was tried, so it must never be
counted as a caption rejection when tuning thresholds. RuTube serves only HLS
with audio muxed into the video, so it downloads `worst[acodec!=none]` and
ffmpeg keeps the sound; its HLS is fast without the VPN, slow through it.
`videos.platform` defaults to 'youtube' (every older row was); `upload_date`
(YYYY-MM-DD) is stored from now on and not backfilled.

**A batch is ordinary transcribe jobs, not a second queue.** The task that
asked for batching specced a separate `queue` table; the owner chose instead
"select several videos in the channel's list and transcribe them one by one",
and the existing `jobs` table already is a crash-safe, single-worker queue.
So a batch is jobs with entry_point `channel` (picked in the panel) or
`batch` (batch.py), run by the same worker in the order queued. One whisper at
a time is all this CPU takes. A batch job left `running` by a dead worker is
requeued on the next start, once (`jobs.requeued`); a non-batch one fails as
before, since someone is watching it.

**Estimates use the median realtime_factor, never a constant**
(`batch.estimate_seconds()`): whisper runs only for non-YouTube videos, which
always go through whisper; every run for YouTube, since that is the mix a
YouTube video gets. 0.25 only while there is no history. It is proportional,
so it under-estimates very short videos, where loading the model dominates.

**Channel listings don't fetch per-video metadata.** `--flat-playlist` gives
id, title and duration from the listing pages; a request per video would
make a 500-video channel crawl. The real `upload_date` arrives when a video
is transcribed. For sorting, the listing's own date is shown: RuTube's
timestamp, YouTube's "3 weeks ago" turned into an approximate date (shown
with ≈). Neither is stored.

**Export header.** Every exported transcript starts with this, identical in
combined and separate mode, so a file pulled out of the zip still says what it
is (export.py; the rule is 52 `═`):

```
════════════════════════════════════════════════════
<title>
<channel> · <platform> · <duration mm:ss>
Опубликовано: <upload_date> · Расшифровано: <submitted_at, local>
<url>
════════════════════════════════════════════════════
```

Duration is total minutes (75:03 for 1h15m). `Расшифровано` is the latest
done submission, in local time. A missing value is `—`. Combined is the
default: the usual destination is a claude.ai project, and one file beats
forty. Separate names each file `<upload_date>-<slug>.txt` (`undated-…`
when the date is missing; the slug keeps Cyrillic). Transcripts only, never
analyses.

**Claude Code, not the API.** Runs on the owner's subscription. Web search is
allowed (fact-checking needs it); file and shell tools are removed via
`--tools`, since it has no business touching this folder. The transcript is
untrusted text — the prompt says so explicitly.

**whisper's stderr is read, not discarded.** It carries live progress
(`--print-progress`), the language-detection probability, and the
temperature-fallback count. Non-zero fallbacks mean the decoder struggled on
that audio; a low `lang_p` usually means a mixed-language video. Both are
logged per submission.

**Confidence comes from full JSON (`-ojf`).** Per-token probabilities are
averaged per segment; `LOW_CONF` (0.60) marks the doubtful ones, and
`as_text()` prefixes them with `~` so both a reader and an analysis prompt can
see which words not to lean on. This exists because a fact-check that
confidently verifies a misheard figure is worse than no fact-check — whisper
rendered Онтарио as "Антарио" on a real run. Flag names drift between
whisper.cpp builds, so `whisper_supports()` checks `-h` once and caches;
never add a whisper flag without going through it.

**Prompt and transcript go together on stdin.** Passing the prompt as the `-p`
query and the transcript separately made the model unsure of the task.

**Follow-ups resume, they do not restart.** Each analysis opens a session with
`--session-id <uuid>` and stores it; `--more` runs `claude -p --resume <uuid>`.
The transcript and the previous answer are already in that session, so only
the question is sent. Sessions are per project directory, so this only works
when run from the project root — remember that when the native messaging host
sets its own cwd.

**On Windows, resolve `claude.cmd` explicitly.** npm installs three shims side
by side and `shutil.which` can return the extensionless Unix one, which
Windows can't execute.

## What's logged and why

`videos` holds what's permanently true (platform, title, channel, duration,
upload_date). `entry_point` on a submission is `page`, `link`, `channel` (a
pick from a channel's list) or `batch` (batch.py).
`submissions` holds one row per run: entry point, source, the three caption
numbers, rejection reasons, per-stage timings. `analyses` holds one row per
Claude run — original or follow-up — with `seq`, `session_id`, `parent_id`,
`question`, `duration_s`, `opened_at` and `verdict`.

The point of `opened_at` and `verdict` is that nothing else can capture them.
YouTube's watch history will eventually say what was watched; only this tool
knows whether an analysis was ever read, and whether the video was skipped as
a result. A Google Takeout export of watch history was taken as a
before-baseline; the joins go on `video_id`, and only views *after* a
submission count.

## The UI

Six screens, designed and agreed. Mockups in `design/` — read them before
building any of it; `design/README.md` maps each file to the screen.

**Two tabs, one panel: Transcribe and Analyse.** They share state, so they are
one component, not two pages. Each has its own accent: Transcribe is blue
`#1E6C88`, Analyse is clay `#A8492A` (Claude's colour; the blue is its
complement). The accent, the tab underline and the background tint all
transition over 380 ms when you switch — one `--accent` custom property on the
panel root, transitioned, is how to build it.

**Orange `#D97757` is reserved for "an analysis exists".** Checkmarks on
transcribed rows, and the unread dot. It is the Analyse tab's colour showing up
on the Transcribe side on purpose. Note `#D97757` only makes 3:1 on white, so
it is fine for a glyph and must never carry button text — that is what the
deeper `#A8492A` is for.

**The Transcribe tab** is: a "Transcribe video on page" button showing the
detected title, a link field, worker status, then the list. Rows are named by
video title with channel underneath. In-progress rows keep their place and show
time remaining; failed rows offer Retry. Failures are rows in the db too.

**The Analyse tab lists "Transcribed, not analysed" above "Done".** Running
jobs first, then videos waiting for an analysis, then finished analyses.
Analysing a new video is the common action and must not sit below a growing
history (the mockup has Done first; the owner reversed it after use).

**Choosing a prompt happens on the row, when you press Analyse** — never in a
sheet, a dropdown, or a bar that remembers a selection. Pressing Analyse
expands that video's row into a list: `+ ask` first, then the saved prompts
with their descriptions. There is no "currently selected prompt" anywhere in
the UI, and nothing should reintroduce one.

**`+ ask` expands the same row into a textarea**, under the video's name and
channel so you can see what you are asking about. Enter sends, Shift+Enter
breaks the line. Cancel is one ✕ and nothing navigated.

**The prompt bar is storage, not a picker.** `My prompts · 6` plus `+ add`.
Opening it lists each prompt with its description and how often it has been
used — `never used` is the signal to delete or rewrite it. The add form has
exactly three fields: name (also the filename, also what you pick from the
list), one-line description, and the prompt body.

**A transcript opens as a screen sliding in from the right.** Serif body,
timestamps in a quiet left column — never raw JSON. Its chrome is blue, since
it belongs to the Transcribe side; the single clay element is the Analyse
button, the control that hands the job over.

**A screen's actions sit at the top, never in a bottom bar.** The owner
decided this after using the panel: the Analyse button on a transcript, the
ask field on an analysis, and Cancel/Save on the prompt form all live in the
screen's header, next to back and the other controls. There is no point
travelling to the bottom of the screen for them. (The mockups in `design/`
still show bottom bars; this rule overrides them.) Within the header the
order is: back and the name, then a row of labelled tool buttons under the
name (Copy, Download, Transcript, Delete — Delete pushed to the right, in
red), then the screen's main action (Analyse, or the ask field). Icon-only
buttons squeezed beside the title were tried and replaced.

**Destructive buttons take two presses** (`confirmTwice()` in sidepanel.js):
the first turns the label into the question — "Delete + 2 follow-ups?" — and
the second acts. No dialogs anywhere in the panel.

**Multi-select on the Transcribe list** is entered from the Select button in
the list header or a right-click on a row — never a long-press, which is a
touch idiom and fights text selection. It covers export (one .txt, or a
separate .zip) and delete alike: being able to select several things and do
only one thing with them feels broken. Queued and running rows can't be
ticked. Escape or Done leaves it.

**A channel's videos open as a screen** (channel.js) from the intake button
on a channel page ("Show this channel's videos") or a pasted channel link.
Sortable by date, title and length (press again to reverse); each row shows
its estimate; the batch button carries the count and total ("Transcribe 12
videos · ~4h 20m") and recalculates per tick. Videos already in the panel show
as transcribed, queued or running instead of a checkbox. They are queued in
the order shown.

**Settings** open from the sliders button at the right end of the tab bar.
The screen edits the `settings` table through the host; each change saves
immediately and applies to the next run.

**An open analysis is clay throughout**, with a permanent ask field in the
header. The verdict (Watched / Partly / Skipped) is a compact inline card
at the *end* of the answer, not in the header — it is a one-time action
and should not occupy the bar forever. Keep those three buttons visually
neutral: that column is the skip-rate metric and must not nudge.

**Worth-watching segments always carry a reason.** Timestamp range, its length,
and a sentence on what is in it — a bare timestamp just hands the decision back
to the user. Close with total worth watching against full runtime. The prompts
in `prompts/` enforce this; do not "simplify" it back to a list of timestamps.

**Follow-ups stack under the original as one document**, not a separate chat.
Questions sit right-aligned with the clay rule on that side, so the column
reads as the user's; answers are full-width in the analysis's own serif.

**Analysis is slow** (2–4 min for a fact-check), so it is always a background
job with a notification, never something waited on with the panel open.

## Still to build

1. Both tabs, all six screens, the native host and notifications are built.
   Host protocol: 4-byte little-endian length, then JSON. **Never print to
   stdout** in the host — host.py re-points fd 1 at host.log before any
   import, and replies go through a saved duplicate of it. A command's
   own parameters must never be called `id`: that is the request id.
2. Prompt descriptions: decided — frontmatter with `description:` only.
   The name is the filename stem and nothing else, so they can't disagree.
   `analyze.read_prompt()` strips it; only the body ever reaches Claude.
3. The analysis screen renders free markdown. The mockup's structured cards
   (OK/OFF chips, segment cards) are approximated: verdict words in table
   cells become chips, lists under a "worth watching" heading become cards.
   Asking the prompts for structured output would get closer — not done.
4. Renaming a prompt splits its usage count: history keeps the old name.
5. VAD is wired but off (`WHISPER_VAD = False`). It needs
   `models/ggml-silero-v6.2.0.bin` from huggingface.co/ggml-org/whisper-vad.
   `bench_vad.py` times it against the baseline and reports words kept —
   turn it on only if a real video shows a saving without losing words.
6. Estimates are proportional to length only. A fixed per-video term
   (model load, ~10 s) would fix short videos; the timings to fit it are in
   `submissions`.
7. Feed the low-confidence spans into the analysis prompt, so `factcheck`
   hedges on figures whisper was unsure of. The data is there; nothing
   consumes it yet.

## Conventions

- Timestamps stored as UTC ISO-8601 with offset; local hour derived at
  analysis time.
- **Delete files, not rows.** Every delete — the panel's Delete buttons,
  the Remove on a failed row, `retention_days` — is a *soft* delete
  (`store.soft_delete_video()` / `soft_delete_analysis()`): it sets
  `deleted_at` and removes the transcript and analysis files, and the
  `videos` / `submissions` / `analyses` rows stay. Those rows are the
  behavioural record the project exists to build; deleting them punches
  holes in the before/after comparison, and it would happen exactly on the
  videos that were skipped — the interesting half of the data. So: reads
  that serve the UI or CLI (`store.recent/thread/last_video_id/
  latest_analysis`, and host.py's own list queries) filter
  `deleted_at IS NULL`; anything used for metrics (the notebook, the
  Takeout join) must not. Deleting an analysis takes its follow-ups with it
  and the UI says how many before the second press. Submitting a deleted
  video again clears its `deleted_at`. `store.purge()` is the only hard
  delete and is deliberately wired to nothing.
- **Settings live in the database, not in constants.** Anything a user might
  tune (thresholds, whisper model/threads/language, VAD, analysis language,
  retention) is read through `settings.get()` / `settings.all()`, which fall
  back to the module constant when the `settings` table has no row for it.
  The constants at the top of each module (marked `[setting]`) are therefore
  *defaults only* — reading `transcribe.MIN_COVERAGE` directly is a bug,
  because the panel will have changed the value in the db and the CLI would
  silently disagree. Read at call time, never at import: the panel changes a
  setting between two runs and the second run must see it. Scripts that try
  values without saving them (bench_vad.py) use `settings.override()`.
  New tunables: add the constant, then a row in `settings.SPEC` with a
  validator — `set()` rejects bad values before they reach a run.
- Constants that are not user settings (paths, whisper beam/best-of, the
  yt-dlp format) still live at the top of their module.
- `transcribe_audio()` in transcribe.py is the seam for swapping the whisper
  backend — change its body, nothing else.
- **schema.sql holds tables only. Every CREATE INDEX lives in
  `store.migrate()`**, in the `INDEXES` list, and runs after the ALTER TABLE
  loop. schema.sql is executed before migrate(), so an index naming a column
  that migrate() adds (`parent_id`) fails on every database created earlier.
  Same for any future CHECK, view or trigger over a migrated column.
- Don't verify by re-reading files you just wrote; the tools error on failure.