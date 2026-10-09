"""Command line entry point: `jarvis chat`, `jarvis autopilot`, `jarvis plan`, ..."""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.markdown import Markdown
from rich.prompt import Prompt

console = Console()


def _setup(args, wrap_notifier=None):
    from .autopilot import Autopilot
    from .brain import Jarvis
    from .config import load_settings
    from .markets.trading import Trader
    from .memory import Memory
    from .platforms import build_platforms
    from .telegram import TelegramRemote

    settings = load_settings(args.config)
    memory = Memory(settings.db_path)
    platforms = build_platforms(settings)
    remote = TelegramRemote.from_env()
    notifier = remote.send if remote else None
    if wrap_notifier:
        notifier = wrap_notifier(notifier)
    trader = Trader(settings, memory, notifier) if settings.trading.enabled else None
    jarvis = Jarvis(settings, memory, platforms, notifier=notifier, trader=trader)
    return Autopilot(jarvis, notifier=notifier), remote


def cmd_init(args) -> None:
    for src, dst in (("config.example.yaml", "config.yaml"), (".env.example", ".env")):
        if Path(dst).exists():
            console.print(f"[yellow]{dst} already exists - leaving it alone[/]")
        else:
            shutil.copy(src, dst)
            console.print(f"[green]Created {dst}[/]")
    console.print("Next: add your ANTHROPIC_API_KEY and platform keys to .env, describe your brand in "
                  "config.yaml, then run [bold]jarvis chat[/].")


def cmd_chat(args) -> None:
    from .autopilot import format_pending

    autopilot, _ = _setup(args)
    s = autopilot.settings
    console.print(f"[bold cyan]JARVIS[/] online. Mode: {s.mode}{' (dry run)' if s.dry_run else ''}. "
                  f"Platforms: {', '.join(autopilot.platforms) or 'none enabled'}.")
    console.print("[dim]Type anything. Commands: /pending /approve <id|all> /reject <id> /plan /briefing "
                  "/videos /markets /portfolio /halt /resume /new /quit[/]")
    while True:
        try:
            text = Prompt.ask(f"[bold]{s.owner_name}[/]").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text in ("/quit", "/exit"):
            break
        with console.status("Jarvis is thinking..."):
            reply = autopilot.handle_command(text)
        console.print(Markdown(f"**JARVIS:** {reply}"))
    pending = autopilot.memory.list_posts("pending_approval")
    if pending:
        console.print(f"\n[yellow]{len(pending)} item(s) still awaiting approval:[/]\n{format_pending(pending)}")
    console.print("[cyan]Goodbye, sir.[/]")


def cmd_autopilot(args) -> None:
    autopilot, remote = _setup(args)
    if remote:
        remote.start(autopilot.handle_command)
        console.print("[green]Telegram remote control active.[/]")
    autopilot.run_forever()


def cmd_once(job: str):
    def run(args) -> None:
        autopilot, _ = _setup(args)
        result = getattr(autopilot, job)()
        if isinstance(result, str):
            console.print(Markdown(result))
    return run


def cmd_pending(args) -> None:
    from .autopilot import format_pending

    autopilot, _ = _setup(args)
    console.print(format_pending(autopilot.memory.list_posts("pending_approval", limit=100)) or "Nothing pending.")


def cmd_approve(args) -> None:
    autopilot, _ = _setup(args)
    console.print(autopilot.handle_command("/approve " + " ".join(args.ids)))


def cmd_reject(args) -> None:
    autopilot, _ = _setup(args)
    console.print(autopilot.handle_command(f"/reject {args.id}"))


def cmd_web(args) -> None:
    import os
    from collections import deque

    import uvicorn

    from .web.server import create_app, make_feed_notifier

    password = os.environ.get("JARVIS_WEB_PASSWORD") or None
    local_only = args.host in ("127.0.0.1", "localhost", "::1")
    if password is None and not local_only:
        console.print("[red]Refusing to open Jarvis to the network without a password.[/] "
                      "Set JARVIS_WEB_PASSWORD in .env (or use --host 127.0.0.1).")
        sys.exit(1)
    feed: deque = deque(maxlen=200)
    autopilot, remote = _setup(args, wrap_notifier=lambda fwd: make_feed_notifier(feed, fwd))
    if remote and not args.no_autopilot:
        remote.start(autopilot.handle_command)
    app = create_app(autopilot, password, feed, run_scheduler=not args.no_autopilot)
    url = f"http://{'localhost' if local_only or args.host == '0.0.0.0' else args.host}:{args.port}"
    console.print(f"[bold cyan]JARVIS[/] dashboard at [link={url}]{url}[/link]"
                  + ("" if password else " [dim](no password - local access only)[/]"))
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


def cmd_youtube_auth(args) -> None:
    from dotenv import load_dotenv

    from .platforms.youtube import authorize

    load_dotenv()
    authorize(args.secrets)
    console.print("[green]YouTube connected.[/] Enable it under platforms: in config.yaml.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="jarvis", description="Your AI social media manager.")
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("init", help="create config.yaml and .env from the examples").set_defaults(fn=cmd_init)
    sub.add_parser("chat", help="talk to Jarvis").set_defaults(fn=cmd_chat)
    sub.add_parser("autopilot", help="run 24/7: plan, post, reply, report").set_defaults(fn=cmd_autopilot)
    sub.add_parser("plan", help="plan today's content now").set_defaults(fn=cmd_once("plan_day"))
    sub.add_parser("engage", help="check and answer mentions now").set_defaults(fn=cmd_once("engage"))
    sub.add_parser("publish", help="publish anything that's due now").set_defaults(fn=cmd_once("publish_due"))
    sub.add_parser("briefing", help="get today's briefing").set_defaults(fn=cmd_once("briefing"))
    sub.add_parser("pending", help="show posts awaiting approval").set_defaults(fn=cmd_pending)
    sub.add_parser("videos", help="process new videos in the YouTube inbox").set_defaults(fn=cmd_once("video_inbox"))
    sub.add_parser("markets", help="run a paper-trading desk session").set_defaults(fn=cmd_once("market_desk"))
    sub.add_parser("portfolio", help="show the paper portfolio").set_defaults(
        fn=lambda a: console.print(_setup(a)[0].handle_command("/portfolio")))
    p = sub.add_parser("web", help="open the web dashboard (and run autopilot in the background)")
    p.add_argument("--host", default=os.environ.get("JARVIS_HOST", "127.0.0.1"),
                   help="127.0.0.1 = this computer only; 0.0.0.0 = reachable from other devices/servers")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    p.add_argument("--no-autopilot", action="store_true", help="dashboard only, don't run scheduled jobs")
    p.set_defaults(fn=cmd_web)
    p = sub.add_parser("youtube-auth", help="connect your YouTube channel (one-time browser login)")
    p.add_argument("--secrets", help="path to the OAuth client JSON (default: $YOUTUBE_CLIENT_SECRETS)")
    p.set_defaults(fn=cmd_youtube_auth)
    p = sub.add_parser("approve", help="approve posts: jarvis approve 3 4 | jarvis approve all")
    p.add_argument("ids", nargs="+")
    p.set_defaults(fn=cmd_approve)
    p = sub.add_parser("reject", help="reject a post")
    p.add_argument("id")
    p.set_defaults(fn=cmd_reject)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s",
                        handlers=[RichHandler(console=console, show_path=False)])
    for noisy in ("httpx", "httpx2", "anthropic", "apscheduler", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not getattr(args, "fn", None):
        parser.print_help()
        sys.exit(1)
    args.fn(args)


if __name__ == "__main__":
    main()
