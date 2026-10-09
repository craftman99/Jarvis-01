"""YouTube adapter (YouTube Data API v3): upload videos and Shorts, answer comments, read stats."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .base import Interaction, Metrics, Platform, PlatformError, PublishResult

log = logging.getLogger("jarvis.youtube")

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.force-ssl"]
TOKEN_PATH = Path("data/youtube_token.json")


def authorize(client_secrets: str | None = None) -> None:
    """One-time browser login. Saves a refreshable token to data/youtube_token.json."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets = client_secrets or os.environ.get("YOUTUBE_CLIENT_SECRETS", "client_secret.json")
    if not Path(secrets).exists():
        raise PlatformError(f"OAuth client file not found: {secrets} (see README > YouTube)")
    creds = InstalledAppFlow.from_client_secrets_file(secrets, SCOPES).run_local_server(port=0)
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(creds.to_json())


def _credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN_PATH.exists():
        raise PlatformError("not authorized yet - run `jarvis youtube-auth`")
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
        else:
            raise PlatformError("YouTube token is invalid - run `jarvis youtube-auth` again")
    return creds


class YouTubePlatform(Platform):
    name = "youtube"
    max_chars = 5000          # description length
    requires_media = True

    def __init__(self, privacy: str = "public", category_id: str = "22"):
        from googleapiclient.discovery import build

        self.api = build("youtube", "v3", credentials=_credentials(), cache_discovery=False)
        self.privacy = privacy
        self.category_id = category_id
        channels = self.api.channels().list(part="id", mine=True).execute().get("items", [])
        if not channels:
            raise PlatformError("this Google account has no YouTube channel")
        self.channel_id = channels[0]["id"]

    def upload_video(self, description: str, meta: dict) -> PublishResult:
        from googleapiclient.http import MediaFileUpload

        path = meta["file"]
        if not Path(path).exists():
            raise PlatformError(f"video file not found: {path}")
        title = meta["title"][:100]
        if meta.get("is_short") and "#shorts" not in (title + description).lower():
            description = f"{description}\n\n#Shorts"
        body = {
            "snippet": {"title": title, "description": description, "tags": meta.get("tags", [])[:30],
                        "categoryId": self.category_id},
            "status": {"privacyStatus": self.privacy, "selfDeclaredMadeForKids": False},
        }
        request = self.api.videos().insert(part="snippet,status", body=body,
                                           media_body=MediaFileUpload(path, chunksize=-1, resumable=True))
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status:
                log.info("Uploading %s: %d%%", Path(path).name, int(status.progress() * 100))
        vid = response["id"]
        url = f"https://youtube.com/shorts/{vid}" if meta.get("is_short") else f"https://youtu.be/{vid}"
        return PublishResult(remote_id=vid, url=url)

    def publish(self, text: str, media_url: str | None = None) -> PublishResult:
        raise PlatformError("YouTube posts are video uploads - use create_video_upload")

    def reply(self, remote_id: str, text: str) -> PublishResult:
        body = {"snippet": {"parentId": remote_id, "textOriginal": text}}
        comment = self.api.comments().insert(part="snippet", body=body).execute()
        return PublishResult(remote_id=comment["id"])

    def fetch_interactions(self) -> list[Interaction]:
        resp = self.api.commentThreads().list(part="snippet", allThreadsRelatedToChannelId=self.channel_id,
                                              maxResults=50, order="time", textFormat="plainText").execute()
        out = []
        for thread in resp.get("items", []):
            top = thread["snippet"]["topLevelComment"]
            snip = top["snippet"]
            if snip.get("authorChannelId", {}).get("value") == self.channel_id:
                continue  # our own comment
            out.append(Interaction(remote_id=top["id"], author=snip.get("authorDisplayName", "someone"),
                                   text=snip.get("textOriginal", ""), kind="comment"))
        return out

    def fetch_metrics(self, remote_id: str) -> Metrics:
        items = self.api.videos().list(part="statistics", id=remote_id).execute().get("items", [])
        if not items:
            return Metrics()
        st = items[0]["statistics"]
        return Metrics(likes=int(st.get("likeCount", 0)), replies=int(st.get("commentCount", 0)),
                       impressions=int(st.get("viewCount", 0)))
