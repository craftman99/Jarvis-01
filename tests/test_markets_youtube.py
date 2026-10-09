import pytest

from jarvis import ops
from jarvis.config import PlatformSettings, Settings
from jarvis.markets import data, risk
from jarvis.markets.data import MarketDataError, Quote
from jarvis.markets.trading import Trader, TradeError, asset_key
from jarvis.memory import Memory
from jarvis.platforms import DryRunPlatform
from jarvis.tools import Toolbox, ToolError

SAFE_EXTRA = {"liquidity_usd": 1e6, "age_hours": 100, "buys_24h": 100, "sells_24h": 90}


class Prices:
    """Fake market: set prices per asset, used as the Trader's quote function."""

    def __init__(self, **prices):
        self.p = {}
        for k, v in prices.items():
            self.p[k] = v

    def __call__(self, market, asset, chain):
        key = asset_key(market, asset)
        if key not in self.p:
            raise MarketDataError(f"no price for {asset}")
        sym = asset.split("/")[0].upper() if market != "dex" else "MEME"
        return Quote(market, asset, sym, self.p[key], extra=dict(SAFE_EXTRA))


@pytest.fixture
def desk():
    settings = Settings(timezone="UTC")
    settings.platforms = {"x": PlatformSettings(True, 3), "youtube": PlatformSettings(True, 1)}
    memory = Memory(":memory:")
    prices = Prices()
    prices.p.update({"stock:AAPL": 100.0, "crypto:DOGE/USD": 0.10, "dex:memeaddr": 0.001})
    sent = []
    trader = Trader(settings, memory, sent.append, quote_fn=prices)
    return settings, memory, trader, prices, sent


def test_paper_round_trip_pnl(desk):
    _, _, trader, prices, _ = desk
    buy = trader.place("stock", "AAPL", "buy", 50, "breakout")
    assert buy["status"] == "filled" and buy["mode"] == "paper"
    assert buy["stop_loss_pct"] == 15 and buy["take_profit_pct"] == 40
    prices.p["stock:AAPL"] = 110.0
    sell = trader.place("stock", "AAPL", "sell", None, "target")
    assert sell["status"] == "filled"
    snap = trader.snapshot()
    assert snap["positions"] == []
    assert 4.0 < snap["realized_pnl_all_time_usd"] < 5.0       # +10% minus simulated slippage
    assert snap["cash_usd"] == pytest.approx(1000 + snap["realized_pnl_all_time_usd"], abs=0.02)


def test_limits(desk):
    settings, _, trader, prices, _ = desk
    with pytest.raises(TradeError, match="per-trade"):
        trader.place("stock", "AAPL", "buy", 51, "too big")
    with pytest.raises(TradeError, match="no open position"):
        trader.place("stock", "AAPL", "sell", None, "nothing to sell")
    settings.trading.max_open_positions = 1
    trader.place("stock", "AAPL", "buy", 10, "a")
    with pytest.raises(TradeError, match="max of 1"):
        trader.place("crypto", "DOGE/USD", "buy", 10, "b")
    trader.place("stock", "AAPL", "buy", 10, "adding to an existing position is fine")


def test_kill_switch_blocks_buys_not_sells(desk):
    _, _, trader, _, _ = desk
    trader.place("stock", "AAPL", "buy", 20, "a")
    trader.set_halted(True)
    with pytest.raises(TradeError, match="halted"):
        trader.place("crypto", "DOGE/USD", "buy", 10, "b")
    assert trader.place("stock", "AAPL", "sell", None, "exit")["status"] == "filled"


def test_daily_loss_limit(desk):
    settings, _, trader, prices, _ = desk
    settings.trading.max_daily_loss_usd = 10
    trader.place("stock", "AAPL", "buy", 50, "a")
    prices.p["stock:AAPL"] = 70.0                               # -30% -> ~$15 unrealized loss
    with pytest.raises(TradeError, match="daily loss"):
        trader.place("crypto", "DOGE/USD", "buy", 10, "b")


def test_memecoin_scam_check_blocks_buy(desk, monkeypatch):
    _, _, trader, _, _ = desk
    monkeypatch.setattr(risk, "solana_security", lambda addr: {"red_flags": ["mint authority enabled"],
                                                                "warnings": []})
    with pytest.raises(TradeError, match="AVOID"):
        trader.place("dex", "memeaddr", "buy", 20, "moon", chain="solana")
    monkeypatch.setattr(risk, "solana_security", lambda addr: {"red_flags": [], "warnings": []})
    assert trader.place("dex", "memeaddr", "buy", 20, "clean", chain="solana")["status"] == "filled"


def test_memecoin_exposure_cap(desk, monkeypatch):
    settings, _, trader, prices, _ = desk
    monkeypatch.setattr(risk, "solana_security", lambda addr: {"red_flags": [], "warnings": []})
    settings.trading.max_memecoin_exposure_usd = 30
    trader.place("dex", "memeaddr", "buy", 20, "a", chain="solana")
    with pytest.raises(TradeError, match="exposure"):
        trader.place("dex", "memeaddr", "buy", 20, "b", chain="solana")


def test_stop_loss_and_take_profit_exits(desk):
    _, _, trader, prices, sent = desk
    trader.place("stock", "AAPL", "buy", 50, "a", stop_loss_pct=10, take_profit_pct=20)
    trader.place("crypto", "DOGE/USD", "buy", 50, "b", stop_loss_pct=10, take_profit_pct=20)
    prices.p["stock:AAPL"] = 85.0
    prices.p["crypto:DOGE/USD"] = 0.13
    exits = trader.check_exits()
    assert len(exits) == 2 and trader.positions() == {}
    assert any("Stop-Loss" in m for m in sent) and any("Take-Profit" in m for m in sent)


def test_watchlist_alerts(desk, monkeypatch):
    settings, _, trader, _, _ = desk
    settings.trading.watchlist = ["BTC/USD"]
    price = {"v": 100.0}
    monkeypatch.setattr(data, "crypto_quotes", lambda assets, ex: [Quote("crypto", "BTC/USD", "BTC", price["v"])])
    assert trader.watch() == []                                 # sets the baseline
    price["v"] = 107.0
    assert "+7.0%" in trader.watch()[0]
    price["v"] = 108.0
    assert trader.watch() == []                                 # baseline moved to 107


# ---- data + risk parsing ------------------------------------------------------

PAIR = {"chainId": "solana", "dexId": "raydium", "url": "https://dexscreener.com/x",
        "baseToken": {"address": "memeaddr", "name": "Meme", "symbol": "MEME"},
        "priceUsd": "0.0012", "priceChange": {"h1": 5, "h24": 40}, "volume": {"h24": 250000},
        "liquidity": {"usd": 80000}, "marketCap": 1200000, "txns": {"h24": {"buys": 900, "sells": 700}},
        "pairCreatedAt": 0}


def test_token_quote_picks_most_liquid_pool(monkeypatch):
    thin = {**PAIR, "liquidity": {"usd": 10}, "priceUsd": "9"}
    monkeypatch.setattr(data, "_get", lambda url, **kw: [thin, PAIR])
    q = data.token_quote("solana", "memeaddr")
    assert q.price == 0.0012 and q.extra["liquidity_usd"] == 80000 and q.symbol == "MEME"


def test_evm_security_flags(monkeypatch):
    info = {"is_honeypot": "1", "buy_tax": "0.02", "sell_tax": "0.25", "is_open_source": "1", "is_mintable": "1",
            "holders": [{"percent": "0.6", "is_locked": 0}], "lp_holders": [{"percent": "0.9", "is_locked": 1}]}
    monkeypatch.setattr(risk, "_get", lambda url, **kw: {"code": 1, "result": {"0xabc": info}})
    out = risk.evm_security("base", "0xABC")
    assert any("HONEYPOT" in f for f in out["red_flags"])
    assert any("taxes" in f for f in out["red_flags"])
    assert any("top 10" in f for f in out["red_flags"])
    assert any("minted" in w for w in out["warnings"])


def test_assess_token_verdicts(monkeypatch):
    q = data._pair_to_quote(PAIR)
    monkeypatch.setattr(risk, "_get", lambda url, **kw: {"score_normalised": 2, "risks": [], "lpLockedPct": 100})
    assert risk.assess_token("solana", "memeaddr", q, 50000)["verdict"] == "SPECULATIVE"
    monkeypatch.setattr(risk, "_get", lambda url, **kw: {"risks": [{"name": "Freeze Authority still enabled",
                                                                    "level": "danger"}]})
    report = risk.assess_token("solana", "memeaddr", q, 50000)
    assert report["verdict"] == "AVOID" and report["red_flags"]


def test_scanner_outage_is_treated_as_risk(monkeypatch):
    q = data._pair_to_quote(PAIR)

    def down(url, **kw):
        raise MarketDataError("HTTP 503")

    monkeypatch.setattr(risk, "_get", down)
    report = risk.assess_token("solana", "memeaddr", q, 50000)
    assert any("unavailable" in w for w in report["warnings"])


# ---- tools: trading + YouTube ----------------------------------------------------

def test_trading_tools_exposed_and_owner_only(desk):
    settings, memory, trader, _, _ = desk
    platforms = {"x": DryRunPlatform("x", 280), "youtube": DryRunPlatform("youtube", 5000, True)}
    names = {t["name"] for t in Toolbox(settings, memory, platforms, False, trader=trader).definitions()}
    assert {"place_paper_trade", "check_token_risk", "create_video_upload"} <= names
    assert "set_trading_halt" not in names
    owner = {t["name"] for t in Toolbox(settings, memory, platforms, True, trader=trader).definitions()}
    assert "set_trading_halt" in owner
    settings.trading.enabled = False
    off = {t["name"] for t in Toolbox(settings, memory, platforms, True, trader=trader).definitions()}
    assert "place_paper_trade" not in off


def test_auto_paper_trade_switch(desk):
    settings, memory, trader, _, _ = desk
    settings.trading.auto_paper_trade = False
    tb = Toolbox(settings, memory, {}, owner_present=False, trader=trader)
    with pytest.raises(ToolError, match="auto_paper_trade"):
        tb.execute("place_paper_trade", {"market": "stock", "asset": "AAPL", "side": "buy",
                                         "usd_amount": 10, "reason": "x"})
    owner = Toolbox(settings, memory, {}, owner_present=True, trader=trader)
    assert '"filled"' in owner.execute("place_paper_trade", {"market": "stock", "asset": "AAPL", "side": "buy",
                                                             "usd_amount": 10, "reason": "owner asked"})


def test_no_shilling_held_assets(desk):
    settings, memory, trader, _, _ = desk
    trader.place("crypto", "DOGE/USD", "buy", 10, "a")
    tb = Toolbox(settings, memory, {"x": DryRunPlatform("x", 280)}, owner_present=True, trader=trader)
    with pytest.raises(ToolError, match="no shilling"):
        tb.tool_create_post("x", "$DOGE to the moon", "now", "r")
    with pytest.raises(ToolError, match="contract address"):
        tb.tool_create_post("x", "0x6982508145454ce325ddbe47a25d4ec3d2311933 gem", "now", "r")


def test_youtube_inbox_upload_flow(desk, tmp_path):
    settings, memory, _, _, _ = desk
    settings.youtube_inbox_dir = str(tmp_path)
    (tmp_path / "shorts").mkdir()
    video = tmp_path / "shorts" / "clip.mp4"
    video.write_bytes(b"fake video")
    (tmp_path / "shorts" / "clip.txt").write_text("about my desk setup")
    platforms = {"x": DryRunPlatform("x", 280), "youtube": DryRunPlatform("youtube", 5000, True)}
    owner = Toolbox(settings, memory, platforms, owner_present=True)

    [item] = owner.tool_list_video_inbox()
    assert item["is_short"] and item["owner_notes"] == "about my desk setup"
    with pytest.raises(ToolError, match="video uploads"):
        owner.tool_create_post("youtube", "hi", "now", "r")

    post = owner.tool_create_video_upload(str(video), "My $40 desk upgrade", "Here's how.", ["desk"], "now",
                                          "first short", owner_confirmed=True)
    assert post["status"] == "published"
    assert owner.tool_list_video_inbox() == []                  # not offered twice
    assert video.exists()                                       # dry run never moves your files
    with pytest.raises(ToolError, match="not a new video"):
        owner.tool_create_video_upload(str(video), "again", "d", [], "now", "r")


def test_archive_moves_uploaded_video(tmp_path):
    (tmp_path / "long").mkdir()
    v = tmp_path / "long" / "ep1.mp4"
    v.write_bytes(b"x")
    ops._archive_video(str(v))
    assert (tmp_path / "uploaded" / "ep1.mp4").exists() and not v.exists()
