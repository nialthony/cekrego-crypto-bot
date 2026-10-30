# bot/handlers/ai_helpers.py
import time
import re
from bot.utils import escape_md, _safe_float, _fit_cell, _fmt_ago_short

ASK_THREAD_CACHE = {}
ASK_THREAD_MSG_INDEX = {}
ASK_THREAD_TTL = 7200  # detik
ASK_THREAD_MAX_TURNS = 16

def _cleanup_ask_thread_cache():
    now = time.time()
    expired_roots = [
        root_key for root_key, item in ASK_THREAD_CACHE.items()
        if now - float(item.get("updated", item.get("time", 0)) or 0) > ASK_THREAD_TTL
    ]
    if not expired_roots:
        return

    expired_set = set(expired_roots)
    for root_key in expired_roots:
        ASK_THREAD_CACHE.pop(root_key, None)

    stale_msg_keys = [k for k, v in ASK_THREAD_MSG_INDEX.items() if v in expired_set]
    for k in stale_msg_keys:
        ASK_THREAD_MSG_INDEX.pop(k, None)

def _ask_speaker_label(user):
    if not user:
        return "user"
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"
    full_name = getattr(user, "full_name", None)
    if full_name:
        return str(full_name)
    return str(getattr(user, "id", "user"))

def _create_ask_thread(chat_id: int, bot_message_id: int, user, user_text: str, bot_text: str):
    _cleanup_ask_thread_cache()
    now = time.time()
    root_key = (int(chat_id), int(bot_message_id))
    history = []
    if user_text:
        history.append({"role": "user", "text": str(user_text), "speaker": _ask_speaker_label(user)})
    if bot_text:
        history.append({"role": "assistant", "text": str(bot_text), "speaker": "bot"})

    ASK_THREAD_CACHE[root_key] = {
        "time": now,
        "updated": now,
        "owner_user_id": getattr(user, "id", None),
        "history": history[-ASK_THREAD_MAX_TURNS:],
    }
    ASK_THREAD_MSG_INDEX[root_key] = root_key
    return root_key

def _get_ask_thread_by_reply(chat_id: int, replied_bot_message_id: int):
    _cleanup_ask_thread_cache()
    idx_key = (int(chat_id), int(replied_bot_message_id))
    root_key = ASK_THREAD_MSG_INDEX.get(idx_key)
    if not root_key:
        return None, None
    thread = ASK_THREAD_CACHE.get(root_key)
    return root_key, thread

def _append_ask_thread_turn(
    chat_id: int,
    replied_bot_message_id: int = None,
    new_bot_message_id: int = 0,
    user = None,
    user_text: str = "",
    bot_text: str = "",
    reply_to_id: int = None,
):
    target_reply_id = replied_bot_message_id if replied_bot_message_id is not None else reply_to_id
    root_key, thread = _get_ask_thread_by_reply(chat_id, target_reply_id)
    if not root_key or not thread:
        return False
    if user_text:
        thread["history"].append({"role": "user", "text": str(user_text), "speaker": _ask_speaker_label(user)})
    if bot_text:
        thread["history"].append({"role": "assistant", "text": str(bot_text), "speaker": "bot"})
    thread["history"] = thread["history"][-ASK_THREAD_MAX_TURNS:]
    thread["updated"] = time.time()
    ASK_THREAD_MSG_INDEX[(int(chat_id), int(new_bot_message_id))] = root_key
    return True

def _build_ask_response_markdown(response: dict):
    def _escape_md_keep_double_bold(text: str):
        s = str(text or "")
        out = []
        last = 0
        for m in re.finditer(r"\*\*(.+?)\*\*", s):
            start, end = m.span()
            if start < last:
                continue
            out.append(escape_md(s[last:start]))
            inner = m.group(1).strip()
            if inner:
                out.append(f"*{escape_md(inner)}*")
            last = end
        out.append(escape_md(s[last:]))
        return "".join(out)

    raw_text = str((response or {}).get("text") or "")
    sources = (response or {}).get("sources", []) or []
    lines_out = []
    for raw_line in raw_text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if not stripped:
            lines_out.append("")
            continue

        m = re.match(r"^[\*\-]\s+\*\*(.+?)\*\*:\s*(.+)$", stripped)
        if m:
            title = escape_md(m.group(1).strip())
            body = _escape_md_keep_double_bold(m.group(2).strip())
            lines_out.append(f"> *{title}*")
            if body:
                lines_out.append(f"> {body}")
            continue

        m2 = re.match(r"^[\*\-]\s+(.+)$", stripped)
        if m2:
            lines_out.append(f"> {_escape_md_keep_double_bold(m2.group(1).strip())}")
            continue

        lines_out.append(_escape_md_keep_double_bold(stripped))

    final_answer = "\n".join(lines_out).strip()
    sources_text = ""
    if sources:
        sources_text = "\n\n>📚 *Sumber:*\n"
        seen_urls = set()
        count = 0
        for src in sources:
            url = (src or {}).get("url", "")
            if not url or url in seen_urls:
                continue
            title = escape_md((src or {}).get("title", "Source"))
            sources_text += f"> • [{title}]({url})\n"
            seen_urls.add(url)
            count += 1
            if count >= 3:
                break

    return f"{final_answer}{sources_text}"

def _calc_rrr(entry, tp, sl, action):
    try:
        e = float(entry)
        t = float(tp)
        s = float(sl)
        risk = abs(e - s)
        reward = abs(t - e)
        if risk <= 0:
            return None
        return reward / risk
    except:
        return None

def _infer_tech_action(tech: dict):
    if not tech:
        return "WAIT", "L0/S0"
    summary = tech.get("summary", {})
    l_pts = int(summary.get("long_points", 0) or 0)
    s_pts = int(summary.get("short_points", 0) or 0)
    tag = f"L{l_pts}/S{s_pts}"
    if l_pts >= 4 and l_pts > s_pts + 1:
        return "LONG", tag
    elif s_pts >= 4 and s_pts > l_pts + 1:
        return "SHORT", tag
    return "WAIT", tag

def _build_fallback_trade_levels(price: float, action: str):
    if not price or price <= 0:
        return None, None, None
    if action == "LONG":
        return price, price * 1.05, price * 0.97
    elif action == "SHORT":
        return price, price * 0.95, price * 1.03
    return None, None, None

def _refine_ai_signal(ai_json: dict, tech: dict, coin: dict) -> dict:
    if not isinstance(ai_json, dict):
        ai_json = {"action": "WAIT", "reasoning": "Invalid signal"}
    
    action = str(ai_json.get("action", "WAIT")).upper()
    tech_act, tech_tag = _infer_tech_action(tech)
    
    current_price = (
        coin.get("market_data", {}).get("current_price", {}).get("usd")
        if isinstance(coin, dict) else None
    )
    if not current_price and isinstance(tech, dict):
        current_price = tech.get("last_price")

    entry = ai_json.get("entry")
    tp = ai_json.get("takeProfit")
    sl = ai_json.get("stopLoss")

    if action in ("LONG", "SHORT"):
        if not entry or not tp or not sl:
            fb_e, fb_tp, fb_sl = _build_fallback_trade_levels(current_price, action)
            ai_json["entry"] = entry or fb_e
            ai_json["takeProfit"] = tp or fb_tp
            ai_json["stopLoss"] = sl or fb_sl

        if "rrr" not in ai_json or not ai_json["rrr"]:
            calculated_rrr = _calc_rrr(ai_json.get("entry"), ai_json.get("takeProfit"), ai_json.get("stopLoss"), action)
            if calculated_rrr is not None:
                ai_json["rrr"] = round(calculated_rrr, 2)

    return {
        "signal": ai_json,
        "tech_action": tech_act,
        "tech_points": tech_tag
    }

def _build_sources_text(sources, max_items=4):
    if not sources:
        return ""
    seen = set()
    out = ["", ">*Sources:*"]
    count = 0
    for src in sources:
        url = src.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        title = escape_md(src.get("title", "Source"))
        out.append(f"• [{title}]({url})")
        count += 1
        if count >= max_items:
            break
    return "\n".join(out) if count else ""

def _build_thesis_text(data: dict, sources: list):
    coin = data.get("coin", {})
    parsed = data.get("parsed", {})

    coin_name = escape_md(parsed.get("coin") or coin.get("name") or "-")
    symbol = escape_md((parsed.get("symbol") or coin.get("symbol") or "").upper())
    stance = str(parsed.get("stance", "neutral")).lower()
    horizon = escape_md(str(parsed.get("horizon", "-")).upper())
    risk_level = escape_md(str(parsed.get("risk_level", "-")).upper())
    one_liner = escape_md(parsed.get("one_liner", "-"))
    summary = escape_md(parsed.get("summary", "-"))

    stance_icon = "🟢" if stance == "bullish" else "🔴" if stance == "bearish" else "🟡"

    def section(title, items):
        rows = [f"*{title}:*"]
        for item in (items or [])[:5]:
            rows.append(f"• {escape_md(item)}")
        if len(rows) == 1:
            rows.append("• \\-")
        return "\n".join(rows)

    lines = [
        f"🧠 *THESIS: {coin_name} \\({symbol}\\)*",
        f"Stance: {stance_icon} *{escape_md(stance.upper())}* \\| Horizon: *{horizon}* \\| Risk: *{risk_level}*",
        "",
        f">_{one_liner}_",
        "",
        section("Bull Case", parsed.get("bull_case")),
        "",
        section("Bear Case", parsed.get("bear_case")),
        "",
        section("Watch Signals", parsed.get("watch_signals")),
        "",
        section("Invalidation", parsed.get("invalidation")),
        "",
        f"*Summary:*\n_{summary}_",
    ]
    return "\n".join(lines) + _build_sources_text(sources, max_items=4)

def _build_liq_table_text(events: list, status: dict, label: str = "ALL"):
    header = f"{'No':>2} {'SYM':<10} {'Side':<5} {'Notional':>10} {'Price':>12} {'Ago':>5}"
    lines = ["LIQUIDATION FEED", f"FILTER: {label}", header, "-" * len(header)]

    long_total = 0.0
    short_total = 0.0
    for i, ev in enumerate(events[:20], 1):
        side = str(ev.get("side") or "-").upper()
        notion = _safe_float(ev.get("notional_usd"))
        if side == "LONG":
            long_total += notion
        elif side == "SHORT":
            short_total += notion
        sym = _fit_cell(ev.get('symbol', '-'), 10)
        p_str = f"{_safe_float(ev.get('price')):,.2f}"
        not_str = f"${notion:,.0f}"
        ago_str = _fmt_ago_short(ev.get('ts') or 0)
        lines.append(f"{i:>2} {sym} {side:<5} {not_str:>10} {p_str:>12} {ago_str:>5}")

    lines.append("-" * len(header))
    lines.append(f"Long: ${long_total:,.0f} | Short: ${short_total:,.0f}")
    lines.append(
        f"WS: {'ON' if status.get('connected') else 'OFF'} | "
        f"Buffer: {status.get('buffer_size', 0)} | "
        f"Last: {_fmt_ago_short(status.get('last_ws_ts') or 0)}"
    )
    return "```\n" + "\n".join(lines) + "\n```"
