# bot/handlers/market_handlers.py
import re
import html
import time
import hashlib
import asyncio
import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
)
from aiogram.filters import Command

from config import ADMIN_ID
from bot.utils import (
    escape_md, fmt_usd_raw, fmt_idr_raw, fmt_pct_raw, fmt_amount_raw,
    parse_amount_with_suffix, parse_fiat_conversion_text,
    is_rate_limited, resolve_symbol_candidates, WIB,
    COMMON_FIAT_CODES, _fit_cell, _fmt_compact, _safe_float, _fmt_ago_short
)
import bot.services as services
import bot.database as database

router = Router()

def _prune_dict_cache(target_dict: dict, max_size: int = 500, ttl: float = None):
    now = time.time()
    if ttl:
        expired_keys = [k for k, v in list(target_dict.items()) if isinstance(v, dict) and now - v.get("time", now) > ttl]
        for k in expired_keys:
            target_dict.pop(k, None)
    if len(target_dict) > max_size:
        excess = len(target_dict) - int(max_size * 0.8)
        for k in list(target_dict.keys())[:excess]:
            target_dict.pop(k, None)

PRICE_REFRESH_CACHE = {}
PRICE_REFRESH_TTL = 3600  # 1 jam

def _store_price_refresh(amount: float, symbol: str, coin_id: str, real_name: str = None) -> str:
    _prune_dict_cache(PRICE_REFRESH_CACHE, max_size=500, ttl=PRICE_REFRESH_TTL)
    token = hashlib.md5(f"{amount}:{coin_id}:{time.time()}".encode()).hexdigest()[:12]
    PRICE_REFRESH_CACHE[token] = {
        "amount": amount,
        "symbol": symbol,
        "coin_id": coin_id,
        "real_name": real_name,
        "time": time.time()
    }
    return token

NON_CRYPTO_WORDS = {
    "orang", "hari", "menit", "jam", "detik", "bulan", "tahun", "minggu",
    "kali", "buah", "biji", "ekor", "lembar", "persen", "tiket",
    "bot", "user", "admin", "member", "pesan", "chat", "akun", "device",
    "step", "langkah", "poin", "point", "stars", "star", "bintang",
    "malam", "pagi", "siang", "sore", "ribu", "juta", "miliar", "k", "rb", "jt"
}

SYMBOL_PICK_CACHE = {}
SYMBOL_PICK_TTL = 180  # detik
MARKET_PICK_CACHE = {}
MARKET_PICK_TTL = 180  # detik
THESIS_PICK_CACHE = {}
THESIS_PICK_TTL = 180  # detik
DEX_REFRESH_CACHE = {}
DEX_REFRESH_TTL = 1800  # detik

DEX_KEYWORDS = (
    "uniswap", "pancake", "sushi", "curve", "balancer", "raydium", "orca",
    "jupiter", "meteora", "camelot", "quickswap", "spooky", "traderjoe",
    "kyberswap", "1inch", "matcha", "woofi", "aerodrome", "syncswap",
    "lifinity", "swap", "amm"
)

def _store_symbol_pick(user_id: int, amount: float, symbol: str, matches: list) -> str:
    _prune_dict_cache(SYMBOL_PICK_CACHE, max_size=500, ttl=SYMBOL_PICK_TTL)
    token = hashlib.md5(f"{user_id}:{symbol}:{time.time()}".encode()).hexdigest()[:12]
    SYMBOL_PICK_CACHE[token] = {
        "user_id": user_id,
        "amount": amount,
        "symbol": symbol,
        "matches": matches,
        "time": time.time()
    }
    return token

def _store_market_pick(user_id: int, query: str, matches: list) -> str:
    _prune_dict_cache(MARKET_PICK_CACHE, max_size=500, ttl=MARKET_PICK_TTL)
    token = hashlib.md5(f"{user_id}:{query}:{time.time()}".encode()).hexdigest()[:12]
    MARKET_PICK_CACHE[token] = {
        "user_id": user_id,
        "query": query,
        "matches": matches,
        "time": time.time()
    }
    return token

def _looks_like_dex(name: str, identifier: str = "") -> bool:
    s = f"{name} {identifier}".lower()
    return any(k in s for k in DEX_KEYWORDS)

def _split_market_lines(lines, max_chars=3500):
    chunks = []
    current = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current and (current_len + line_len > max_chars):
            chunks.append(current)
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len
    if current:
        chunks.append(current)
    return chunks

def _build_market_exchange_rows(tickers: list):
    cex_map = {}
    dex_map = {}
    for t in tickers or []:
        market = t.get("market") or {}
        name = market.get("name") or "Unknown"
        ident = market.get("identifier") or ""
        base = str(t.get("base") or "").upper()
        target = str(t.get("target") or "").upper()
        pair = f"{base}/{target}"
        vol = float((t.get("converted_volume") or {}).get("usd") or 0.0)

        is_dex = _looks_like_dex(name, ident)
        if not is_dex and not (target in ("USDT", "USD") or base in ("USDT", "USD")):
            continue

        target_map = dex_map if is_dex else cex_map
        if name not in target_map:
            target_map[name] = {
                "kind": "DEX" if is_dex else "CEX",
                "name": name,
                "pairs": 0,
                "best_pair": pair,
                "best_pair_vol": vol,
                "vol": 0.0,
            }
        rec = target_map[name]
        rec["pairs"] += 1
        rec["vol"] += vol
        if vol > rec["best_pair_vol"]:
            rec["best_pair_vol"] = vol
            rec["best_pair"] = pair

    cex_rows = sorted(cex_map.values(), key=lambda r: r["vol"], reverse=True)
    dex_rows = sorted(dex_map.values(), key=lambda r: r["vol"], reverse=True)
    rows = cex_rows + dex_rows
    return rows, len(cex_rows), len(dex_rows)
async def send_market_result(msg_obj: Message, coin: dict):
    coin_id = coin.get("id")
    coin_name = coin.get("name") or coin_id.replace("-", " ").title()
    coin_symbol = (coin.get("symbol") or "").upper()

    tickers = await services.get_coin_tickers(coin_id, max_pages=8)
    if not tickers:
        await msg_obj.edit_text("⚠️ Data market tidak ditemukan\\.", parse_mode="MarkdownV2")
        return

    rows, cex_count, dex_count = _build_market_exchange_rows(tickers)
    if not rows:
        await msg_obj.edit_text("⚠️ Tidak ada market CEX/DEX yang cocok\\.", parse_mode="MarkdownV2")
        return

    header = f"{'No':>3} {'Type':<4} {'Exchange':<20} {'Pairs':>5} {'Best Pair':<14} {'Vol24h USD':>10}"
    sep = "-" * len(header)
    data_lines = []

    for idx, row in enumerate(rows, 1):
        data_lines.append(
            f"{idx:>3} "
            f"{_fit_cell(row['kind'], 4)} "
            f"{_fit_cell(row['name'], 20)} "
            f"{row['pairs']:>5} "
            f"{_fit_cell(row['best_pair'], 14)} "
            f"{_fit_cell(_fmt_compact(row['vol']), 10)}"
        )

    chunks = _split_market_lines(data_lines)
    esc_sym = escape_md(coin_symbol)
    esc_name = escape_md(coin_name)
    title = (
        f"📊 *MARKET LISTING \\(CEX:USDT \\| DEX:ALL\\): {esc_sym} \\({esc_name}\\)*\n"
        f"> Exchanges: `{len(rows)}` \\| CEX: `{cex_count}` \\| DEX: `{dex_count}`\n"
        f"> _Source: CoinGecko Tickers_"
    )

    first = f"{title}\n\n```\n{header}\n{sep}\n" + "\n".join(chunks[0]) + "\n```"
    await msg_obj.edit_text(first, parse_mode="MarkdownV2", disable_web_page_preview=True)

    if len(chunks) > 1:
        for i, part in enumerate(chunks[1:], start=2):
            text = (
                f"*MARKET TABLE* \\({i}/{len(chunks)}\\)\n\n"
                f"```\n{header}\n{sep}\n"
                + "\n".join(part) +
                "\n```"
            )
            await msg_obj.answer(text, parse_mode="MarkdownV2", disable_web_page_preview=True)

def _build_admin_stats_text(stats: dict):
    top_rows = stats.get("top_commands") or []
    lines = []
    lines.append("ADMIN ANALYTICS DASHBOARD")
    lines.append("=" * 72)
    lines.append(f"TOTAL CHATS      : {stats.get('total_chats', 0)}")
    lines.append(f"PRIVATE / GROUP  : {stats.get('private_chats', 0)} / {stats.get('group_chats', 0)}")
    lines.append(f"ACTIVE 24H/7D/30D: {stats.get('active_24h', 0)} / {stats.get('active_7d', 0)} / {stats.get('active_30d', 0)}")
    lines.append(f"TOTAL MESSAGES   : {stats.get('total_messages', 0)}")
    lines.append(f"MSG 24H / 7D     : {stats.get('msg_24h', 0)} / {stats.get('msg_7d', 0)}")
    lines.append(f"CMD 24H / 7D     : {stats.get('cmd_24h', 0)} / {stats.get('cmd_7d', 0)}")
    lines.append(f"SUB DAILY       : {stats.get('daily_subscribers', 0)}")
    lines.append("")
    lines.append("TOP COMMANDS (7D)")
    lines.append(f"{'No':>2} {'Command':<20} {'Hits':>6}")
    lines.append("-" * 72)
    for i, row in enumerate(top_rows[:10], 1):
        cmd = str(row[0] or "-")[:20]
        hits = int(row[1] or 0)
        lines.append(f"{i:>2} {cmd:<20} {hits:>6}")
    if not top_rows:
        lines.append("No command data yet.")
    return "```\n" + "\n".join(lines) + "\n```"

def _fmt_ago_short(ts_ms: int):
    try:
        ts = int(ts_ms) / 1000 if int(ts_ms) > 10_000_000_000 else int(ts_ms)
    except:
        return "-"
    delta = max(int(time.time() - ts), 0)
    if delta < 60:
        return f"{delta}s"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"

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
        lines.append(
            f"{i:>2} "
            f"{_fit_cell(ev.get('symbol', '-'), 10)} "
            f"{_fit_cell(side, 5)} "
            f"{_fit_cell(_fmt_compact(notion), 10)} "
            f"{_fit_cell(fmt_usd_raw(ev.get('price') or 0), 12)} "
            f"{_fit_cell(_fmt_ago_short(ev.get('timestamp') or 0), 5)}"
        )

    lines.append("")
    lines.append(f"LONG liq : {_fmt_compact(long_total)} USD")
    lines.append(f"SHORT liq: {_fmt_compact(short_total)} USD")
    lines.append(
        f"WS: {'ON' if status.get('connected') else 'OFF'} | "
        f"Buffer: {status.get('buffer_size', 0)} | "
        f"Last: {_fmt_ago_short(status.get('last_ws_ts') or 0)}"
    )
    return "```\n" + "\n".join(lines) + "\n```"

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

async def send_price_result(msg_obj: Message, amount: float, symbol: str, coin_id: str, real_name: str = None):
    data = await services.get_price(coin_id, "usd,idr")
    if not data or coin_id not in data:
        await msg_obj.edit_text("❌ Gagal ambil data API\\.", parse_mode="MarkdownV2")
        return

    d = data[coin_id]
    p_usd = d.get('usd', 0) or 0
    p_idr = d.get('idr', 0) or 0

    final_name_str = real_name if real_name else coin_id.replace("-", " ").title()
    display_symbol = (symbol or "").upper()

    esc_sym = escape_md(display_symbol)
    esc_name = escape_md(final_name_str)
    esc_amount = escape_md(f"{amount:,}".rstrip('0').rstrip('.'))
    ref_token = _store_price_refresh(amount, display_symbol, coin_id, final_name_str)

    if amount == 1.0:
        chg_24h = d.get('usd_24h_change', 0) or 0
        vol_24h = d.get('usd_24h_vol', 0) or 0
        mcap = d.get('usd_market_cap', 0) or 0
        arrow = "🟢" if chg_24h >= 0 else "🔴"
        sign = "+" if chg_24h >= 0 else ""

        text = (
            f"*{esc_amount} {esc_sym} \\({esc_name}\\)*\n\n"
            f"*Harga:*\n"
            f"> `${fmt_usd_raw(p_usd)}`\n"
            f"> `Rp{fmt_idr_raw(p_idr)}`\n\n"
            f"*Statistik:*\n"
            f"> *Change 24h:* {arrow} `{sign}{fmt_pct_raw(chg_24h)}`\n"
            f"> *Volume:* `${fmt_usd_raw(vol_24h)}`\n"
            f"> *Market Cap:* `${fmt_usd_raw(mcap)}`\n\n"
            f"👇 _Klik tombol di bawah untuk chart atau refresh:_"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 1 Hari", callback_data=f"chart:{coin_id}:{symbol}:1"),
             InlineKeyboardButton(text="📅 7 Hari", callback_data=f"chart:{coin_id}:{symbol}:7")],
            [InlineKeyboardButton(text="🗓️ 30 Hari", callback_data=f"chart:{coin_id}:{symbol}:30"),
             InlineKeyboardButton(text="🔄 Refresh", callback_data=f"px_ref:{ref_token}")]
        ])
        await msg_obj.edit_text(text, parse_mode="MarkdownV2", reply_markup=kb)
        return

    total_usd = amount * p_usd
    total_idr = amount * p_idr
    text = (
        f"```\n"
        f"{amount:,.4f} {display_symbol} ({final_name_str})\n\n"
        f"{fmt_usd_raw(total_usd)} USD\n"
        f"{fmt_idr_raw(total_idr)} IDR\n"
        f"```"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Refresh", callback_data=f"px_ref:{ref_token}")]
    ])
    await msg_obj.edit_text(text, parse_mode="MarkdownV2", reply_markup=kb)



@router.message(Command("p", "price"))
async def cmd_price(message: Message):
    content = (message.text or "").strip()
    parts = content.split()
    if len(parts) < 2:
        await message.reply("`Usage: /p [jumlah] <symbol>`\n_Contoh: `/p btc` atau `/p 50 doge`_", parse_mode="MarkdownV2")
        return
    
    amount = 1.0
    symbol = ""
    if len(parts) == 2:
        symbol = parts[1].lower()
    else:
        try:
            amount = float(parts[1])
            symbol = parts[2].lower()
        except ValueError:
            symbol = parts[1].lower()
            try:
                amount = float(parts[2])
            except ValueError:
                pass

    if symbol in NON_CRYPTO_WORDS:
        await message.reply(f"⚠️ `{escape_md(symbol)}` bukan simbol koin crypto\\.", parse_mode="MarkdownV2")
        return

    msg_wait = await message.reply("⏳ _Wait\\.\\.\\._", parse_mode="MarkdownV2")
    try:
        resolved = await resolve_symbol_candidates(symbol)
        status = resolved.get("status")

        if status == "not_found":
            await msg_wait.edit_text(f"⚠️ Coin `{escape_md(symbol)}` tidak ditemukan\\.", parse_mode="MarkdownV2")
            return

        if status == "ambiguous":
            matches = resolved.get("matches", [])[:6]
            if len(matches) < 2:
                await msg_wait.edit_text(f"⚠️ Coin `{escape_md(symbol)}` tidak ditemukan\\.", parse_mode="MarkdownV2")
                return

            token = _store_symbol_pick(message.from_user.id, amount, symbol, matches)
            rows = []
            for idx, coin in enumerate(matches):
                btn_text = f"{coin.get('name')} ({coin.get('symbol')})"
                rows.append([InlineKeyboardButton(text=btn_text[:60], callback_data=f"px_sel:{token}:{idx}")])

            kb = InlineKeyboardMarkup(inline_keyboard=rows)
            await msg_wait.edit_text(
                f"🤔 Ditemukan beberapa coin untuk `{escape_md(symbol)}`\\. Pilih yang benar:",
                parse_mode="MarkdownV2",
                reply_markup=kb
            )
            return

        coin = resolved.get("coin", {})
        coin_symbol = (coin.get("symbol") or symbol).lower()
        coin_id = coin.get("id")
        coin_name = coin.get("name")

        if not coin_id:
            await msg_wait.edit_text(f"⚠️ Coin `{escape_md(symbol)}` tidak ditemukan\\.", parse_mode="MarkdownV2")
            return

        await send_price_result(msg_wait, amount, coin_symbol, coin_id, coin_name)
    except Exception as e:
        logging.error(f"Cmd Price Error: {e}")
        await msg_wait.edit_text(f"❌ Error: `{escape_md(str(e))}`", parse_mode="MarkdownV2")

@router.message(Command("c", "chart"))
async def cmd_chart(message: Message):
    content = (message.text or "").strip()
    parts = content.split()
    if len(parts) < 2:
        await message.reply("`Usage: /c <symbol> [1|7|30]`\n_Contoh: `/c btc` atau `/c sol 7`_", parse_mode="MarkdownV2")
        return
    
    symbol = parts[1].lower()
    days = "1"
    if len(parts) >= 3 and parts[2] in ("1", "7", "30"):
        days = parts[2]

    msg_wait = await message.reply("📈 _Membuat chart\\.\\.\\._", parse_mode="MarkdownV2")
    try:
        resolved = await resolve_symbol_candidates(symbol)
        if resolved.get("status") == "not_found":
            await msg_wait.edit_text(f"⚠️ Coin `{escape_md(symbol)}` tidak ditemukan\\.", parse_mode="MarkdownV2")
            return

        coin = resolved.get("coin") or (resolved.get("matches", [{}])[0])
        coin_id = coin.get("id")
        if not coin_id:
            await msg_wait.edit_text(f"⚠️ Coin `{escape_md(symbol)}` tidak ditemukan\\.", parse_mode="MarkdownV2")
            return

        buf = await services.generate_chart(coin_id, symbol, days)
        if buf:
            caption = f"📈 *Chart {escape_md(symbol.upper())} \\- {escape_md(days)} Hari*\n_Source: CoinGecko_"
            await message.reply_photo(
                BufferedInputFile(buf.getvalue(), filename=f"{symbol}.png"),
                caption=caption,
                parse_mode="MarkdownV2"
            )
            await msg_wait.delete()
        else:
            await msg_wait.edit_text("❌ Gagal membuat chart\\.", parse_mode="MarkdownV2")
    except Exception as e:
        logging.error(f"Cmd Chart Error: {e}")
        await msg_wait.edit_text(f"❌ Error: `{escape_md(str(e))}`", parse_mode="MarkdownV2")

@router.message(F.text.regexp(r"^(\d+(\.\d+)?)\s+([a-zA-Z0-9-]+)$"))
async def handle_price_check(message: Message):
    try:
        parts = message.text.split()
        amount = float(parts[0])
        symbol = parts[1].lower()

        # Abaikan jika kata umum non-crypto
        if symbol in NON_CRYPTO_WORDS:
            return

        is_group = message.chat.type in ("group", "supergroup")

        # Jika di grup, jangan kirim pesan loading sebelum verifikasi koin
        resolved = await resolve_symbol_candidates(symbol)
        status = resolved.get("status")

        if status == "not_found":
            # Silent ignore di grup agar tidak mengganggu percakapan normal
            if not is_group:
                await message.reply(f"⚠️ Coin `{escape_md(symbol)}` tidak ditemukan\\.", parse_mode="MarkdownV2")
            return

        msg_wait = await message.reply("⏳ _Wait\\.\\.\\._", parse_mode="MarkdownV2")

        if status == "ambiguous":
            matches = resolved.get("matches", [])[:6]
            if len(matches) < 2:
                await msg_wait.edit_text(f"⚠️ Coin `{escape_md(symbol)}` tidak ditemukan\\.", parse_mode="MarkdownV2")
                return

            token = _store_symbol_pick(message.from_user.id, amount, symbol, matches)
            rows = []
            for idx, coin in enumerate(matches):
                btn_text = f"{coin.get('name')} ({coin.get('symbol')})"
                rows.append([InlineKeyboardButton(text=btn_text[:60], callback_data=f"px_sel:{token}:{idx}")])

            kb = InlineKeyboardMarkup(inline_keyboard=rows)
            await msg_wait.edit_text(
                f"🤔 Ditemukan beberapa coin untuk `{escape_md(symbol)}`\\. Pilih yang benar:",
                parse_mode="MarkdownV2",
                reply_markup=kb
            )
            return

        coin = resolved.get("coin", {})
        coin_symbol = (coin.get("symbol") or symbol).lower()
        coin_id = coin.get("id")
        coin_name = coin.get("name")

        if not coin_id:
            if not is_group:
                await msg_wait.edit_text(f"⚠️ Coin `{escape_md(symbol)}` tidak ditemukan\\.", parse_mode="MarkdownV2")
            else:
                await msg_wait.delete()
            return

        await send_price_result(msg_wait, amount, coin_symbol, coin_id, coin_name)

    except Exception as e:
        if message.chat.type == "private":
            await message.reply(f"❌ Error: `{escape_md(str(e))}`", parse_mode="MarkdownV2")

# --- 1.1 CALLBACK PILIH COIN (AMBIGUOUS SYMBOL) ---
@router.callback_query(F.data.startswith("px_sel:"))
async def callback_price_select(call: CallbackQuery):
    try:
        _, token, idx_raw = call.data.split(":")
        picked = SYMBOL_PICK_CACHE.get(token)

        if not picked:
            await call.answer("Pilihan sudah expired. Coba ketik ulang simbol.", show_alert=True)
            return

        if time.time() - picked.get("time", 0) > SYMBOL_PICK_TTL:
            SYMBOL_PICK_CACHE.pop(token, None)
            await call.answer("Pilihan sudah expired. Coba ketik ulang simbol.", show_alert=True)
            return

        if picked.get("user_id") != call.from_user.id:
            await call.answer("Pilihan ini bukan untuk akun kamu.", show_alert=True)
            return

        try:
            idx = int(idx_raw)
        except:
            idx = -1

        matches = picked.get("matches", [])
        if idx < 0 or idx >= len(matches):
            await call.answer("Pilihan tidak valid.", show_alert=True)
            return

        coin = matches[idx]
        amount = float(picked.get("amount", 1))
        await call.answer("Diproses...")

        SYMBOL_PICK_CACHE.pop(token, None)
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.edit_text("⏳ _Wait\\.\\.\\._", parse_mode="MarkdownV2")

        await send_price_result(
            call.message,
            amount,
            (coin.get("symbol") or picked.get("raw_symbol") or "").lower(),
            coin.get("id"),
            coin.get("name")
        )
    except Exception as e:
        logging.error(f"Price Select Error: {e}")
        await call.message.reply("❌ Gagal memproses pilihan coin\\.", parse_mode="MarkdownV2")

# --- 2. CALLBACK CHART ---
@router.callback_query(F.data.startswith("chart:"))
async def callback_chart(call: CallbackQuery):
    try:
        _, coin_id, symbol, days = call.data.split(":")
        await call.answer("Sedang membuat chart...")
        await call.message.bot.send_chat_action(call.message.chat.id, "upload_photo")

        buf = await services.generate_chart(coin_id, symbol, days)

        if buf:
            caption = f"📈 *Chart {escape_md(symbol.upper())} \\- {escape_md(days)} Hari*\n_Source: CoinGecko_"
            await call.message.answer_photo(
                BufferedInputFile(buf.getvalue(), filename=f"{symbol}.png"),
                caption=caption,
                parse_mode="MarkdownV2"
            )
        else:
            await call.message.reply("❌ Gagal membuat chart\\.", parse_mode="MarkdownV2")
    except Exception as e:
        await call.message.reply(f"Error: {e}")

# --- 2.1 CALLBACK REFRESH HARGA COIN ---
@router.callback_query(F.data.startswith("px_ref:"))
async def callback_price_refresh(call: CallbackQuery):
    try:
        ref_token = call.data.split(":", 1)[1]
        item = PRICE_REFRESH_CACHE.get(ref_token)
        if not item:
            await call.answer("Tombol refresh sudah expired. Cek harga ulang.", show_alert=True)
            return

        amount = item.get("amount", 1.0)
        symbol = item.get("symbol", "")
        coin_id = item.get("coin_id")
        real_name = item.get("real_name")

        data = await services.get_price(coin_id, "usd,idr", force_refresh=True)
        if not data or coin_id not in data:
            await call.answer("❌ Gagal update harga dari API.", show_alert=True)
            return

        d = data[coin_id]
        p_usd = d.get('usd', 0) or 0
        p_idr = d.get('idr', 0) or 0
        final_name_str = real_name if real_name else coin_id.replace("-", " ").title()
        display_symbol = (symbol or "").upper()
        esc_sym = escape_md(display_symbol)
        esc_name = escape_md(final_name_str)
        esc_amount = escape_md(f"{amount:,}".rstrip('0').rstrip('.'))

        if amount == 1.0:
            chg_24h = d.get('usd_24h_change', 0) or 0
            vol_24h = d.get('usd_24h_vol', 0) or 0
            mcap = d.get('usd_market_cap', 0) or 0
            arrow = "🟢" if chg_24h >= 0 else "🔴"
            sign = "+" if chg_24h >= 0 else ""
            now_wib = datetime.now(WIB).strftime("%H:%M:%S")

            text = (
                f"*{esc_amount} {esc_sym} \\({esc_name}\\)*\n\n"
                f"*Harga:*\n"
                f"> `${fmt_usd_raw(p_usd)}`\n"
                f"> `Rp{fmt_idr_raw(p_idr)}`\n\n"
                f"*Statistik:*\n"
                f"> *Change 24h:* {arrow} `{sign}{fmt_pct_raw(chg_24h)}`\n"
                f"> *Volume:* `${fmt_usd_raw(vol_24h)}`\n"
                f"> *Market Cap:* `${fmt_usd_raw(mcap)}`\n\n"
                f"🕒 _Update: {now_wib} WIB_\n"
                f"👇 _Klik tombol di bawah untuk chart atau refresh:_"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📊 1 Hari", callback_data=f"chart:{coin_id}:{symbol}:1"),
                 InlineKeyboardButton(text="📅 7 Hari", callback_data=f"chart:{coin_id}:{symbol}:7")],
                [InlineKeyboardButton(text="🗓️ 30 Hari", callback_data=f"chart:{coin_id}:{symbol}:30"),
                 InlineKeyboardButton(text="🔄 Refresh", callback_data=f"px_ref:{ref_token}")],
            ])
            await call.message.edit_text(text, parse_mode="MarkdownV2", reply_markup=kb)
        else:
            total_usd = amount * p_usd
            total_idr = amount * p_idr
            now_wib = datetime.now(WIB).strftime("%H:%M:%S")
            text = (
                f"```\n"
                f"{amount:,.4f} {display_symbol} ({final_name_str})\n\n"
                f"{fmt_usd_raw(total_usd)} USD\n"
                f"{fmt_idr_raw(total_idr)} IDR\n\n"
                f"Update: {now_wib} WIB\n"
                f"```"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Refresh", callback_data=f"px_ref:{ref_token}")]
            ])
            await call.message.edit_text(text, parse_mode="MarkdownV2", reply_markup=kb)

        item["time"] = time.time()
        await call.answer("⚡ Harga berhasil di-refresh!")
    except Exception as e:
        if "message is not modified" in str(e).lower():
            await call.answer("⚡ Harga sudah yang paling update!")
            return
        logging.error(f"Price Refresh Error: {e}")
        await call.answer("Gagal me-refresh harga.")

# --- 3. TRENDING ---
@router.message(Command("trend"))
async def cmd_trending(message: Message):
    limited, remain = is_rate_limited(message.from_user.id, "trend")
    if limited:
        await message.reply(f"⏳ Tunggu {remain} detik dulu ya 😄", parse_mode=None)
        return

    msg = await message.reply("🌏 _Mencari coin yang lagi rame\\.\\.\\._", parse_mode="MarkdownV2")
    try:
        data = await services.get_trending()
        if not data:
            await msg.edit_text("⚠️ Data kosong\\.", parse_mode="MarkdownV2")
            return
        lines = ["🔥 *TOP 10 TRENDING SEARCH*", ""]
        for i, item in enumerate(data[:10], 1):
            c = item['item']
            name = escape_md(c.get('name'))
            sym = escape_md(c.get('symbol'))
            rank = c.get('market_cap_rank')
            rank_str = f"Rank: {rank}" if rank else "Rank: \\-"
            lines.append(f">{i} *{name}* \\(`{sym}`\\) \\| _{rank_str}_")
        lines.append("")
        lines.append("⏳ _Wait for it\\.\\.\\._")
        final_text = "\n".join(lines)
        await msg.edit_text(final_text, parse_mode="MarkdownV2")
        await asyncio.sleep(2)
        final_text_celebrate = final_text.replace("⏳ _Wait for it\\.\\.\\._", "🎉 *Ikan hiu makan tomat, Alopyupuuul\\!* 🚀")
        await msg.edit_text(final_text_celebrate, parse_mode="MarkdownV2")
    except Exception as e:
        await msg.edit_text(f"Error: `{escape_md(str(e))}`", parse_mode="MarkdownV2")

# --- 4. GAINERS / LOSERS ---
@router.message(Command("gnr", "lsr"))
async def cmd_movers(message: Message):
    cmd_text = (message.text or "").lower()
    key = "gnr" if "gnr" in cmd_text else "lsr"
    limited, remain = is_rate_limited(message.from_user.id, key)
    if limited:
        await message.reply(f"⏳ Tunggu {remain} detik dulu ya 😄", parse_mode=None)
        return

    is_gnr = "gnr" in cmd_text
    label = "GAINERS" if is_gnr else "LOSERS"
    emoji = "🚀" if is_gnr else "🥀"
    msg = await message.reply(f"{emoji} _Scanning market\\.\\.\\._", parse_mode="MarkdownV2")
    try:
        coins = await services.get_top_coins(limit=50)
        if not coins:
            await msg.edit_text("⚠️ Gagal ambil data\\.", parse_mode="MarkdownV2")
            return
        sorted_coins = sorted(coins, key=lambda x: x.get('price_change_percentage_24h', 0) or 0, reverse=is_gnr)
        lines = [f"{emoji} *TOP 10 {label} 24H*", ""]
        for i, c in enumerate(sorted_coins[:10], 1):
            sym = escape_md((c.get('symbol', '') or '').upper())
            price = fmt_usd_raw(c.get('current_price', 0) or 0)
            change = c.get('price_change_percentage_24h', 0) or 0
            change_str = fmt_pct_raw(change)
            lines.append(f">{i}\\. *{sym}* \\(`${price}`\\) \\| `{change_str}`")
        await msg.edit_text("\n".join(lines), parse_mode="MarkdownV2")
    except Exception as e:
        await msg.edit_text(f"Error: `{escape_md(str(e))}`", parse_mode="MarkdownV2")

# --- 0.6. IDR TO CRYPTO ---
@router.message(F.text.regexp(r"^([\d\.]+)(rb|jt)?\s+idr\s+([a-zA-Z0-9-]+)$"))
async def handle_idr_conversion(message: Message):
    try:
        match = re.match(r"^([\d\.]+)(rb|jt)?\s+idr\s+([a-zA-Z0-9-]+)$", (message.text or "").lower())
        if not match:
            return

        num_str, suffix, symbol = match.groups()
        amount_idr = parse_amount_with_suffix(num_str, suffix or "")

        msg = await message.reply("🧮 _Kalkulasi Rupiah\\.\\.\\._", parse_mode="MarkdownV2")

        real_name = None
        coin_id = await database.get_preferred_id(symbol)
        if not coin_id:
            results = await services.search_coin(symbol)
            if results:
                coin_id = results[0]['id']
                real_name = results[0]['name']
            else:
                await msg.edit_text(f"⚠️ Coin `{escape_md(symbol)}` tidak ditemukan\\.", parse_mode="MarkdownV2")
                return

        data = await services.get_price(coin_id, "idr")
        if data and coin_id in data:
            price = data[coin_id].get('idr', 0) or 0
            dapat = amount_idr / price if price else 0
            final_name = real_name if real_name else coin_id.replace("-", " ").title()

            resp = (
                f"```\n"
                f"KONVERSI IDR ➔ {symbol.upper()}\n\n"
                f"Modal: Rp{fmt_idr_raw(amount_idr)}\n"
                f"Koin:  {final_name}\n"
                f"Dapat: {fmt_usd_raw(dapat)} {symbol.upper()}\n"
                f"Kurs:  Rp{fmt_idr_raw(price)}\n"
                f"```"
            )
            await msg.edit_text(resp, parse_mode="MarkdownV2")
        else:
            await msg.edit_text("❌ Gagal ambil harga IDR\\.", parse_mode="MarkdownV2")
    except Exception as e:
        await message.reply(f"Error: `{escape_md(str(e))}`", parse_mode="MarkdownV2")

# --- 0.7. FIAT HELPER / CONVERTER ---
@router.message(Command("fiat", "kurs"))
async def cmd_fiat_helper(message: Message):
    limited, remain = is_rate_limited(message.from_user.id, "fiat")
    if limited:
        await message.reply(f"Tunggu {remain} detik dulu ya :)", parse_mode=None)
        return

    content = message.text or message.caption or ""
    parts = content.split(maxsplit=1)
    query = parts[1].strip() if len(parts) > 1 else ""

    currencies = await services.get_fiat_currencies()
    if not currencies:
        await message.reply("Gagal ambil daftar fiat saat ini.", parse_mode=None)
        return

    if not query:
        common_map = {item.get("code"): item for item in currencies}
        lines = [
            "FIAT HELPER",
            "",
            "Usage:",
            "/fiat idr",
            "/fiat yen",
            "100 usd idr",
            "50rb idr jpy",
            "",
            f"Supported: {len(currencies)} kode fiat",
            "",
            "Contoh populer:",
        ]
        for code in COMMON_FIAT_CODES:
            item = common_map.get(code)
            if not item:
                continue
            symbol = f" ({item.get('symbol')})" if item.get("symbol") else ""
            lines.append(f"{code} - {item.get('name')}{symbol}")
        await message.reply("\n".join(lines), parse_mode=None)
        return

    matches = await services.search_fiat_currencies(query, limit=12)
    if not matches:
        await message.reply(
            f"Fiat '{query}' tidak ditemukan.\nCoba contoh: /fiat idr atau /fiat yen",
            parse_mode=None
        )
        return

    exact = next((item for item in matches if item.get("code", "").lower() == query.lower()), None)
    if exact:
        symbol = f" ({exact.get('symbol')})" if exact.get("symbol") else ""
        numeric = f"\nISO Numeric: {exact.get('numeric')}" if exact.get("numeric") else ""
        text = (
            f"FIAT DETAIL\n\n"
            f"Code: {exact.get('code')}\n"
            f"Name: {exact.get('name')}{symbol}"
            f"{numeric}"
        )
        await message.reply(text, parse_mode=None)
        return

    lines = [f"FIAT MATCHES untuk '{query}'", ""]
    for item in matches[:10]:
        symbol = f" ({item.get('symbol')})" if item.get("symbol") else ""
        lines.append(f"{item.get('code')} - {item.get('name')}{symbol}")
    await message.reply("\n".join(lines), parse_mode=None)

@router.message(F.text.regexp(r"^\s*(\d+(?:\.\d+)?)\s*(rb|jt)?\s+[a-zA-Z]{3}\s+(?:to\s+)?[a-zA-Z]{3}\s*$"))
async def handle_fiat_to_fiat_conversion(message: Message):
    try:
        parsed = parse_fiat_conversion_text(message.text)
        if not parsed:
            return

        base = parsed["base"]
        quote = parsed["quote"]
        amount = parsed["amount"]

        currencies = await services.get_fiat_currencies()
        valid_codes = {item.get("code", "").upper() for item in currencies}
        if base not in valid_codes or quote not in valid_codes:
            return

        msg = await message.reply("Cek kurs fiat...", parse_mode=None)
        data = await services.convert_fiat(amount, base, quote)
        if not data:
            await msg.edit_text("Gagal ambil kurs fiat.", parse_mode=None)
            return

        result = data.get("result", 0) or 0
        rate = data.get("rate", 0) or 0

        text = (
            "KONVERSI FIAT\n\n"
            f"{fmt_amount_raw(amount)} {base} = {fmt_amount_raw(result)} {quote}\n"
            f"Rate: 1 {base} = {fmt_usd_raw(rate)} {quote}\n"
            f"Date: {data.get('date') or '-'}\n"
        )
        await msg.edit_text(text, parse_mode=None)
    except Exception as e:
        await message.reply(f"Error: `{escape_md(str(e))}`", parse_mode="MarkdownV2")

# --- 0.8. MARKET LISTING ---
@router.message(Command("market"))
async def cmd_market(message: Message):
    limited, remain = is_rate_limited(message.from_user.id, "market")
    if limited:
        await message.reply(f"⏳ Tunggu {remain} detik dulu ya 😄", parse_mode=None)
        return

    content = message.text or message.caption or ""
    parts = content.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply(
            "📊 *Market Listing*\n\n"
            "Usage: `/market [symbol]`\n"
            "Example: `/market night`\n"
            "Note: _CEX hanya USDT, DEX semua pair_",
            parse_mode="MarkdownV2"
        )
        return

    query = parts[1].strip().lower()
    msg_wait = await message.reply(
        f"🔍 _Mencari market listing untuk '{escape_md(query)}'\\.\\.\\._",
        parse_mode="MarkdownV2"
    )

    try:
        resolved = await resolve_symbol_candidates(query)
        status = resolved.get("status")

        if status == "not_found":
            await msg_wait.edit_text(f"⚠️ Coin `{escape_md(query)}` tidak ditemukan\\.", parse_mode="MarkdownV2")
            return

        if status == "ambiguous":
            matches = resolved.get("matches", [])[:6]
            if len(matches) < 2:
                await msg_wait.edit_text(f"⚠️ Coin `{escape_md(query)}` tidak ditemukan\\.", parse_mode="MarkdownV2")
                return

            token = _store_market_pick(message.from_user.id, query, matches)
            rows = []
            for idx, coin in enumerate(matches):
                btn_text = f"{coin.get('name')} ({coin.get('symbol')})"
                rows.append([InlineKeyboardButton(text=btn_text[:60], callback_data=f"mkt_sel:{token}:{idx}")])

            kb = InlineKeyboardMarkup(inline_keyboard=rows)
            await msg_wait.edit_text(
                f"🤔 Ditemukan beberapa coin untuk `{escape_md(query)}`\\. Pilih yang benar:",
                parse_mode="MarkdownV2",
                reply_markup=kb
            )
            return

        coin = resolved.get("coin", {})
        if not coin.get("id"):
            await msg_wait.edit_text(f"⚠️ Coin `{escape_md(query)}` tidak ditemukan\\.", parse_mode="MarkdownV2")
            return

        await send_market_result(msg_wait, coin)

    except Exception as e:
        logging.error(f"Market Cmd Error: {e}")
        await msg_wait.edit_text("❌ Terjadi kesalahan sistem\\.", parse_mode="MarkdownV2")

@router.callback_query(F.data.startswith("mkt_sel:"))
async def callback_market_select(call: CallbackQuery):
    try:
        _, token, idx_raw = call.data.split(":")
        picked = MARKET_PICK_CACHE.get(token)

        if not picked:
            await call.answer("Pilihan sudah expired. Coba /market lagi.", show_alert=True)
            return

        if time.time() - picked.get("time", 0) > MARKET_PICK_TTL:
            MARKET_PICK_CACHE.pop(token, None)
            await call.answer("Pilihan sudah expired. Coba /market lagi.", show_alert=True)
            return

        if picked.get("user_id") != call.from_user.id:
            await call.answer("Pilihan ini bukan untuk akun kamu.", show_alert=True)
            return

        try:
            idx = int(idx_raw)
        except:
            idx = -1

        matches = picked.get("matches", [])
        if idx < 0 or idx >= len(matches):
            await call.answer("Pilihan tidak valid.", show_alert=True)
            return

        coin = matches[idx]
        await call.answer("Diproses...")
        MARKET_PICK_CACHE.pop(token, None)
        await call.message.edit_reply_markup(reply_markup=None)
        await call.message.edit_text("⏳ _Wait\\.\\.\\._", parse_mode="MarkdownV2")
        await send_market_result(call.message, coin)

    except Exception as e:
        logging.error(f"Market Select Error: {e}")
        await call.message.reply("❌ Gagal memproses pilihan market\\.", parse_mode="MarkdownV2")

# --- 0.8. DAILY REPORT SUBSCRIPTION ---
@router.message(Command("daily"))
async def cmd_daily_subscription(message: Message):
    limited, remain = is_rate_limited(message.from_user.id, "daily")
    if limited:
        await message.reply(f"⏳ Tunggu {remain} detik dulu ya 😄", parse_mode=None)
        return

    parts = (message.text or "").strip().split(maxsplit=1)
    arg = parts[1].strip().lower() if len(parts) > 1 else "status"
    if arg in ("on", "1", "enable", "aktif"):
        await database.set_subscription(message.chat.id, "daily", True)
    elif arg in ("off", "0", "disable", "mati"):
        await database.set_subscription(message.chat.id, "daily", False)

    state = await database.get_subscription(message.chat.id)
    status = "ON ✅" if state.get("daily", 1) else "OFF ❌"
    await message.reply(
        f"📅 Daily report status: *{status}*\n"
        "Jadwal kirim: `07:00` & `23:00` UTC\\+7\n"
        "Usage: `/daily on` atau `/daily off`",
        parse_mode="MarkdownV2"
    )

# ==============================================================================
# 🦅 DEXSCREENER ON-CHAIN RADAR (CLEAN CARD STYLE & SMART FEATURES)
# ==============================================================================
def _format_dex_compact(val):
    try:
        n = float(val or 0)
    except:
        return "$0"
    if n >= 1_000_000_000:
        return f"${n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"${n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"${n / 1_000:.2f}K"
    return f"${n:.2f}"

def _format_dex_price(price_usd, price_native=None, quote_symbol=None):
    try:
        p = float(price_usd or 0)
    except:
        p = 0.0

    if p >= 1000:
        usd_str = f"${p:,.2f}"
    elif p >= 1:
        usd_str = f"${p:,.3f}"
    elif p >= 0.0001:
        usd_str = f"${p:,.6f}"
    elif p > 0:
        usd_str = f"${p:,.10f}".rstrip("0")
    else:
        usd_str = "$0.00"

    if price_native and quote_symbol:
        try:
            pn = float(price_native)
            if pn < 0.000001 and pn > 0:
                native_str = f"{pn:,.10f}".rstrip("0")
            else:
                native_str = f"{pn:,.6g}"
            return f"{usd_str} ({native_str} {html.escape(str(quote_symbol).upper())})"
        except:
            pass
    return usd_str

def _format_dex_age(created_at_ms):
    if not created_at_ms:
        return "N/A"
    try:
        diff_sec = max(0, time.time() - (float(created_at_ms) / 1000.0))
        if diff_sec < 60:
            return "Baru saja"
        if diff_sec < 3600:
            return f"{int(diff_sec // 60)}m lalu"
        if diff_sec < 86400:
            h = int(diff_sec // 3600)
            m = int((diff_sec % 3600) // 60)
            return f"{h}h {m}m lalu"
        days = int(diff_sec // 86400)
        return f"{days} hari lalu"
    except:
        return "N/A"

def _format_pct_html(val):
    try:
        n = float(val or 0)
    except:
        return "0.00%"
    sign = "+" if n > 0 else ""
    icon = "🟢" if n > 0 else "🔴" if n < 0 else "⚪"
    return f"{icon} {sign}{n:.2f}%"

CHAIN_BADGES = {
    "solana": ("🪐 SOLANA", "Solana"),
    "base": ("🔵 BASE", "Base"),
    "ethereum": ("🦄 ETHEREUM", "Ethereum"),
    "bsc": ("🟡 BNB CHAIN", "BNB Chain"),
    "arbitrum": ("🔷 ARBITRUM", "Arbitrum"),
    "polygon": ("💜 POLYGON", "Polygon"),
    "avalanche": ("🔺 AVALANCHE", "Avalanche"),
    "sui": ("💧 SUI", "Sui"),
    "tron": ("🔴 TRON", "Tron"),
    "optimism": ("🔴 OPTIMISM", "Optimism"),
    "blast": ("💥 BLAST", "Blast"),
    "fantom": ("👻 FANTOM", "Fantom"),
}

def _get_chain_badge(chain_id):
    c = str(chain_id or "").lower()
    return CHAIN_BADGES.get(c, (f"⛓️ {c.upper()}", c.title()))

def _build_dex_card(pair: dict, other_pairs=None, security: dict = None):
    chain_id = str(pair.get("chainId") or "unknown").lower()
    chain_badge, chain_title = _get_chain_badge(chain_id)
    dex_raw = str(pair.get("dexId") or "DEX").lower()
    dex_display_map = {
        "pumpswap": "Pumpswap",
        "pumpfun": "Pumpfun",
        "raydium": "Raydium",
        "orca": "Orca",
        "meteora": "Meteora",
        "uniswap": "Uniswap",
        "pancakeswap": "PancakeSwap",
        "sushiswap": "SushiSwap",
        "aerodrome": "Aerodrome",
        "curve": "Curve",
    }
    dex_name = dex_display_map.get(dex_raw, dex_raw.title())

    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    token_name = html.escape(str(base.get("name") or "Unknown"))
    token_symbol = html.escape(str(base.get("symbol") or "?").upper())
    quote_symbol = html.escape(str(quote.get("symbol") or "?").upper())
    ca = str(base.get("address") or "-")
    pair_addr = str(pair.get("pairAddress") or "")

    price_str = _format_dex_price(pair.get("priceUsd"), pair.get("priceNative"), quote_symbol)
    mcap = pair.get("marketCap") or pair.get("fdv") or 0
    fdv = pair.get("fdv") or 0
    liq_data = pair.get("liquidity") or {}
    liq_usd = float(liq_data.get("usd") or 0)
    age_str = _format_dex_age(pair.get("pairCreatedAt"))

    # Changes
    pc = pair.get("priceChange") or {}
    c_5m = _format_pct_html(pc.get("m5"))
    c_1h = _format_pct_html(pc.get("h1"))
    c_6h = _format_pct_html(pc.get("h6"))
    c_24h = _format_pct_html(pc.get("h24"))

    # Order flow
    tx = pair.get("txns", {}).get("h24") or pair.get("txns", {}).get("h1") or {}
    buys = int(tx.get("buys") or 0)
    sells = int(tx.get("sells") or 0)
    total_tx = buys + sells
    vol_24h = _format_dex_compact((pair.get("volume") or {}).get("h24") or 0)

    if total_tx > 0:
        buy_pct = (buys / total_tx) * 100
        if buy_pct >= 60:
            flow_label = f"🟢 Bullish ({buy_pct:.0f}% Buys)"
        elif buy_pct <= 40:
            flow_label = f"🔴 Bearish ({100 - buy_pct:.0f}% Sells)"
        else:
            flow_label = f"🟡 Balanced ({buy_pct:.0f}% Buys)"
    else:
        flow_label = "⚪ No Data"

    # Smart Risk Warnings
    warnings = []
    if liq_usd < 10000:
        warnings.append("⚠️ <b>LOW LIQUIDITY (&lt;$10K):</b> Risiko slippage ekstrem / rawan rug pull!")
    elif liq_usd < 35000:
        warnings.append("⚠️ <b>MODERATE LIQUIDITY:</b> Likuiditas terbatas, waspada fluktuasi tajam.")

    if total_tx >= 20:
        if sells > (buys * 2.5):
            warnings.append("🚨 <b>DUMP ALERT:</b> Tekanan jual masif mendominasi transaksi!")
        elif buys > (sells * 2.5):
            warnings.append("🚀 <b>MOMENTUM ALERT:</b> Akumulasi beli sangat kuat!")

    if float(mcap) > 0 and (liq_usd / float(mcap)) < 0.02 and liq_usd > 0:
        warnings.append("⚠️ <b>LOW LIQ RATIO (&lt;2% MC):</b> Likuiditas tipis dibanding Market Cap.")

    warning_block = ""
    if warnings:
        warning_block = "\n" + "\n".join(warnings) + "\n"

    security_block = ""
    if security:
        security_block = "\n" + services.format_security_block_compact(security)

    text = (
        f"<b>{chain_badge} | {dex_name}</b>\n"
        f"<b>💎 {token_name} (${token_symbol})</b>\n"
        f"<i>Pair: ${token_symbol} / ${quote_symbol}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Price:</b> <code>{price_str}</code>\n"
        f"💎 <b>Market Cap:</b> <code>{_format_dex_compact(mcap)}</code> | <b>FDV:</b> <code>{_format_dex_compact(fdv)}</code>\n"
        f"💧 <b>Liquidity:</b> <code>{_format_dex_compact(liq_usd)}</code>\n"
        f"⏳ <b>Age:</b> <code>{age_str}</code>\n\n"
        f"📊 <b>PRICE CHANGE:</b>\n"
        f"├ 5m  : {c_5m}\n"
        f"├ 1h  : {c_1h}\n"
        f"├ 6h  : {c_6h}\n"
        f"└ 24h : {c_24h}\n\n"
        f"⚡ <b>TRANSACTIONS (24h):</b>\n"
        f"├ Volume : <code>{vol_24h}</code>\n"
        f"├ Buys   : <code>{buys:,}</code>\n"
        f"├ Sells  : <code>{sells:,}</code>\n"
        f"└ Flow   : {flow_label}\n"
        f"{security_block}"
        f"{warning_block}\n"
        f"📝 <b>Contract Address (Tap to copy):</b>\n"
        f"<code>{ca}</code>"
    )

    # Store token in refresh cache
    ref_token = hashlib.md5(f"{chain_id}:{pair_addr}".encode()).hexdigest()[:12]
    _prune_dict_cache(DEX_REFRESH_CACHE, max_size=500, ttl=DEX_REFRESH_TTL)
    DEX_REFRESH_CACHE[ref_token] = {
        "chain": chain_id,
        "pair": pair_addr,
        "ca": ca,
        "time": time.time()
    }

    # Build Buttons
    keyboard = []

    # Row 1: DexScreener + Direct Trade Link
    row1 = []
    dex_url = pair.get("url") or f"https://dexscreener.com/{chain_id}/{pair_addr}"
    row1.append(InlineKeyboardButton(text="🦅 DexScreener", url=dex_url))

    if chain_id == "solana":
        row1.append(InlineKeyboardButton(text="⚡ Photon", url=f"https://photon-sol.tinyastro.io/en/r/@cekrego/{ca}"))
        row1.append(InlineKeyboardButton(text="🐂 BullX", url=f"https://bullx.io/terminal?chainId=1399811149&address={ca}"))
    elif chain_id == "base":
        row1.append(InlineKeyboardButton(text="🦄 Uniswap", url=f"https://app.uniswap.org/swap?outputCurrency={ca}&chain=base"))
    elif chain_id == "ethereum":
        row1.append(InlineKeyboardButton(text="🦄 Uniswap", url=f"https://app.uniswap.org/swap?outputCurrency={ca}&chain=mainnet"))
    elif chain_id == "bsc":
        row1.append(InlineKeyboardButton(text="🥞 Pancake", url=f"https://pancakeswap.finance/swap?outputCurrency={ca}&chain=bsc"))
    else:
        row1.append(InlineKeyboardButton(text="🔄 Swap", url=dex_url))
    keyboard.append(row1)

    # Row 2: Socials (Twitter/X, Telegram)
    socials = (pair.get("info") or {}).get("socials") or []
    social_row = []
    for s in socials:
        stype = (s.get("type") or "").lower()
        surl = s.get("url") or ""
        if not surl:
            continue
        if stype == "twitter" and len(social_row) < 2:
            social_row.append(InlineKeyboardButton(text="🐦 Twitter/X", url=surl))
        elif stype == "telegram" and len(social_row) < 3:
            social_row.append(InlineKeyboardButton(text="💬 Telegram", url=surl))

    websites = (pair.get("info") or {}).get("websites") or []
    if websites and len(social_row) < 3:
        wurl = websites[0].get("url")
        if wurl:
            social_row.append(InlineKeyboardButton(text="🌐 Web", url=wurl))

    if social_row:
        keyboard.append(social_row)

    # Row 3: Other chains / pairs switcher (if available)
    if other_pairs:
        alt_row = []
        seen_dex = {dex_name.lower()}
        for alt in other_pairs:
            alt_chain = str(alt.get("chainId") or "").lower()
            if alt_chain == chain_id and str(alt.get("pairAddress")) == pair_addr:
                continue
            alt_dex_raw = str(alt.get("dexId") or "DEX").lower()
            alt_dex = dex_display_map.get(alt_dex_raw, alt_dex_raw.title())
            if alt_dex.lower() in seen_dex:
                continue
            seen_dex.add(alt_dex.lower())
            alt_ref = hashlib.md5(f"{alt_chain}:{alt.get('pairAddress')}".encode()).hexdigest()[:12]
            DEX_REFRESH_CACHE[alt_ref] = {
                "chain": alt_chain,
                "pair": alt.get("pairAddress"),
                "ca": (alt.get("baseToken") or {}).get("address"),
                "time": time.time()
            }
            alt_row.append(InlineKeyboardButton(
                text=alt_dex,
                callback_data=f"dex_pair:{alt_ref}"
            ))
            if len(alt_row) >= 3:
                break
        if alt_row:
            keyboard.append(alt_row)

    # Row 4: RugCheck Detail + Refresh Button
    row4 = []
    if chain_id == "solana" or chain_id in ("ethereum", "bsc", "base", "arbitrum", "polygon", "optimism", "avalanche", "blast"):
        row4.append(InlineKeyboardButton(text="🛡️ RugCheck Audit", callback_data=f"dex_rug:{ref_token}"))
    row4.append(InlineKeyboardButton(text="🔄 Refresh Data", callback_data=f"dex_ref:{ref_token}"))
    keyboard.append(row4)

    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)

def _is_contract_address(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    if " " in t or "\n" in t or t.startswith("/"):
        return False
    # EVM address (0x + 40 hex)
    if re.match(r"^0x[a-fA-F0-9]{40}$", t):
        return True
    # Solana / Base58 (32-44 chars)
    if 32 <= len(t) <= 44 and re.match(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$", t):
        has_digit = any(c.isdigit() for c in t)
        has_upper = any(c.isupper() for c in t)
        has_lower = any(c.islower() for c in t)
        if (has_digit and (has_upper or has_lower)) or (has_upper and has_lower):
            return True
    return False

async def _process_dex_query(message_or_call, query: str, is_ca: bool = False, edit_message_obj=None):
    q = query.strip()
    if not q:
        return

    if is_ca or _is_contract_address(q):
        pairs = await services.get_dex_token(q)
    else:
        pairs = await services.search_dex_pairs(q)

    if not pairs:
        err_msg = f"❌ Token / Pair untuk <code>{html.escape(q)}</code> tidak ditemukan di DexScreener."
        if edit_message_obj:
            await edit_message_obj.edit_text(err_msg, parse_mode="HTML")
        else:
            await message_or_call.reply(err_msg, parse_mode="HTML")
        return

    best_pair = pairs[0]
    other_pairs = pairs[1:] if len(pairs) > 1 else None
    chain_id = str(best_pair.get("chainId") or "").lower()
    ca = str((best_pair.get("baseToken") or {}).get("address") or "")
    security = await services.get_token_security(chain_id, ca) if ca else None

    card_text, kb = _build_dex_card(best_pair, other_pairs=other_pairs, security=security)

    if edit_message_obj:
        await edit_message_obj.edit_text(card_text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
    else:
        await message_or_call.reply(card_text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

# Command /dex & /ca
@router.message(Command("dex", "ca"))
async def cmd_dex_radar(message: Message):
    limited, remain = is_rate_limited(message.from_user.id, "dex")
    if limited:
        await message.reply(f"⏳ Tunggu {remain} detik dulu ya 😄", parse_mode=None)
        return

    content = message.text or message.caption or ""
    parts = content.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply(
            "🦅 <b>DexScreener On-Chain Radar + RugCheck</b>\n\n"
            "Cek harga, likuiditas, distribusi holder & audit keamanan kontrak DEX/Meme realtime.\n\n"
            "<b>Cara Penggunaan:</b>\n"
            "• <code>/dex [simbol / nama]</code> : Contoh <code>/dex pepe</code> atau <code>/dex bonk</code>\n"
            "• <code>/dex [contract address]</code> : Contoh <code>/dex DezXAZ...</code>\n"
            "• <code>/rug [CA / simbol]</code> : Audit detail RugCheck & Top Holders\n"
            "• <b>Atau langsung paste CA</b> di chat tanpa command!\n",
            parse_mode="HTML"
        )
        return

    query = parts[1].strip()
    msg_wait = await message.reply(
        f"🔍 <i>Scanning DexScreener & RugCheck for <code>{html.escape(query)}</code>...</i>",
        parse_mode="HTML"
    )
    try:
        await _process_dex_query(message, query, edit_message_obj=msg_wait)
    except Exception as e:
        logging.error(f"Dex Cmd Error: {e}")
        await msg_wait.edit_text("❌ Gagal memproses data DexScreener.", parse_mode="HTML")

# Command /rug, /rugcheck, /scan, /audit
@router.message(Command("rug", "rugcheck", "scan", "audit"))
async def cmd_rugcheck_audit(message: Message):
    limited, remain = is_rate_limited(message.from_user.id, "rug")
    if limited:
        await message.reply(f"⏳ Tunggu {remain} detik dulu ya 😄", parse_mode=None)
        return

    content = message.text or message.caption or ""
    parts = content.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply(
            "🛡️ <b>On-Chain Contract & RugCheck Scanner</b>\n\n"
            "Audit keamanan smart contract, LP lock, Mint/Freeze authority, Honeypot, dan distribusi Top Holders (Non-LP).\n\n"
            "<b>Cara Penggunaan:</b>\n"
            "• <code>/rug &lt;contract_address&gt;</code>\n"
            "• <code>/rug &lt;nama / simbol token&gt;</code> (Contoh: <code>/rug bonk</code> atau <code>/rug pepe</code>)",
            parse_mode="HTML"
        )
        return

    query = parts[1].strip()
    msg_wait = await message.reply(
        f"🛡️ <i>Running on-chain security audit for <code>{html.escape(query)}</code>...</i>",
        parse_mode="HTML"
    )
    try:
        pair = None
        chain_id = "solana"
        ca = query

        if _is_contract_address(query):
            if query.startswith("0x"):
                pairs = await services.get_dex_token(query)
                if pairs:
                    pair = pairs[0]
                    chain_id = str(pair.get("chainId") or "ethereum").lower()
                    ca = str((pair.get("baseToken") or {}).get("address") or query)
                else:
                    chain_id = "ethereum"
            else:
                chain_id = "solana"
                pairs = await services.get_dex_token(query)
                if pairs:
                    pair = pairs[0]
        else:
            pairs = await services.search_dex_pairs(query)
            if not pairs:
                await msg_wait.edit_text(f"❌ Token <code>{html.escape(query)}</code> tidak ditemukan.", parse_mode="HTML")
                return
            pair = pairs[0]
            chain_id = str(pair.get("chainId") or "solana").lower()
            ca = str((pair.get("baseToken") or {}).get("address") or "")

        sec = await services.get_token_security(chain_id, ca, force_refresh=True)
        if not sec:
            await msg_wait.edit_text(
                f"⚠️ Data audit keamanan belum tersedia untuk kontrak <code>{html.escape(ca)}</code> di chain <b>{html.escape(chain_id.upper())}</b>.",
                parse_mode="HTML"
            )
            return

        audit_text = services.format_full_rugcheck_card(sec, pair=pair)
        kb_rows = []
        if pair:
            pair_addr = str(pair.get("pairAddress") or "")
            ref_token = hashlib.md5(f"{chain_id}:{pair_addr}".encode()).hexdigest()[:12]
            DEX_REFRESH_CACHE[ref_token] = {
                "chain": chain_id,
                "pair": pair_addr,
                "ca": ca,
                "time": time.time()
            }
            kb_rows.append([
                InlineKeyboardButton(text="📊 Lihat DEX Card", callback_data=f"dex_ref:{ref_token}"),
                InlineKeyboardButton(text="🔄 Refresh Audit", callback_data=f"dex_rug:{ref_token}")
            ])
        if sec.get("url"):
            kb_rows.append([InlineKeyboardButton(text=f"🌐 Buka di {sec.get('source', 'Scanner')}", url=sec["url"])])

        await msg_wait.edit_text(
            audit_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None,
            disable_web_page_preview=True
        )
    except Exception as e:
        logging.error(f"RugCheck Cmd Error: {e}")
        await msg_wait.edit_text("❌ Gagal mengambil laporan audit kontrak.", parse_mode="HTML")

# Auto-detect Contract Address directly pasted in chat
@router.message(F.text.func(_is_contract_address))
async def handle_auto_ca_detect(message: Message):
    limited, remain = is_rate_limited(message.from_user.id, "dex")
    if limited:
        await message.reply(f"⏳ Tunggu {remain} detik dulu ya 😄", parse_mode=None)
        return

    ca = (message.text or "").strip()
    msg_wait = await message.reply(
        f"🦅 <i>Contract Address terdeteksi! Mengambil data on-chain & RugCheck...</i>",
        parse_mode="HTML"
    )
    try:
        await _process_dex_query(message, ca, is_ca=True, edit_message_obj=msg_wait)
    except Exception as e:
        logging.error(f"Auto CA Detect Error: {e}")
        await msg_wait.edit_text("❌ Gagal memproses Contract Address.", parse_mode="HTML")

# Callback RugCheck Detail Button
@router.callback_query(F.data.startswith("dex_rug:"))
async def callback_dex_rugcheck(call: CallbackQuery):
    try:
        ref_token = call.data.split(":", 1)[1]
        item = DEX_REFRESH_CACHE.get(ref_token)
        if not item:
            await call.answer("Sesi sudah expired. Gunakan /rug atau /dex ulang.", show_alert=True)
            return

        await call.answer("🛡️ Mengambil audit keamanan kontrak...")
        chain_id = item.get("chain") or "solana"
        pair_addr = item.get("pair") or ""
        ca = item.get("ca") or ""

        pair_data, sec = await asyncio.gather(
            services.get_dex_pair(chain_id, pair_addr),
            services.get_token_security(chain_id, ca, force_refresh=True),
            return_exceptions=True
        )
        if isinstance(pair_data, Exception):
            pair_data = None
        if isinstance(sec, Exception) or not sec:
            await call.answer("⚠️ Laporan audit belum tersedia untuk token ini.", show_alert=True)
            return

        audit_text = services.format_full_rugcheck_card(sec, pair=pair_data)
        kb_rows = [
            [
                InlineKeyboardButton(text="« Kembali ke DEX Card", callback_data=f"dex_ref:{ref_token}"),
                InlineKeyboardButton(text="🔄 Refresh Audit", callback_data=f"dex_rug:{ref_token}")
            ]
        ]
        if sec.get("url"):
            kb_rows.append([InlineKeyboardButton(text=f"🌐 Buka di {sec.get('source', 'Scanner')}", url=sec["url"])])

        await call.message.edit_text(
            audit_text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
            disable_web_page_preview=True
        )
    except Exception as e:
        if "message is not modified" in str(e).lower():
            return
        logging.error(f"Dex RugCheck Callback Error: {e}")

# Callback Refresh Button
@router.callback_query(F.data.startswith("dex_ref:"))
async def callback_dex_refresh(call: CallbackQuery):
    try:
        ref_token = call.data.split(":", 1)[1]
        item = DEX_REFRESH_CACHE.get(ref_token)
        if not item:
            await call.answer("Sesi refresh sudah expired. Cari ulang dengan /dex.", show_alert=True)
            return

        chain_id = item.get("chain")
        pair_addr = item.get("pair")
        ca = item.get("ca")

        pair_data, sec = await asyncio.gather(
            services.get_dex_pair(chain_id, pair_addr, force_refresh=True),
            services.get_token_security(chain_id, ca, force_refresh=True) if ca else asyncio.sleep(0),
            return_exceptions=True
        )
        if isinstance(pair_data, Exception):
            pair_data = None
        if isinstance(sec, Exception):
            sec = None

        if not pair_data and ca:
            pairs = await services.get_dex_token(ca, force_refresh=True)
            if pairs:
                pair_data = pairs[0]

        if not pair_data:
            await call.answer("Gagal update data DEX saat ini.", show_alert=True)
            return

        card_text, kb = _build_dex_card(pair_data, security=sec)
        await call.message.edit_text(card_text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        await call.answer("⚡ Data & RugCheck berhasil diperbarui!")
    except Exception as e:
        if "message is not modified" in str(e).lower():
            await call.answer("⚡ Harga sudah yang terbaru!")
            return
        logging.error(f"Dex Refresh Error: {e}")
        await call.answer("Terjadi kesalahan saat refresh.")

# Callback Switch Pair / Chain
@router.callback_query(F.data.startswith("dex_pair:"))
async def callback_dex_switch_pair(call: CallbackQuery):
    try:
        ref_token = call.data.split(":", 1)[1]
        item = DEX_REFRESH_CACHE.get(ref_token)
        if not item:
            await call.answer("Pilihan pair expired. Coba cari ulang.", show_alert=True)
            return

        chain_id = item.get("chain")
        pair_addr = item.get("pair")
        ca = item.get("ca")
        pair_data, sec = await asyncio.gather(
            services.get_dex_pair(chain_id, pair_addr),
            services.get_token_security(chain_id, ca) if ca else asyncio.sleep(0),
            return_exceptions=True
        )
        if isinstance(pair_data, Exception) or not pair_data:
            await call.answer("Pair tidak ditemukan.", show_alert=True)
            return
        if isinstance(sec, Exception):
            sec = None

        card_text, kb = _build_dex_card(pair_data, security=sec)
        await call.message.edit_text(card_text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        await call.answer("Switched!")
    except Exception as e:
        logging.error(f"Dex Switch Pair Error: {e}")

# --- 0.9. GRADUATED / ALPHA SCANNER COMMAND ---
@router.message(Command("graduated", "alpha"))
async def cmd_graduated_tokens(message: Message):
    limited, remain = is_rate_limited(message.from_user.id, "graduated")
    if limited:
        await message.reply(f"⏳ Tunggu {remain} detik dulu ya 😄", parse_mode=None)
        return

    recent = services.get_recent_graduated_tokens(limit=5)
    if not recent:
        await message.reply(
            "🎓 <b>Pump.fun ➔ Raydium Graduation Radar</b>\n\n"
            "<i>Belum ada token baru yang lulus sejak bot restart.</i>\n\n"
            "Scanner sedang aktif memantau stream realtime 24/7 dan otomatis mengirimkan sinyal beserta audit RugCheck ke channel <b>@cekrego</b>! ⚡",
            parse_mode="HTML"
        )
        return

    latest = recent[0]
    pair_data = latest.get("pair") or {}
    raw_data = latest.get("data") or {}
    sec_data = latest.get("security")
    if not sec_data and latest.get("mint"):
        sec_data = await services.get_solana_rugcheck(latest.get("mint"))
    card_text, kb = services.build_graduation_signal_card(pair_data, raw_data, security=sec_data)

    if len(recent) > 1:
        hist_btns = []
        for idx, item in enumerate(recent[1:4], 2):
            sym = (item.get("pair", {}).get("baseToken", {})).get("symbol", "?").upper()
            mcap = _format_dex_compact(item.get("pair", {}).get("marketCap") or 0)
            hist_btns.append(InlineKeyboardButton(text=f"#{idx} ${sym} ({mcap})", callback_data=f"dex_tok:{item.get('mint')}"))
        if hist_btns:
            kb.inline_keyboard.append(hist_btns)

    await message.reply(card_text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)

@router.callback_query(F.data.startswith("dex_tok:"))
async def callback_view_graduated_token(call: CallbackQuery):
    try:
        mint = call.data.split(":", 1)[1]
        pairs, sec = await asyncio.gather(
            services.get_dex_token(mint),
            services.get_solana_rugcheck(mint),
            return_exceptions=True
        )
        if isinstance(pairs, Exception) or not pairs:
            await call.answer("Token tidak ditemukan di DexScreener.", show_alert=True)
            return
        if isinstance(sec, Exception):
            sec = None
        card_text, kb = services.build_graduation_signal_card(pairs[0], security=sec)
        await call.message.edit_text(card_text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        await call.answer()
    except Exception as e:
        logging.error(f"Graduated token callback error: {e}")
        await call.answer("Gagal memuat token.")

# --- 1.0. ADMIN STATS (ADMIN ONLY) ---
@router.message(Command("adminstats"))
async def cmd_admin_stats(message: Message):
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        await message.reply("⛔ Admin only\\.", parse_mode="MarkdownV2")
        return

    limited, remain = is_rate_limited(message.from_user.id, "adminstats")
    if limited:
        await message.reply(f"⏳ Tunggu {remain} detik dulu ya 😄", parse_mode=None)
        return

    msg = await message.reply("📊 _Mengambil analytics\\.\\.\\._", parse_mode="MarkdownV2")
    try:
        stats = await database.get_admin_stats()
        text = _build_admin_stats_text(stats)
        await msg.edit_text(text, parse_mode="MarkdownV2")
    except Exception as e:
        logging.error(f"Admin Stats Error: {e}")
        await msg.edit_text("❌ Gagal ambil analytics\\.", parse_mode="MarkdownV2")

# --- ADMIN ADD ---
@router.message(Command("add"))
async def cmd_add(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        await message.reply("⛔ _Akses ditolak\\. Hanya Admin yang dapat menggunakan perintah ini\\._", parse_mode="MarkdownV2")
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.reply("`Usage: /add symbol id`", parse_mode="MarkdownV2")
        return
    await database.add_preferred_id(parts[1], parts[2])
    await message.reply(f"✅ Saved `{escape_md(parts[1])}` ➔ `{escape_md(parts[2])}`", parse_mode="MarkdownV2")

# --- START/HELP INTERACTIVE MENU ---
def _build_help_view(tab: str = "home") -> tuple[str, InlineKeyboardMarkup]:
    tab = (tab or "home").lower().strip()

    if tab == "market":
        text = (
            "📈 <b>MARKET DATA, HARGA &amp; CHART</b>\n"
            "<i>Data harga global real-time, grafik visual &amp; likuidasi futures</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔹 <b>Cek Harga &amp; Konversi Cepat</b>\n"
            "├ <code>/p &lt;koin&gt;</code> — Cek harga &amp; statistik koin\n"
            "├ <code>/p &lt;jumlah&gt; &lt;koin&gt;</code> — Konversi koin ke USD &amp; IDR\n"
            "└ <i>Contoh:</i> <code>/p btc</code> atau <code>/p 50 sol</code>\n\n"
            "🔹 <b>Trigger Chat Otomatis (Tanpa Command)</b>\n"
            "├ <code>1 btc</code> / <code>50 doge</code> — Cek harga langsung di chat\n"
            "├ <code>50rb idr sol</code> — Konversi Rupiah ke jumlah koin\n"
            "└ <code>100 usd idr</code> — Konversi nilai antar mata uang fiat\n\n"
            "🔹 <b>Chart &amp; Eksplorasi Bursa</b>\n"
            "├ <code>/c &lt;koin&gt; [1|7|30]</code> — Grafik harga (1, 7, atau 30 hari)\n"
            "├ <code>/market &lt;koin&gt;</code> — Daftar bursa CEX &amp; DEX beserta volume\n"
            "├ <code>/trend</code> — Top koin paling trending di CoinGecko\n"
            "├ <code>/gnr</code> &amp; <code>/lsr</code> — Top Gainers &amp; Top Losers (24 Jam)\n"
            "└ <code>/liq [koin]</code> — Stream likuidasi posisi Long/Short Binance"
        )
    elif tab == "dex":
        text = (
            "🛡️ <b>ON-CHAIN DEX RADAR &amp; RUGCHECK</b>\n"
            "<i>Pemindaian token DEX real-time &amp; audit keamanan kontrak pintar</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔹 <b>DexScreener On-Chain Radar</b>\n"
            "├ <code>/dex &lt;simbol / nama&gt;</code> — Cari pair DEX berdasarkan nama\n"
            "├ <code>/dex &lt;contract_address&gt;</code> — Cek detail token via CA\n"
            "└ <i>Contoh:</i> <code>/dex bonk</code> atau <code>/dex pepe</code>\n\n"
            "🔹 <b>Smart Contract &amp; RugCheck Scanner</b>\n"
            "├ <code>/rug &lt;CA / simbol&gt;</code> — Audit keamanan kontrak mendalam\n"
            "├ <b>Solana (RugCheck.xyz):</b> Risk Score, Mint/Freeze Authority,\n"
            "│  LP Locked %, Insider Networks &amp; Top 10 Holders (Non-LP)\n"
            "└ <b>EVM (GoPlus Security):</b> Honeypot check, Verified Source,\n"
            "   Buy/Sell Tax, Ownership Renounced &amp; Top Holders\n\n"
            "🔹 <b>Auto-Detect &amp; Pump.fun Graduation Radar</b>\n"
            "├ <b>Paste CA Langsung:</b> Kirim alamat kontrak Solana/EVM di chat\n"
            "│  tanpa command untuk membuka kartu DEX + RugCheck otomatis\n"
            "└ <code>/graduated</code> (<code>/alpha</code>) — Radar koin baru lulus Pump.fun ➔ Raydium"
        )
    elif tab == "ai":
        text = (
            "🧠 <b>AI INTELLIGENCE &amp; QUANT SIGNALS</b>\n"
            "<i>Analisis teknikal, fundamental &amp; sinyal kuantitatif real-time</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔹 <b>AI Trading Signal &amp; Technical Setup</b>\n"
            "├ <code>/ai &lt;koin&gt;</code> — Analisis indikator TA (RSI, MACD, EMA, BB, ATR)\n"
            "│  + sentimen berita untuk menghasilkan setup LONG/SHORT/WAIT,\n"
            "│  level Entry, Take Profit, Stop Loss &amp; Risk Score\n"
            "└ <i>Contoh:</i> <code>/ai btc</code> atau <code>/ai sol</code>\n\n"
            "🔹 <b>Live Quant Signals (signals.x70.ai)</b>\n"
            "├ <code>/signals</code> (<code>/x70</code>) — Daftar sinyal quant yang sedang aktif\n"
            "└ <code>/signals &lt;koin&gt;</code> — Cek Whale Flow &amp; Orderbook koin tertentu\n\n"
            "🔹 <b>Riset Fundamental &amp; Diskusi AI</b>\n"
            "├ <code>/news &lt;koin&gt;</code> — Ringkasan berita terbaru &amp; sentimen pasar\n"
            "├ <code>/thesis &lt;koin&gt;</code> — Analisis Bull/Bear case &amp; katalis utama\n"
            "└ <code>/ask &lt;pertanyaan&gt;</code> — Tanya jawab AI (bisa di-<i>reply</i> berlanjut)"
        )
    elif tab == "tools":
        text = (
            "🛠️ <b>UTILITAS, KALENDER &amp; LAPORAN HARIAN</b>\n"
            "<i>Alat bantu trading harian, jadwal makro &amp; otomatisasi</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "🔹 <b>Kalender Makro &amp; Event Kripto</b>\n"
            "├ <code>/calendar</code> — Jadwal event penting 7 hari ke depan\n"
            "└ <code>/calendar &lt;hari&gt;</code> — Rentang khusus (1–30 hari, UTC &amp; WIB)\n\n"
            "🔹 <b>Kalkulator, Kurs Fiat &amp; Zona Waktu</b>\n"
            "├ <code>/calc &lt;ekspresi&gt;</code> — Kalkulator aman (atau ketik <code>100*25/2</code>)\n"
            "├ <code>/fiat &lt;kode&gt;</code> — Cek kurs &amp; info mata uang fiat (<code>/fiat idr</code>)\n"
            "└ <code>20:00 UTC</code> — Ketik jam UTC di chat untuk auto-konversi ke WIB\n\n"
            "🔹 <b>Laporan Harian &amp; Sistem</b>\n"
            "├ <code>/daily on</code> | <code>/daily off</code> — Langganan laporan pasar otomatis\n"
            "│  setiap pukul 07:00 &amp; 23:00 WIB\n"
            "└ <code>/adminstats</code> — Dashboard analitik penggunaan bot <i>(Admin)</i>"
        )
    elif tab == "all":
        text = (
            "📋 <b>DAFTAR LENGKAP COMMAND CEKREGO</b>\n"
            "<i>Klik pada bagian di bawah untuk membuka/melipat rincian</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n\n"
            "<b>📈 Market, Harga &amp; Chart</b>\n"
            "<blockquote expandable>"
            "• <code>/p &lt;jml&gt; &lt;koin&gt;</code> : Cek harga &amp; konversi\n"
            "• <code>1 btc</code> / <code>50rb idr sol</code> : Trigger konversi cepat\n"
            "• <code>/c &lt;koin&gt; [1|7|30]</code> : Grafik harga visual\n"
            "• <code>/market &lt;koin&gt;</code> : Daftar listing CEX &amp; DEX\n"
            "• <code>/trend</code> : Koin trending di CoinGecko\n"
            "• <code>/gnr</code> | <code>/lsr</code> : Top Gainers &amp; Top Losers 24h\n"
            "• <code>/liq [koin]</code> : Realtime liquidation feed"
            "</blockquote>\n\n"
            "<b>🛡️ On-Chain DEX &amp; RugCheck</b>\n"
            "<blockquote expandable>"
            "• <code>/dex &lt;koin / CA&gt;</code> : Kartu DexScreener + RugCheck\n"
            "• <code>/rug &lt;CA / koin&gt;</code> : Audit keamanan kontrak &amp; Top Holders\n"
            "• <code>Paste CA</code> : Auto-detect kontrak Solana &amp; EVM\n"
            "• <code>/graduated</code> : Radar token lulus Pump.fun ➔ Raydium"
            "</blockquote>\n\n"
            "<b>🧠 AI &amp; Quant Signals</b>\n"
            "<blockquote expandable>"
            "• <code>/ai &lt;koin&gt;</code> : Prediksi sinyal TA + Fundamental\n"
            "• <code>/signals [koin]</code> : Live quant signals (signals.x70.ai)\n"
            "• <code>/news &lt;koin&gt;</code> : Ringkasan berita &amp; sentimen koin\n"
            "• <code>/thesis &lt;koin&gt;</code> : Bull &amp; Bear thesis koin\n"
            "• <code>/ask &lt;pertanyaan&gt;</code> : Tanya jawab AI interaktif"
            "</blockquote>\n\n"
            "<b>🛠️ Utilitas &amp; Laporan</b>\n"
            "<blockquote expandable>"
            "• <code>/calendar [hari]</code> : Kalender event makro &amp; kripto\n"
            "• <code>/calc &lt;ekspresi&gt;</code> : Kalkulator matematika aman\n"
            "• <code>/fiat &lt;kode&gt;</code> : Info mata uang fiat\n"
            "• <code>20:00 UTC</code> : Auto-convert jam UTC ke WIB\n"
            "• <code>/daily on|off</code> : Laporan harian (07:00 &amp; 23:00 WIB)"
            "</blockquote>"
        )
    else:
        tab = "home"
        text = (
            "🦅 <b>CEKREGO CRYPTO TERMINAL</b>\n"
            "<i>On-Chain Radar • Smart Contract Audit • AI Market Intelligence</i>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "Selamat datang di <b>CekRego</b> — asisten riset pasar kripto, pemindai token DEX, "
            "dan auditor keamanan kontrak pintar (Solana &amp; EVM).\n\n"
            "⚡ <b>Pintasan Instan (Tanpa Perlu Command):</b>\n"
            "├ 💵 Ketik <code>1 btc</code> atau <code>50rb idr sol</code> untuk konversi harga\n"
            "├ 🛡️ Paste <b>Contract Address</b> untuk cek DEX + RugCheck otomatis\n"
            "├ 🧮 Ketik <code>1250 * 16500</code> untuk kalkulator cepat\n"
            "└ 🕒 Ketik <code>20:00 UTC</code> untuk konversi jam ke WIB\n\n"
            "👇 <b>Pilih kategori di bawah untuk melihat panduan perintah:</b>"
        )

    def _lbl(key: str, label: str) -> str:
        return f"• {label} •" if tab == key else label

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=_lbl("market", "📈 Market & Harga"), callback_data="help_tab:market"),
            InlineKeyboardButton(text=_lbl("dex", "🛡️ DEX & RugCheck"), callback_data="help_tab:dex"),
        ],
        [
            InlineKeyboardButton(text=_lbl("ai", "🧠 AI & Signals"), callback_data="help_tab:ai"),
            InlineKeyboardButton(text=_lbl("tools", "🛠️ Tools & Daily"), callback_data="help_tab:tools"),
        ],
        [
            InlineKeyboardButton(text=_lbl("home", "🏠 Beranda"), callback_data="help_tab:home"),
            InlineKeyboardButton(text=_lbl("all", "📋 Semua Command"), callback_data="help_tab:all"),
        ],
        [
            InlineKeyboardButton(text="📡 Channel @cekrego", url="https://t.me/cekrego"),
            InlineKeyboardButton(text="⭐ GitHub Repo", url="https://github.com/nialthony/cekrego-crypto-bot"),
        ]
    ])
    return text, kb


@router.message(Command("start", "help"))
async def cmd_start(message: Message):
    parts = (message.text or "").strip().split(maxsplit=1)
    initial_tab = "home"
    if len(parts) > 1:
        arg = parts[1].strip().lower()
        if arg in ("market", "price", "harga"):
            initial_tab = "market"
        elif arg in ("dex", "rug", "rugcheck", "ca"):
            initial_tab = "dex"
        elif arg in ("ai", "signals", "signal", "x70"):
            initial_tab = "ai"
        elif arg in ("tools", "util", "daily", "calendar"):
            initial_tab = "tools"
        elif arg in ("all", "full", "semua"):
            initial_tab = "all"

    text, kb = _build_help_view(initial_tab)
    await message.answer(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)


@router.callback_query(F.data.startswith("help_tab:"))
async def callback_help_tab(call: CallbackQuery):
    try:
        tab = call.data.split(":", 1)[1]
        text, kb = _build_help_view(tab)
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        await call.answer()
    except Exception as e:
        if "message is not modified" in str(e).lower():
            await call.answer()
            return
        logging.error(f"Help Tab Callback Error: {e}")
        await call.answer()
