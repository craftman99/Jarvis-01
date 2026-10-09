"""Common interface every social network adapter implements."""

from __future__ import annotations

import itertools
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

import requests

log = logging.getLogger("jarvis.platforms")


class PlatformError(RuntimeError):
    pass


@dataclass
class PublishResult:
    remote_id: str
    url: str | None = None


@dataclass
class Interaction:
    remote_id: str
    author: str
    text: str
    kind: str = "mention"   # mention | reply | comment


@dataclass
class Metrics:
    likes: int = 0
    reposts: int = 0
    replies: int = 0
    impressions: int = 0


class Platform(ABC):
    name: str = "base"
    max_chars: int = 500
    requires_media: bool = False

    @abstractmethod
    def publish(self, text: str, media_url: str | None = None) -> PublishResult: ...

    @abstractmethod
    def reply(self, remote_id: str, text: str) -> PublishResult: ...

    @abstractmethod
    def fetch_interactions(self) -> list[Interaction]:
        """Recent mentions/comments directed at the account."""

    @abstractmethod
    def fetch_metrics(self, remote_id: str) -> Metrics: ...


def download(url: str, timeout: int = 30) -> bytes:
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.content


class DryRunPlatform(Platform):
    """Pretends to post. Used when dry_run is on or credentials are missing."""

    _ids = itertools.count(1)

    def __init__(self, name: str, max_chars: int = 500, requires_media: bool = False):
        self.name = name
        self.max_chars = max_chars
        self.requires_media = requires_media

    def publish(self, text: str, media_url: str | None = None) -> PublishResult:
        rid = f"dry-{self.name}-{next(self._ids)}"
        log.info("[DRY RUN] %s post %s: %s%s", self.name, rid, text, f" [media: {media_url}]" if media_url else "")
        return PublishResult(remote_id=rid, url=None)

    def reply(self, remote_id: str, text: str) -> PublishResult:
        rid = f"dry-{self.name}-{next(self._ids)}"
        log.info("[DRY RUN] %s reply to %s: %s", self.name, remote_id, text)
        return PublishResult(remote_id=rid)

    def fetch_interactions(self) -> list[Interaction]:
        return []

    def fetch_metrics(self, remote_id: str) -> Metrics:
        return Metrics()
