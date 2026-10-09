"""The tools Claude can use to run your social media, with guardrails enforced in code."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Callable

from . import ops
from .config import Settings
from .memory import Memory, iso, utcnow
from .platforms import Platform

log = logging.getLogger("jarvis.tools")

WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}


class ToolError(Exception):
    """Raised for a bad tool call; the message goes back to Claude so it can fix the call."""


def _obj(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


_STR, _INT = {"type": "string"}, {"type": "integer"}

TOOL_DEFS = [
    {
        "name": "list_platforms",
        "description": "List the connected social networks with their character limits, daily post quota, "
                       "and whether each requires an image.",
        "input_schema": _obj({}, []),
    },
    {
        "name": "create_post",
        "description": (
            "Create an original post for ONE platform. Write natively for that platform (length, tone, "
            "hashtags). Depending on the operating mode the post is either queued for the owner's approval "
            "or scheduled to publish automatically. Call once per platform; never post identical text "
            "to several networks."
        ),
        "input_schema": _obj({
            "platform": {**_STR, "description": "One of the names returned by list_platforms."},
            "text": {**_STR, "description": "The exact post text, within the platform's character limit."},
            "publish_at": {**_STR, "description": "'now', or a local date-time like '2026-10-10T09:00' "
                                                  "in the owner's timezone."},
            "media_url": {**_STR, "description": "Optional public image URL. Required for Instagram."},
            "rationale": {**_STR, "description": "One sentence on why this post, shown to the owner."},
            "owner_confirmed": {"type": "boolean", "description": (
                "true ONLY when the owner, in this conversation, explicitly told you to publish or schedule "
                "this specific content. Otherwise omit it.")},
        }, ["platform", "text", "publish_at", "rationale"]),
    },
    {
        "name": "list_posts",
        "description": "List posts and replies Jarvis manages, newest first.",
        "input_schema": _obj({
            "status": {"type": "string", "enum": ["pending_approval", "scheduled", "published", "failed", "rejected"]},
            "platform": _STR,
            "limit": _INT,
        }, []),
    },
    {
        "name": "edit_post",
        "description": "Change the text, image or time of a post that is still pending approval or scheduled.",
        "input_schema": _obj({
            "post_id": _INT, "text": _STR, "media_url": _STR,
            "publish_at": {**_STR, "description": "'now' or a local date-time like '2026-10-10T09:00'."},
        }, ["post_id"]),
    },
    {
        "name": "approve_post",
        "description": "Approve a pending post or reply so it gets published at its scheduled time. "
                       "Only use when the owner tells you to approve.",
        "input_schema": _obj({"post_id": _INT}, ["post_id"]),
    },
    {
        "name": "reject_post",
        "description": "Cancel a pending or scheduled post.",
        "input_schema": _obj({"post_id": _INT, "reason": _STR}, ["post_id"]),
    },
    {
        "name": "list_interactions",
        "description": "List mentions, replies and comments people left for the account.",
        "input_schema": _obj({
            "status": {"type": "string", "enum": ["new", "handled", "ignored", "flagged", "all"]},
            "limit": _INT,
        }, []),
    },
    {
        "name": "reply_to_interaction",
        "description": "Write a reply to a mention or comment (by interaction id). Queued or published "
                       "according to the operating mode.",
        "input_schema": _obj({
            "interaction_id": _INT,
            "text": _STR,
            "owner_confirmed": {"type": "boolean", "description": "Same rule as create_post."},
        }, ["interaction_id", "text"]),
    },
    {
        "name": "mark_interaction",
        "description": "Mark a mention/comment as 'ignored' (spam, nothing to say), 'handled', or 'flagged' "
                       "(needs the owner personally: complaints, press, partnerships, anything sensitive).",
        "input_schema": _obj({
            "interaction_id": _INT,
            "status": {"type": "string", "enum": ["ignored", "handled", "flagged"]},
            "note": _STR,
        }, ["interaction_id", "status"]),
    },
    {
        "name": "get_performance",
        "description": "Engagement stats for published posts over the last N days, best first, plus totals "
                       "per platform. Use it to learn what resonates.",
        "input_schema": _obj({"days": _INT}, []),
    },
    {
        "name": "remember",
        "description": "Save a durable fact or lesson to long-term memory (e.g. owner preferences, what "
                       "content performs well, upcoming launches). Same key overwrites.",
        "input_schema": _obj({"key": _STR, "value": _STR}, ["key", "value"]),
    },
    {
        "name": "forget",
        "description": "Delete a long-term memory entry by key.",
        "input_schema": _obj({"key": _STR}, ["key"]),
    },
    {
        "name": "recall",
        "description": "Read everything in long-term memory.",
        "input_schema": _obj({}, []),
    },
    {
        "name": "notify_owner",
        "description": "Send the owner a short message (their phone via Telegram if set up, otherwise the log). "
                       "Use for flagged mentions, failures, or opportunities that need a human.",
        "input_schema": _obj({"message": _STR}, ["message"]),
    },
]

OWNER_ONLY = {"approve_post"}


class Toolbox:
    def __init__(self, settings: Settings, memory: Memory, platforms: dict[str, Platform],
                 owner_present: bool, notifier: Callable[[str], None] | None = None):
        self.settings = settings
        self.memory = memory
        self.platforms = platforms
        self.owner_present = owner_present
        self.notifier = notifier or (lambda msg: log.warning("NOTIFY OWNER: %s", msg))
        self._replies_this_run = 0

    def definitions(self) -> list[dict]:
        tools = [t for t in TOOL_DEFS if self.owner_present or t["name"] not in OWNER_ONLY]
        return tools + [WEB_SEARCH]

    def execute(self, name: str, tool_input: dict) -> str:
        """Run one tool. Raises ToolError for problems Claude should correct."""
        if name in OWNER_ONLY and not self.owner_present:
            raise ToolError(f"{name} requires the owner")
        handler = getattr(self, f"tool_{name}", None)
        if handler is None or not isinstance(tool_input, dict):
            raise ToolError(f"unknown tool or malformed input: {name}")
        try:
            result = handler(**tool_input)
        except TypeError as exc:
            raise ToolError(f"bad arguments for {name}: {exc}") from exc
        return result if isinstance(result, str) else json.dumps(result, default=str)

    # ---- helpers -----------------------------------------------------------
    def _platform(self, name: str) -> Platform:
        if name not in self.platforms:
            raise ToolError(f"platform {name!r} is not enabled; enabled: {sorted(self.platforms)}")
        return self.platforms[name]

    def _parse_time(self, value: str) -> datetime:
        if value.strip().lower() == "now":
            return utcnow()
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise ToolError(f"publish_at must be 'now' or ISO like 2026-10-10T09:00, got {value!r}") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.settings.tz)
        if dt < utcnow() - timedelta(minutes=5):
            raise ToolError("publish_at is in the past")
        return dt

    def _status_for(self, owner_confirmed: bool | None) -> str:
        if self.owner_present and owner_confirmed:
            return "scheduled"
        if not self.owner_present and self.settings.autopilot:
            return "scheduled"
        return "pending_approval"

    def _publish_if_due(self, post_id: int) -> dict:
        post = self.memory.get_post(post_id)
        if post["status"] == "scheduled" and post["scheduled_at"] <= iso(utcnow()):
            post = ops.publish_post(self.memory, self.platforms, post)
        return self._summarize(post)

    def _summarize(self, post: dict) -> dict:
        out = {k: post[k] for k in ("id", "platform", "status", "text", "url", "error") if post.get(k)}
        if post.get("scheduled_at"):
            out["scheduled_for"] = datetime.fromisoformat(post["scheduled_at"]).astimezone(
                self.settings.tz).strftime("%a %b %d %H:%M %Z")
        return out

    # ---- tools -------------------------------------------------------------
    def tool_list_platforms(self) -> list[dict]:
        return [{"name": n, "max_chars": p.max_chars, "requires_image": p.requires_media,
                 "posts_per_day": self.settings.platforms[n].posts_per_day}
                for n, p in self.platforms.items()]

    def tool_create_post(self, platform: str, text: str, publish_at: str, rationale: str,
                         media_url: str | None = None, owner_confirmed: bool | None = None) -> dict:
        p = self._platform(platform)
        problems = ops.check_text(p, text, self.settings.avoid, media_url)
        if problems:
            raise ToolError("post rejected by guardrails: " + "; ".join(problems))
        when = self._parse_time(publish_at)

        if not self.owner_present:  # daily quota only binds Jarvis acting on its own
            local = when.astimezone(self.settings.tz)
            start = local.replace(hour=0, minute=0, second=0, microsecond=0)
            used = self.memory.count_original_posts_between(platform, start, start + timedelta(days=1))
            quota = self.settings.platforms[platform].posts_per_day
            if used >= quota:
                raise ToolError(f"daily quota reached for {platform} on {local.date()} ({used}/{quota})")

        post_id = self.memory.add_post(platform, text, self._status_for(owner_confirmed),
                                       media_url=media_url, scheduled_at=when, rationale=rationale)
        return self._publish_if_due(post_id)

    def tool_list_posts(self, status: str | None = None, platform: str | None = None, limit: int = 20) -> list[dict]:
        return [self._summarize(p) | ({"rationale": p["rationale"]} if p["rationale"] else {})
                for p in self.memory.list_posts(status, platform, min(limit, 50))]

    def tool_edit_post(self, post_id: int, text: str | None = None, media_url: str | None = None,
                       publish_at: str | None = None) -> dict:
        post = self.memory.get_post(post_id)
        if not post or post["status"] not in ("pending_approval", "scheduled"):
            raise ToolError(f"post {post_id} not found or no longer editable")
        fields = {}
        if text is not None or media_url is not None:
            new_text = text if text is not None else post["text"]
            new_media = media_url if media_url is not None else post["media_url"]
            problems = ops.check_text(self._platform(post["platform"]), new_text, self.settings.avoid,
                                      None if post["reply_to_remote_id"] else new_media)
            if problems:
                raise ToolError("edit rejected by guardrails: " + "; ".join(problems))
            fields.update(text=new_text, media_url=new_media)
        if publish_at is not None:
            fields["scheduled_at"] = iso(self._parse_time(publish_at))
        # Jarvis editing an approved post on its own sends it back for review.
        if not self.owner_present and not self.settings.autopilot:
            fields["status"] = "pending_approval"
        self.memory.update_post(post_id, **fields)
        return self._publish_if_due(post_id)

    def tool_approve_post(self, post_id: int) -> dict:
        post = self.memory.get_post(post_id)
        if not post or post["status"] != "pending_approval":
            raise ToolError(f"post {post_id} is not pending approval")
        fields = {"status": "scheduled"}
        if post["scheduled_at"] and post["scheduled_at"] < iso(utcnow()):
            fields["scheduled_at"] = iso(utcnow())  # missed its slot: go now
        self.memory.update_post(post_id, **fields)
        return self._publish_if_due(post_id)

    def tool_reject_post(self, post_id: int, reason: str | None = None) -> dict:
        post = self.memory.get_post(post_id)
        if not post or post["status"] not in ("pending_approval", "scheduled"):
            raise ToolError(f"post {post_id} is not pending or scheduled")
        self.memory.update_post(post_id, status="rejected", error=reason)
        return {"id": post_id, "status": "rejected"}

    def tool_list_interactions(self, status: str = "new", limit: int = 20) -> list[dict]:
        rows = self.memory.list_interactions(None if status == "all" else status, min(limit, 50))
        return [{k: r[k] for k in ("id", "platform", "author", "kind", "text", "status", "note")} for r in rows]

    def tool_reply_to_interaction(self, interaction_id: int, text: str, owner_confirmed: bool | None = None) -> dict:
        inter = self.memory.get_interaction(interaction_id)
        if not inter:
            raise ToolError(f"interaction {interaction_id} not found")
        if not self.owner_present:
            if not self.settings.auto_reply:
                raise ToolError("auto_reply is disabled in config; flag it for the owner instead")
            if self._replies_this_run >= self.settings.max_replies_per_run:
                raise ToolError("reply limit for this run reached; leave the rest for the next run")
        problems = ops.check_text(self._platform(inter["platform"]), text, self.settings.avoid)
        if problems:
            raise ToolError("reply rejected by guardrails: " + "; ".join(problems))
        self._replies_this_run += 1
        post_id = self.memory.add_post(inter["platform"], text, self._status_for(owner_confirmed),
                                       scheduled_at=utcnow(), reply_to_remote_id=inter["remote_id"],
                                       interaction_id=interaction_id, rationale=f"Reply to {inter['author']}")
        self.memory.set_interaction_status(interaction_id, "handled", note=f"reply post #{post_id}")
        return self._publish_if_due(post_id)

    def tool_mark_interaction(self, interaction_id: int, status: str, note: str | None = None) -> dict:
        if not self.memory.get_interaction(interaction_id):
            raise ToolError(f"interaction {interaction_id} not found")
        self.memory.set_interaction_status(interaction_id, status, note)
        if status == "flagged":
            inter = self.memory.get_interaction(interaction_id)
            self.notifier(f"🚩 {inter['platform']} {inter['author']}: \"{inter['text'][:200]}\""
                          + (f"\nJarvis: {note}" if note else ""))
        return {"id": interaction_id, "status": status}

    def tool_get_performance(self, days: int = 7) -> dict:
        rows = self.memory.performance(utcnow() - timedelta(days=days), limit=50)
        totals: dict[str, dict] = {}
        for r in rows:
            t = totals.setdefault(r["platform"], {"posts": 0, "likes": 0, "reposts": 0, "replies": 0})
            t["posts"] += 1
            for k in ("likes", "reposts", "replies"):
                t[k] += r[k]
        return {"days": days, "totals_by_platform": totals,
                "top_posts": [{k: r[k] for k in ("id", "platform", "text", "likes", "reposts", "replies")}
                              for r in rows[:5]],
                "bottom_posts": [{k: r[k] for k in ("id", "platform", "text", "likes", "reposts", "replies")}
                                 for r in rows[-3:]] if len(rows) > 5 else []}

    def tool_remember(self, key: str, value: str) -> str:
        self.memory.remember(key, value)
        return f"remembered {key!r}"

    def tool_forget(self, key: str) -> str:
        return f"forgot {key!r}" if self.memory.forget(key) else f"nothing stored under {key!r}"

    def tool_recall(self) -> list[dict]:
        return self.memory.notes()

    def tool_notify_owner(self, message: str) -> str:
        self.notifier(message)
        return "sent"
