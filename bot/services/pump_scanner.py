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


def build_graduation_signal_card(pair: dict, raw_event: dict = None, security: dict = None) -> tuple[str, InlineKeyboardMarkup]:
    """
    Format kartu sinyal kelulusan Pump.fun -> Raydium dengan gaya minimalis & bersih.
    """
    base = pair.get("baseToken") or {}
    quote = pair.get("quoteToken") or {}
    name = html.escape(base.get("name") or "Unknown")
    symbol = html.escape((base.get("symbol") or "???").upper())
    quote_sym = html.escape((quote.get("symbol") or "SOL").upper())
    ca = html.escape(base.get("address") or "")

    price_usd = float(pair.get("priceUsd") or 0.0)
    mcap = float(pair.get("marketCap") or pair.get("fdv") or 0.0)
    liq_usd = float((pair.get("liquidity") or {}).get("usd") or 0.0)

    txns_h1 = (pair.get("txns") or {}).get("h1") or {}
    buys = int(txns_h1.get("buys") or 0)
    sells = int(txns_h1.get("sells") or 0)
    total_tx = buys + sells
    ratio_str = f"{(buys / total_tx * 100):.0f}% Buy" if total_tx > 0 else "New Pool"

    if price_usd <= 0:
        p_str = "$0.00"
    elif price_usd < 0.0001:
        p_str = f"${price_usd:.8f}".rstrip("0").rstrip(".")
    elif price_usd < 1:
        p_str = f"${price_usd:.6f}".rstrip("0").rstrip(".")
    else:
        p_str = f"${price_usd:,.4f}".rstrip("0").rstrip(".")

    info = pair.get("info") or {}
    socials = info.get("socials") or []
    has_twitter = any((s.get("type") or "").lower() == "twitter" for s in socials)
    has_tg = any((s.get("type") or "").lower() == "telegram" for s in socials)

    if has_twitter and has_tg:
        social_status = "Twitter &amp; TG Active"
    elif has_twitter:
        social_status = "Twitter Active"
    elif has_tg:
        social_status = "Telegram Active"
    else:
        social_status = "No Socials"

    if security:
        verdict = security.get("verdict", "GOOD")
        score = security.get("score_normalised", 0)
        mint_ok = security.get("mint_authority") is None
        freeze_ok = security.get("freeze_authority") is None
        lp_locked = float(security.get("lp_locked_pct") or 100.0)
        top10 = float(security.get("top10_pct") or 0.0)
        insiders = int(security.get("insiders_count") or 0)
        insider_str = f" | Insiders: {insiders}" if insiders > 0 else ""

        safety_lines = [
            f"RugCheck   : {verdict} (Risk: {score}/100)",
            f"LP Status  : {lp_locked:.0f}% Locked/Burned",
            f"Authority  : Mint {'Revoked' if mint_ok else 'ACTIVE'} | Freeze {'Disabled' if freeze_ok else 'ACTIVE'}",
            f"Top 10 Hold: {top10:.1f}%{insider_str}",
            f"Community  : {social_status}",
        ]
    else:
        safety_lines = [
            "LP Status  : 100% Burned &amp; Locked",
            "Authority  : Mint Revoked | Freeze Disabled",
            f"Community  : {social_status}",
        ]

    pre_lines = [
        f"GRADUATED TO RAYDIUM: {name} ({symbol})",
        f"Pair: {symbol}/{quote_sym} | Pool: Raydium",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"Price      : {p_str}",
        f"Market Cap : ${mcap:,.0f}",
        f"Liquidity  : ${liq_usd:,.0f}",
        f"Txns (1h)  : {buys}B / {sells}S ({ratio_str})",
        "━━━━━━━━━━━━━━━━━━━━━",
        *safety_lines,
    ]

    text = f"<pre>{'\n'.join(pre_lines)}</pre>\n<b>CA:</b> <code>{ca}</code>"

    row1 = [
        InlineKeyboardButton(text="Photon", url=f"https://photon-sol.tinyastro.io/en/r/@cekrego/{ca}"),
        InlineKeyboardButton(text="BullX", url=f"https://bullx.io/terminal?chainId=1399811149&address={ca}"),
    ]
    row2 = [
        InlineKeyboardButton(text="DexScreener", url=pair.get("url") or f"https://dexscreener.com/solana/{ca}"),
        InlineKeyboardButton(text="RugCheck", url=f"https://rugcheck.xyz/tokens/{ca}"),
    ]
    for s in socials:
        stype = (s.get("type") or "").lower()
        surl = s.get("url")
        if stype == "twitter" and surl and len(row2) < 3:
            row2.append(InlineKeyboardButton(text="Twitter/X", url=surl))
        elif stype == "telegram" and surl and len(row2) < 3:
            row2.append(InlineKeyboardButton(text="Telegram", url=surl))

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
