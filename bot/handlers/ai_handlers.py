# bot/handlers/ai_handlers.py
import logging
from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command

from bot.utils import (
    escape_md, fmt_usd_raw, fmt_pct_raw, is_rate_limited, resolve_symbol_candidates,
    extract_first_json, _safe_float, _safe_int, _clamp, _parse_ai_number,
    _normalize_action, _derive_risk_label
)
from .ai_helpers import (
    _refine_ai_signal, _build_ask_response_markdown, _create_ask_thread,
    _get_ask_thread_by_reply, _append_ask_thread_turn, ASK_THREAD_CACHE
)
from bot.services.x70_signals import (
    get_x70_signals, get_x70_signal_for_coin, format_x70_signal_card
)
import bot.services as services

router = Router()


@router.message(Command("ai"))
async def cmd_ai(message: Message):
    limited, remain = is_rate_limited(message.from_user.id, "ai")
    if limited:
        await message.reply(f"⏳ Santuy bro, tunggu {remain} detik dulu ya 😄", parse_mode=None)
        return

    content = message.text or message.caption or ""
    parts = content.split()

    if len(parts) < 2:
        await message.reply(
            "🤖 *AI Market Sniper* 🎯\n\n"
            "Strategi: _Scalping Telemetry \\+ Sentiment Analysis_\n"
            "Usage: `/ai [symbol]`\n"
            "Example: `/ai sol`",
            parse_mode="MarkdownV2"
        )
        return

    symbol = parts[1].lower()
    msg_wait = await message.reply(
        f"🔍 _Scanning Market & Hype for '{escape_md(symbol)}'\\.\\.\\._",
        parse_mode="MarkdownV2"
    )

    try:
        resolved = await resolve_symbol_candidates(symbol)
        status = resolved.get("status")

        if status == "not_found":
            await msg_wait.edit_text("❌ Koin tidak ditemukan\\.", parse_mode="MarkdownV2")
            return

        if status == "direct":
            coin = resolved.get("coin", {})
            await run_ai_analysis(msg_wait, coin['id'])
            return

        matches = resolved.get("matches", [])[:5]
        if not matches:
            await msg_wait.edit_text("❌ Koin tidak ditemukan\\.", parse_mode="MarkdownV2")
            return

        keyboard = []
        for coin in matches:
            rank = coin.get('market_cap_rank')
            rank_str = f"#{rank}" if rank else "N/A"
            btn_text = f"{coin.get('name')} ({coin.get('symbol')}) {rank_str}"
            keyboard.append([InlineKeyboardButton(text=btn_text[:60], callback_data=f"ai_sel:{coin.get('id')}")])

        kb = InlineKeyboardMarkup(inline_keyboard=keyboard)
        await msg_wait.edit_text(
            "🤖 Pilih koin untuk dianalisa:",
            parse_mode="MarkdownV2",
            reply_markup=kb
        )

    except Exception as e:
        logging.error(f"AI Cmd Error: {e}")
        await msg_wait.edit_text("❌ Terjadi kesalahan sistem\\.", parse_mode="MarkdownV2")


@router.callback_query(F.data.startswith("ai_sel:"))
async def callback_ai_select(call: CallbackQuery):
    await call.answer()
    coin_id = call.data.split(":")[1]
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.edit_text("🧠 _Analyzing Data & Searching News\\.\\.\\._", parse_mode="MarkdownV2")
    await run_ai_analysis(call.message, coin_id)


async def run_ai_analysis(message_obj, coin_id):
    try:
        analysis = await services.get_market_analysis(coin_id)

        if not analysis or not analysis.get('gemini'):
            await message_obj.edit_text("❌ Data tidak cukup/AI error\\.", parse_mode="MarkdownV2")
            return

        coin = analysis['coin']
        gemini_data = analysis['gemini']  # {text, sources}
        tech = analysis['tech']

        ai_json = extract_first_json(gemini_data.get('text', ''))
        if not ai_json:
            ai_json = {"action": "WAIT", "reasoning": "Parse Error", "confidence": 0}
        validated = _refine_ai_signal(ai_json, tech, coin)
        ai_json = validated["signal"]

        action = _normalize_action(ai_json.get("action"))
        if action == "WAIT":
            sig_icon = "⚪ WAIT / NEUTRAL"
            entry_display = "N/A"
            tp_display = "N/A"
            sl_display = "N/A"
        elif action == "LONG":
            sig_icon = "🟢 LONG / BUY"
            entry_display = fmt_usd_raw(_safe_float(ai_json.get("entry")))
            tp_display = fmt_usd_raw(_safe_float(ai_json.get("takeProfit")))
            sl_display = fmt_usd_raw(_safe_float(ai_json.get("stopLoss")))
        else:
            sig_icon = "🔴 SHORT / SELL"
            entry_display = fmt_usd_raw(_safe_float(ai_json.get("entry")))
            tp_display = fmt_usd_raw(_safe_float(ai_json.get("takeProfit")))
            sl_display = fmt_usd_raw(_safe_float(ai_json.get("stopLoss")))

        risk_score_int = _clamp(_safe_int(ai_json.get("risk_score"), 0), 0, 10)
        risk_label = str(ai_json.get("risk_label") or _derive_risk_label(risk_score_int))
        rrr_raw = ai_json.get("rrr", "N/A")
        rrr = f"{_safe_float(rrr_raw):.2f}" if _parse_ai_number(rrr_raw) is not None and action != "WAIT" else "N/A"
        pos_risk = str(ai_json.get("position_risk_pct", "N/A"))
        confidence = _clamp(_safe_int(ai_json.get('confidence'), 0), 0, 100)
        setup_quality = _clamp(_safe_int(ai_json.get("setup_quality"), 0), 0, 100)
        tech_action = validated.get("tech_action", "WAIT")
        tech_points = validated.get("tech_points", "L0/S0")

        news_summary = escape_md(ai_json.get('news_summary', 'No major news found.'))

        sources_text = ""
        raw_sources = gemini_data.get('sources', []) or []
        if raw_sources:
            sources_text = ">*Sources:*\n"
            for src in raw_sources[:3]:
                title = escape_md(src.get('title', 'Source'))
                url = src.get('url', '')
                sources_text += f"• [{title}]({url})\n"

        price = fmt_usd_raw(coin.get('current_price', 0))
        change = coin.get('price_change_percentage_24h', 0) or 0
        arrow = "📈" if change >= 0 else "📉"
        conf_str = f"{confidence}%"
        setup_str = f"{setup_quality}/100"

        final_text = (
            f"```\n"
            f"AI PREDICTION: {coin.get('name','').upper()}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Price: ${price}\n"
            f"24h: {arrow} {fmt_pct_raw(change)} | Confidence: {conf_str}\n"
            f"Bias: {tech.get('global_bias','-')} | RSI: {float(tech.get('rsi',0)):.1f}\n"
            f"Risk: {risk_label} ({risk_score_int}/10) | RRR: {rrr} | Risk/Trade: {pos_risk}\n"
            f"Setup Quality: {setup_str} | Tech Vote: {tech_action} ({tech_points})\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"SIGNAL: {sig_icon}\n"
            f"ENTRY: {entry_display}\n"
            f"TP: {tp_display}\n"
            f"SL: {sl_display}\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"Reasoning:\n"
            f"{ai_json.get('reasoning','-')}\n"
            f"```\n"
            f">*Market News:*\n"
            f">_{news_summary}_\n"
            f">{sources_text}\n"
            f"*Disclaimer: AI Prediction based on Tech\\+Fund data\\. DYOR\\!*"
        )

        await message_obj.edit_text(final_text, parse_mode="MarkdownV2", disable_web_page_preview=True)

    except Exception as e:
        logging.error(f"Run AI Error: {e}")
        await message_obj.edit_text(f"❌ Error: `{escape_md(str(e))}`", parse_mode="MarkdownV2")


@router.message(Command("ask"))
async def cmd_ask_general(message: Message):
    limited, remain = is_rate_limited(message.from_user.id, "ask")
    if limited:
        await message.reply(f"⏳ Tunggu {remain} detik dulu ya, biar bot gak ngos-ngosan 😅", parse_mode=None)
        return

    content = message.text or message.caption or ""
    parts = content.split(maxsplit=1)

    if len(parts) < 2:
        await message.reply(
            "🤖 *Fitur Tanya AI*\n\n"
            "Gunakan command ini untuk bertanya apa saja (Umum, Coding, Sejarah, dll\\.)\n"
            "Contoh:\n"
            "`/ask Siapa penemu lampu pijar?`\n"
            "`/ask Buatkan resep nasi goreng`",
            parse_mode="MarkdownV2"
        )
        return

    query = parts[1].strip()
    msg_wait = await message.reply("🤔 _Sedang berpikir\\.\\.\\._", parse_mode="MarkdownV2")

    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        response = await services.ask_general_question(query)

        if not response or not response.get('text'):
            await msg_wait.edit_text("❌ Maaf, AI sedang sibuk atau error\\.", parse_mode="MarkdownV2")
            return

        final_response = _build_ask_response_markdown(response)
        await msg_wait.edit_text(final_response, parse_mode="MarkdownV2", disable_web_page_preview=True)
        _create_ask_thread(
            chat_id=message.chat.id,
            bot_message_id=msg_wait.message_id,
            user=message.from_user,
            user_text=query,
            bot_text=(response or {}).get("text", "")
        )

    except Exception as e:
        logging.error(f"Ask Cmd Error: {e}")
        await msg_wait.edit_text("❌ Terjadi kesalahan sistem\\.", parse_mode="MarkdownV2")


# --- ASK THREAD CONTINUATION (fallback reply handler, keep near end) ---
@router.message(F.reply_to_message, F.text)
async def handle_ask_reply_thread(message: Message):
    text = (message.text or "").strip()
    if not text or text.startswith("/"):
        return

    replied = message.reply_to_message
    if not replied or not replied.from_user or not replied.from_user.is_bot:
        return

    _, thread = _get_ask_thread_by_reply(message.chat.id, replied.message_id)
    if not thread:
        return

    limited, remain = is_rate_limited(message.from_user.id, "ask")
    if limited:
        await message.reply(f"⏳ Tunggu {remain} detik dulu ya, biar bot gak ngos-ngosan 😅", parse_mode=None)
        return

    msg_wait = await message.reply("🤔 _Sedang berpikir\\.\\.\\._", parse_mode="MarkdownV2")
    await message.bot.send_chat_action(message.chat.id, "typing")

    try:
        response = await services.ask_general_question(text, history=thread.get("history") or [])
        if not response or not response.get("text"):
            await msg_wait.edit_text("❌ Maaf, AI sedang sibuk atau error\\.", parse_mode="MarkdownV2")
            return

        final_response = _build_ask_response_markdown(response)
        await msg_wait.edit_text(final_response, parse_mode="MarkdownV2", disable_web_page_preview=True)
        _append_ask_thread_turn(
            chat_id=message.chat.id,
            replied_bot_message_id=replied.message_id,
            new_bot_message_id=msg_wait.message_id,
            user=message.from_user,
            user_text=text,
            bot_text=(response or {}).get("text", "")
        )
    except Exception as e:
        logging.error(f"Ask Reply Thread Error: {e}")
        await msg_wait.edit_text("❌ Terjadi kesalahan sistem\\.", parse_mode="MarkdownV2")


@router.message(Command("signals", "x70"))
async def cmd_x70_signals(message: Message):
    """Menampilkan live quant signals dari signals.x70.ai (Trading Skills)."""
    user_id = message.from_user.id if message.from_user else 0
    limited, remain = is_rate_limited(user_id, "signals")
    if limited:
        await message.reply(f"⏳ Tunggu {remain} detik...")
        return

    parts = (message.text or "").strip().split()
    target_coin = parts[1].upper() if len(parts) > 1 else None

    if target_coin:
        signal = await get_x70_signal_for_coin(target_coin)
        if not signal:
            await message.reply(
                f"ℹ️ Belum ada sinyal quant aktif untuk #{target_coin} di <code>signals.x70.ai</code>.\n"
                f"Gunakan <code>/signals</code> untuk melihat seluruh sinyal quant yang sedang aktif.",
                parse_mode="HTML"
            )
            return
        card_text = format_x70_signal_card(signal)
        await message.reply(card_text, parse_mode="HTML")
        return

    signals = await get_x70_signals(limit=3)
    if not signals:
        await message.reply(
            "ℹ️ Belum ada sinyal quant aktif di <code>signals.x70.ai</code> saat ini. Coba beberapa saat lagi.",
            parse_mode="HTML"
        )
        return

    cards = [format_x70_signal_card(s) for s in signals[:2]]
    body = "\n\n━━━━━━━━━━━━━━━━━━━━\n\n".join(cards)
    text = (
        f"📡 <b>LIVE QUANT SIGNALS (SIGNALS.X70.AI)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{body}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    await message.reply(text, parse_mode="HTML")
