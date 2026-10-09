"""Jarvis web dashboard: a password-protected control panel served by FastAPI."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..autopilot import Autopilot
from ..brain import Conversation
from ..memory import iso, utcnow
from ..ops import VIDEO_EXTS, scan_video_inbox
from ..tools import Toolbox, ToolError

log = logging.getLogger("jarvis.web")
STATIC = Path(__file__).parent / "static"
COOKIE = "jarvis_session"
JOBS = {"plan": "plan_day", "engage": "engage", "briefing": "briefing", "markets": "market_desk",
        "videos": "video_inbox", "publish": "publish_due"}


class ChatIn(BaseModel):
    message: str


class LoginIn(BaseModel):
    password: str


class EditIn(BaseModel):
    text: str


class ReplyIn(BaseModel):
    text: str


class HaltIn(BaseModel):
    halted: bool


def create_app(autopilot: Autopilot, password: str | None, feed: deque, run_scheduler: bool = True) -> FastAPI:
    state: dict = {"scheduler": None, "failed_logins": 0}

    @asynccontextmanager
    async def lifespan(_app):
        if run_scheduler:
            state["scheduler"] = autopilot.build_scheduler(background=True)
            state["scheduler"].start()
        yield
        if state["scheduler"]:
            state["scheduler"].shutdown(wait=False)

    app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    s = autopilot.settings
    memory = autopilot.memory
    secret = os.environ.get("JARVIS_SECRET") or secrets.token_hex(32)
    token = hmac.new(secret.encode(), b"owner:" + (password or "").encode(), hashlib.sha256).hexdigest()
    chat_lock = threading.Lock()
    state["conversation"] = Conversation(autopilot.jarvis)

    # ---- auth ------------------------------------------------------------------
    def authed(request: Request) -> bool:
        return password is None or hmac.compare_digest(request.cookies.get(COOKIE, ""), token)

    def require_owner(request: Request) -> None:
        if not authed(request):
            raise HTTPException(401, "login required")

    def owner_tools() -> Toolbox:
        return Toolbox(s, memory, autopilot.platforms, owner_present=True, notifier=autopilot.notify,
                       trader=autopilot.trader)

    @app.exception_handler(ToolError)
    async def tool_error(_, exc: ToolError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/login")
    def login_page():
        return FileResponse(STATIC / "login.html")

    @app.post("/api/login")
    def login(body: LoginIn):
        if password is None or hmac.compare_digest(body.password, password):
            state["failed_logins"] = 0
            resp = JSONResponse({"ok": True})
            resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=30 * 86400,
                            secure=os.environ.get("JARVIS_HTTPS") == "1")
            return resp
        state["failed_logins"] += 1
        time.sleep(min(5, state["failed_logins"]))  # slow down password guessing
        raise HTTPException(401, "wrong password")

    @app.post("/api/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE)
        return resp

    @app.get("/")
    def index(request: Request):
        if not authed(request):
            return RedirectResponse("/login")
        return FileResponse(STATIC / "index.html")

    # ---- overview ------------------------------------------------------------------
    @app.get("/api/status", dependencies=[Depends(require_owner)])
    def status():
        start = datetime.now(s.tz).replace(hour=0, minute=0, second=0, microsecond=0)
        published = [p for p in memory.list_posts("published", limit=500) if (p["published_at"] or "") >= iso(start)]
        sched = state["scheduler"]
        jobs = []
        if sched:
            for job in sched.get_jobs():
                if job.next_run_time:
                    jobs.append({"id": job.id, "next_run": job.next_run_time.isoformat()})
        trading = None
        if autopilot.trader:
            positions = autopilot.trader.positions()
            trading = {"halted": autopilot.trader.halted, "open_positions": len(positions),
                       "cash_usd": autopilot.trader.paper_cash(),
                       "realized_all_time_usd": autopilot.trader.realized_pnl()}
        return {
            "owner": s.owner_name, "brand": s.brand_name, "mode": s.mode, "dry_run": s.dry_run,
            "timezone": s.timezone, "model": s.model,
            "platforms": [{"name": n, "dry_run": type(p).__name__ == "DryRunPlatform",
                           "posts_per_day": s.platforms[n].posts_per_day} for n, p in autopilot.platforms.items()],
            "counts": {"pending": len(memory.list_posts("pending_approval", limit=500)),
                       "scheduled": len(memory.list_posts("scheduled", limit=500)),
                       "published_today": len(published),
                       "failed": len(memory.list_posts("failed", limit=500)),
                       "new_mentions": len(memory.list_interactions("new", limit=500)),
                       "flagged": len(memory.list_interactions("flagged", limit=500))},
            "autopilot_running": bool(sched), "jobs": sorted(jobs, key=lambda j: j["next_run"]),
            "trading": trading, "youtube": "youtube" in autopilot.platforms,
        }

    @app.get("/api/activity", dependencies=[Depends(require_owner)])
    def activity():
        return list(reversed(feed))

    @app.get("/api/performance", dependencies=[Depends(require_owner)])
    def performance(days: int = 7):
        return owner_tools().tool_get_performance(days)

    # ---- chat ------------------------------------------------------------------------
    @app.post("/api/chat", dependencies=[Depends(require_owner)])
    def chat(body: ChatIn):
        text = body.message.strip()
        if not text:
            raise HTTPException(400, "empty message")
        with chat_lock:
            if text.startswith("/"):
                reply = autopilot.handle_command(text)
            else:
                reply = state["conversation"].send(text)
        return {"reply": reply}

    @app.post("/api/chat/reset", dependencies=[Depends(require_owner)])
    def chat_reset():
        with chat_lock:
            state["conversation"] = Conversation(autopilot.jarvis)
        return {"ok": True}

    # ---- posts ---------------------------------------------------------------------------
    def post_view(p: dict) -> dict:
        out = dict(p)
        out["meta"] = json.loads(p["meta"]) if p.get("meta") else None
        out["kind"] = "reply" if p["reply_to_remote_id"] else ("video" if out["meta"] else "post")
        return out

    @app.get("/api/posts", dependencies=[Depends(require_owner)])
    def posts(status: str | None = None, limit: int = 100):
        return [post_view(p) for p in memory.list_posts(status or None, limit=min(limit, 500))]

    @app.post("/api/posts/{post_id}/approve", dependencies=[Depends(require_owner)])
    def approve(post_id: int):
        return owner_tools().tool_approve_post(post_id)

    @app.post("/api/posts/approve-all", dependencies=[Depends(require_owner)])
    def approve_all():
        tb = owner_tools()
        results = []
        for p in memory.list_posts("pending_approval", limit=500):
            try:
                results.append(tb.tool_approve_post(p["id"]))
            except ToolError as exc:
                results.append({"id": p["id"], "error": str(exc)})
        return results

    @app.post("/api/posts/{post_id}/reject", dependencies=[Depends(require_owner)])
    def reject(post_id: int):
        return owner_tools().tool_reject_post(post_id, "rejected from dashboard")

    @app.patch("/api/posts/{post_id}", dependencies=[Depends(require_owner)])
    def edit(post_id: int, body: EditIn):
        return owner_tools().tool_edit_post(post_id, text=body.text)

    # ---- inbox -------------------------------------------------------------------------------
    @app.get("/api/interactions", dependencies=[Depends(require_owner)])
    def interactions(status: str = "new"):
        return memory.list_interactions(None if status == "all" else status, limit=200)

    @app.post("/api/interactions/{iid}/reply", dependencies=[Depends(require_owner)])
    def reply(iid: int, body: ReplyIn):
        return owner_tools().tool_reply_to_interaction(iid, body.text, owner_confirmed=True)

    @app.post("/api/interactions/{iid}/ignore", dependencies=[Depends(require_owner)])
    def ignore(iid: int):
        return owner_tools().tool_mark_interaction(iid, "ignored", "ignored from dashboard")

    # ---- jobs --------------------------------------------------------------------------------
    @app.post("/api/run/{job}", dependencies=[Depends(require_owner)])
    def run_job(job: str):
        if job not in JOBS:
            raise HTTPException(404, "unknown job")
        result = getattr(autopilot, JOBS[job])()
        if job == "publish":
            result = "Published everything that was due."
        return {"result": result or "Done."}

    # ---- YouTube --------------------------------------------------------------------------------
    @app.get("/api/videos", dependencies=[Depends(require_owner)])
    def videos():
        queued = [post_view(p) for p in memory.list_posts(platform="youtube", limit=50)]
        return {"inbox": scan_video_inbox(s.youtube_inbox_dir, memory.queued_video_files()), "queued": queued}

    @app.post("/api/videos", dependencies=[Depends(require_owner)])
    def upload_video(file: UploadFile = File(...), kind: str = Form("long"), notes: str = Form("")):
        if kind not in ("long", "shorts"):
            raise HTTPException(400, "kind must be long or shorts")
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(file.filename or "video.mp4").name).strip("-.") or "video"
        if Path(name).suffix.lower() not in VIDEO_EXTS:
            raise HTTPException(400, f"unsupported video type; use one of {sorted(VIDEO_EXTS)}")
        folder = Path(s.youtube_inbox_dir) / kind
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / name
        if dest.exists():
            dest = folder / f"{dest.stem}-{int(time.time())}{dest.suffix}"
        with dest.open("wb") as out:
            shutil.copyfileobj(file.file, out, length=8 * 1024 * 1024)
        if notes.strip():
            dest.with_suffix(".txt").write_text(notes.strip())
        autopilot.notify(f"🎬 New {kind} video uploaded to the inbox: {dest.name}")
        return {"file": str(dest), "size_mb": round(dest.stat().st_size / 1e6, 1)}

    # ---- markets ---------------------------------------------------------------------------------
    def need_trader():
        if not autopilot.trader:
            raise HTTPException(404, "trading is disabled in config")
        return autopilot.trader

    @app.get("/api/portfolio", dependencies=[Depends(require_owner)])
    def portfolio():
        trader = need_trader()
        return trader.snapshot() | {"limits": trader.limits(),
                                    "starting_cash": s.trading.paper_starting_cash}

    @app.get("/api/trades", dependencies=[Depends(require_owner)])
    def trades(limit: int = 50):
        need_trader()
        return list(reversed(memory.list_trades("paper", limit=100000)))[:min(limit, 500)]

    @app.post("/api/trading/halt", dependencies=[Depends(require_owner)])
    def halt(body: HaltIn):
        need_trader().set_halted(body.halted)
        return {"halted": body.halted}

    @app.get("/api/watchlist", dependencies=[Depends(require_owner)])
    def watchlist():
        need_trader()
        return owner_tools().tool_market_quotes(s.trading.watchlist)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def make_feed_notifier(feed: deque, forward=None):
    def notify(message: str) -> None:
        feed.append({"time": iso(utcnow()), "message": message})
        if forward:
            forward(message)
    return notify

