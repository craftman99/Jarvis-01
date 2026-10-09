"""Memecoin scam checker: combines on-chain security scans with market structure red flags."""

from __future__ import annotations

from .data import MarketDataError, Quote, _get

GOPLUS = "https://api.gopluslabs.io/api/v1/token_security"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens"
EVM_CHAIN_IDS = {"ethereum": "1", "base": "8453", "bsc": "56", "arbitrum": "42161"}


def _flag(value) -> bool:
    return str(value) == "1"


def _pct(value) -> float:
    try:
        return float(value) * 100
    except (TypeError, ValueError):
        return 0.0


def evm_security(chain: str, address: str) -> dict:
    """GoPlus token security for EVM chains. Returns {'red_flags': [...], 'warnings': [...], ...}."""
    data = _get(f"{GOPLUS}/{EVM_CHAIN_IDS[chain]}", params={"contract_addresses": address})
    result = (data or {}).get("result") or {}
    info = result.get(address.lower()) or next(iter(result.values()), None)
    if not info:
        raise MarketDataError("security scan returned no data for this token")
    red, warn = [], []
    if _flag(info.get("is_honeypot")):
        red.append("HONEYPOT: you can buy but not sell")
    if _flag(info.get("cannot_sell_all")):
        red.append("holders cannot sell their full balance")
    if _flag(info.get("hidden_owner")) or _flag(info.get("can_take_back_ownership")):
        red.append("hidden owner / ownership can be reclaimed")
    if _flag(info.get("owner_change_balance")):
        red.append("owner can change balances")
    if _flag(info.get("is_mintable")):
        warn.append("supply can be minted (inflated) by the owner")
    if _flag(info.get("transfer_pausable")):
        warn.append("transfers can be paused")
    if _flag(info.get("is_blacklisted")):
        warn.append("contract has a blacklist")
    if _flag(info.get("slippage_modifiable")):
        warn.append("taxes can be changed by the owner")
    if str(info.get("is_open_source")) == "0":
        red.append("contract source code is not verified")
    buy_tax, sell_tax = _pct(info.get("buy_tax")), _pct(info.get("sell_tax"))
    if max(buy_tax, sell_tax) > 10:
        red.append(f"high taxes (buy {buy_tax:.0f}%, sell {sell_tax:.0f}%)")
    holders = info.get("holders") or []
    top10 = sum(_pct(h.get("percent")) for h in holders[:10] if not _flag(h.get("is_locked")))
    if top10 > 50:
        red.append(f"top 10 wallets hold {top10:.0f}% of supply")
    elif top10 > 30:
        warn.append(f"top 10 wallets hold {top10:.0f}% of supply")
    lp_locked = sum(_pct(h.get("percent")) for h in (info.get("lp_holders") or []) if _flag(h.get("is_locked")))
    if lp_locked < 50:
        warn.append(f"only {lp_locked:.0f}% of liquidity is locked (rug-pull risk)")
    return {"source": "GoPlus", "red_flags": red, "warnings": warn,
            "holders": info.get("holder_count"), "buy_tax_pct": buy_tax, "sell_tax_pct": sell_tax,
            "top10_pct": round(top10, 1), "lp_locked_pct": round(lp_locked, 1)}


def solana_security(address: str) -> dict:
    """RugCheck summary for Solana tokens."""
    data = _get(f"{RUGCHECK}/{address}/report/summary") or {}
    red, warn = [], []
    for risk in data.get("risks") or []:
        text = f"{risk.get('name')}: {risk.get('description') or risk.get('value') or ''}".strip(": ")
        (red if risk.get("level") == "danger" else warn).append(text)
    lp_locked = data.get("lpLockedPct")
    if isinstance(lp_locked, (int, float)) and lp_locked < 50:
        warn.append(f"only {lp_locked:.0f}% of liquidity is locked (rug-pull risk)")
    return {"source": "RugCheck", "red_flags": red, "warnings": warn,
            "rugcheck_score": data.get("score_normalised", data.get("score")), "lp_locked_pct": lp_locked}


def market_flags(q: Quote, min_liquidity_usd: float) -> tuple[list[str], list[str]]:
    red, warn = [], []
    liq = q.extra.get("liquidity_usd") or 0
    if liq < min_liquidity_usd:
        red.append(f"thin liquidity (${liq:,.0f} < ${min_liquidity_usd:,.0f}) - hard to sell")
    age = q.extra.get("age_hours")
    if age is not None and age < 24:
        warn.append(f"pool is only {age:.0f}h old")
    vol = q.volume_24h_usd or 0
    if liq and vol / liq > 20:
        warn.append("volume is >20x liquidity - possible wash trading or a frenzy")
    buys, sells = q.extra.get("buys_24h") or 0, q.extra.get("sells_24h") or 0
    if buys > 50 and sells < buys * 0.1:
        red.append("almost nobody is selling - possible honeypot")
    if (q.change_24h_pct or 0) > 500:
        warn.append(f"up {q.change_24h_pct:.0f}% in 24h - chasing pumps is how people get dumped on")
    return red, warn


def assess_token(chain: str, address: str, quote: Quote, min_liquidity_usd: float) -> dict:
    """Full risk report with a 0-100 risk score and a verdict."""
    red, warn = market_flags(quote, min_liquidity_usd)
    scan: dict = {}
    try:
        scan = solana_security(address) if chain == "solana" else (
            evm_security(chain, address) if chain in EVM_CHAIN_IDS else {})
        if not scan:
            warn.append(f"no security scanner for chain {chain}")
    except MarketDataError as exc:
        warn.append(f"security scan unavailable ({exc}) - treat as high risk")
    red += scan.get("red_flags", [])
    warn += scan.get("warnings", [])
    score = min(100, 30 * len(red) + 8 * len(warn) + 10)  # every memecoin starts at "risky"
    verdict = "AVOID" if red else ("HIGH RISK" if score >= 40 else "SPECULATIVE")
    return {
        "symbol": quote.symbol, "chain": chain, "address": address, "price_usd": quote.price,
        "liquidity_usd": quote.extra.get("liquidity_usd"), "market_cap": quote.extra.get("market_cap"),
        "volume_24h_usd": quote.volume_24h_usd, "change_24h_pct": quote.change_24h_pct,
        "age_hours": quote.extra.get("age_hours"), "risk_score": score, "verdict": verdict,
        "red_flags": red, "warnings": warn,
        **{k: v for k, v in scan.items() if k not in ("red_flags", "warnings")},
    }
