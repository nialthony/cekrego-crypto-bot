# bot/services/x70_signals.py
import time
import logging
from typing import Optional, Dict, Any, List
from config import X70_SIGNALS_API_KEY, X70_SIGNALS_BASE_URL
from .coingecko import init_session

_SIGNALS_CACHE: Dict[str, Any] = {"data": [], "ts": 0}
_CACHE_TTL = 120  # 2 menit


async def get_x70_signals(limit: int = 10) -> List[Dict[str, Any]]:
    """
    Mengambil sinyal quant real-time dari signals.x70.ai (berbasis roman-rr/trading-skills).
    Menggunakan endpoint /api/signals atau /api/signals/free sebagai fallback.
    """
    now = time.time()
    if _SIGNALS_CACHE["data"] and (now - _SIGNALS_CACHE["ts"] < _CACHE_TTL):
        return _SIGNALS_CACHE["data"][:limit]

    headers = {"Accept": "application/json", "User-Agent": "CekRegoBot/2.0"}
    if X70_SIGNALS_API_KEY:
        headers["Authorization"] = f"Bearer {X70_SIGNALS_API_KEY}"
        url = f"{X70_SIGNALS_BASE_URL}/api/signals?status=ACTIVE&limit={limit}"
    else:
        url = f"{X70_SIGNALS_BASE_URL}/api/signals/free"

    try:
        session = await init_session()
        async with session.get(url, headers=headers) as resp:
            if resp.status in (401, 402, 403) and url.endswith("/api/signals"):
                free_url = f"{X70_SIGNALS_BASE_URL}/api/signals/free"
                async with session.get(free_url, headers={"Accept": "application/json"}) as resp2:
                    if resp2.status != 200:
                        return []
                    raw = await resp2.json()
            elif resp.status == 200:
                raw = await resp.json()
            else:
                logging.warning(f"x70 signals status {resp.status}")
                return []

            signals = []
            if isinstance(raw, dict):
                if "signals" in raw and isinstance(raw["signals"], list):
                    signals = raw["signals"]
                elif "signal" in raw and isinstance(raw["signal"], dict):
                    signals = [raw["signal"]]
                elif "data" in raw and isinstance(raw["data"], list):
                    signals = raw["data"]
            elif isinstance(raw, list):
                signals = raw

            if signals:
                _SIGNALS_CACHE["data"] = signals
                _SIGNALS_CACHE["ts"] = now

            return signals[:limit]
    except Exception as e:
        logging.error(f"Error fetching x70 signals: {e}")
        return _SIGNALS_CACHE["data"][:limit] if _SIGNALS_CACHE["data"] else []


async def get_x70_signal_for_coin(symbol: str) -> Optional[Dict[str, Any]]:
    """Cari sinyal x70 yang aktif untuk koin tertentu (misal BTC, ETH, SOL)."""
    clean_sym = symbol.upper().replace("USDT", "").replace("USD", "").strip()
    signals = await get_x70_signals(limit=25)
    for sig in signals:
        coin = str(sig.get("coin") or sig.get("symbol") or "").upper().replace("USDT", "")
        if coin == clean_sym:
            return sig
    return None


def format_x70_signal_text(signal: Dict[str, Any]) -> str:
    """Format teks ringkas sinyal x70 untuk disuntikkan ke dalam prompt AI."""
    if not signal:
        return "Tidak ada sinyal quant x70 aktif untuk koin ini."

    coin = signal.get("coin", "UNKNOWN")
    direction = signal.get("direction", "NEUTRAL")
    conf = signal.get("confidence", 0)
    entry = signal.get("entryPrice", 0)
    sl = signal.get("stopLoss", 0)
    tp = signal.get("takeProfit", 0)
    lev = signal.get("leverage", 1)
    rrr = signal.get("riskRewardRatio", 0)
    tags = ", ".join(signal.get("tags") or [])
    mdata = "; ".join(signal.get("marketData") or [])

    return (
        f"[X70 QUANT SIGNAL] Coin: {coin} | Direction: {direction} | Confidence: {conf}% | "
        f"Entry: {entry} | TP: {tp} | SL: {sl} | Recommended Leverage: {lev}x | R/R: {rrr} | "
        f"Tags: {tags} | Market Microstructure & Whale Flow: {mdata}"
    )


def format_x70_signal_card(signal: Dict[str, Any]) -> str:
    """Format kartu sinyal x70.ai dengan tampilan bersih dan minimalis."""
    coin = signal.get("coin", "CRYPTO").upper()
    direction = str(signal.get("direction", "NEUTRAL")).upper()
    dir_str = "LONG / BULLISH" if direction == "BULLISH" else ("SHORT / BEARISH" if direction == "BEARISH" else "NEUTRAL")
    conf = signal.get("confidence", 0)

    entry = float(signal.get("entryPrice") or 0.0)
    sl = float(signal.get("stopLoss") or 0.0)
    tp = float(signal.get("takeProfit") or 0.0)
    lev = int(signal.get("leverage") or 2)
    rrr = float(signal.get("riskRewardRatio") or 0.0)

    tags = signal.get("tags") or []
    tags_str = ", ".join(f"#{t}" for t in tags[:5]) if tags else "-"

    market_data = signal.get("marketData") or []
    md_lines = [f"- {item}" for item in market_data[:3]]
    md_block = "\n".join(md_lines) if md_lines else "- Volume & orderbook analysis active"

    def _fmt(val):
        if val >= 100:
            return f"${val:,.2f}"
        elif val >= 1:
            return f"${val:,.4f}"
        return f"${val:,.6f}"

    return (
        f"<pre>"
        f"QUANT SIGNAL: {coin}USDT\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Bias       : {dir_str}\n"
        f"Confidence : {conf}%\n"
        f"Entry      : {_fmt(entry)}\n"
        f"Take Profit: {_fmt(tp)}\n"
        f"Stop Loss  : {_fmt(sl)}\n"
        f"Leverage   : {lev}x (R/R 1:{rrr:.1f})\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"Whale &amp; Flow:\n"
        f"{md_block}\n\n"
        f"Tags: {tags_str}\n"
        f"Source: signals.x70.ai"
        f"</pre>"
    )
