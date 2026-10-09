"""The always-on loop: plans content, publishes on time, answers mentions, reports back."""

from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler

from . import ops
from .brain import Conversation, Jarvis
from .tools import Toolbox, ToolError

log = logging.getLogger("jarvis.autopilot")


class Autopilot:
    def __init__(self, jarvis: Jarvis, notifier=None):
        self.jarvis = jarvis
        self.settings = jarvis.settings
        self.memory = jarvis.memory
        self.platforms = jarvis.platforms
        self.notify = notifier or (lambda msg: log.info("NOTIFY: %s", msg))
        self.conversation = Conversation(jarvis)

    # ---- jobs --------------------------------------------------------------
    def plan_day(self) -> str:
        now = datetime.now(self.settings.tz)
        slots = [t for t in self.settings.posting_times if t > now.strftime("%H:%M")]
        if not slots:
            return "No posting slots left today."
        quotas = ", ".join(f"{n}: {self.settings.platforms[n].posts_per_day}/day" for n in self.platforms)
        report = self.jarvis.task(
            f"Plan and create today's content. Quotas - {quotas}. Remaining posting slots today: "
            f"{', '.join(slots)} (date {now.date()}). First review recent performance, memory and what has "
            "already been scheduled today (list_posts), then research timely angles on our topics. Spread "
            "posts across the slots, vary formats, and tailor each post to its network."
        )
        self._nudge_approvals()
        return report

    def publish_due(self) -> None:
        for post in ops.publish_due(self.memory, self.platforms):
            if post["status"] == "failed":
                self.notify(f"⚠️ Post #{post['id']} on {post['platform']} failed: {post['error']}")

    def engage(self) -> str:
        new = ops.sync_interactions(self.memory, self.platforms)
        pending = self.memory.list_interactions("new", limit=50)
        if not pending:
            return "No new mentions."
        if not self.settings.auto_reply:
            if new:
                self.notify(f"💬 {new} new mention(s)/comment(s) are waiting for you.")
            return f"{new} new interactions (auto_reply off)."
        report = self.jarvis.task(
            f"There are {len(pending)} new mentions/comments. Use list_interactions, then for each one: reply "
            "helpfully, mark it ignored if it's spam/noise, or flag it for the owner if it's sensitive."
        )
        self._nudge_approvals()
        return report

    def refresh_metrics(self) -> None:
        n = ops.refresh_metrics(self.memory, self.platforms)
        log.info("Refreshed metrics for %s posts", n)

    def briefing(self) -> str:
        report = self.jarvis.task(
            "Write the owner's end-of-day briefing (under 200 words, plain text, no markdown tables): what "
            "went out today and how it did, notable conversations, anything pending approval or flagged, and "
            "2-3 sharp content ideas for tomorrow. Save any durable lesson with remember."
        )
        self.notify("🗒️ Daily briefing\n\n" + report)
        return report

    def _nudge_approvals(self) -> None:
        pending = self.memory.list_posts("pending_approval", limit=50)
        if pending:
            self.notify(f"📝 {len(pending)} item(s) awaiting your approval.\n\n" + format_pending(pending)
                        + "\n\nReply /approve <id>, /approve all, or tell me what to change.")

    # ---- remote commands (Telegram) ----------------------------------------
    def handle_command(self, text: str) -> str:
        parts = text.strip().split()
        cmd, args = parts[0].lower(), parts[1:]
        owner = Toolbox(self.settings, self.memory, self.platforms, owner_present=True, notifier=self.notify)
        try:
            if cmd in ("/start", "/help"):
                from .telegram import HELP
                return HELP
            if cmd == "/pending":
                return format_pending(self.memory.list_posts("pending_approval", limit=50)) or "Nothing pending."
            if cmd == "/approve" and args:
                ids = ([p["id"] for p in self.memory.list_posts("pending_approval", limit=200)]
                       if args[0] == "all" else [int(a) for a in args])
                return "\n".join(f"#{i}: {owner.execute('approve_post', {'post_id': i})}" for i in ids) or "Nothing pending."
            if cmd == "/reject" and args:
                return owner.execute("reject_post", {"post_id": int(args[0]), "reason": " ".join(args[1:]) or None})
            if cmd == "/plan":
                return self.plan_day()
            if cmd == "/briefing":
                return self.briefing()
            if cmd == "/new":
                self.conversation = Conversation(self.jarvis)
                return "Fresh start. What can I do for you?"
        except (ToolError, ValueError) as exc:
            return f"Couldn't do that: {exc}"
        return self.conversation.send(text)

    # ---- run ---------------------------------------------------------------
    def run_forever(self) -> None:
        s = self.settings
        sched = BlockingScheduler(timezone=s.tz, job_defaults={"coalesce": True, "max_instances": 1,
                                                              "misfire_grace_time": 300})

        def at(hhmm: str) -> dict:
            h, m = hhmm.split(":")
            return {"hour": int(h), "minute": int(m)}

        sched.add_job(self._safe(self.plan_day), "cron", **at(s.plan_content_at), id="plan")
        sched.add_job(self._safe(self.publish_due), "interval", minutes=1, id="publish")
        sched.add_job(self._safe(self.engage), "interval", minutes=s.check_mentions_every_minutes, id="engage")
        sched.add_job(self._safe(self.refresh_metrics), "interval", minutes=s.refresh_metrics_every_minutes,
                      id="metrics")
        sched.add_job(self._safe(self.briefing), "cron", **at(s.daily_briefing_at), id="briefing")
        log.info("Jarvis autopilot online - mode=%s dry_run=%s platforms=%s", s.mode, s.dry_run,
                 ", ".join(self.platforms) or "none")
        self.notify(f"🤖 Jarvis online. Mode: {s.mode}{' (dry run)' if s.dry_run else ''}. "
                    f"Managing: {', '.join(self.platforms) or 'no platforms yet'}.")
        sched.start()

    def _safe(self, fn):
        def wrapper():
            try:
                result = fn()
                if isinstance(result, str):
                    log.info("%s: %s", fn.__name__, result)
            except Exception as exc:
                log.exception("job %s failed", fn.__name__)
                self.notify(f"⚠️ Job {fn.__name__} failed: {exc}")
        wrapper.__name__ = fn.__name__
        return wrapper


def format_pending(posts: list[dict]) -> str:
    lines = []
    for p in posts:
        kind = "reply" if p["reply_to_remote_id"] else "post"
        lines.append(f"#{p['id']} [{p['platform']} {kind}] {p['text']}"
                     + (f"\n   why: {p['rationale']}" if p.get("rationale") else ""))
    return "\n".join(lines)
