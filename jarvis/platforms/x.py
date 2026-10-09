"""X / Twitter adapter (tweepy, API v2 + v1.1 media upload)."""

from __future__ import annotations

import os
import tempfile

from .base import Interaction, Metrics, Platform, PlatformError, PublishResult, download


class XPlatform(Platform):
    name = "x"
    max_chars = 280

    def __init__(self):
        import tweepy

        keys = {k: os.environ.get(k) for k in
                ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")}
        missing = [k for k, v in keys.items() if not v]
        if missing:
            raise PlatformError(f"missing credentials: {', '.join(missing)}")
        self._tweepy = tweepy
        self.client = tweepy.Client(
            bearer_token=os.environ.get("X_BEARER_TOKEN"),
            consumer_key=keys["X_API_KEY"], consumer_secret=keys["X_API_SECRET"],
            access_token=keys["X_ACCESS_TOKEN"], access_token_secret=keys["X_ACCESS_TOKEN_SECRET"],
        )
        auth = tweepy.OAuth1UserHandler(keys["X_API_KEY"], keys["X_API_SECRET"],
                                        keys["X_ACCESS_TOKEN"], keys["X_ACCESS_TOKEN_SECRET"])
        self.api_v1 = tweepy.API(auth)
        me = self.client.get_me(user_auth=True).data
        self.user_id, self.username = me.id, me.username
        self._since_id = None

    def _url(self, tweet_id: str) -> str:
        return f"https://x.com/{self.username}/status/{tweet_id}"

    def publish(self, text: str, media_url: str | None = None) -> PublishResult:
        media_ids = None
        if media_url:
            with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
                f.write(download(media_url))
                f.flush()
                media_ids = [self.api_v1.media_upload(filename=f.name).media_id]
        resp = self.client.create_tweet(text=text, media_ids=media_ids)
        tid = str(resp.data["id"])
        return PublishResult(remote_id=tid, url=self._url(tid))

    def reply(self, remote_id: str, text: str) -> PublishResult:
        resp = self.client.create_tweet(text=text, in_reply_to_tweet_id=remote_id)
        tid = str(resp.data["id"])
        return PublishResult(remote_id=tid, url=self._url(tid))

    def fetch_interactions(self) -> list[Interaction]:
        resp = self.client.get_users_mentions(
            self.user_id, since_id=self._since_id, max_results=50,
            expansions=["author_id"], user_auth=True,
        )
        users = {u.id: u.username for u in (resp.includes or {}).get("users", [])}
        out = []
        for tweet in resp.data or []:
            out.append(Interaction(remote_id=str(tweet.id), author="@" + users.get(tweet.author_id, "unknown"),
                                   text=tweet.text, kind="mention"))
        if resp.meta and resp.meta.get("newest_id"):
            self._since_id = resp.meta["newest_id"]
        return out

    def fetch_metrics(self, remote_id: str) -> Metrics:
        tweet = self.client.get_tweet(remote_id, tweet_fields=["public_metrics"], user_auth=True).data
        m = (tweet.public_metrics if tweet else None) or {}
        return Metrics(likes=m.get("like_count", 0), reposts=m.get("retweet_count", 0) + m.get("quote_count", 0),
                       replies=m.get("reply_count", 0), impressions=m.get("impression_count", 0))
