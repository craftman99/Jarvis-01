from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from jarvis import ops
from jarvis.autopilot import Autopilot
from jarvis.brain import Conversation, Jarvis
from jarvis.config import PlatformSettings, Settings
from jarvis.memory import Memory, utcnow
from jarvis.platforms import DryRunPlatform, Interaction
from jarvis.tools import Toolbox, ToolError


def make_settings(**overrides) -> Settings:
    s = Settings(timezone="UTC", avoid=["politics"], **overrides)
    s.platforms = {"x": PlatformSettings(True, 2), "instagram": PlatformSettings(True, 1)}
    return s


@pytest.fixture
def env():
    settings = make_settings()
    memory = Memory(":memory:")
    platforms = {"x": DryRunPlatform("x", 280), "instagram": DryRunPlatform("instagram", 2200, True)}
    return settings, memory, platforms


def future(hours=2) -> str:
    return (datetime.now(tz=utcnow().tzinfo) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M")


def test_review_mode_queues_for_approval(env):
    settings, memory, platforms = env
    tb = Toolbox(settings, memory, platforms, owner_present=False)
    post = tb.tool_create_post("x", "Ship small, ship often.", future(), "consistency theme")
    assert post["status"] == "pending_approval"


def test_autopilot_publishes_immediately_when_now(env):
    settings, memory, platforms = env
    settings.mode = "autopilot"
    tb = Toolbox(settings, memory, platforms, owner_present=False)
    post = tb.tool_create_post("x", "Hello world", "now", "test")
    assert post["status"] == "published"


def test_owner_confirmed_only_counts_when_owner_present(env):
    settings, memory, platforms = env
    jarvis_alone = Toolbox(settings, memory, platforms, owner_present=False)
    assert jarvis_alone.tool_create_post("x", "a", "now", "r", owner_confirmed=True)["status"] == "pending_approval"
    owner = Toolbox(settings, memory, platforms, owner_present=True)
    assert owner.tool_create_post("x", "b", "now", "r", owner_confirmed=True)["status"] == "published"


def test_guardrails(env):
    settings, memory, platforms = env
    tb = Toolbox(settings, memory, platforms, owner_present=True)
    with pytest.raises(ToolError, match="280"):
        tb.tool_create_post("x", "x" * 281, "now", "r")
    with pytest.raises(ToolError, match="off-limits"):
        tb.tool_create_post("x", "Let's talk Politics", "now", "r")
    with pytest.raises(ToolError, match="image"):
        tb.tool_create_post("instagram", "caption", "now", "r")
    with pytest.raises(ToolError, match="not enabled"):
        tb.tool_create_post("tiktok", "hi", "now", "r")
    with pytest.raises(ToolError, match="past"):
        tb.tool_create_post("x", "hi", "2020-01-01T09:00", "r")


def test_daily_quota_applies_to_jarvis_only(env):
    settings, memory, platforms = env
    tb = Toolbox(settings, memory, platforms, owner_present=False)
    when = future(1)
    tb.tool_create_post("x", "one", when, "r")
    tb.tool_create_post("x", "two", when, "r")
    with pytest.raises(ToolError, match="quota"):
        tb.tool_create_post("x", "three", when, "r")
    Toolbox(settings, memory, platforms, owner_present=True).tool_create_post("x", "owner's extra", when, "r")


def test_approve_requires_owner_and_publishes(env):
    settings, memory, platforms = env
    post = Toolbox(settings, memory, platforms, owner_present=False).tool_create_post("x", "hi", "now", "r")
    with pytest.raises(ToolError):
        Toolbox(settings, memory, platforms, owner_present=False).execute("approve_post", {"post_id": post["id"]})
    owner = Toolbox(settings, memory, platforms, owner_present=True)
    assert '"published"' in owner.execute("approve_post", {"post_id": post["id"]})


def test_interactions_sync_dedupe_and_reply(env):
    settings, memory, platforms = env
    settings.mode = "autopilot"
    platforms["x"].fetch_interactions = lambda: [Interaction("t1", "@fan", "love this!")]
    assert ops.sync_interactions(memory, platforms) == 1
    assert ops.sync_interactions(memory, platforms) == 0  # already known
    inter = memory.list_interactions("new")[0]
    tb = Toolbox(settings, memory, platforms, owner_present=False)
    reply = tb.tool_reply_to_interaction(inter["id"], "Thank you!")
    assert reply["status"] == "published"
    assert memory.get_interaction(inter["id"])["status"] == "handled"


def test_flagging_notifies_owner(env):
    settings, memory, platforms = env
    sent = []
    iid = memory.add_interaction("x", "t9", "@reporter", "Can I interview you?", "mention")
    Toolbox(settings, memory, platforms, False, notifier=sent.append).tool_mark_interaction(iid, "flagged", "press")
    assert sent and "@reporter" in sent[0]


def test_failed_publish_is_recorded(env):
    settings, memory, platforms = env

    def boom(text, media_url=None):
        raise RuntimeError("rate limited")

    platforms["x"].publish = boom
    pid = memory.add_post("x", "hi", "scheduled", scheduled_at=utcnow())
    [post] = ops.publish_due(memory, platforms)
    assert post["id"] == pid and post["status"] == "failed" and "rate limited" in post["error"]


def test_memory_notes_and_performance(env):
    _, memory, _ = env
    memory.remember("Best Time", "mornings")
    assert memory.notes()[0]["key"] == "best time"
    pid = memory.add_post("x", "hit", "published")
    memory.update_post(pid, published_at=utcnow().isoformat(timespec="seconds"), remote_id="r1")
    memory.save_metrics(pid, likes=10, reposts=2, replies=1)
    assert memory.performance(utcnow() - timedelta(days=1))[0]["score"] == 17


# ---- agent loop with a scripted fake Claude --------------------------------

def block(**kw):
    return SimpleNamespace(**kw)


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def test_agent_loop_runs_tools_and_returns_text(env):
    settings, memory, platforms = env
    client = FakeClient([
        block(stop_reason="tool_use", usage=None, content=[
            block(type="tool_use", id="tu1", name="create_post",
                  input={"platform": "x", "text": "Monday motivation", "publish_at": future(), "rationale": "r"}),
            block(type="tool_use", id="tu2", name="create_post",
                  input={"platform": "x", "text": "politics!", "publish_at": "now", "rationale": "r"}),
        ]),
        block(stop_reason="end_turn", usage=None, content=[block(type="text", text="Queued 1 post, sir.")]),
    ])
    jarvis = Jarvis(settings, memory, platforms, client=client)
    assert jarvis.task("plan today") == "Queued 1 post, sir."

    results = client.calls[1]["messages"][2]["content"]  # user turn carrying the tool results
    assert [r["is_error"] for r in results] == [False, True]
    assert client.calls[0]["model"] == "claude-opus-5-5"
    assert client.calls[0]["fallbacks"] == "default"
    assert any(t.get("name") == "web_search" for t in client.calls[0]["tools"])
    assert not any(t.get("name") == "approve_post" for t in client.calls[0]["tools"])
    assert len(memory.list_posts("pending_approval")) == 1


def test_refusal_is_handled(env):
    settings, memory, platforms = env
    client = FakeClient([block(stop_reason="refusal", usage=None, content=[])])
    reply = Conversation(Jarvis(settings, memory, platforms, client=client)).send("hi")
    assert "can't help" in reply


def test_telegram_style_commands(env):
    settings, memory, platforms = env
    pid = memory.add_post("x", "draft", "pending_approval", scheduled_at=utcnow())
    ap = Autopilot(Jarvis(settings, memory, platforms, client=FakeClient([])))
    assert f"#{pid}" in ap.handle_command("/pending")
    assert "published" in ap.handle_command("/approve all")
    assert ap.handle_command("/approve abc").startswith("Couldn't")
