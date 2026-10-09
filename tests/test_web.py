from collections import deque
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from jarvis.autopilot import Autopilot
from jarvis.brain import Jarvis
from jarvis.config import PlatformSettings, Settings
from jarvis.markets.data import Quote
from jarvis.markets.trading import Trader, asset_key
from jarvis.memory import Memory, utcnow
from jarvis.platforms import DryRunPlatform
from jarvis.web.server import create_app, make_feed_notifier


class FakeClient:
    def __init__(self):
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        return SimpleNamespace(stop_reason="end_turn", usage=None,
                               content=[SimpleNamespace(type="text", text="Right away, Boss.")])


def build(tmp_path, password="hunter2"):
    settings = Settings(timezone="UTC", youtube_inbox_dir=str(tmp_path / "videos"))
    settings.platforms = {"x": PlatformSettings(True, 3), "youtube": PlatformSettings(True, 1)}
    memory = Memory(":memory:")
    feed = deque(maxlen=50)
    notify = make_feed_notifier(feed)
    prices = {"stock:AAPL": 100.0}
    trader = Trader(settings, memory, notify, quote_fn=lambda m, a, c: Quote(m, a, a, prices[asset_key(m, a)]))
    platforms = {"x": DryRunPlatform("x", 280), "youtube": DryRunPlatform("youtube", 5000, True)}
    jarvis = Jarvis(settings, memory, platforms, notifier=notify, client=FakeClient(), trader=trader)
    ap = Autopilot(jarvis, notifier=notify)
    app = create_app(ap, password, feed, run_scheduler=False)
    return TestClient(app), memory, trader


@pytest.fixture
def web(tmp_path):
    client, memory, trader = build(tmp_path)
    assert client.post("/api/login", json={"password": "hunter2"}).status_code == 200
    return client, memory, trader


def test_requires_login(tmp_path):
    client, _, _ = build(tmp_path)
    assert client.get("/api/status").status_code == 401
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    assert client.get("/api/status").status_code == 401


def test_no_password_mode_is_open(tmp_path):
    client, _, _ = build(tmp_path, password=None)
    assert client.get("/api/status").status_code == 200


def test_status_and_dashboard(web):
    client, memory, _ = web
    memory.add_post("x", "draft", "pending_approval", scheduled_at=utcnow())
    s = client.get("/api/status").json()
    assert s["counts"]["pending"] == 1 and s["youtube"] and s["trading"]["cash_usd"] == 1000
    assert "JARVIS" in client.get("/").text


def test_approve_edit_reject(web):
    client, memory, _ = web
    a = memory.add_post("x", "first", "pending_approval", scheduled_at=utcnow())
    b = memory.add_post("x", "second", "pending_approval", scheduled_at=utcnow())
    assert client.patch(f"/api/posts/{a}", json={"text": "first, edited"}).json()["text"] == "first, edited"
    assert client.post(f"/api/posts/{a}/approve").json()["status"] == "published"
    assert client.post(f"/api/posts/{b}/reject").json()["status"] == "rejected"
    r = client.patch(f"/api/posts/{a}", json={"text": "too late"})
    assert r.status_code == 400 and "no longer editable" in r.json()["detail"]
    assert client.patch(f"/api/posts/{b}", json={"text": "x" * 300}).status_code == 400


def test_chat_and_commands(web):
    client, _, _ = web
    assert client.post("/api/chat", json={"message": "hello"}).json()["reply"] == "Right away, Boss."
    assert "PAPER portfolio" in client.post("/api/chat", json={"message": "/portfolio"}).json()["reply"]


def test_inbox_reply(web):
    client, memory, _ = web
    iid = memory.add_interaction("x", "t1", "@fan", "great post", "mention")
    assert client.get("/api/interactions").json()[0]["author"] == "@fan"
    assert client.post(f"/api/interactions/{iid}/reply", json={"text": "thanks!"}).json()["status"] == "published"
    assert client.get("/api/interactions").json() == []


def test_video_upload(web, tmp_path):
    client, _, _ = web
    r = client.post("/api/videos", data={"kind": "shorts", "notes": "desk tour"},
                    files={"file": ("../../My Clip!.mp4", b"fake-bytes", "video/mp4")})
    assert r.status_code == 200
    saved = tmp_path / "videos" / "shorts" / "My-Clip-.mp4"
    assert saved.exists() and saved.with_suffix(".txt").read_text() == "desk tour"
    inbox = client.get("/api/videos").json()["inbox"]
    assert inbox[0]["is_short"] and inbox[0]["owner_notes"] == "desk tour"
    bad = client.post("/api/videos", data={"kind": "long"}, files={"file": ("x.exe", b"x", "application/x")})
    assert bad.status_code == 400
    assert any("New shorts video" in a["message"] for a in client.get("/api/activity").json())


def test_markets(web):
    client, _, trader = web
    trader.place("stock", "AAPL", "buy", 25, "test")
    pf = client.get("/api/portfolio").json()
    assert pf["positions"][0]["symbol"] == "AAPL" and pf["limits"]["max_usd_per_trade"] == 50
    assert client.get("/api/trades").json()[0]["side"] == "buy"
    assert client.post("/api/trading/halt", json={"halted": True}).json()["halted"] is True
    assert trader.halted


def test_run_job(web):
    client, _, _ = web
    assert client.post("/api/run/briefing").json()["result"] == "Right away, Boss."
    assert client.post("/api/run/nope").status_code == 404
