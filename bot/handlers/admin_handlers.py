import re
import time
import hashlib
import logging
from datetime import datetime

from aiogram import Router, F
from aiogram.types import (
    Message, InlineQuery, InlineQueryResultArticle, InputTextMessageContent
)
from aiogram.filters import Command

from config import ADMIN_ID
from bot.utils import (
    escape_md, fmt_usd_raw, fmt_idr_raw, fmt_pct_raw,
    _fit_cell, _fmt_compact, _safe_float
)

import bot.database as database
import bot.services as services

router = Router()

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



# ==============================================================================
# ⚡ INLINE MODE HANDLER
# ==============================================================================
@router.inline_query()
async def inline_query_handler(query: InlineQuery):
    try:
        text = query.query.strip().lower()
        results = []

        if not text:
            popular_coins = [
                {"id": "bitcoin", "name": "Bitcoin", "symbol": "BTC", "thumb": "https://assets.coingecko.com/coins/images/1/large/bitcoin.png"},
                {"id": "ethereum", "name": "Ethereum", "symbol": "ETH", "thumb": "https://assets.coingecko.com/coins/images/279/large/ethereum.png"},
                {"id": "solana", "name": "Solana", "symbol": "SOL", "thumb": "https://assets.coingecko.com/coins/images/4128/large/solana.png"},
            ]
            for coin in popular_coins:
                msg_content = InputTextMessageContent(message_text=f"1 {coin['symbol']}", parse_mode=None)
                results.append(
                    InlineQueryResultArticle(
                        id=coin['id'],
                        title=f"{coin['name']} ({coin['symbol']})",
                        description=f"Tap untuk cek harga 1 {coin['symbol']}",
                        thumbnail_url=coin['thumb'],
                        input_message_content=msg_content
                    )
                )
            await query.answer(results, cache_time=10, is_personal=False)
            return

        match = re.match(r"^(\d+(\.\d+)?)\s+([a-zA-Z0-9-]+)$", text)
        search_keyword = text
        amount_prefix = ""
        input_amount = "1"

        if match:
            input_amount = match.group(1)
            search_keyword = match.group(3)
            amount_prefix = f"{input_amount} "

        search_results = await services.search_coin(search_keyword)

        for coin in search_results[:10]:
            coin_id = coin.get('id')
            name = coin.get('name')
            symbol = coin.get('symbol', '').upper()
            thumb = coin.get('large')

            result_id = hashlib.md5(f"{coin_id}_{input_amount}".encode()).hexdigest()
            message_to_send = f"{input_amount} {symbol}"

            title_text = f"{amount_prefix}{symbol} ({name})" if amount_prefix else f"{name} ({symbol})"
            desc_text = f"Tap untuk cek harga {message_to_send}"

            msg_content = InputTextMessageContent(message_text=message_to_send, parse_mode=None)

            results.append(
                InlineQueryResultArticle(
                    id=result_id,
                    title=title_text,
                    description=desc_text,
                    thumbnail_url=thumb,
                    input_message_content=msg_content
                )
            )

        await query.answer(results, cache_time=60, is_personal=False)

    except Exception as e:
        logging.error(f"Inline Query Error: {e}")
        await query.answer([], cache_time=5)





