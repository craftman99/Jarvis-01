"""Paper trading engine (simulated money) with hard risk limits enforced in code."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable


from ..config import Settings
from ..memory import Memory, iso, utcnow
from . import data, risk
from .data import MarketDataError, Quote

log = logging.getLogger("jarvis.trading")

SLIPPAGE = {"dex": 0.01, "crypto": 0.002, "stock": 0.0005}   # simulated cost of trading in paper mode


class TradeError(Exception):
    """A trade was refused (limits, bad input, broker failure). The message explains why."""


def asset_key(market: str, asset: str) -> str:
    return f"{market}:{asset.lower() if market == 'dex' else asset.upper()}"


class Trader:
    def __init__(self, settings: Settings, memory: Memory, notifier: Callable[[str], None] | None = None,
                 quote_fn: Callable[[str, str, str | None], Quote] | None = None):
        self.settings = settings
        self.t = settings.trading
        self.memory = memory
        self.notify = notifier or (lambda msg: log.warning("NOTIFY OWNER: %s", msg))
        self._quote_fn = quote_fn or self._fetch_quote

    # Paper only: Jarvis simulates fills at live market prices. It never places real orders.
    mode = "paper"

    @property
    def halted(self) -> bool:
        return bool(self.memory.get_state("trading_halted", False))

    def set_halted(self, halted: bool) -> None:
        self.memory.set_state("trading_halted", halted)

    # ---- prices ------------------------------------------------------------
    def _fetch_quote(self, market: str, asset: str, chain: str | None) -> Quote:
        if market == "stock":
            quotes = data.stock_quotes([asset.upper()])
        elif market == "crypto":
            quotes = data.crypto_quotes([asset.upper()], self.t.crypto_exchange)
        elif market == "dex":
            if not chain:
                raise TradeError("dex tokens need a chain (solana, ethereum, base)")
            quotes = [data.token_quote(chain, asset)]
        else:
            raise TradeError(f"unknown market {market!r}; use stock, crypto or dex")
        if not quotes:
            raise TradeError(f"no price for {asset}")
        return quotes[0]

    def quote(self, market: str, asset: str, chain: str | None = None) -> Quote:
        try:
            return self._quote_fn(market, asset, chain)
        except MarketDataError as exc:
            raise TradeError(str(exc)) from exc

    # ---- portfolio -----------------------------------------------------------
    def positions(self) -> dict[str, dict]:
        """Open positions (average-cost accounting) from the filled trade log."""
        pos: dict[str, dict] = {}
        for tr in self.memory.list_trades(self.mode, "filled", limit=100000):
            key = asset_key(tr["market"], tr["asset"])
            p = pos.setdefault(key, {"market": tr["market"], "asset": tr["asset"], "symbol": tr["symbol"],
                                     "chain": tr["chain"], "qty": 0.0, "cost_usd": 0.0})
            if tr["side"] == "buy":
                p["qty"] += tr["qty"]
                p["cost_usd"] += tr["usd"]
                p["stop_loss_pct"], p["take_profit_pct"] = tr["stop_loss_pct"], tr["take_profit_pct"]
                p.setdefault("opened_at", tr["filled_at"])
            else:
                avg = p["cost_usd"] / p["qty"] if p["qty"] else 0
                p["cost_usd"] -= avg * tr["qty"]
                p["qty"] -= tr["qty"]
                if p["qty"] <= 1e-12:
                    del pos[key]
        return pos

    def realized_pnl(self, since: datetime | None = None) -> float:
        """Realized profit/loss from sells (average cost), optionally only those filled since a time."""
        qty: dict[str, float] = {}
        cost: dict[str, float] = {}
        pnl = 0.0
        cutoff = iso(since) if since else ""
        for tr in self.memory.list_trades(self.mode, "filled", limit=100000):
            key = asset_key(tr["market"], tr["asset"])
            if tr["side"] == "buy":
                qty[key] = qty.get(key, 0) + tr["qty"]
                cost[key] = cost.get(key, 0) + tr["usd"]
            else:
                avg = cost.get(key, 0) / qty[key] if qty.get(key) else 0
                if (tr["filled_at"] or "") >= cutoff:
                    pnl += tr["usd"] - avg * tr["qty"]
                cost[key] = cost.get(key, 0) - avg * tr["qty"]
                qty[key] = qty.get(key, 0) - tr["qty"]
        return round(pnl, 2)

    def paper_cash(self) -> float:
        cash = self.t.paper_starting_cash
        for tr in self.memory.list_trades("paper", "filled", limit=100000):
            cash += -tr["usd"] if tr["side"] == "buy" else tr["usd"]
        return round(cash, 2)

    def snapshot(self) -> dict:
        """Portfolio with live prices and P&L."""
        rows, value, unrealized = [], 0.0, 0.0
        for p in self.positions().values():
            try:
                price = self.quote(p["market"], p["asset"], p["chain"]).price
            except TradeError:
                price = p["cost_usd"] / p["qty"]
            mv = p["qty"] * price
            pnl = mv - p["cost_usd"]
            value += mv
            unrealized += pnl
            rows.append({"market": p["market"], "symbol": p["symbol"], "asset": p["asset"], "chain": p["chain"],
                         "qty": round(p["qty"], 8), "avg_price": p["cost_usd"] / p["qty"], "price": price,
                         "value_usd": round(mv, 2), "pnl_usd": round(pnl, 2),
                         "pnl_pct": round(pnl / p["cost_usd"] * 100, 1) if p["cost_usd"] else 0,
                         "stop_loss_pct": p.get("stop_loss_pct"), "take_profit_pct": p.get("take_profit_pct")})
        out = {"mode": self.mode, "halted": self.halted, "positions": rows,
               "positions_value_usd": round(value, 2), "unrealized_pnl_usd": round(unrealized, 2),
               "realized_pnl_today_usd": self.realized_pnl(self._start_of_day()),
               "realized_pnl_all_time_usd": self.realized_pnl()}
        if self.mode == "paper":
            out["cash_usd"] = self.paper_cash()
            out["total_equity_usd"] = round(out["cash_usd"] + value, 2)
            out["return_pct"] = round((out["total_equity_usd"] / self.t.paper_starting_cash - 1) * 100, 2)
        return out

    def _start_of_day(self) -> datetime:
        return datetime.now(self.settings.tz).replace(hour=0, minute=0, second=0, microsecond=0)

    def limits(self) -> dict:
        t = self.t
        return {"mode": self.mode, "max_usd_per_trade": t.max_usd_per_trade,
                "max_daily_loss_usd": t.max_daily_loss_usd, "max_open_positions": t.max_open_positions,
                "max_memecoin_exposure_usd": t.max_memecoin_exposure_usd, "halted": self.halted}

    # ---- placing trades -------------------------------------------------------
    def place(self, market: str, asset: str, side: str, usd_amount: float | None, reason: str,
              chain: str | None = None, stop_loss_pct: float | None = None,
              take_profit_pct: float | None = None) -> dict:
        t = self.t
        if not t.enabled:
            raise TradeError("trading is disabled in config")
        if side not in ("buy", "sell"):
            raise TradeError("side must be buy or sell")
        if market == "dex" and chain not in t.memecoin_chains:
            raise TradeError(f"chain must be one of {t.memecoin_chains}")
        q = self.quote(market, asset, chain)
        key = asset_key(market, q.asset or asset)
        held = self.positions().get(key)

        if side == "sell":
            if not held:
                raise TradeError(f"no open position in {asset}")
            qty = held["qty"] if not usd_amount else min(held["qty"], usd_amount / q.price)
            usd = qty * q.price
            sl, tp = None, None
        else:
            if self.halted:
                raise TradeError("trading is halted (kill switch). The owner can resume it.")
            if not usd_amount or usd_amount <= 0:
                raise TradeError("usd_amount must be positive")
            if usd_amount > t.max_usd_per_trade:
                raise TradeError(f"${usd_amount:.2f} exceeds the ${t.max_usd_per_trade:.2f} per-trade limit")
            if not held and len(self.positions()) >= t.max_open_positions:
                raise TradeError(f"already at the max of {t.max_open_positions} open positions")
            snap = self.snapshot()
            day_loss = -(snap["realized_pnl_today_usd"] + min(0.0, snap["unrealized_pnl_usd"]))
            if day_loss >= t.max_daily_loss_usd:
                raise TradeError(f"daily loss limit hit (${day_loss:.2f} of ${t.max_daily_loss_usd:.2f}) - "
                                 "no new buys today")
            if usd_amount > snap["cash_usd"]:
                raise TradeError(f"not enough paper cash (${snap['cash_usd']:.2f})")
            if market == "dex":
                exposure = sum(p["value_usd"] for p in snap["positions"] if p["market"] == "dex")
                if exposure + usd_amount > t.max_memecoin_exposure_usd:
                    raise TradeError(f"memecoin exposure would be ${exposure + usd_amount:.2f}, over the "
                                     f"${t.max_memecoin_exposure_usd:.2f} cap")
                report = risk.assess_token(chain, asset, q, t.min_liquidity_usd)
                if report["verdict"] == "AVOID":
                    raise TradeError("scam checker says AVOID: " + "; ".join(report["red_flags"]))
            usd = usd_amount
            qty = usd / q.price
            sl = stop_loss_pct if stop_loss_pct is not None else t.default_stop_loss_pct
            tp = take_profit_pct if take_profit_pct is not None else t.default_take_profit_pct

        trade_id = self.memory.add_trade(
            mode=self.mode, market=market, asset=q.asset or asset, symbol=q.symbol, chain=chain, side=side,
            qty=qty, price=q.price, usd=round(usd, 2), status="pending", reason=reason,
            stop_loss_pct=sl, take_profit_pct=tp)
        try:
            fill = self._paper_fill(self.memory.get_trade(trade_id))
        except Exception as exc:
            self.memory.update_trade(trade_id, status="failed", error=str(exc)[:500])
            raise TradeError(f"paper fill failed: {exc}") from exc
        self.memory.update_trade(trade_id, **fill)
        tr = self.memory.get_trade(trade_id)
        log.info("Paper trade #%s %s %s $%.2f @ %s", trade_id, tr["side"], tr["symbol"], tr["usd"], tr["price"])
        return self._summary(tr)

    def _paper_fill(self, tr: dict) -> dict:
        q = self.quote(tr["market"], tr["asset"], tr["chain"])
        slip = SLIPPAGE[tr["market"]]
        price = q.price * (1 + slip if tr["side"] == "buy" else 1 - slip)
        if tr["side"] == "buy":
            qty, usd = tr["usd"] / price, tr["usd"]
        else:
            qty = min(tr["qty"], self.positions()[asset_key(tr["market"], tr["asset"])]["qty"])
            usd = qty * price
        return {"status": "filled", "price": price, "qty": qty, "usd": round(usd, 2), "filled_at": iso(utcnow())}

    @staticmethod
    def _summary(tr: dict) -> dict:
        return {k: tr[k] for k in ("id", "mode", "market", "symbol", "side", "usd", "price", "qty", "status",
                                   "stop_loss_pct", "take_profit_pct", "error") if tr.get(k) is not None}

    # ---- automatic exits --------------------------------------------------------
    def check_exits(self) -> list[dict]:
        """Sell positions that hit their stop-loss or take-profit. Runs without approval: exits reduce risk."""
        done = []
        for p in list(self.positions().values()):
            try:
                price = self.quote(p["market"], p["asset"], p["chain"]).price
            except TradeError as exc:
                log.debug("no price for %s: %s", p["symbol"], exc)
                continue
            pnl_pct = (p["qty"] * price / p["cost_usd"] - 1) * 100 if p["cost_usd"] else 0
            sl, tp = p.get("stop_loss_pct"), p.get("take_profit_pct")
            why = ("stop-loss" if sl is not None and pnl_pct <= -sl else
                   "take-profit" if tp is not None and pnl_pct >= tp else None)
            if not why:
                continue
            try:
                result = self.place(p["market"], p["asset"], "sell", None, f"{why} at {pnl_pct:+.1f}%",
                                    chain=p["chain"])
            except TradeError as exc:
                self.notify(f"⚠️ {why} sell of {p['symbol']} failed: {exc}")
                continue
            self.notify(f"{'🛑' if why == 'stop-loss' else '💰'} {why.title()}: sold {p['symbol']} "
                        f"({pnl_pct:+.1f}%, ${result.get('usd', 0):.2f}) [paper]")
            done.append(result)
        return done

    def watch(self) -> list[str]:
        """Alert-worthy moves on the watchlist since the last check."""
        if not self.t.watchlist:
            return []
        baseline = self.memory.get_state("watch_baseline", {})
        alerts, quotes = [], []
        stocks = [a for a in self.t.watchlist if data.market_of(a) == "stock"]
        cryptos = [a for a in self.t.watchlist if data.market_of(a) == "crypto"]
        for fetch, assets in ((data.stock_quotes, stocks),
                              (lambda a: data.crypto_quotes(a, self.t.crypto_exchange), cryptos)):
            if not assets:
                continue
            try:
                quotes += fetch(assets)
            except (MarketDataError, Exception) as exc:
                log.warning("watchlist quotes failed: %s", exc)
        for q in quotes:
            prev = baseline.get(q.asset)
            if prev:
                move = (q.price / prev - 1) * 100
                if abs(move) >= self.t.alert_move_pct:
                    alerts.append(f"{'📈' if move > 0 else '📉'} {q.symbol} {move:+.1f}% to {q.price:,.6g}")
                    baseline[q.asset] = q.price
            else:
                baseline[q.asset] = q.price
        self.memory.set_state("watch_baseline", baseline)
        return alerts

    def held_symbols(self) -> set[str]:
        return {p["symbol"] for p in self.positions().values()}
