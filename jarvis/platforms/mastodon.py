"""Mastodon adapter (Mastodon.py)."""

from __future__ import annotations

import html
import os
import re

from .base import Interaction, Metrics, Platform, PlatformError, PublishResult, download

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", text or "")).strip()


class MastodonPlatform(Platform):
    name = "mastodon"
    max_chars = 500

    def __init__(self):
        from mastodon import Mastodon

        base, token = os.environ.get("MASTODON_API_BASE_URL"), os.environ.get("MASTODON_ACCESS_TOKEN")
        if not base or not token:
            raise PlatformError("missing credentials: MASTODON_API_BASE_URL, MASTODON_ACCESS_TOKEN")
        self.api = Mastodon(access_token=token, api_base_url=base)
        self._since_id = None

    def publish(self, text: str, media_url: str | None = None) -> PublishResult:
        media_ids = None
        if media_url:
            media = self.api.media_post(download(media_url), mime_type="image/jpeg", description=text[:400])
            media_ids = [media["id"]]
        status = self.api.status_post(text, media_ids=media_ids)
        return PublishResult(remote_id=str(status["id"]), url=status.get("url"))

    def reply(self, remote_id: str, text: str) -> PublishResult:
        parent = self.api.status(remote_id)
        status = self.api.status_post(f"@{parent['account']['acct']} {text}", in_reply_to_id=remote_id)
        return PublishResult(remote_id=str(status["id"]), url=status.get("url"))

    def fetch_interactions(self) -> list[Interaction]:
        notes = self.api.notifications(types=["mention"], since_id=self._since_id, limit=40)
        out = []
        for n in notes:
            status = n.get("status")
            if status:
                out.append(Interaction(remote_id=str(status["id"]), author="@" + n["account"]["acct"],
                                       text=_strip_html(status["content"]), kind="mention"))
        if notes:
            self._since_id = notes[0]["id"]
        return out

    def fetch_metrics(self, remote_id: str) -> Metrics:
        s = self.api.status(remote_id)
        return Metrics(likes=s.get("favourites_count", 0), reposts=s.get("reblogs_count", 0),
                       replies=s.get("replies_count", 0))
