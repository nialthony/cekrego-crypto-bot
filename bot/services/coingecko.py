# bot/services/coingecko.py
import aiohttp
import asyncio
import logging
import time
from datetime import datetime, timezone
import numpy as np
import pandas as pd

from config import COINGECKO_API_KEY

_session = None

def _prune_cache(cache_dict, max_size=300, ttl=300):
    now = time.time()
    expired = [k for k, v in list(cache_dict.items()) if isinstance(v, dict) and now - v.get("time", 0) > ttl]
    for k in expired:
        cache_dict.pop(k, None)
    if len(cache_dict) > max_size:
        excess = len(cache_dict) - int(max_size * 0.8)
        for k in list(cache_dict.keys())[:excess]:
            cache_dict.pop(k, None)

PRICE_CACHE = {}
SEARCH_CACHE = {}
TICKERS_CACHE = {}
FIAT_RATE_CACHE = {}
FIAT_LIST_CACHE = {"data": None, "time": 0}
ANALYSIS_CACHE = {}

async def init_session():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    return _session

async def close_session():
    global _session
    if _session and not _session.closed:
        await _session.close()

async def get_json_url(url, params=None, headers=None):
    session = await init_session()
    try:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            elif resp.status == 429:
                logging.warning(f"Rate limited by {url} (HTTP 429)")
                return None
            else:
                logging.error(f"HTTP Error {resp.status} fetching {url}")
                return None
    except Exception as e:
        logging.error(f"Network error fetching {url}: {e}")
        return None

async def get_json(endpoint, params=None):
    session = await init_session()
    url = f"https://api.coingecko.com/api/v3/{endpoint}"
    headers = {"accept": "application/json"}
    if COINGECKO_API_KEY:
        headers["x-cg-demo-api-key"] = COINGECKO_API_KEY

    try:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            elif resp.status == 429:
                logging.warning(f"Coingecko Rate Limit Hit! (Endpoint: {endpoint})")
                return None
            else:
                logging.error(f"Coingecko API Error {resp.status} on {endpoint}")
                return None
    except Exception as e:
        logging.error(f"Coingecko Network Error on {endpoint}: {e}")
        return None

async def get_price(crypto_id, vs_currency="usd", force_refresh=False):
    cache_key = f"{crypto_id}_{vs_currency}"
    now = time.time()
    _prune_cache(PRICE_CACHE, max_size=300, ttl=60)

    if not force_refresh and cache_key in PRICE_CACHE:
        if now - PRICE_CACHE[cache_key]["time"] < 60:
            return PRICE_CACHE[cache_key]["data"]

    params = {
        "ids": crypto_id,
        "vs_currencies": vs_currency,
        "include_24hr_vol": "true",
        "include_24hr_change": "true",
        "include_market_cap": "true"
    }
    data = await get_json("simple/price", params)
    if data and crypto_id in data:
        PRICE_CACHE[cache_key] = {"data": data, "time": now}
    return data

async def get_fiat_currencies():
    now = time.time()
    if FIAT_LIST_CACHE["data"] and now - FIAT_LIST_CACHE["time"] < 86400:
        return FIAT_LIST_CACHE["data"]

    data = await get_json("simple/supported_vs_currencies")
    if data and isinstance(data, list):
        normalized = sorted({c.upper() for c in data if isinstance(c, str)})
        FIAT_LIST_CACHE["data"] = normalized
        FIAT_LIST_CACHE["time"] = now
        return normalized

    fallback = [
        "USD", "IDR", "EUR", "GBP", "JPY", "SGD", "MYR", "AUD", "CAD",
        "CNY", "HKD", "KRW", "INR", "THB", "PHP", "TWD", "VND", "BRL",
        "RUB", "TRY", "CHF", "NZD", "MXN", "ZAR", "AED", "SAR"
    ]
    FIAT_LIST_CACHE["data"] = sorted(set(fallback))
    FIAT_LIST_CACHE["time"] = now
    return FIAT_LIST_CACHE["data"]

async def search_fiat_currencies(query, limit=12):
    q = (query or "").strip().upper()
    currencies = await get_fiat_currencies()
    if not q:
        return currencies[:limit]

    exact = [c for c in currencies if c == q]
    prefix = [c for c in currencies if c.startswith(q) and c != q]
    contains = [c for c in currencies if q in c and not c.startswith(q)]
    return (exact + prefix + contains)[:limit]

async def get_fiat_rate(base_currency, quote_currency):
    base = (base_currency or "").strip().lower()
    quote = (quote_currency or "").strip().lower()
    if not base or not quote:
        return None
    if base == quote:
        return 1.0

    cache_key = f"{base}_{quote}"
    now = time.time()
    _prune_cache(FIAT_RATE_CACHE, max_size=200, ttl=180)

    if cache_key in FIAT_RATE_CACHE:
        if now - FIAT_RATE_CACHE[cache_key]["time"] < 180:
            return FIAT_RATE_CACHE[cache_key]["rate"]

    data = await get_json("simple/price", {
        "ids": "tether",
        "vs_currencies": f"{base},{quote}"
    })
    if not data or "tether" not in data:
        return None

    tether_rates = data["tether"]
    base_in_usdt = tether_rates.get(base)
    quote_in_usdt = tether_rates.get(quote)
    if not base_in_usdt or not quote_in_usdt:
        return None

    rate = float(quote_in_usdt) / float(base_in_usdt)
    FIAT_RATE_CACHE[cache_key] = {"rate": rate, "time": now}
    return rate

async def convert_fiat(amount, base_currency, quote_currency):
    rate = await get_fiat_rate(base_currency, quote_currency)
    if rate is None:
        return None, None
    try:
        converted = float(amount) * float(rate)
        return converted, rate
    except:
        return None, None

async def search_coin(query):
    now = time.time()
    _prune_cache(SEARCH_CACHE, max_size=300, ttl=300)
    if query in SEARCH_CACHE:
        if now - SEARCH_CACHE[query]["time"] < 300:
            return SEARCH_CACHE[query]["data"]

    data = await get_json("search", {"query": query})
    if data and "coins" in data:
        res = data["coins"]
        SEARCH_CACHE[query] = {"data": res, "time": now}
        return res
    return []

async def get_trending():
    data = await get_json("search/trending")
    return data.get("coins", []) if data else []

async def get_top_coins(limit=50):
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": limit,
        "page": 1,
        "sparkline": "false",
        "price_change_percentage": "24h"
    }
    return await get_json("coins/markets", params) or []

async def get_coin_tickers(coin_id, max_pages=6):
    now = time.time()
    _prune_cache(TICKERS_CACHE, max_size=100, ttl=180)
    if coin_id in TICKERS_CACHE:
        if now - TICKERS_CACHE[coin_id]["time"] < 180:
            return TICKERS_CACHE[coin_id]["data"]

    all_tickers = []
    seen = set()
    for page in range(1, max_pages + 1):
        params = {"page": page}
        data = await get_json(f"coins/{coin_id}/tickers", params=params)
        if not data or not isinstance(data, dict):
            break
        tickers = data.get("tickers") or []
        if not tickers:
            break
        for item in tickers:
            market = (item.get("market") or {}).get("identifier") or (item.get("market") or {}).get("name")
            target = item.get("target")
            key = (market, target, item.get("trade_url"))
            if key not in seen:
                seen.add(key)
                all_tickers.append(item)
        if len(tickers) < 100:
            break

    if all_tickers:
        TICKERS_CACHE[coin_id] = {"data": all_tickers, "time": now}
    return all_tickers

def calculate_advanced_indicators(ohlc_data):
    def _default_tech():
        return {
            "close": 0.0,
            "current_price": 0.0,
            "last_price": 0.0,
            "rsi": 50.0,
            "ema_9": 0.0,
            "ema_21": 0.0,
            "ema_50": 0.0,
            "ema_200": 0.0,
            "ema_cross": "Neutral",
            "trend": "NEUTRAL",
            "global_bias": "NEUTRAL",
            "macd": 0.0,
            "macd_signal": 0.0,
            "macd_hist": 0.0,
            "macd_cross": "Neutral",
            "stoch_k": 50.0,
            "stoch_d": 50.0,
            "bb_upper": 0.0,
            "bb_lower": 0.0,
            "bb_position": "Normal",
            "atr": 0.0,
            "atr_pct": 0.0,
            "support": 0.0,
            "resistance": 0.0,
            "pivot": {"R1": 0.0, "S1": 0.0, "R2": 0.0, "S2": 0.0}
        }

    if not ohlc_data or len(ohlc_data) < 5:
        return _default_tech()

    try:
        df = pd.DataFrame(ohlc_data, columns=['time', 'open', 'high', 'low', 'close'])
        df['time'] = pd.to_datetime(df['time'], unit='ms')
        df.set_index('time', inplace=True)

        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=min(14, len(df))).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=min(14, len(df))).mean()
        rs = gain / (loss.replace(0, np.nan))
        df['RSI'] = 100 - (100 / (1 + rs))

        df['EMA_9'] = df['close'].ewm(span=9, adjust=False).mean()
        df['EMA_21'] = df['close'].ewm(span=21, adjust=False).mean()
        df['EMA_50'] = df['close'].ewm(span=50, adjust=False).mean()
        df['EMA_200'] = df['close'].ewm(span=200, adjust=False).mean()

        ema12 = df['close'].ewm(span=12, adjust=False).mean()
        ema26 = df['close'].ewm(span=26, adjust=False).mean()
        df['MACD'] = ema12 - ema26
        df['MACD_Signal'] = df['MACD'].ewm(span=9, adjust=False).mean()
        df['MACD_Hist'] = df['MACD'] - df['MACD_Signal']

        roll_window = min(14, len(df))
        low_roll = df['low'].rolling(window=roll_window).min()
        high_roll = df['high'].rolling(window=roll_window).max()
        denom = (high_roll - low_roll).replace(0, np.nan)
        df['Stoch_K'] = 100 * ((df['close'] - low_roll) / denom)
        df['Stoch_D'] = df['Stoch_K'].rolling(window=3).mean()

        df['SMA_20'] = df['close'].rolling(window=min(20, len(df))).mean()
        df['STD_20'] = df['close'].rolling(window=min(20, len(df))).std()
        df['BB_Upper'] = df['SMA_20'] + (df['STD_20'] * 2)
        df['BB_Lower'] = df['SMA_20'] - (df['STD_20'] * 2)

        df['TR'] = np.maximum(
            df['high'] - df['low'],
            np.maximum(
                abs(df['high'] - df['close'].shift()),
                abs(df['low'] - df['close'].shift())
            )
        )
        df['ATR'] = df['TR'].rolling(window=min(14, len(df))).mean()

        recent_pivot = df.iloc[-min(5, len(df)):]
        support = float(recent_pivot['low'].min() or 0.0)
        resistance = float(recent_pivot['high'].max() or 0.0)

        latest = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else latest

        def _clean_float(val, default=0.0):
            try:
                f = float(val)
                if np.isnan(f) or np.isinf(f):
                    return default
                return f
            except:
                return default

        close_price = _clean_float(latest['close'])
        ema9_val = _clean_float(latest['EMA_9'], close_price)
        ema21_val = _clean_float(latest['EMA_21'], close_price)
        ema50_val = _clean_float(latest['EMA_50'], ema21_val)
        ema200_val = _clean_float(latest['EMA_200'], ema50_val)
        atr_val = _clean_float(latest.get('ATR'), close_price * 0.02)
        atr_pct = _clean_float((atr_val / close_price * 100) if close_price > 0 else 2.0)

        r1 = _clean_float(resistance, close_price * 1.02)
        s1 = _clean_float(support, close_price * 0.98)
        r2 = _clean_float(r1 * 1.01)
        s2 = _clean_float(s1 * 0.99)

        trend = "UPTREND" if ema9_val > ema21_val else "DOWNTREND"
        bias = "BULLISH" if ema9_val > ema50_val else "BEARISH"

        return {
            "close": close_price,
            "current_price": close_price,
            "last_price": close_price,
            "rsi": _clean_float(latest['RSI'], 50.0),
            "ema_9": ema9_val,
            "ema_21": ema21_val,
            "ema_50": ema50_val,
            "ema_200": ema200_val,
            "ema_cross": "Bullish" if ema9_val > ema21_val else "Bearish",
            "trend": trend,
            "global_bias": bias,
            "macd": _clean_float(latest['MACD']),
            "macd_signal": _clean_float(latest['MACD_Signal']),
            "macd_hist": _clean_float(latest['MACD_Hist']),
            "macd_cross": "Bullish" if latest['MACD'] > latest['MACD_Signal'] and prev['MACD'] <= prev['MACD_Signal'] else ("Bearish" if latest['MACD'] < latest['MACD_Signal'] and prev['MACD'] >= prev['MACD_Signal'] else "Neutral"),
            "stoch_k": _clean_float(latest['Stoch_K'], 50.0),
            "stoch_d": _clean_float(latest['Stoch_D'], 50.0),
            "bb_upper": _clean_float(latest['BB_Upper'], close_price * 1.03),
            "bb_lower": _clean_float(latest['BB_Lower'], close_price * 0.97),
            "bb_position": "Overbought" if close_price >= _clean_float(latest['BB_Upper']) else ("Oversold" if close_price <= _clean_float(latest['BB_Lower']) else "Normal"),
            "atr": atr_val,
            "atr_pct": atr_pct,
            "support": s1,
            "resistance": r1,
            "pivot": {"R1": r1, "S1": s1, "R2": r2, "S2": s2}
        }
    except Exception as e:
        logging.error(f"Error calculating indicators: {e}")
        return _default_tech()


async def get_market_analysis(coin_id):
    now = time.time()
    _prune_cache(ANALYSIS_CACHE, max_size=100, ttl=300)
    if coin_id in ANALYSIS_CACHE:
        if now - ANALYSIS_CACHE[coin_id]["time"] < 300:
            return ANALYSIS_CACHE[coin_id]["data"]

    task_coin_data = get_json(f"coins/{coin_id}", {"localization": "false", "tickers": "false", "community_data": "true", "developer_data": "false"})
    task_ohlc = get_json(f"coins/{coin_id}/ohlc", {"vs_currency": "usd", "days": "1"})
    task_btc_data = get_json("coins/bitcoin", {"localization": "false", "tickers": "false", "community_data": "false", "developer_data": "false"})
    task_fng = get_json_url("https://api.alternative.me/fng/?limit=1")

    coin_data, ohlc, btc_data, fng = await asyncio.gather(
        task_coin_data, task_ohlc, task_btc_data, task_fng,
        return_exceptions=True
    )

    if isinstance(coin_data, Exception) or not coin_data:
        return None

    btc_trend = "Netral"
    btc_change = 0.0
    if not isinstance(btc_data, Exception) and btc_data:
        btc_change = btc_data.get('market_data', {}).get('price_change_percentage_24h', 0) or 0.0
        btc_trend = "Bullish Kuat" if btc_change > 3 else ("Bearish Kuat" if btc_change < -3 else "Konsolidasi")

    fng_value = "50"
    fng_class = "Neutral"
    if not isinstance(fng, Exception) and fng and "data" in fng and len(fng["data"]) > 0:
        fng_value = fng["data"][0].get("value", "50")
        fng_class = fng["data"][0].get("value_classification", "Neutral")

    market = coin_data.get('market_data', {})
    tech = calculate_advanced_indicators(ohlc if not isinstance(ohlc, Exception) else [])

    market_summary = {
        "price": market.get('current_price', {}).get('usd', 0),
        "change_24h": market.get('price_change_percentage_24h', 0) or 0,
        "high_24h": market.get('high_24h', {}).get('usd', 0),
        "low_24h": market.get('low_24h', {}).get('usd', 0),
        "volume": market.get('total_volume', {}).get('usd', 0),
        "market_cap": market.get('market_cap', {}).get('usd', 0),
        "sentiment_up": coin_data.get('sentiment_votes_up_percentage', 50),
        "sentiment_down": coin_data.get('sentiment_votes_down_percentage', 50),
        "fng_index": f"{fng_value} ({fng_class})"
    }

    from .telethon_feed import get_marketfeed_context
    from .gemini_ai import get_gemini_analysis

    feed_context = await get_marketfeed_context(max_items=6, max_age_hours=24)
    ai_raw = await get_gemini_analysis(
        coin_data.get('name', coin_id),
        coin_data.get('symbol', coin_id).upper(),
        market_summary,
        tech,
        btc_trend,
        btc_change,
        marketfeed_context=feed_context
    )

    final_result = {
        "name": coin_data.get('name'),
        "symbol": coin_data.get('symbol').upper(),
        "coin": {
            "name": coin_data.get('name', coin_id),
            "symbol": coin_data.get('symbol', coin_id).upper(),
            "current_price": market_summary.get("price", 0),
            "price_change_percentage_24h": market_summary.get("change_24h", 0)
        },
        "market": market_summary,
        "tech": tech,
        "btc_trend": btc_trend,
        "ai": ai_raw,
        "gemini": ai_raw
    }

    ANALYSIS_CACHE[coin_id] = {"data": final_result, "time": now}
    return final_result

