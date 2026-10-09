"""Facebook Page and Instagram Business adapters (Meta Graph API over HTTPS)."""

from __future__ import annotations

import os

import requests

from .base import Interaction, Metrics, Platform, PlatformError, PublishResult

GRAPH = "https://graph.facebook.com/v21.0"


class _Graph:
    def __init__(self):
        self.token = os.environ.get("META_PAGE_ACCESS_TOKEN")
        if not self.token:
            raise PlatformError("missing credentials: META_PAGE_ACCESS_TOKEN")

    def get(self, path: str, **params) -> dict:
        return self._check(requests.get(f"{GRAPH}/{path}", params={**params, "access_token": self.token}, timeout=30))

    def post(self, path: str, **data) -> dict:
        return self._check(requests.post(f"{GRAPH}/{path}", data={**data, "access_token": self.token}, timeout=60))

    @staticmethod
    def _check(resp: requests.Response) -> dict:
        body = resp.json() if resp.content else {}
        if resp.status_code >= 400 or "error" in body:
            raise PlatformError(f"Graph API error: {body.get('error', {}).get('message', resp.text)}")
        return body


class FacebookPlatform(Platform):
    name = "facebook"
    max_chars = 5000

    def __init__(self):
        self.page_id = os.environ.get("META_PAGE_ID")
        if not self.page_id:
            raise PlatformError("missing credentials: META_PAGE_ID")
        self.graph = _Graph()

    def publish(self, text: str, media_url: str | None = None) -> PublishResult:
        if media_url:
            body = self.graph.post(f"{self.page_id}/photos", url=media_url, caption=text)
            pid = body.get("post_id") or body["id"]
        else:
            pid = self.graph.post(f"{self.page_id}/feed", message=text)["id"]
        return PublishResult(remote_id=pid, url=f"https://www.facebook.com/{pid}")

    def reply(self, remote_id: str, text: str) -> PublishResult:
        cid = self.graph.post(f"{remote_id}/comments", message=text)["id"]
        return PublishResult(remote_id=cid)

    def fetch_interactions(self) -> list[Interaction]:
        feed = self.graph.get(f"{self.page_id}/feed", limit=10,
                              fields="comments.limit(25){id,from,message}")
        out = []
        for post in feed.get("data", []):
            for c in post.get("comments", {}).get("data", []):
                author = c.get("from", {})
                if author.get("id") == self.page_id:
                    continue  # our own replies
                out.append(Interaction(remote_id=c["id"], author=author.get("name", "someone"),
                                       text=c.get("message", ""), kind="comment"))
        return out

    def fetch_metrics(self, remote_id: str) -> Metrics:
        d = self.graph.get(remote_id, fields="reactions.summary(true).limit(0),comments.summary(true).limit(0),shares")
        return Metrics(likes=d.get("reactions", {}).get("summary", {}).get("total_count", 0),
                       replies=d.get("comments", {}).get("summary", {}).get("total_count", 0),
                       reposts=d.get("shares", {}).get("count", 0))


class InstagramPlatform(Platform):
    name = "instagram"
    max_chars = 2200
    requires_media = True

    def __init__(self):
        self.ig_user_id = os.environ.get("META_IG_USER_ID")
        if not self.ig_user_id:
            raise PlatformError("missing credentials: META_IG_USER_ID")
        self.graph = _Graph()

    def publish(self, text: str, media_url: str | None = None) -> PublishResult:
        if not media_url:
            raise PlatformError("Instagram posts need an image (media_url).")
        container = self.graph.post(f"{self.ig_user_id}/media", image_url=media_url, caption=text)["id"]
        mid = self.graph.post(f"{self.ig_user_id}/media_publish", creation_id=container)["id"]
        link = self.graph.get(mid, fields="permalink").get("permalink")
        return PublishResult(remote_id=mid, url=link)

    def reply(self, remote_id: str, text: str) -> PublishResult:
        rid = self.graph.post(f"{remote_id}/replies", message=text)["id"]
        return PublishResult(remote_id=rid)

    def fetch_interactions(self) -> list[Interaction]:
        media = self.graph.get(f"{self.ig_user_id}/media", limit=10, fields="id")
        me = self.graph.get(self.ig_user_id, fields="username").get("username")
        out = []
        for m in media.get("data", []):
            comments = self.graph.get(f"{m['id']}/comments", fields="id,text,username")
            for c in comments.get("data", []):
                if c.get("username") == me:
                    continue
                out.append(Interaction(remote_id=c["id"], author="@" + c.get("username", "someone"),
                                       text=c.get("text", ""), kind="comment"))
        return out

    def fetch_metrics(self, remote_id: str) -> Metrics:
        d = self.graph.get(remote_id, fields="like_count,comments_count")
        return Metrics(likes=d.get("like_count", 0), replies=d.get("comments_count", 0))
