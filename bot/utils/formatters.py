# bot/utils/formatters.py
import re
from datetime import datetime, timezone, timedelta

UTC = timezone.utc
WIB = timezone(timedelta(hours=7))

COMMON_FIAT_CODES = (
    "IDR", "USD", "EUR", "SGD", "JPY", "CNY", "HKD",
    "MYR", "THB", "VND", "GBP", "AUD"
)

def escape_md(text):
    if text is None:
        return ""
    text = str(text)
    special_chars = r'_*[]()~`>#+-=|{}.!'
    return re.sub(f"([{re.escape(special_chars)}])", r"\\\1", text)

def fmt_usd_raw(price):
    if price is None:
        return "0"
    try:
        price = float(price)
    except:
        return "0"
    if price < 1 and price > -1 and price != 0:
        return f"{price:,.6f}"
    return f"{price:,.2f}"

def fmt_idr_raw(price):
    if price is None:
        return "0"
    try:
        price = float(price)
    except:
        return "0"
    return f"{price:,.0f}"

def fmt_pct_raw(val):
    if val is None:
        return "0.00%"
    try:
        val = float(val)
    except:
        return "0.00%"
    return f"{val:,.2f}%"

def fmt_amount_raw(value):
    if value is None:
        return "0"
    try:
        n = float(value)
    except:
        return "0"
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:,.4f}".rstrip("0").rstrip(".")

def parse_amount_with_suffix(num_str: str, suffix: str = ""):
    amount = float(num_str)
    if suffix == "rb":
        amount *= 1_000
    elif suffix == "jt":
        amount *= 1_000_000
    return amount

def parse_fiat_conversion_text(text: str):
    raw = (text or "").strip()
    m_code = re.match(r"^(\d+(?:\.\d+)?)\s*([a-zA-Z]{3})\s+([a-zA-Z]{3})$", raw)
    if m_code:
        amt = float(m_code.group(1))
        f_from = m_code.group(2).upper()
        f_to = m_code.group(3).upper()
        if f_from in COMMON_FIAT_CODES and f_to in COMMON_FIAT_CODES:
            return amt, f_from, f_to

    m_idr = re.match(r"^(\d+(?:\.\d+)?)(rb|jt)?\s*(idr|rp)?\s+([a-zA-Z]{3})$", raw, re.IGNORECASE)
    if m_idr:
        num_str, suffix, cur_prefix, target_fiat = m_idr.groups()
        suffix = (suffix or "").lower()
        cur_prefix = (cur_prefix or "").lower()
        target_fiat = target_fiat.upper()
        if (suffix or cur_prefix) and target_fiat in COMMON_FIAT_CODES:
            return parse_amount_with_suffix(num_str, suffix), "IDR", target_fiat

    return None

def safe_mdv2_truncate(text: str, limit: int = 3500):
    esc = escape_md(text or "")
    if len(esc) <= limit:
        return esc
    out = esc[:limit]
    while out.endswith("\\"):
        out = out[:-1]
    return out

def parse_time_utc_to_wib_label(hh: int, mm: int):
    base_utc = datetime(2000, 1, 1, hh, mm, tzinfo=UTC)
    wib_dt = base_utc.astimezone(WIB)
    day_note = ""
    if wib_dt.day > base_utc.day:
        day_note = " (+1 hari)"
    elif wib_dt.day < base_utc.day:
        day_note = " (-1 hari)"
    return wib_dt.strftime("%H:%M"), day_note

def convert_utc_time_string_to_wib(time_utc_str: str):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(time_utc_str or ""))
    if not m:
        return "TBA"
    hh = int(m.group(1))
    mm = int(m.group(2))
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        return "TBA"
    wib, note = parse_time_utc_to_wib_label(hh, mm)
    return f"{wib}{note}"

def _safe_float(val, default=0.0):
    try:
        if val is None:
            return default
        return float(val)
    except:
        return default

def _safe_int(val, default=0):
    try:
        if val is None:
            return default
        return int(val)
    except:
        return default

def _clamp(val, min_val, max_val):
    try:
        return max(min_val, min(max_val, val))
    except:
        return min_val

def _parse_ai_number(val):
    if val is None:
        return None
    try:
        return float(val)
    except:
        m = re.search(r"[-+]?\d*\.?\d+", str(val))
        return float(m.group(0)) if m else None

def _normalize_action(action):
    act = str(action or "").strip().upper()
    if act in ("BUY", "LONG"):
        return "LONG"
    if act in ("SELL", "SHORT"):
        return "SHORT"
    return "WAIT"

def _derive_risk_label(score: int):
    if score <= 3:
        return "LOW"
    elif score <= 6:
        return "MEDIUM"
    elif score <= 8:
        return "HIGH"
    return "EXTREME"

def _fit_cell(text, width: int):
    s = str(text or "")
    if len(s) > width:
        return s[:width-2] + ".." if width > 2 else s[:width]
    return s.ljust(width)

def _fmt_compact(val):
    try:
        n = float(val)
    except:
        return "-"
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.2f}K"
    return f"{n:.2f}"

def _fmt_ago_short(ts):
    if not ts:
        return "-"
    import time
    delta = int(time.time() - float(ts))
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"

def extract_first_json(text: str) -> dict:
    """Ambil object JSON pertama dari output AI dengan parser brace-aware."""
    if not text:
        return {}
    import json
    s = str(text)

    start = -1
    depth = 0
    in_str = False
    escape = False

    for i, ch in enumerate(s):
        if start == -1:
            if ch == "{":
                start = i
                depth = 1
                in_str = False
                escape = False
            continue

        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = s[start:i + 1]
                try:
                    return json.loads(candidate)
                except:
                    start = -1
                    depth = 0
                    in_str = False
                    escape = False
    return {}

