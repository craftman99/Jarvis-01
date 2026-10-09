# J.A.R.V.I.S. — your AI social media manager

Jarvis is an always-on assistant, powered by Claude, that runs your social media accounts and your
YouTube channel. It plans content, writes a separate version for each network, posts on schedule,
answers mentions and comments, learns from what does well, and sends you a daily briefing. It also runs
a **markets desk**: market alerts, a memecoin scam checker, and a paper-trading account that builds an
honest track record. You can message it from your phone.

```
You: Jarvis, we launch the new course Friday. Build hype all week, but keep it classy.
JARVIS: Right away, Boss. I've drafted 9 posts across X, Bluesky and Instagram, Tue-Fri:
        a teaser, a behind-the-scenes, a question to the audience, and a launch-day post...
        All 9 are waiting for your approval (/pending).
```

## What it does

| Feature | Details |
|---|---|
| 🧠 **Daily content planning** | Every morning it checks what performed well, looks up current trends with web search, and writes the day's posts for each network's posting times. |
| ✍️ **Writes for each network** | X is short and punchy, Mastodon gets CamelCase hashtags, Instagram gets captions with images. It never posts the same text everywhere. |
| ⏰ **Scheduler** | Publishes posts at their scheduled times and retries anything you approve after its time slot passed. |
| 💬 **Replies to mentions and comments** | Answers fans, ignores spam and trolls, and **flags** anything sensitive (complaints, press, deals, legal) for you instead of replying. |
| ✅ **Approval queue (review mode)** | Jarvis drafts and you approve with one tap. Switch to `autopilot` when you trust it. |
| 📈 **Analytics** | Pulls likes, reposts and replies for each post and uses them to improve future content. |
| 🗂️ **Long-term memory** | Remembers your preferences, upcoming launches and lessons learned (in a local SQLite database). |
| 📱 **Telegram remote control** | Chat with Jarvis, approve posts and get alerts from your phone. |
| 🗒️ **Daily briefing** | An evening summary: what went out, how it did, open conversations, and ideas for tomorrow. |
| 🎬 **YouTube** | Drop finished videos into `videos/long/` or `videos/shorts/`, optionally with a `.txt` file of notes. Jarvis writes the title, description and tags, schedules the upload, answers comments and tracks views. |
| 📊 **Markets desk** | Watchlist alerts for stocks and crypto, a scan of new memecoins on Solana, Ethereum and Base, and a scam/rug-pull checker that looks for honeypots, taxes, mint authority, holder concentration and locked liquidity. |
| 🧪 **Paper trading** | Jarvis trades simulated money at real market prices using a built-in day-trading and memecoin playbook. Every trade has a stop-loss, a take-profit and a written reason, and he tracks P&L. |
| 🛡️ **Guardrails in code** | Checks character limits, blocks off-limits topics, caps posts per day and replies per run, only takes orders from you, and treats text in mentions as data rather than instructions. Dry-run mode is on by default. |

**Supported networks:** X/Twitter, Bluesky, Mastodon, Facebook Pages, Instagram Business, YouTube.
To add another network, implement the five methods in `jarvis/platforms/base.py`.

## Quick start

```bash
git clone https://github.com/craftman99/Jarvis-01 && cd Jarvis-01
python -m venv .venv && source .venv/bin/activate
pip install -e ".[all]"

jarvis init            # creates config.yaml and .env
# 1. put your ANTHROPIC_API_KEY (console.anthropic.com) in .env
# 2. describe your brand, voice, topics and off-limits subjects in config.yaml
# 3. enable the networks you want under `platforms:`

jarvis chat            # talk to Jarvis
```

Jarvis starts with **`dry_run: true`**: everything works, but "published" posts only go to the log.
When you're happy with what it writes, add your platform keys to `.env` and set `dry_run: false`.

### Run it 24/7

```bash
jarvis autopilot
```

This runs the whole loop: content planning at `plan_content_at`, publishing every minute, checking
mentions every 15 minutes, refreshing metrics every hour, and the briefing at `daily_briefing_at`.
Keep it running on any always-on machine (a small VPS, a Raspberry Pi, or `tmux`/`systemd`/Docker).

### Other commands

```bash
jarvis plan            # plan today's content now
jarvis engage          # check and answer mentions now
jarvis pending         # see what is waiting for approval
jarvis approve 4 5     # or: jarvis approve all
jarvis reject 6
jarvis briefing
jarvis videos          # process new videos in the YouTube inbox
jarvis markets         # run a paper-trading desk session
jarvis portfolio       # paper portfolio and P&L
```

In chat or Telegram you can also say things like *"approve everything except the Facebook one"*,
*"make #12 funnier"*, *"post this on X right now: …"*, *"what did best this week?"*, or
*"remember that we never post on Sundays"*.

## Getting platform keys

| Network | Where | `.env` keys |
|---|---|---|
| X | developer.x.com → create an app with **Read and Write** permissions → keys and tokens | `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_TOKEN_SECRET`, `X_BEARER_TOKEN` |
| Bluesky | Settings → Privacy and security → App passwords | `BLUESKY_HANDLE`, `BLUESKY_APP_PASSWORD` |
| Mastodon | Preferences → Development → New application (read + write scopes) | `MASTODON_API_BASE_URL`, `MASTODON_ACCESS_TOKEN` |
| Facebook / Instagram | developers.facebook.com → app with Pages + Instagram Graph API → long-lived **Page** access token | `META_PAGE_ID`, `META_PAGE_ACCESS_TOKEN`, `META_IG_USER_ID` |
| YouTube | Google Cloud console → enable **YouTube Data API v3** → OAuth client ID (type *Desktop app*) → download the JSON as `client_secret.json`. Then run `jarvis youtube-auth` once and sign in | `YOUTUBE_CLIENT_SECRETS` |
| Stock prices | Free account at alpaca.markets → paper trading API keys (used only for market data) | `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` |
| Telegram (remote) | Message @BotFather → `/newbot`. Start `jarvis autopilot`, message your bot, and it replies with your chat id | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_CHAT_ID` |

Note: X's API tiers limit how many posts and mention reads you get. Mention-reading needs at least the
Basic tier. If a network can't connect, Jarvis logs a warning and runs that network in dry-run mode.

## YouTube

1. Connect your channel once with `jarvis youtube-auth`, then set `youtube: { enabled: true }` under `platforms:`.
2. Export a finished video into `videos/long/` or `videos/shorts/`. Optionally add a notes file with the
   same name (`my-video.txt`) covering the topic, key points and timestamps for chapters.
3. Every 30 minutes Jarvis picks up new files. He writes the title, description and tags, schedules the
   upload (after your approval in review mode), and can post a teaser on your other networks.
   Uploaded files move to `videos/uploaded/`.

Note: Google keeps uploads from unverified API projects **private** until the project passes their
audit. Request the audit in the Google Cloud console, or switch videos to public yourself in YouTube
Studio in the meantime. Each upload uses 1,600 of the default 10,000 daily API quota units, so roughly
six uploads a day.

## Markets desk (paper trading)

> **Important:** this is not financial advice and there are no guaranteed profits. Most day traders lose
> money and most memecoins go to zero. Jarvis trades **simulated money only** and never places real
> orders. Use his track record to decide whether a strategy is worth trying yourself.

- **Alerts:** you get a message when anything on your `watchlist` moves more than `alert_move_pct`.
- **Memecoin scanner:** every hour Jarvis scans newly listed tokens (DexScreener). He runs the promising
  ones through the scam checker (RugCheck for Solana, GoPlus for Ethereum and Base, plus checks on
  liquidity and trading patterns) and messages you about standouts and fast-spreading scams.
- **Paper trading:** starts with `paper_starting_cash`. The code enforces limits: maximum per trade,
  daily loss limit, maximum open positions, a cap on total memecoin exposure, and no buying a token the
  scam checker marks AVOID. Stop-losses and take-profits are checked every 5 minutes.
- **Track record:** use `jarvis portfolio`, `/portfolio` on Telegram, or ask "how's the desk doing?".
  The daily briefing includes the day's trades and lessons.
- **Pause:** `/halt` stops new buys (exits still run) and `/resume` restarts them.
- **No shilling:** Jarvis cannot post token contract addresses, or cashtags (like `$DOGE`) of assets the
  desk holds, on your social accounts.

## Modes and safety

- **`mode: review`** (default): every post and reply waits in the approval queue. Jarvis messages you
  when there's something to review.
- **`mode: autopilot`**: Jarvis publishes and replies on its own, limited by `posts_per_day` per network
  and `max_replies_per_run`.
- Whatever the mode, a post only goes out without approval when **you** tell Jarvis to post it
  (in chat or Telegram) or when autopilot is on. Only you can approve posts. Jarvis ignores
  Telegram messages from anyone else.

## Under the hood

```
jarvis/
  brain.py       Claude agent loop (tool use + web search, adaptive thinking, refusal fallback)
  tools.py       the tools Claude can call, with guardrails enforced in code
  autopilot.py   scheduled jobs (plan, publish, engage, metrics, briefing) + remote commands
  ops.py         publishing, syncing mentions and fetching metrics (no AI involved)
  memory.py      SQLite: posts, approval queue, interactions, metrics, long-term notes
  platforms/     X, Bluesky, Mastodon, Facebook, Instagram, YouTube adapters + dry-run
  markets/       market data, memecoin scam checker, paper-trading engine with risk limits
  knowledge/     the trading playbook Jarvis follows
  telegram.py    phone remote control
  cli.py         the `jarvis` command
```

- Model: `claude-opus-5-5`, which you can change in `config.yaml`. `effort` sets how hard it thinks:
  `medium` is cheaper, `xhigh` gives the best quality.
- The system prompt contains no timestamps, so prompt caching keeps repeat runs cheap.
- Tests: `pip install -e ".[all,dev]" && pytest`. They use a fake Claude client, so you don't need an API key.
