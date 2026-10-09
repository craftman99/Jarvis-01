"""Market data: stocks (Alpaca), crypto on exchanges (ccxt), and on-chain memecoins (DexScreener)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

import requests

DEXSCREENER = "https://api.dexscreener.com"
ALPACA_DATA = "https://data.alpaca.markets/v2"


class MarketDataError(RuntimeError):
    pass


@dataclass
class Quote:
    market: str                 # stock | crypto | dex
    asset: str                  # AAPL / DOGE/USD / token address
    symbol: str
    price: float
    change_24h_pct: float | None = None
    volume_24h_usd: float | None = None
    extra: dict = field(default_factory=dict)


def market_of(asset: str) -> str:
    """'DOGE/USD' -> crypto, 'AAPL' -> stock."""
    return "crypto" if "/" in asset else "stock"


def _get(url: str, **kwargs) -> dict | list:
    try:
        resp = requests.get(url, timeout=20, **kwargs)
    except requests.RequestException as exc:
        raise MarketDataError(f"network error: {exc}") from exc
    if resp.status_code >= 400:
        raise MarketDataError(f"{url.split('?')[0]} returned HTTP {resp.status_code}")
    return resp.json()


def _f(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---- stocks ------------------------------------------------------------------

def _alpaca_headers() -> dict:
    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise MarketDataError("stock prices need ALPACA_API_KEY / ALPACA_SECRET_KEY in .env (free paper account at alpaca.markets)")
    return {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}


def stock_quotes(symbols: list[str]) -> list[Quote]:
    data = _get(f"{ALPACA_DATA}/stocks/snapshots", params={"symbols": ",".join(symbols)},
                headers=_alpaca_headers())
    quotes = []
    for sym, snap in (data or {}).items():
        if not snap:
            continue
        price = _f((snap.get("latestTrade") or {}).get("p")) or _f((snap.get("dailyBar") or {}).get("c"))
        prev = _f((snap.get("prevDailyBar") or {}).get("c"))
        day = snap.get("dailyBar") or {}
        if price is None:
            continue
        quotes.append(Quote("stock", sym, sym, price,
                            change_24h_pct=round((price / prev - 1) * 100, 2) if prev else None,
                            volume_24h_usd=(_f(day.get("v")) or 0) * price,
                            extra={"day_high": day.get("h"), "day_low": day.get("l"), "open": day.get("o")}))
    return quotes


# ---- crypto on a centralized exchange ------------------------------------------

_exchanges: dict = {}


def exchange(name: str):
    """Public (no keys) exchange client - used for prices only."""
    import ccxt

    if name not in _exchanges:
        _exchanges[name] = getattr(ccxt, name)({"enableRateLimit": True})
    return _exchanges[name]


def crypto_quotes(symbols: list[str], exchange_name: str) -> list[Quote]:
    ex = exchange(exchange_name)
    quotes = []
    for sym in symbols:
        try:
            t = ex.fetch_ticker(sym)
        except Exception as exc:
            raise MarketDataError(f"{exchange_name} has no ticker for {sym}: {exc}") from exc
        price = _f(t.get("last")) or _f(t.get("close"))
        if price is None:
            continue
        quotes.append(Quote("crypto", sym, sym.split("/")[0], price, change_24h_pct=_f(t.get("percentage")),
                            volume_24h_usd=_f(t.get("quoteVolume")),
                            extra={"high_24h": t.get("high"), "low_24h": t.get("low")}))
    return quotes


# ---- on-chain tokens (memecoins) --------------------------------------------------

def _pair_to_quote(pair: dict) -> Quote:
    base = pair.get("baseToken") or {}
    txns = (pair.get("txns") or {}).get("h24") or {}
    created = pair.get("pairCreatedAt")
    return Quote(
        "dex", base.get("address", ""), base.get("symbol", "?"), _f(pair.get("priceUsd")) or 0.0,
        change_24h_pct=_f((pair.get("priceChange") or {}).get("h24")),
        volume_24h_usd=_f((pair.get("volume") or {}).get("h24")),
        extra={
            "chain": pair.get("chainId"),
            "name": base.get("name"),
            "liquidity_usd": _f((pair.get("liquidity") or {}).get("usd")) or 0.0,
            "market_cap": _f(pair.get("marketCap")) or _f(pair.get("fdv")),
            "change_1h_pct": _f((pair.get("priceChange") or {}).get("h1")),
            "change_5m_pct": _f((pair.get("priceChange") or {}).get("m5")),
            "buys_24h": txns.get("buys"), "sells_24h": txns.get("sells"),
            "age_hours": round((time.time() * 1000 - created) / 3.6e6, 1) if created else None,
            "dex": pair.get("dexId"), "url": pair.get("url"),
        },
    )


def token_quote(chain: str, address: str) -> Quote:
    """Best (most liquid) pool for a token."""
    pairs = _get(f"{DEXSCREENER}/tokens/v1/{chain}/{address}")
    pairs = [p for p in (pairs or []) if (p.get("baseToken") or {}).get("address", "").lower() == address.lower()]
    if not pairs:
        raise MarketDataError(f"no trading pools found for {address} on {chain}")
    best = max(pairs, key=lambda p: _f((p.get("liquidity") or {}).get("usd")) or 0)
    return _pair_to_quote(best)


def token_quotes(chain: str, addresses: list[str]) -> list[Quote]:
    out = []
    for i in range(0, len(addresses), 30):  # API accepts up to 30 addresses per call
        pairs = _get(f"{DEXSCREENER}/tokens/v1/{chain}/{','.join(addresses[i:i + 30])}") or []
        best: dict[str, dict] = {}
        for p in pairs:
            addr = (p.get("baseToken") or {}).get("address", "")
            if addr and (_f((p.get("liquidity") or {}).get("usd")) or 0) >= (
                    _f((best.get(addr, {}).get("liquidity") or {}).get("usd")) or 0):
                best[addr] = p
        out.extend(_pair_to_quote(p) for p in best.values())
    return out


def new_tokens(chains: list[str], limit: int = 30) -> list[dict]:
    """Recently listed/promoted tokens on the given chains."""
    seen, out = set(), []
    for endpoint in ("token-profiles/latest/v1", "token-boosts/latest/v1"):
        try:
            items = _get(f"{DEXSCREENER}/{endpoint}") or []
        except MarketDataError:
            continue
        for item in items:
            key = (item.get("chainId"), item.get("tokenAddress"))
            if key[0] in chains and key[1] and key not in seen:
                seen.add(key)
                out.append({"chain": key[0], "address": key[1],
                            "description": (item.get("description") or "")[:200]})
    return out[:limit]


def search_tokens(query: str) -> list[Quote]:
    data = _get(f"{DEXSCREENER}/latest/dex/search", params={"q": query})
    return [_pair_to_quote(p) for p in (data or {}).get("pairs", [])[:10]]
