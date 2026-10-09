"""Build the set of platform adapters Jarvis is allowed to use."""

from __future__ import annotations

import logging

from ..config import Settings
from .base import DryRunPlatform, Interaction, Metrics, Platform, PlatformError, PublishResult

log = logging.getLogger("jarvis.platforms")

# name -> (import path, class name, max_chars, requires_media)
_REGISTRY = {
    "x": ("jarvis.platforms.x", "XPlatform", 280, False),
    "bluesky": ("jarvis.platforms.bluesky", "BlueskyPlatform", 300, False),
    "mastodon": ("jarvis.platforms.mastodon", "MastodonPlatform", 500, False),
    "facebook": ("jarvis.platforms.meta", "FacebookPlatform", 5000, False),
    "instagram": ("jarvis.platforms.meta", "InstagramPlatform", 2200, True),
}


def build_platforms(settings: Settings) -> dict[str, Platform]:
    """Real adapters for enabled networks; dry-run stand-ins when dry_run is on or setup fails."""
    import importlib

    platforms: dict[str, Platform] = {}
    for name in settings.enabled_platforms():
        module_path, cls_name, max_chars, requires_media = _REGISTRY[name]
        if settings.dry_run:
            platforms[name] = DryRunPlatform(name, max_chars, requires_media)
            continue
        try:
            cls = getattr(importlib.import_module(module_path), cls_name)
            platforms[name] = cls()
            log.info("Connected to %s", name)
        except Exception as exc:  # missing SDK, bad credentials, network...
            log.warning("Could not connect to %s (%s) - using dry-run mode for it.", name, exc)
            platforms[name] = DryRunPlatform(name, max_chars, requires_media)
    return platforms


__all__ = ["build_platforms", "Platform", "DryRunPlatform", "Interaction", "Metrics",
           "PlatformError", "PublishResult"]
