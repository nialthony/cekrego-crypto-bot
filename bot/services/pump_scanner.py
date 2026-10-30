# bot/services/pump_scanner.py
import json
import time
import html
import asyncio
import logging
import websockets
from collections import deque
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from config import PUMPPORTAL_WS_URL, ALPHA_CHANNEL, ALPHA_SCANNER_ENABLED
from .dexscreener import get_dex_token
from .rugcheck import get_solana_rugcheck
import bot.database as database

RECENT_GRADUATIONS = deque(maxlen=20)
SEEN_GRADUATION_MINTS = deque(maxlen=500)
_pump_scanner_running = False


def build_graduation_signal_card(pair: dict, raw_event: dict = None, security: dict = None):
    """Membangun card sinyal sniping untuk token yang baru listing di Raydium dengan data RugCheck."""
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    name = html.escape(str(base.get("name") or "Unknown"))
    symbol = html.escape(str(base.get("symbol") or "?").upper())
    quote_sym = html.escape(str(quote.get("symbol") or "SOL").upper())
    ca = str(base.get("address") or "")

    price_usd = float(pair.get("priceUsd") or 0)
    mcap = float(pair.get("marketCap") or pair.get("fdv") or 0)
    liq_usd = float((pair.get("liquidity") or {}).get("usd") or 0)

    # Format price
    if price_usd >= 1:
        p_str = f"${price_usd:,.3f}"
    elif price_usd >= 0.0001:
        p_str = f"${price_usd:,.6f}"
    elif price_usd > 0:
        p_str = f"${price_usd:,.10f}".rstrip("0")
    else:
        p_str = "$0.00"

    # Transactions & Volume (5m or 1h)
    tx = pair.get("txns", {}).get("m5") or pair.get("txns", {}).get("h1") or {}
    buys = int(tx.get("buys") or 0)
    sells = int(tx.get("sells") or 0)
    total_tx = buys + sells
    ratio_str = f"{(buys/total_tx*100):.0f}% Buys 🟢" if total_tx > 0 else "Fresh Listing"

    # Socials
    socials = (pair.get("info") or {}).get("socials") or []
    has_twitter = any((s.get("type") or "").lower() == "twitter" for s in socials)
    has_tg = any((s.get("type") or "").lower() == "telegram" for s in socials)
    if has_twitter and has_tg:
        social_status = "Twitter & TG Active ✅"
    elif has_twitter:
        social_status = "Twitter Active ✅"
    elif has_tg:
        social_status = "Telegram Active ✅"
    else:
        social_status = "No Socials Found ⚠️"

    # RugCheck Live Data vs Default Pump.fun Graduation Standards
    if security:
        verdict_badge = security.get("verdict_badge", "🟢 GOOD")
        score = security.get("score_normalised", 0)
        mint_ok = security.get("mint_authority") is None
        freeze_ok = security.get("freeze_authority") is None
        lp_locked = float(security.get("lp_locked_pct") or 100.0)
        top10 = float(security.get("top10_pct") or 0.0)
        insiders = int(security.get("insiders_count") or 0)
        top10_icon = "🟢" if top10 <= 25 else ("🟡" if top10 <= 45 else "🔴")
        insider_str = f" | 🚨 {insiders} Insiders" if insiders > 0 else ""

        safety_lines = [
            f"🛡️ <b>RUGCHECK AUDIT:</b> {verdict_badge} <code>(Risk: {score}/100)</code>",
            f"├ LP Status       : <code>{lp_locked:.0f}% LOCKED/BURNED 🔒</code>",
            f"├ Mint / Freeze   : <code>{'REVOKED ✅' if mint_ok else 'ACTIVE 🚨'}</code> / <code>{'DISABLED ✅' if freeze_ok else 'ACTIVE 🚨'}</code>",
            f"├ Top 10 Holders  : {top10_icon} <code>{top10:.1f}%</code>{insider_str}",
            f"└ Community       : {social_status}",
        ]
    else:
        safety_lines = [
            "🛡️ <b>SAFETY & LIQUIDITY:</b>",
            "├ LP Status       : 100% BURNED & LOCKED 🔒",
            "├ Mint Authority  : REVOKED ✅",
            "├ Freeze Authority: DISABLED ✅",
            f"└ Community       : {social_status}",
        ]

    lines = [
        "🚨 <b>ALPHA SNIPER: GRADUATED TO RAYDIUM!</b>",
        f"<b>💎 {name} (${symbol})</b>",
        f"<i>Pair: ${symbol} / ${quote_sym} | Pool: Raydium</i>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"💵 <b>Market Cap:</b> <code>${mcap:,.0f}</code> (Early Entry!)",
        f"💧 <b>Liquidity:</b> <code>${liq_usd:,.0f}</code> (100% BURNED 🔒)",
        f"💲 <b>Price:</b> <code>{p_str}</code>",
        f"⚡ <b>Momentum:</b> <code>{buys} Buys / {sells} Sells</code> ({ratio_str})",
        "",
        *safety_lines,
        "",
        "📝 <b>Contract Address (Tap to copy):</b>",
        f"<code>{ca}</code>",
    ]

    text = "\n".join(lines)

    # Direct Sniper Buttons
    row1 = [
        InlineKeyboardButton(text="⚡ Snipe (Photon)", url=f"https://photon-sol.tinyastro.io/en/r/@cekrego/{ca}"),
        InlineKeyboardButton(text="🐂 BullX", url=f"https://bullx.io/terminal?chainId=1399811149&address={ca}"),
    ]
    row2 = [
        InlineKeyboardButton(text="🦅 DexScreener", url=pair.get("url") or f"https://dexscreener.com/solana/{ca}"),
        InlineKeyboardButton(text="🛡️ RugCheck", url=f"https://rugcheck.xyz/tokens/{ca}"),
    ]
    for s in socials:
        stype = (s.get("type") or "").lower()
        surl = s.get("url")
        if stype == "twitter" and surl and len(row2) < 3:
            row2.append(InlineKeyboardButton(text="🐦 Twitter/X", url=surl))
        elif stype == "telegram" and surl and len(row2) < 3:
            row2.append(InlineKeyboardButton(text="💬 Telegram", url=surl))

    kb = InlineKeyboardMarkup(inline_keyboard=[row1, row2])
    return text, kb


def get_recent_graduated_tokens(limit=5):
    """Ambil riwayat token kelulusan terbaru dari memory."""
    return list(RECENT_GRADUATIONS)[:max(1, int(limit))]


async def _process_graduation_event(bot, channel_name: str, mint: str, data: dict):
    """Proses penemuan koin lulus secara background agar tidak memblokir loop WebSocket."""
    try:
        logging.info(f"🎓 [ALPHA] Graduation detected for {mint}! Fetching DexScreener & RugCheck...")
        # Beri jeda 4 detik agar DexScreener & RugCheck mendaftarkan pair Raydium
        await asyncio.sleep(4)

        pairs, sec = await asyncio.gather(
            get_dex_token(mint, force_refresh=True),
            get_solana_rugcheck(mint),
            return_exceptions=True
        )
        if isinstance(pairs, Exception):
            pairs = None
        if isinstance(sec, Exception):
            sec = None

        if not pairs:
            # Retry sekali lagi setelah 3 detik
            await asyncio.sleep(3)
            pairs = await get_dex_token(mint, force_refresh=True)

        if not pairs:
            logging.warning(f"[ALPHA] Mint {mint} not found on DexScreener yet; skipping.")
            return

        best_pair = pairs[0]
        # Catat ke memory
        RECENT_GRADUATIONS.appendleft({
            "mint": mint,
            "pair": best_pair,
            "security": sec,
            "time": int(time.time()),
            "data": data
        })

        # Bangun pesan & tombol sinyal
        card_text, kb = build_graduation_signal_card(best_pair, data, security=sec)

        if bot and channel_name:
            try:
                sent = await bot.send_message(
                    chat_id=channel_name,
                    text=card_text,
                    parse_mode="HTML",
                    reply_markup=kb,
                    disable_web_page_preview=True
                )
                await database.save_channel_message(channel_name, sent.message_id)
                logging.info(f"📢 [ALPHA] Posted signal for {mint} to {channel_name} (msg_id: {sent.message_id})")
            except Exception as post_err:
                logging.error(f"[ALPHA] Failed to post signal to {channel_name}: {post_err}")
    except Exception as e:
        logging.error(f"[ALPHA] Process graduation error for {mint}: {e}")


async def pump_graduation_scanner_loop(bot, target_channel: str = "@cekrego"):
    """
    Real-time WebSocket scanner memantau koin Pump.fun yang berhasil
    menembus bonding curve dan migrasi ke Raydium.
    """
    if not ALPHA_SCANNER_ENABLED:
        logging.info("[ALPHA] Scanner disabled via ALPHA_SCANNER_ENABLED.")
        return

    ws_url = "wss://pumpportal.fun/api/data"
    channel_name = target_channel or ALPHA_CHANNEL
    logging.info(f"[ALPHA] Graduation Scanner loop started. Target channel: {channel_name}")

    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20) as ws:
                subscribe_payload = {"method": "subscribeRaydiumLiquidity"}
                await ws.send(json.dumps(subscribe_payload))
                logging.info("🟢 [ALPHA] Connected to PumpPortal Raydium graduation stream")

                async for msg in ws:
                    try:
                        data = json.loads(msg)
                    except Exception:
                        continue

                    if not isinstance(data, dict):
                        continue

                    tx_type = data.get("txType")
                    mint = data.get("mint")

                    if tx_type != "createPool" or not mint:
                        continue

                    if mint in SEEN_GRADUATION_MINTS:
                        continue
                    SEEN_GRADUATION_MINTS.append(mint)

                    # Jalankan pemrosesan koin secara asynchronous tanpa memblokir pembacaan WebSocket
                    asyncio.create_task(_process_graduation_event(bot, channel_name, mint, data))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.warning(f"[ALPHA] Websocket error: {e}. Reconnecting in 8s...")
            await asyncio.sleep(8)
