"""Remote control: chat with Jarvis and approve posts from your phone via a Telegram bot."""

from __future__ import annotations

import logging
import os
import threading
import time

import requests

log = logging.getLogger("jarvis.telegram")

HELP = """I'm at your service. Just talk to me, or use:
/pending - posts waiting for your approval
/approve <id> or /approve all
/reject <id>
/plan - plan today's content now
/briefing - today's report
/videos - process new videos in the YouTube inbox
/markets - run a paper-trading desk session now
/portfolio - paper portfolio and P&L
/halt, /resume - pause or resume paper trading
/new - start a fresh conversation"""


class TelegramRemote:
    def __init__(self, token: str, owner_chat_id: str | None):
        self.base = f"https://api.telegram.org/bot{token}"
        self.owner_chat_id = str(owner_chat_id) if owner_chat_id else None
        self._offset = None

    @classmethod
    def from_env(cls) -> "TelegramRemote | None":
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        return cls(token, os.environ.get("TELEGRAM_OWNER_CHAT_ID")) if token else None

    def send(self, text: str, chat_id: str | None = None) -> None:
        chat_id = chat_id or self.owner_chat_id
        if not chat_id:
            log.warning("Telegram owner chat id not set; message dropped: %s", text)
            return
        for i in range(0, max(len(text), 1), 4000):  # Telegram's message size limit
            try:
                requests.post(f"{self.base}/sendMessage", json={"chat_id": chat_id, "text": text[i:i + 4000]},
                              timeout=30)
            except requests.RequestException as exc:
                log.warning("Telegram send failed: %s", exc)

    def start(self, handler) -> threading.Thread:
        """Poll for messages in a background thread; handler(text) -> reply text."""
        thread = threading.Thread(target=self._loop, args=(handler,), daemon=True, name="telegram")
        thread.start()
        return thread

    def _loop(self, handler) -> None:
        while True:
            try:
                resp = requests.get(f"{self.base}/getUpdates",
                                    params={"timeout": 30, "offset": self._offset}, timeout=40).json()
            except (requests.RequestException, ValueError) as exc:
                log.warning("Telegram poll failed: %s", exc)
                time.sleep(5)
                continue
            for update in resp.get("result", []):
                self._offset = update["update_id"] + 1
                msg = update.get("message") or {}
                chat_id, text = str(msg.get("chat", {}).get("id", "")), msg.get("text")
                if not text:
                    continue
                if not self.owner_chat_id:
                    self.send(f"Set TELEGRAM_OWNER_CHAT_ID={chat_id} in your .env and restart me.", chat_id)
                    continue
                if chat_id != self.owner_chat_id:
                    continue  # Jarvis only takes orders from the owner
                try:
                    reply = handler(text)
                except Exception as exc:
                    log.exception("handling Telegram message failed")
                    reply = f"Something went wrong: {exc}"
                self.send(reply or "Done.", chat_id)
