# bot/services/x70_signals.py
import asyncio
import logging
import time
from typing import Dict, Any, Optional, List
import aiohttp

from config import X70_SIGNALS_API_KEY, X70_SIGNALS_BASE_URL
from .coingecko import init_session

# Cache signals: {"time": ts, "data": [...]}
_SIGNALS_CACHE: Dict[str, Dict[str, Any]] = {}
_SIGNALS_CACHE_TTL = 45  # 45 detik cache agar responsif dan hemat rate limit

def _get_headers(api_key: Optional[str] = None) -> Dict[str, str]:
    key = (api_key or X70_SIGNALS_API_KEY or "").strip()
    return {
        "X-Api-Key": key,
        "Accept": "application/json",
        "User-Agent": "CekRego-CryptoBot/2.0"
    }

async def get_x70_signals(
    coin: Optional[str] = None,
    limit: int = 10,
    api_key: Optional[str] = None,
    force_refresh: bool = False
) -> List[Dict[str, Any]]:
    """
    Mengambil sinyal quant live dari signals.x70.ai (roman-rr/trading-skills).
    Mendukung filter coin spesifik (misal 'BTC') dan pagination.
    """
    key = (api_key or X70_SIGNALS_API_KEY or "").strip()
    if not key:
        return []

    cache_key = f"{coin or 'ALL'}:{limit}"
    now = time.time()
    if not force_refresh and cache_key in _SIGNALS_CACHE:
        c = _SIGNALS_CACHE[cache_key]
        if now - c.get("time", 0) < _SIGNALS_CACHE_TTL:
            return c.get("data", [])

    url = f"{X70_SIGNALS_BASE_URL}/api/skill/signals"
    params = {"limit": str(max(1, min(50, limit)))}
    if coin:
        clean_coin = str(coin).strip().upper()
        if clean_coin.endswith("USDT"):
            clean_coin = clean_coin[:-4]
        params["coin"] = clean_coin

    session = await init_session()
    try:
        async with session.get(url, headers=_get_headers(key), params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                signals = data.get("signals", [])
                _SIGNALS_CACHE[cache_key] = {"time": now, "data": signals}
                return signals
            else:
                err_txt = await resp.text()
                logging.warning(f"[X70_SIGNALS] API HTTP {resp.status}: {err_txt[:150]}")
    except Exception as e:
        logging.error(f"[X70_SIGNALS] Request exception: {e}")

    return _SIGNALS_CACHE.get(cache_key, {}).get("data", [])

async def get_x70_signal_for_coin(coin: str, api_key: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Mengambil sinyal quant live x70.ai khusus untuk koin tertentu (e.g. 'BTC').
    """
    clean_coin = str(coin).strip().upper()
    if clean_coin.endswith("USDT"):
        clean_coin = clean_coin[:-4]

    signals = await get_x70_signals(coin=clean_coin, limit=5, api_key=api_key)
    for s in signals:
        if str(s.get("coin", "")).upper() == clean_coin:
            return s
    return None

async def get_x70_stats(api_key: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Mengambil metrik performa & track record dari signals.x70.ai.
    """
    key = (api_key or X70_SIGNALS_API_KEY or "").strip()
    if not key:
        return None

    url = f"{X70_SIGNALS_BASE_URL}/api/skill/stats"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_get_headers(key), timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception as e:
        logging.error(f"[X70_SIGNALS] Stats exception: {e}")
    return None

def format_x70_signal_card(signal: Dict[str, Any]) -> str:
    """Format kartu sinyal x70.ai untuk ditampilkan di chat Telegram."""
    coin = signal.get("coin", "CRYPTO").upper()
    direction = str(signal.get("direction", "NEUTRAL")).upper()
    dir_badge = "🟢 <b>BULLISH (LONG)</b>" if direction == "BULLISH" else ("🔴 <b>BEARISH (SHORT)</b>" if direction == "BEARISH" else "⚪ <b>NEUTRAL</b>")
    conf = signal.get("confidence", 0)

    entry = float(signal.get("entryPrice") or 0.0)
    sl = float(signal.get("stopLoss") or 0.0)
    tp = float(signal.get("takeProfit") or 0.0)
    lev = int(signal.get("leverage") or 2)
    rrr = float(signal.get("riskRewardRatio") or 0.0)

    tags = signal.get("tags") or []
    tags_str = ", ".join(f"#{t}" for t in tags[:5]) if tags else "-"

    market_data = signal.get("marketData") or []
    md_lines = []
    for item in market_data[:3]:
        md_lines.append(f"• <i>{item}</i>")
    md_block = "\n".join(md_lines) if md_lines else "• <i>Analisis anomali volume & orderbook aktif</i>"

    def _fmt(val):
        if val >= 100:
            return f"${val:,.2f}"
        elif val >= 1:
            return f"${val:,.4f}"
        return f"${val:,.6f}"

    return (
        f"⚡ <b>[X70 QUANT SIGNAL — TRADING SKILLS]</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 <b>Pair:</b> #{coin}USDT\n"
        f"🎯 <b>Bias Quant:</b> {dir_badge}\n"
        f"🧠 <b>Confidence:</b> {conf}% (33 Research Algorithms)\n\n"
        f"💵 <b>Entry:</b> <code>{_fmt(entry)}</code>\n"
        f"🎯 <b>Take Profit:</b> <code>{_fmt(tp)}</code>\n"
        f"🛡️ <b>Stop Loss:</b> <code>{_fmt(sl)}</code>\n"
        f"⚡ <b>Leverage Rekomendasi:</b> {lev}x (R/R: 1:{rrr:.1f})\n\n"
        f"📊 <b>Market Microstructure & Whales:</b>\n"
        f"{md_block}\n\n"
        f"🏷️ <b>Tags:</b> <code>{tags_str}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>Sumber: signals.x70.ai (Verified Quantitative MoE Engine)</i>"
    )
