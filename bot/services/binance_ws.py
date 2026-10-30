# bot/services/binance_ws.py
import json
import time
import asyncio
import logging
import aiohttp
from collections import deque
from datetime import datetime, timezone

from config import BINANCE_LIQ_WS_URL, LIQUIDATION_SYMBOLS

http_session = None
LIQUIDATION_EVENTS = deque(maxlen=500)
LIQUIDATION_LOCK = asyncio.Lock()
LIQUIDATION_LAST_WS_TS = 0
LIQUIDATION_WS_CONNECTED = False
LIQUIDATION_BUFFER = deque(maxlen=300)
LIQUIDATION_FEED_RUNNING = False
_last_ws_msg_time = 0


def _normalize_liq_event(raw):
    """
    Normalize Binance futures forceOrder payload into one dict.
    Supports multiple payload shapes.
    """
    if not raw:
        return None

    data = raw.get("data", raw) if isinstance(raw, dict) else raw
    if isinstance(data, list):
        out = []
        for item in data:
            ev = _normalize_liq_event(item)
            if ev:
                out.append(ev)
        return out

    o = data.get("o", data.get("order", {})) if isinstance(data, dict) else {}
    if not o:
        return None

    symbol = str(o.get("s") or "").upper()
    side = str(o.get("S") or "").upper()  # SELL means long liquidation, BUY means short liquidation
    qty = float(o.get("q") or 0)
    price = float(o.get("p") or 0)
    avg_price = float(o.get("ap") or price or 0)
    event_ts = int(data.get("E") or o.get("T") or 0)
    notional = qty * (avg_price or price)

    if not symbol or qty <= 0:
        return None

    # Binance force order side semantics:
    # SELL liquidation usually closes a long position -> LONG liq
    # BUY liquidation usually closes a short position -> SHORT liq
    liq_side = "LONG" if side == "SELL" else "SHORT" if side == "BUY" else side

    return {
        "symbol": symbol,
        "side": liq_side,
        "raw_side": side,
        "qty": qty,
        "price": avg_price or price,
        "notional_usd": notional,
        "timestamp": event_ts,
    }

async def liquidation_ws_loop():
    global LIQUIDATION_LAST_WS_TS, LIQUIDATION_WS_CONNECTED
    ws_url = "wss://fstream.binance.com/ws/!forceOrder@arr"

    while True:
        local_session = False
        session = None
        try:
            session = http_session
            if session is None:
                session = aiohttp.ClientSession()
                local_session = True

            async with session.ws_connect(ws_url, heartbeat=30, autoping=True) as ws:
                LIQUIDATION_WS_CONNECTED = True
                logging.info("🟢 Binance liquidation WS connected")

                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            payload = json.loads(msg.data)
                        except Exception:
                            continue

                        normalized = _normalize_liq_event(payload)
                        if not normalized:
                            continue

                        items = normalized if isinstance(normalized, list) else [normalized]
                        now_ms = int(time.time() * 1000)
                        async with LIQUIDATION_LOCK:
                            for item in items:
                                LIQUIDATION_EVENTS.append(item)
                                LIQUIDATION_LAST_WS_TS = max(LIQUIDATION_LAST_WS_TS, int(item.get("timestamp") or now_ms))
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                        break

            LIQUIDATION_WS_CONNECTED = False
        except asyncio.CancelledError:
            LIQUIDATION_WS_CONNECTED = False
            raise
        except Exception as e:
            LIQUIDATION_WS_CONNECTED = False
            logging.warning(f"[LIQ_WS] reconnecting after error: {e}")
        finally:
            if local_session and session is not None:
                try:
                    await session.close()
                except Exception:
                    pass

        await asyncio.sleep(5)

async def get_recent_liquidations(limit=50, max_age_sec=3600, symbols=None):
    symbols_set = {str(s).upper() for s in (symbols or []) if s}
    cutoff_ms = int((time.time() - max_age_sec) * 1000)
    async with LIQUIDATION_LOCK:
        rows = list(LIQUIDATION_EVENTS)

    out = []
    for item in reversed(rows):
        if int(item.get("timestamp") or 0) < cutoff_ms:
            continue
        if symbols_set and item.get("symbol") not in symbols_set:
            continue
        out.append(item)
        if len(out) >= limit:
            break
    return out

def get_liquidation_feed_status():
    return {
        "connected": LIQUIDATION_WS_CONNECTED,
        "last_ws_ts": LIQUIDATION_LAST_WS_TS,
        "buffer_size": len(LIQUIDATION_EVENTS),
    }

# --- CORE API FUNCTIONS ---
