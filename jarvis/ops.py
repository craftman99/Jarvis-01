"""Mechanical jobs that don't need the AI: publishing, syncing mentions, refreshing metrics."""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta

from .memory import Memory, iso, utcnow
from .platforms import DryRunPlatform, Platform

log = logging.getLogger("jarvis.ops")


_ADDRESS_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b|\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def check_text(platform: Platform, text: str, avoid: list[str], media_url: str | None = None,
               held_assets: set[str] | None = None) -> list[str]:
    """Guardrails applied to everything before it can be queued or published."""
    problems = []
    if _ADDRESS_RE.search(text):
        problems.append("contains a token contract address - Jarvis never promotes tokens on social media")
    cashtags = {t.upper() for t in re.findall(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b", text)}
    shilling = cashtags & {a.upper() for a in (held_assets or set())}
    if shilling:
        problems.append(f"mentions assets we hold ({', '.join(sorted(shilling))}) - no shilling our own bags")
    if not text.strip():
        problems.append("text is empty")
    if len(text) > platform.max_chars:
        problems.append(f"text is {len(text)} chars; {platform.name} allows {platform.max_chars}")
    lowered = text.lower()
    hits = [w for w in avoid if w.strip() and w.lower() in lowered]
    if hits:
        problems.append(f"mentions off-limits topics: {', '.join(hits)}")
    if platform.requires_media and not media_url:
        problems.append(f"{platform.name} posts require a media_url (image)")
    return problems


def publish_post(memory: Memory, platforms: dict[str, Platform], post: dict) -> dict:
    """Publish one queued post (or reply). Returns the updated row."""
    platform = platforms.get(post["platform"])
    if platform is None:
        memory.update_post(post["id"], status="failed", error=f"platform {post['platform']} is not enabled")
        return memory.get_post(post["id"])
    try:
        meta = json.loads(post["meta"]) if post.get("meta") else None
        if post["reply_to_remote_id"]:
            result = platform.reply(post["reply_to_remote_id"], post["text"])
        elif meta and meta.get("file"):
            result = platform.upload_video(post["text"], meta)
        else:
            result = platform.publish(post["text"], post["media_url"])
    except Exception as exc:
        log.exception("Publishing post %s failed", post["id"])
        memory.update_post(post["id"], status="failed", error=str(exc)[:500])
    else:
        memory.update_post(post["id"], status="published", published_at=iso(utcnow()),
                           remote_id=result.remote_id, url=result.url, error=None)
        if post["interaction_id"]:
            memory.set_interaction_status(post["interaction_id"], "handled")
        if meta and meta.get("file") and not isinstance(platform, DryRunPlatform):
            _archive_video(meta["file"])
        log.info("Published post %s on %s %s", post["id"], post["platform"], result.url or result.remote_id)
    return memory.get_post(post["id"])


def _archive_video(path: str) -> None:
    """Move an uploaded video (and its notes file) into <inbox>/uploaded/."""
    from pathlib import Path

    src = Path(path)
    if not src.exists():
        return
    dest = src.parent.parent / "uploaded" if src.parent.name in ("long", "shorts") else src.parent / "uploaded"
    dest.mkdir(parents=True, exist_ok=True)
    for f in (src, src.with_suffix(".txt")):
        if f.exists():
            f.rename(dest / f.name)


VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}


def scan_video_inbox(inbox_dir: str, already_queued: set[str]) -> list[dict]:
    """New videos dropped in <inbox>/long and <inbox>/shorts, with any notes from <name>.txt."""
    from pathlib import Path

    found = []
    for kind in ("long", "shorts"):
        folder = Path(inbox_dir) / kind
        folder.mkdir(parents=True, exist_ok=True)
        for f in sorted(folder.iterdir()):
            if f.suffix.lower() in VIDEO_EXTS and str(f) not in already_queued:
                notes = f.with_suffix(".txt")
                found.append({"file": str(f), "is_short": kind == "shorts",
                              "size_mb": round(f.stat().st_size / 1e6, 1),
                              "owner_notes": notes.read_text()[:2000] if notes.exists() else ""})
    return found


def publish_due(memory: Memory, platforms: dict[str, Platform]) -> list[dict]:
    return [publish_post(memory, platforms, post) for post in memory.due_posts()]


def sync_interactions(memory: Memory, platforms: dict[str, Platform]) -> int:
    """Pull new mentions/comments from every platform into memory. Returns how many are new."""
    new = 0
    for name, platform in platforms.items():
        try:
            items = platform.fetch_interactions()
        except Exception as exc:
            log.warning("Could not fetch interactions from %s: %s", name, exc)
            continue
        for item in items:
            if memory.add_interaction(name, item.remote_id, item.author, item.text, item.kind):
                new += 1
    return new


def refresh_metrics(memory: Memory, platforms: dict[str, Platform], days: int = 7) -> int:
    since = utcnow() - timedelta(days=days)
    updated = 0
    for post in memory.list_posts(status="published", limit=200):
        if post["reply_to_remote_id"] or not post["remote_id"] or (post["published_at"] or "") < iso(since):
            continue
        platform = platforms.get(post["platform"])
        if not platform:
            continue
        try:
            m = platform.fetch_metrics(post["remote_id"])
        except Exception as exc:
            log.debug("metrics for post %s failed: %s", post["id"], exc)
            continue
        memory.save_metrics(post["id"], m.likes, m.reposts, m.replies, m.impressions)
        updated += 1
    return updated
