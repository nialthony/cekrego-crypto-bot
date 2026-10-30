# bot/services/gemini_ai.py
import asyncio
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
import aiohttp
from google import genai
from google.genai import types

from config import (
    OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_BASE_URL,
    GEMINI_API_KEY, GEMINI_MODEL, GEMINI_MODEL_ASK,
    GEMINI_ASK_PERSONA, GEMINI_ASK_STYLE, GEMINI_ASK_TEMPERATURE
)
from .coingecko import get_json, init_session

GEMINI_SEM = asyncio.Semaphore(5)

_gemini_client = None

def get_gemini_client():
    """Singleton Gemini Client untuk fallback."""
    global _gemini_client
    if _gemini_client is None and GEMINI_API_KEY:
        try:
            _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        except Exception as e:
            logging.error(f"[GEMINI] Failed to init client: {e}")
    return _gemini_client

def get_ai_provider_name() -> str:
    """Mengembalikan nama provider AI aktif."""
    if OPENROUTER_API_KEY:
        return f"OpenRouter ({OPENROUTER_MODEL})"
    if GEMINI_API_KEY:
        return f"Gemini ({GEMINI_MODEL_ASK})"
    return "None"

# ======================================================================
# 🚀 OPENROUTER ASYNC ENGINE (NVIDIA Nemotron 3 Ultra)
# ======================================================================

async def _call_openrouter_async(
    messages: List[Dict[str, str]],
    temperature: float = 0.2,
    max_tokens: int = 400
) -> Optional[Dict[str, Any]]:
    """
    Memanggil OpenRouter Chat Completions API secara non-blocking via aiohttp.
    Default max_tokens diset 400 agar selalu aman di bawah credit limit free tier.
    """
    if not OPENROUTER_API_KEY:
        return None

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://cekrego.id",
        "X-Title": "CekRego Crypto Bot"
    }
    safe_tokens = max(150, min(2500, int(max_tokens)))
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "temperature": float(temperature),
        "max_tokens": safe_tokens
    }

    session = await init_session()
    try:
        async with session.post(
            OPENROUTER_BASE_URL,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=35)
        ) as resp:
            data = await resp.json()
            if resp.status == 200:
                choices = data.get("choices", [])
                if choices:
                    content = choices[0].get("message", {}).get("content", "")
                    return {"text": content, "sources": []}

            err_msg = str(data.get("error", {}).get("message") or f"HTTP {resp.status}")

            # Auto-retry jika terkena limit kredit 402 ("can only afford X")
            if resp.status == 402 and "can only afford" in err_msg:
                match = re.search(r"can only afford (\d+)", err_msg)
                if match:
                    afford = int(match.group(1))
                    retry_tokens = max(120, min(800, afford - 40))
                    payload["max_tokens"] = retry_tokens
                    logging.warning(f"[OPENROUTER] 402 auto-retry with max_tokens={retry_tokens}")
                    async with session.post(
                        OPENROUTER_BASE_URL,
                        headers=headers,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=25)
                    ) as retry_resp:
                        retry_data = await retry_resp.json()
                        if retry_resp.status == 200:
                            retry_choices = retry_data.get("choices", [])
                            if retry_choices:
                                return {"text": retry_choices[0].get("message", {}).get("content", ""), "sources": []}

            logging.error(f"[OPENROUTER] API Error ({resp.status}): {err_msg}")
            return None
    except Exception as e:
        logging.error(f"[OPENROUTER] Request Exception: {e}")
        return None

# ======================================================================
# 🔍 GEMINI FALLBACK SYNC HANDLERS
# ======================================================================

def _extract_gemini_sources(response):
    sources = []
    try:
        if response.candidates and response.candidates[0].grounding_metadata:
            gm = response.candidates[0].grounding_metadata
            if gm.grounding_chunks:
                for chunk in gm.grounding_chunks:
                    if chunk.web:
                        sources.append({"title": chunk.web.title, "url": chunk.web.uri})
    except Exception:
        pass
    return sources

def _call_gemini_sync(api_key, prompt):
    try:
        client = get_gemini_client()
        if not client:
            return None
        response = client.models.generate_content(
            model=GEMINI_MODEL_ASK,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.12
            )
        )
        text = response.text
        return {"text": text, "sources": _extract_gemini_sources(response)}
    except Exception as e:
        logging.error(f"[GEMINI] API Error: {e}")
        return None

# ======================================================================
# 📈 1. MARKET ANALYSIS / PREDICTION (AI SIGNAL)
# ======================================================================

async def get_gemini_analysis(
    coin_name: str,
    symbol: str,
    market_data: Dict[str, Any],
    tech: Dict[str, Any],
    btc_trend: str,
    btc_change: float,
    marketfeed_context: str = ""
) -> Optional[Dict[str, Any]]:
    if not OPENROUTER_API_KEY and not GEMINI_API_KEY:
        return None

    tech_data = tech or {}
    close_price = float(tech_data.get('close') or tech_data.get('current_price') or tech_data.get('last_price') or (market_data or {}).get("price") or 0.0)
    rsi_val = float(tech_data.get('rsi') or 50.0)
    ema_9 = float(tech_data.get('ema_9') or close_price)
    ema_21 = float(tech_data.get('ema_21') or close_price)
    ema_50 = float(tech_data.get('ema_50') or ema_21)
    ema_200 = float(tech_data.get('ema_200') or ema_50)

    ema_trend = "UPWARD CROSS" if ema_9 > ema_50 else "DOWNWARD CROSS" if ema_9 < ema_50 else "STABLE"
    global_bias = str(tech_data.get('global_bias') or ("BULLISH" if ema_9 > ema_21 else "BEARISH"))
    trend = str(tech_data.get('trend') or ("UPWARD" if ema_9 > ema_21 else "DOWNWARD"))

    macd_val = float(tech_data.get('macd') or 0.0)
    macd_signal = float(tech_data.get('macd_signal') or 0.0)
    macd_hist = float(tech_data.get('macd_hist') or 0.0)

    pivot = tech_data.get('pivot') if isinstance(tech_data.get('pivot'), dict) else {}
    r1 = float(pivot.get('R1') or tech_data.get('resistance') or (close_price * 1.02))
    s1 = float(pivot.get('S1') or tech_data.get('support') or (close_price * 0.98))
    r2 = float(pivot.get('R2') or (r1 * 1.01))
    s2 = float(pivot.get('S2') or (s1 * 0.99))

    atr_val = float(tech_data.get('atr') or (close_price * 0.02))
    atr_pct = float(tech_data.get('atr_pct') or ((atr_val / close_price * 100) if close_price > 0 else 2.0))

    chg_24h = float((market_data or {}).get("price_change_percentage_24h") or 0)
    market_cap = float((market_data or {}).get("market_cap") or 0)
    volume_24h = float((market_data or {}).get("total_volume") or 0)
    vol_mcap_pct = (volume_24h / market_cap * 100) if market_cap > 0 else 0.0

    marketfeed_block = ""
    if marketfeed_context:
        marketfeed_block = (
            "\nRECENT MARKETFEED (unverified snippets; use only as hints, not facts):\n"
            f"{marketfeed_context}\n"
        )

    prompt = f"""
You are a crypto trading assistant. Goal: provide a high-accuracy scalping signal that is SAFE and CONSISTENT.
Use ONLY provided data + marketfeed clues. Never invent unsupported values.

ASSETS:
- Coin: {coin_name} ({symbol.upper()})
- Current Price: {close_price:.6f}
- 24h Change: {chg_24h:.2f}%
- Market Cap USD: {market_cap:.2f}
- 24h Volume USD: {volume_24h:.2f}
- Volume/MarketCap: {vol_mcap_pct:.2f}%
- BTC Trend: {btc_trend} (24h change {btc_change:.2f}%)
- Bias: {global_bias}
- Trend: {trend}
- RSI: {rsi_val:.2f}
- EMA Trend: {ema_trend}
- EMA(9/21/50/200): {ema_9:.6f}/{ema_21:.6f}/{ema_50:.6f}/{ema_200:.6f}
- MACD / Signal / Hist: {macd_val:.6f} / {macd_signal:.6f} / {macd_hist:.6f}
- Pivot: R1 {r1:.6f}, S1 {s1:.6f}, R2 {r2:.6f}, S2 {s2:.6f}
- ATR(14): {atr_val:.6f}
- ATR(%): {atr_pct:.3f}%
{marketfeed_block}


DECISION RULES (STRICT):
1) If major risk news exists (hack/exploit, insolvency, delisting, major unlock <=7d, strong regulatory risk) -> action MUST be "WAIT".
2) If trend/momentum conflict strongly (e.g., RSI extreme + weak confirmation) -> action MUST be "WAIT".
3) If action is WAIT: entry/stopLoss/takeProfit/rrr/risk_label/position_risk_pct must be "N/A", risk_score=0.
4) If action is LONG/SHORT:
   - Keep entry close to current price (max deviation 2.5*ATR unless strong reason).
   - Use Pivot + ATR; stop distance 0.6*ATR to 1.8*ATR.
   - Enforce valid geometry:
     LONG -> stopLoss < entry < takeProfit
     SHORT -> takeProfit < entry < stopLoss
   - RRR must be >= 1.2 (prefer 1.5-2.5).
5) Keep reasoning <= 35 words. news_summary <= 28 words.
6) Confidence should reflect uncertainty; do not overstate when signals conflict.

RISK MANAGEMENT:
- Provide risk_score 1..10 (10 = highest risk).
- Provide risk_label: LOW (1-3), MED (4-6), HIGH (7-10).
- Provide position_risk_pct suggestion: LOW=1.0, MED=0.7, HIGH=0.4 (percent of account risked).
- Provide rrr as number (e.g., 1.8). If WAIT, set risk_score=0, risk_label="N/A", position_risk_pct="N/A", rrr="N/A".
- Provide setup_quality 0..100 that reflects signal cleanliness/consistency.

OUTPUT: JSON ONLY (no markdown fences, no extra text)
{{
  "action": "LONG" | "SHORT" | "WAIT",
  "entry": "number or N/A",
  "stopLoss": "number or N/A",
  "takeProfit": "number or N/A",
  "rrr": "number or N/A",
  "risk_score": 0-10,
  "risk_label": "LOW" | "MED" | "HIGH" | "N/A",
  "position_risk_pct": "number or N/A",
  "confidence": 0-100,
  "setup_quality": 0-100,
  "reasoning": "max 40 words",
  "news_summary": "max 30 words"
}}
"""

    # 1. Primary: OpenRouter (NVIDIA Nemotron 3 Ultra)
    if OPENROUTER_API_KEY:
        messages = [
            {"role": "system", "content": "You are a crypto quantitative analysis assistant. Always return valid raw JSON only."},
            {"role": "user", "content": prompt}
        ]
        res = await _call_openrouter_async(messages, temperature=0.2, max_tokens=1500)
        if res and res.get("text"):
            return res

    # 2. Secondary: Fallback to Gemini
    if GEMINI_API_KEY:
        loop = asyncio.get_running_loop()
        async with GEMINI_SEM:
            return await loop.run_in_executor(None, _call_gemini_sync, GEMINI_API_KEY, prompt)

    return None

# ======================================================================
# 💬 2. GENERAL AI CONVERSATION / ASK
# ======================================================================

def _build_ask_system_instruction() -> str:
    persona_map = {
        "analyst": (
            "You are a pragmatic analyst assistant inside a Telegram bot. "
            "Be clear, factual, and critical when needed."
        ),
        "teacher": (
            "You are a patient teacher assistant inside a Telegram bot. "
            "Explain concepts simply and progressively."
        ),
        "friendly": (
            "You are a helpful and friendly assistant inside a Telegram bot. "
            "Be warm but concise."
        ),
        "skeptic": (
            "You are a skeptical analyst assistant inside a Telegram bot. "
            "Challenge weak assumptions and state uncertainty clearly."
        ),
    }
    style_map = {
        "natural": (
            "Answer directly and concisely in Indonesian (Bahasa Indonesia)."
        ),
        "structured": (
            "Answer in Indonesian (Bahasa Indonesia) using this structure when relevant: "
            "'Jawaban singkat', 'Alasan singkat', 'Asumsi/Keterbatasan'. "
            "Keep each section concise."
        ),
        "brief": (
            "Answer in Indonesian (Bahasa Indonesia) in 3-6 short lines maximum."
        ),
    }
    return " ".join([
        persona_map.get(GEMINI_ASK_PERSONA, persona_map["analyst"]),
        style_map.get(GEMINI_ASK_STYLE, style_map["natural"]),
        "If the user asks for code, provide clean code.",
        "Do not expose hidden chain-of-thought; provide only concise reasoning summaries."
    ])

def _call_gemini_general_sync(api_key, user_query, history=None):
    try:
        client = get_gemini_client()
        if not client:
            return None

        sys_instruction = _build_ask_system_instruction()
        contents_payload = user_query
        if history:
            turns = []
            for item in list(history)[-10:]:
                role = str(item.get("role") or "").lower()
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                speaker = str(item.get("speaker") or "").strip()
                text = text[:1000]
                label = "Assistant" if role == "assistant" else (f"User ({speaker})" if speaker else "User")
                turns.append(f"{label}: {text}")

            if turns:
                contents_payload = (
                    "Lanjutkan percakapan berikut dengan memakai konteks sebelumnya. "
                    "Jawab pesan terbaru secara natural dalam Bahasa Indonesia.\n\n"
                    "Riwayat percakapan:\n"
                    f"{chr(10).join(turns)}\n\n"
                    f"Pesan terbaru:\n{user_query}"
                )

        response = client.models.generate_content(
            model=GEMINI_MODEL_ASK,
            contents=contents_payload,
            config=types.GenerateContentConfig(
                system_instruction=sys_instruction,
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=GEMINI_ASK_TEMPERATURE
            )
        )
        return {"text": response.text, "sources": _extract_gemini_sources(response)}
    except Exception as e:
        logging.error(f"[GEMINI] General Chat Error: {e}")
        return None

async def ask_general_question(query: str, history=None) -> Optional[Dict[str, Any]]:
    if not OPENROUTER_API_KEY and not GEMINI_API_KEY:
        return None

    sys_instruction = _build_ask_system_instruction()

    # 1. Primary: OpenRouter (NVIDIA Nemotron 3 Ultra)
    if OPENROUTER_API_KEY:
        messages = [{"role": "system", "content": sys_instruction}]
        if history:
            for item in list(history)[-10:]:
                role = str(item.get("role") or "").lower()
                text = str(item.get("text") or "").strip()
                if not text:
                    continue
                speaker = str(item.get("speaker") or "").strip()
                role_val = "assistant" if role == "assistant" else "user"
                content_val = f"[{speaker}]: {text}" if (role_val == "user" and speaker) else text
                messages.append({"role": role_val, "content": content_val[:1000]})
        messages.append({"role": "user", "content": str(query).strip()})

        res = await _call_openrouter_async(messages, temperature=GEMINI_ASK_TEMPERATURE, max_tokens=1800)
        if res and res.get("text"):
            return res

    # 2. Secondary: Fallback to Gemini
    if GEMINI_API_KEY:
        loop = asyncio.get_running_loop()
        async with GEMINI_SEM:
            return await loop.run_in_executor(None, _call_gemini_general_sync, GEMINI_API_KEY, query, history)

    return None

# ======================================================================
# 📰 3. COIN NEWS DIGEST (/news)
# ======================================================================

def _call_gemini_coin_news_sync(api_key, query):
    try:
        client = get_gemini_client()
        if not client:
            return None
        prompt = f"""
You are a crypto news analyst. Find recent news for coin/topic: {query}
Use Google search grounding.

Return JSON ONLY:
{{
  "topic": "string",
  "sentiment": "bullish|neutral|bearish",
  "summary": "max 60 words",
  "items": [
    {{
      "headline": "max 80 chars",
      "impact": "bullish|neutral|bearish",
      "why": "max 25 words"
    }}
  ]
}}
Rules:
- 3 to 5 items
- If no reliable news, return neutral and empty items
"""
        response = client.models.generate_content(
            model=GEMINI_MODEL_ASK,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.2
            )
        )
        return {"text": response.text, "sources": _extract_gemini_sources(response)}
    except Exception as e:
        logging.error(f"[GEMINI] Coin News Error: {e}")
        return None

async def get_coin_news_digest(query: str) -> Optional[Dict[str, Any]]:
    if not OPENROUTER_API_KEY and not GEMINI_API_KEY:
        return None

    prompt = f"""You are a crypto market analyst. Provide a digest of recent developments for coin/topic: {query}
Return JSON ONLY:
{{
  "topic": "{query}",
  "sentiment": "bullish|neutral|bearish",
  "summary": "max 60 words in Indonesian",
  "items": [
    {{
      "headline": "max 80 chars",
      "impact": "bullish|neutral|bearish",
      "why": "max 25 words in Indonesian"
    }}
  ]
}}
Rules:
- 3 to 5 items
- Output valid JSON only, no markdown formatting.
"""

    if OPENROUTER_API_KEY:
        messages = [
            {"role": "system", "content": "You are a professional crypto market news intelligence analyst. Output raw JSON only."},
            {"role": "user", "content": prompt}
        ]
        res = await _call_openrouter_async(messages, temperature=0.2, max_tokens=1500)
        if res and res.get("text"):
            return res

    if GEMINI_API_KEY:
        loop = asyncio.get_running_loop()
        async with GEMINI_SEM:
            return await loop.run_in_executor(None, _call_gemini_coin_news_sync, GEMINI_API_KEY, query)

    return None

# ======================================================================
# 📅 4. MARKET CALENDAR (/calendar)
# ======================================================================

def _call_gemini_calendar_sync(api_key, days: int = 7):
    try:
        client = get_gemini_client()
        if not client:
            return None
        prompt = f"""
You are a market calendar assistant for crypto traders.
Find important upcoming events in the next {days} days (crypto + macro), prioritize:
- FOMC / CPI / NFP / rates / ETF decisions
- major token unlocks / mainnet / exchange listings if widely reported

Use Google search grounding.

Return JSON ONLY:
{{
  "window_days": {days},
  "events": [
    {{
      "date": "YYYY-MM-DD",
      "time_utc": "HH:MM or TBA",
      "category": "macro|crypto",
      "title": "max 80 chars",
      "impact": "high|med|low",
      "note": "max 25 words"
    }}
  ]
}}
Rules:
- 5 to 12 events
- sort by earliest date/time
- if uncertain time, use TBA
"""
        response = client.models.generate_content(
            model=GEMINI_MODEL_ASK,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.15
            )
        )
        return {"text": response.text, "sources": _extract_gemini_sources(response)}
    except Exception as e:
        logging.error(f"[GEMINI] Calendar Error: {e}")
        return None

async def get_market_calendar(days: int = 7) -> Optional[Dict[str, Any]]:
    if not OPENROUTER_API_KEY and not GEMINI_API_KEY:
        return None

    days = max(1, min(int(days), 30))
    prompt = f"""You are a market calendar assistant for crypto traders.
List important upcoming macro & crypto market events for the next {days} days (FOMC, CPI, NFP, token unlocks, network upgrades, ETF decisions).
Return JSON ONLY:
{{
  "window_days": {days},
  "events": [
    {{
      "date": "YYYY-MM-DD",
      "time_utc": "HH:MM or TBA",
      "category": "macro|crypto",
      "title": "max 80 chars",
      "impact": "high|med|low",
      "note": "max 25 words in Indonesian"
    }}
  ]
}}
Rules:
- 5 to 10 events
- Output valid JSON only.
"""

    if OPENROUTER_API_KEY:
        messages = [
            {"role": "system", "content": "You are a macroeconomic and crypto calendar specialist. Output raw JSON only."},
            {"role": "user", "content": prompt}
        ]
        res = await _call_openrouter_async(messages, temperature=0.15, max_tokens=1500)
        if res and res.get("text"):
            return res

    if GEMINI_API_KEY:
        loop = asyncio.get_running_loop()
        async with GEMINI_SEM:
            return await loop.run_in_executor(None, _call_gemini_calendar_sync, GEMINI_API_KEY, days)

    return None

# ======================================================================
# 🎯 5. COIN THESIS (/thesis)
# ======================================================================

def _call_gemini_coin_thesis_sync(api_key, coin_name, symbol, market_snapshot):
    try:
        client = get_gemini_client()
        if not client:
            return None

        price = float(market_snapshot.get("current_price") or 0)
        mcap = float(market_snapshot.get("market_cap") or 0)
        vol = float(market_snapshot.get("total_volume") or 0)
        chg = float(market_snapshot.get("price_change_percentage_24h") or 0)
        rank = market_snapshot.get("market_cap_rank") or "N/A"

        prompt = f"""
You are a crypto investment analyst. Build a balanced thesis for this coin using grounded web search and the provided snapshot.

COIN:
- Name: {coin_name}
- Symbol: {str(symbol).upper()}
- Price USD: {price}
- Market Cap USD: {mcap}
- 24h Volume USD: {vol}
- 24h Change %: {chg}
- Market Cap Rank: {rank}

TASK:
Create a practical thesis for traders/investors in Indonesian language (Bahasa Indonesia), concise but useful.

Return JSON ONLY:
{{
  "coin": "{coin_name}",
  "symbol": "{str(symbol).upper()}",
  "stance": "bullish|neutral|bearish",
  "horizon": "scalp|swing|position",
  "one_liner": "max 25 words",
  "bull_case": ["3-5 bullet points"],
  "bear_case": ["3-5 bullet points"],
  "watch_signals": ["3-5 bullet points"],
  "invalidation": ["2-4 bullet points"],
  "risk_level": "low|med|high",
  "summary": "max 70 words"
}}

Rules:
- Mention uncertainty if data/news mixed.
- Avoid price target hallucinations.
"""
        response = client.models.generate_content(
            model=GEMINI_MODEL_ASK,
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=0.25
            )
        )
        return {"text": response.text, "sources": _extract_gemini_sources(response)}
    except Exception as e:
        logging.error(f"[GEMINI] Thesis Error: {e}")
        return None

async def get_coin_thesis(coin_id: str) -> Optional[Dict[str, Any]]:
    if not OPENROUTER_API_KEY and not GEMINI_API_KEY:
        return None

    detail = await get_json("/coins/markets", {"vs_currency": "usd", "ids": coin_id})
    if not detail:
        return None
    coin = detail[0]

    coin_name = coin.get("name")
    symbol = str(coin.get("symbol", "")).upper()
    price = float(coin.get("current_price") or 0)
    mcap = float(coin.get("market_cap") or 0)
    vol = float(coin.get("total_volume") or 0)
    chg = float(coin.get("price_change_percentage_24h") or 0)
    rank = coin.get("market_cap_rank") or "N/A"

    prompt = f"""You are a crypto investment analyst. Build a balanced thesis for this coin using the provided market snapshot.

COIN:
- Name: {coin_name}
- Symbol: {symbol}
- Price USD: {price}
- Market Cap USD: {mcap}
- 24h Volume USD: {vol}
- 24h Change %: {chg}
- Market Cap Rank: {rank}

TASK:
Create a practical thesis for traders/investors in Indonesian language (Bahasa Indonesia), concise but useful.

Return JSON ONLY:
{{
  "coin": "{coin_name}",
  "symbol": "{symbol}",
  "stance": "bullish|neutral|bearish",
  "horizon": "scalp|swing|position",
  "one_liner": "max 25 words",
  "bull_case": ["3-5 bullet points"],
  "bear_case": ["3-5 bullet points"],
  "watch_signals": ["3-5 bullet points"],
  "invalidation": ["2-4 bullet points"],
  "risk_level": "low|med|high",
  "summary": "max 70 words"
}}
Rules: Output raw JSON only.
"""

    if OPENROUTER_API_KEY:
        messages = [
            {"role": "system", "content": "You are a professional crypto investment thesis researcher. Output raw JSON only."},
            {"role": "user", "content": prompt}
        ]
        res = await _call_openrouter_async(messages, temperature=0.25, max_tokens=1500)
        if res and res.get("text"):
            return {"coin": coin, "thesis": res}

    if GEMINI_API_KEY:
        loop = asyncio.get_running_loop()
        async with GEMINI_SEM:
            thesis = await loop.run_in_executor(
                None,
                _call_gemini_coin_thesis_sync,
                GEMINI_API_KEY,
                coin.get("name"),
                coin.get("symbol"),
                coin
            )
        if thesis:
            return {"coin": coin, "thesis": thesis}

    return None

# Backward compatibility aliases
get_ai_analysis = get_gemini_analysis
ask_ai = ask_general_question
