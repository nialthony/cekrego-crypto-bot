# bot/handlers/news_handlers.py
import time
import hashlib
import logging
from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command

from bot.utils import (
    escape_md, safe_mdv2_truncate, is_rate_limited,
    convert_utc_time_string_to_wib, extract_first_json,
    resolve_symbol_candidates, _safe_float, _fit_cell
)
from .ai_helpers import (
    _build_thesis_text, _build_sources_text, _build_liq_table_text
)
import bot.services as services

router = Router()

THESIS_PICK_CACHE = {}
THESIS_PICK_TTL = 180  # detik


def _cleanup_thesis_pick_cache():
    now = time.time()
    expired_tokens = [
        token for token, item in THESIS_PICK_CACHE.items()
        if now - item.get("time", 0) > THESIS_PICK_TTL
    ]
    for token in expired_tokens:
        THESIS_PICK_CACHE.pop(token, None)


def _store_thesis_pick(user_id: int, raw_query: str, matches: list) -> str:
    _cleanup_thesis_pick_cache()
    now = time.time()
    token = hashlib.md5(f"thesis:{user_id}:{raw_query}:{now}".encode()).hexdigest()[:12]
    THESIS_PICK_CACHE[token] = {
        "time": now,
        "user_id": user_id,
        "raw_query": raw_query,
        "matches": matches,
    }
    return token


# --- 0.A LIQUIDATION FEED ---
@router.message(Command("liq"))
async def cmd_liquidation_feed(message: Message):
    limited, remain = is_rate_limited(message.from_user.id, "liq")
    if limited:
        await message.reply(f"⏳ Tunggu {remain} detik dulu ya 😄", parse_mode=None)
        return

    parts = (message.text or "").split(maxsplit=1)
    symbol_filter = None
    label = "MAJOR/ALL"
    if len(parts) > 1 and parts[1].strip():
        raw = parts[1].strip().upper()
        if raw not in ("ALL", "*"):
            symbol_filter = raw if raw.endswith("USDT") else f"{raw}USDT"
            label = symbol_filter
        else:
            label = "ALL"

    msg = await message.reply("💥 _Mengambil liquidation feed\\.\\.\\._", parse_mode="MarkdownV2")
    try:
        status = services.get_liquidation_feed_status()
        events = await services.get_recent_liquidations(
            limit=50,
            max_age_sec=3600,
            symbols=[symbol_filter] if symbol_filter else None
        )
        if not events:
            extra = "WS belum connect / belum ada event baru sejak bot start\\." if not status.get("buffer_size") else "Tidak ada event yang cocok di 1 jam terakhir\\."
            await msg.edit_text(f"⚠️ {extra}", parse_mode="MarkdownV2")
            return

        events = sorted(events, key=lambda x: (int(x.get("timestamp") or 0), _safe_float(x.get("notional_usd"))), reverse=True)
        text = _build_liq_table_text(events, status, label)
        await msg.edit_text(text, parse_mode="MarkdownV2")
    except Exception as e:
        logging.error(f"Liq Cmd Error: {e}")
        await msg.edit_text("❌ Gagal ambil liquidation feed\\.", parse_mode="MarkdownV2")


# --- 0.B COIN NEWS DIGEST ---
@router.message(Command("news"))
async def cmd_coin_news(message: Message):
    limited, remain = is_rate_limited(message.from_user.id, "news")
    if limited:
        await message.reply(f"⏳ Tunggu {remain} detik dulu ya 😄", parse_mode=None)
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply(
            "📰 *Coin News Digest*\n\n"
            "Usage: `/news [coin]`\n"
            "Example: `/news sol`",
            parse_mode="MarkdownV2"
        )
        return

    query = parts[1].strip()
    msg = await message.reply(f"📰 _Cari news untuk '{escape_md(query)}'\\.\\.\\._", parse_mode="MarkdownV2")
    try:
        res = await services.get_coin_news_digest(query)
        if not res or not res.get("text"):
            await msg.edit_text("⚠️ News tidak tersedia saat ini\\.", parse_mode="MarkdownV2")
            return

        parsed = extract_first_json(res.get("text", ""))
        if not parsed:
            fallback = safe_mdv2_truncate(res.get("text", ""), 3500)
            await msg.edit_text(fallback, parse_mode="MarkdownV2", disable_web_page_preview=True)
            return

        topic = escape_md(parsed.get("topic", query))
        sentiment = str(parsed.get("sentiment", "neutral")).lower()
        senti_emoji = "🟢" if sentiment == "bullish" else "🔴" if sentiment == "bearish" else "🟡"
        summary = escape_md(parsed.get("summary", "-"))
        items = parsed.get("items") or []

        lines = [
            f"📰 *NEWS DIGEST: {topic}*",
            f"Sentiment: {senti_emoji} *{escape_md(sentiment.upper())}*",
            "",
            f">_{summary}_",
            "",
        ]
        for i, item in enumerate(items[:5], 1):
            impact = str(item.get("impact", "neutral")).lower()
            icon = "🟢" if impact == "bullish" else "🔴" if impact == "bearish" else "🟡"
            headline = escape_md(item.get("headline", "-"))
            why = escape_md(item.get("why", "-"))
            lines.append(f"{i}\\. {icon} *{headline}*")
            lines.append(f"   _{why}_")

        text = "\n".join(lines) + _build_sources_text(res.get("sources"), max_items=4)
        await msg.edit_text(text, parse_mode="MarkdownV2", disable_web_page_preview=True)
    except Exception as e:
        logging.error(f"News Cmd Error: {e}")
        await msg.edit_text("❌ Gagal ambil news\\.", parse_mode="MarkdownV2")


# --- 0.B2 COIN THESIS ---
@router.message(Command("thesis"))
async def cmd_coin_thesis(message: Message):
    limited, remain = is_rate_limited(message.from_user.id, "thesis")
    if limited:
        await message.reply(f"⏳ Tunggu {remain} detik dulu ya 😄", parse_mode=None)
        return

    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.reply(
            "🧠 *Coin Thesis*\n\n"
            "Usage: `/thesis [coin]`\n"
            "Example: `/thesis sol`",
            parse_mode="MarkdownV2"
        )
        return

    query = parts[1].strip().lower()
    msg = await message.reply(f"🧠 _Menyusun thesis untuk '{escape_md(query)}'\\.\\.\\._", parse_mode="MarkdownV2")
    try:
        resolved = await resolve_symbol_candidates(query)
        status = resolved.get("status")

        if status == "not_found":
            await msg.edit_text(f"⚠️ Coin `{escape_md(query)}` tidak ditemukan\\.", parse_mode="MarkdownV2")
            return

        if status == "ambiguous":
            matches = resolved.get("matches", [])[:6]
            if len(matches) < 2:
                await msg.edit_text(f"⚠️ Coin `{escape_md(query)}` tidak ditemukan\\.", parse_mode="MarkdownV2")
                return

            token = _store_thesis_pick(message.from_user.id, query, matches)
            rows = []
            for idx, coin in enumerate(matches):
                btn_text = f"{coin.get('name')} ({coin.get('symbol')})"
                rows.append([InlineKeyboardButton(text=btn_text[:60], callback_data=f"ths_sel:{token}:{idx}")])
            kb = InlineKeyboardMarkup(inline_keyboard=rows)
            await msg.edit_text(
                f"🤔 Ditemukan beberapa coin untuk `{escape_md(query)}`\\. Pilih yang benar:",
                parse_mode="MarkdownV2",
                reply_markup=kb
            )
            return

        coin = resolved.get("coin", {})
        coin_id = coin.get("id")
        if not coin_id:
            await msg.edit_text(f"⚠️ Coin `{escape_md(query)}` tidak ditemukan\\.", parse_mode="MarkdownV2")
            return

        result = await services.get_coin_thesis(coin_id)
        if not result or not result.get("thesis"):
            await msg.edit_text("⚠️ Thesis belum tersedia saat ini\\.", parse_mode="MarkdownV2")
            return

        parsed = extract_first_json((result.get("thesis") or {}).get("text", ""))
        if not parsed:
            raw = safe_mdv2_truncate((result.get("thesis") or {}).get("text", ""), 3500)
            await msg.edit_text(raw or "⚠️ Gagal parse thesis\\.", parse_mode="MarkdownV2", disable_web_page_preview=True)
            return

        text = _build_thesis_text(
            {"coin": result.get("coin") or {}, "parsed": parsed},
            (result.get("thesis") or {}).get("sources") or []
        )
        await msg.edit_text(text, parse_mode="MarkdownV2", disable_web_page_preview=True)
    except Exception as e:
        logging.error(f"Thesis Cmd Error: {e}")
        await msg.edit_text("❌ Gagal membuat thesis\\.", parse_mode="MarkdownV2")


@router.callback_query(F.data.startswith("ths_sel:"))
async def callback_thesis_select(call: CallbackQuery):
    try:
        _, token, idx_raw = call.data.split(":")
        picked = THESIS_PICK_CACHE.get(token)
        if not picked:
            await call.answer("Pilihan sudah expired. Coba /thesis lagi.", show_alert=True)
            return
        if time.time() - picked.get("time", 0) > THESIS_PICK_TTL:
            THESIS_PICK_CACHE.pop(token, None)
            await call.answer("Pilihan sudah expired. Coba /thesis lagi.", show_alert=True)
            return
        if picked.get("user_id") != call.from_user.id:
            await call.answer("Pilihan ini bukan untuk akun kamu.", show_alert=True)
            return

        try:
            idx = int(idx_raw)
        except Exception:
            idx = -1
        matches = picked.get("matches", [])
        if idx < 0 or idx >= len(matches):
            await call.answer("Pilihan tidak valid.", show_alert=True)
            return

        coin = matches[idx]
        await call.answer("Diproses...")
        THESIS_PICK_CACHE.pop(token, None)
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.edit_text("🧠 _Menyusun thesis\\.\\.\\._", parse_mode="MarkdownV2")

        result = await services.get_coin_thesis(coin.get("id"))
        if not result or not result.get("thesis"):
            await call.message.edit_text("⚠️ Thesis belum tersedia saat ini\\.", parse_mode="MarkdownV2")
            return

        parsed = extract_first_json((result.get("thesis") or {}).get("text", ""))
        if not parsed:
            raw = safe_mdv2_truncate((result.get("thesis") or {}).get("text", ""), 3500)
            await call.message.edit_text(raw or "⚠️ Gagal parse thesis\\.", parse_mode="MarkdownV2", disable_web_page_preview=True)
            return

        text = _build_thesis_text(
            {"coin": result.get("coin") or {}, "parsed": parsed},
            (result.get("thesis") or {}).get("sources") or []
        )
        await call.message.edit_text(text, parse_mode="MarkdownV2", disable_web_page_preview=True)
    except Exception as e:
        logging.error(f"Thesis Select Error: {e}")
        await call.message.reply("❌ Gagal memproses thesis coin\\.", parse_mode="MarkdownV2")


# --- 0.C MARKET CALENDAR ---
@router.message(Command("calendar"))
async def cmd_market_calendar(message: Message):
    limited, remain = is_rate_limited(message.from_user.id, "calendar")
    if limited:
        await message.reply(f"⏳ Tunggu {remain} detik dulu ya 😄", parse_mode=None)
        return

    parts = (message.text or "").split(maxsplit=1)
    days = 7
    if len(parts) > 1:
        try:
            days = max(1, min(int(parts[1].strip()), 30))
        except Exception:
            days = 7

    msg = await message.reply(f"🗓️ _Menyusun market calendar {days} hari ke depan\\.\\.\\._", parse_mode="MarkdownV2")
    try:
        res = await services.get_market_calendar(days)
        if not res or not res.get("text"):
            await msg.edit_text("⚠️ Calendar tidak tersedia saat ini\\.", parse_mode="MarkdownV2")
            return

        parsed = extract_first_json(res.get("text", ""))
        events = (parsed or {}).get("events") or []
        if not events:
            await msg.edit_text("⚠️ Event calendar kosong\\.", parse_mode="MarkdownV2")
            return

        norm = []
        for ev in events:
            date_s = str(ev.get("date", "")).strip()
            utc_s = str(ev.get("time_utc", "TBA")).strip().upper().replace(".", ":")
            cat = str(ev.get("category", "-")).upper()[:5]
            impact = str(ev.get("impact", "-")).upper()[:4]
            title = str(ev.get("title", "-"))
            note = str(ev.get("note", ""))
            wib_s = convert_utc_time_string_to_wib(utc_s) if utc_s != "TBA" else "TBA"
            norm.append({
                "date": date_s,
                "utc": utc_s,
                "wib": wib_s,
                "cat": cat,
                "impact": impact,
                "title": title,
                "note": note,
            })

        norm.sort(key=lambda x: (x["date"], "99:99" if x["utc"] == "TBA" else x["utc"]))

        header = f"{'No':>2} {'Date':<10} {'UTC':<8} {'WIB':<13} {'Cat':<5} {'Imp':<4} {'Event':<32}"
        sep = "-" * len(header)
        rows = [header, sep]
        for i, ev in enumerate(norm[:15], 1):
            rows.append(
                f"{i:>2} "
                f"{_fit_cell(ev['date'],10)} "
                f"{_fit_cell(ev['utc'],8)} "
                f"{_fit_cell(ev['wib'],13)} "
                f"{_fit_cell(ev['cat'],5)} "
                f"{_fit_cell(ev['impact'],4)} "
                f"{_fit_cell(ev['title'],32)}"
            )

        notes = []
        for i, ev in enumerate(norm[:10], 1):
            if ev["note"]:
                notes.append(f"{i}\\. {escape_md(ev['note'])}")

        text = (
            f"🗓️ *MARKET CALENDAR* \\(next {days}d\\)\n"
            f"> Time columns: `UTC` and `WIB \\(UTC\\+7\\)`\n\n"
            f"```\n" + "\n".join(rows) + "\n```"
        )
        if notes:
            text += "\n*Notes:*\n" + "\n".join(notes)
        text += _build_sources_text(res.get("sources"), max_items=4)
        await msg.edit_text(text, parse_mode="MarkdownV2", disable_web_page_preview=True)
    except Exception as e:
        logging.error(f"Calendar Cmd Error: {e}")
        await msg.edit_text("❌ Gagal ambil market calendar\\.", parse_mode="MarkdownV2")
