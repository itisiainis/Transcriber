"""
channel.py — a channel's videos, listed for picking what to transcribe.

yt-dlp's flat-playlist mode (the --flat-playlist flag; `extract_flat` in the
API) reads the channel's listing pages only: id, title and duration, one
request per page of ~30 videos. No per-video metadata: that is a request per
video and would make a 500-video channel crawl. The real upload date is
filled in when a video is actually transcribed (fetch_metadata).

Listing dates, for sorting only: RuTube's listing carries a timestamp, and
YouTube's carries "3 weeks ago", which yt-dlp turns into an approximate
date when asked (`approximate_date`). Neither is stored.

    python channel.py https://www.youtube.com/@veritasium
    python channel.py https://rutube.ru/channel/40243153/
"""

import re
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse

from yt_dlp import YoutubeDL

import store
import transcribe as tx

CHANNEL_LIMIT = 500         # newest first; a listing past this is cut off, and says so

# Path shapes that are a channel, per platform -> the listing yt-dlp is given.
CHANNEL_PATHS = {
    "youtube": (r"^/(@[^/]+|channel/UC[\w-]{22}|c/[^/]+|user/[^/]+)", "https://www.youtube.com/{}/videos"),
    "rutube": (r"^/(channel/\d+|u/\w+)", "https://rutube.ru/{}/videos/"),
}


def parse_channel(url: str) -> tuple[str, str]:
    """Channel URL -> (platform, the listing URL). Raises ValueError otherwise."""
    url = url.strip()
    u = urlparse(url if "//" in url else "//" + url)
    host = (u.hostname or "").lower()
    for platform, p in tx.PLATFORMS.items():
        if any(host == h or host.endswith("." + h) for h in p["hosts"]):
            pattern, listing = CHANNEL_PATHS[platform]
            m = re.match(pattern, u.path)
            if not m:
                raise ValueError(f"not a channel link: {url}")
            return platform, listing.format(m.group(1))
    raise ValueError(f"unsupported site: {host or url}")


def mark_present(conn, videos: list[dict]) -> list[dict]:
    """`present` = the video has a row that isn't deleted, i.e. it shows in the panel."""
    ids = [v["video_id"] for v in videos]
    present = set()
    for i in range(0, len(ids), 500):       # SQLite's variable limit
        chunk = ids[i:i + 500]
        present |= {r["video_id"] for r in conn.execute(
            f"SELECT video_id FROM videos WHERE deleted_at IS NULL "
            f"AND video_id IN ({','.join('?' * len(chunk))})", chunk)}
    for v in videos:
        v["present"] = v["video_id"] in present
    return videos


def enumerate_channel(url: str, limit: int = CHANNEL_LIMIT) -> dict:
    """
    -> {platform, channel, url, videos: [{platform, video_id, title, duration,
        listed_date, index, present}], truncated}

    `index` is the channel's own order, newest first. `listed_date` is the
    listing's date (approximate on YouTube) or None.
    """
    platform, listing = parse_channel(url)
    opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "extract_flat": "in_playlist",
        "playlistend": limit + 1,           # one past the limit says whether it was cut
        "extractor_args": {"youtubetab": {"approximate_date": [""]}},
    }
    if tx.YTDLP_PROXY:
        opts["proxy"] = tx.YTDLP_PROXY
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(listing, download=False)

    videos = []
    for e in info.get("entries") or []:
        vid = e.get("id")
        if not vid:
            continue
        try:
            # Only ids parse_url accepts: a listing can hold playlists or live rooms.
            p, vid = tx.parse_url(vid)
        except ValueError:
            continue
        ts = e.get("timestamp") or e.get("release_timestamp")
        videos.append({
            "platform": p, "video_id": vid, "title": e.get("title"),
            "duration": int(e["duration"]) if e.get("duration") else None,
            "listed_date": (datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
                            if ts else None),
            "index": len(videos),
        })
    truncated = len(videos) > limit
    videos = videos[:limit]

    if store.DB_PATH.exists():
        conn = store.connect()
        try:
            mark_present(conn, videos)
        finally:
            conn.close()
    else:
        for v in videos:
            v["present"] = False

    name = (info.get("channel") or info.get("uploader") or info.get("title") or "")
    return {
        "platform": platform, "url": listing, "truncated": truncated, "videos": videos,
        "channel": re.sub(r"\s+-\s+Videos$", "", name) or None,
    }


if __name__ == "__main__":
    r = enumerate_channel(sys.argv[1])
    print(f"{r['channel'] or r['url']} · {r['platform']} · {len(r['videos'])} videos"
          + (" (cut off)" if r["truncated"] else ""))
    for v in r["videos"]:
        mark = "✓" if v["present"] else " "
        dur = f"{v['duration'] // 60}:{v['duration'] % 60:02d}" if v["duration"] else "?"
        print(f"{mark} {v['video_id']}  {v['listed_date'] or '—':10}  {dur:>7}  {v['title']}")
