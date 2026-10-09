"""Bluesky adapter (atproto). Remote ids are post URIs (at://...)."""

from __future__ import annotations

import os

from .base import Interaction, Metrics, Platform, PlatformError, PublishResult, download


class BlueskyPlatform(Platform):
    name = "bluesky"
    max_chars = 300

    def __init__(self):
        from atproto import Client, models

        handle, password = os.environ.get("BLUESKY_HANDLE"), os.environ.get("BLUESKY_APP_PASSWORD")
        if not handle or not password:
            raise PlatformError("missing credentials: BLUESKY_HANDLE, BLUESKY_APP_PASSWORD")
        self.models = models
        self.client = Client()
        self.client.login(handle, password)
        self.handle = handle

    def _url(self, uri: str) -> str:
        return f"https://bsky.app/profile/{self.handle}/post/{uri.rsplit('/', 1)[-1]}"

    def publish(self, text: str, media_url: str | None = None) -> PublishResult:
        if media_url:
            post = self.client.send_image(text=text, image=download(media_url), image_alt=text[:300])
        else:
            post = self.client.send_post(text=text)
        return PublishResult(remote_id=post.uri, url=self._url(post.uri))

    def reply(self, remote_id: str, text: str) -> PublishResult:
        posts = self.client.get_posts([remote_id]).posts
        if not posts:
            raise PlatformError(f"post not found: {remote_id}")
        parent = posts[0]
        parent_ref = self.models.create_strong_ref(parent)
        existing = getattr(parent.record, "reply", None)
        root_ref = existing.root if existing else parent_ref
        post = self.client.send_post(
            text=text, reply_to=self.models.AppBskyFeedPost.ReplyRef(parent=parent_ref, root=root_ref)
        )
        return PublishResult(remote_id=post.uri, url=self._url(post.uri))

    def fetch_interactions(self) -> list[Interaction]:
        resp = self.client.app.bsky.notification.list_notifications()
        out = []
        for n in resp.notifications:
            if n.reason in ("mention", "reply"):  # duplicates are dropped by Memory
                out.append(Interaction(remote_id=n.uri, author="@" + n.author.handle,
                                       text=getattr(n.record, "text", ""), kind=n.reason))
        return out

    def fetch_metrics(self, remote_id: str) -> Metrics:
        posts = self.client.get_posts([remote_id]).posts
        if not posts:
            return Metrics()
        p = posts[0]
        return Metrics(likes=p.like_count or 0, reposts=(p.repost_count or 0) + (p.quote_count or 0),
                       replies=p.reply_count or 0)
