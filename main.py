# main.py
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Any, Awaitable

from aiogram import Bot, Dispatcher, BaseMiddleware
from aiogram.types import Message, TelegramObject

from config import BOT_TOKEN, ALPHA_CHANNEL
from bot.handlers import router
from bot import services, database

UTC7 = timezone(timedelta(hours=7))
DAILY_SLOTS_UTC7 = ((7, 0), (23, 0))

# Konfigurasi Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S"
)

# --- MIDDLEWARE: CCTV PENCATAT USERNAME ---
class UserLoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        if isinstance(event, Message):
            user = event.from_user
            if user:
                identity = f"@{user.username}" if user.username else f"{user.first_name}"
            else:
                identity = "unknown_user"
            chat_type = event.chat.type.title() if event.chat and event.chat.type else "Unknown"

            msg_text = event.text if event.text else "[Media/Sticker]"
            clean_text = msg_text.replace("\n", " ")[:50]

            logging.info(f"👤 {identity} [{chat_type}] ➔ {clean_text}")
            try:
                await database.track_user_activity(
                    chat_id=event.chat.id,
                    user_id=user.id if user else 0,
                    username=user.username if user else "",
                    first_name=user.first_name if user else "",
                    chat_type=event.chat.type if event.chat else "unknown",
                    text=event.text or event.caption or ""
                )
            except Exception as e:
                logging.warning(f"Track activity gagal: {e}")

        return await handler(event, data)

def _fmt_price(v):
    try:
        n = float(v or 0)
    except Exception:
        return "0"
    if 0 < abs(n) < 1:
        return f"{n:,.6f}"
    return f"{n:,.2f}"

def _fmt_compact(v):
    try:
        n = float(v or 0)
    except Exception:
        return "0"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.2f}K"
    return f"{n:.2f}"

def _build_daily_report_text(slot_dt_utc7, coins, trending):
    lines = []
    lines.append("DAILY MARKET REPORT (UTC+7)")
    lines.append(f"SLOT : {slot_dt_utc7.strftime('%Y-%m-%d %H:%M')}")
    lines.append("=" * 72)
    lines.append("TOP MARKET MOVERS")
    lines.append(f"{'No':>2} {'SYM':<8} {'PRICE(USD)':>12} {'24H%':>8} {'VOL24H':>12}")
    lines.append("-" * 72)

    for i, c in enumerate((coins or [])[:10], 1):
        sym = str(c.get("symbol", "")).upper().replace("`", "'")[:8]
        price = _fmt_price(c.get("current_price"))
        chg = float(c.get("price_change_percentage_24h") or 0)
        vol = _fmt_compact(c.get("total_volume") or c.get("usd_24h_vol") or 0)
        lines.append(f"{i:>2} {sym:<8} {price:>12} {chg:>8.2f} {vol:>12}")

    lines.append("")
    lines.append("TRENDING SEARCH")
    lines.append(f"{'No':>2} {'NAME':<24} {'SYM':<8} {'RANK':>6}")
    lines.append("-" * 72)

    for i, item in enumerate((trending or [])[:10], 1):
        d = item.get("item", {})
        name = str(d.get("name") or "-").replace("`", "'")[:24]
        sym = str(d.get("symbol") or "-").upper().replace("`", "'")[:8]
        rank = str(d.get("market_cap_rank") or "-")
        lines.append(f"{i:>2} {name:<24} {sym:<8} {rank:>6}")

    lines.append("")
    lines.append("NFA. DYOR.")
    return "```\n" + "\n".join(lines) + "\n```"

def _next_daily_slot_utc7(now_utc7: datetime):
    candidates = []
    for h, m in DAILY_SLOTS_UTC7:
        dt = now_utc7.replace(hour=h, minute=m, second=0, microsecond=0)
        if dt <= now_utc7:
            dt += timedelta(days=1)
        candidates.append(dt)
    return min(candidates)

async def _broadcast_daily_report(bot: Bot, slot_dt_utc7: datetime):
    chats = await database.get_broadcast_chat_ids("daily")
    if not chats:
        logging.info("Daily report skip: no subscribers.")
        return

    coins = await services.get_top_coins(limit=50)
    trending = await services.get_trending()
    if not coins:
        logging.warning("Daily report skip: market data kosong.")
        return

    sorted_coins = sorted(
        coins,
        key=lambda x: abs(float(x.get("price_change_percentage_24h") or 0)),
        reverse=True
    )
    text = _build_daily_report_text(slot_dt_utc7, sorted_coins[:10], trending[:10] if trending else [])

    sent = 0
    for chat_id in chats:
        try:
            await bot.send_message(chat_id, text, parse_mode="MarkdownV2", disable_web_page_preview=True)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            err_str = str(e).lower()
            if "bot was blocked" in err_str or "chat not found" in err_str or "user is deactivated" in err_str:
                await database.set_subscription(chat_id, "daily", False)
                logging.info(f"Unsubscribed dead/blocked chat {chat_id}")
            else:
                logging.warning(f"Daily report gagal kirim ke {chat_id}: {e}")
    logging.info(f"Daily report sent: {sent}/{len(chats)} chats.")

async def _daily_report_loop(bot: Bot):
    while True:
        now_local = datetime.now(UTC7)
        target = _next_daily_slot_utc7(now_local)
        wait_sec = max((target - now_local).total_seconds(), 0) + 2
        logging.info(f"Daily report next slot: {target.strftime('%Y-%m-%d %H:%M')} UTC+7")
        await asyncio.sleep(wait_sec)
        try:
            await _broadcast_daily_report(bot, target)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.error(f"Daily report loop error: {e}")

async def _channel_auto_delete_loop(bot: Bot, channel: str = "@cekrego", max_age_days: int = 7):
    """Loop pembersih otomatis pesan sinyal di channel yang sudah lebih dari 7 hari."""
    logging.info(f"🧹 Auto-delete loop initialized (Target: {channel}, Max age: {max_age_days} days).")
    while True:
        try:
            expired = await database.get_expired_channel_messages(max_age_days=max_age_days)
            if expired:
                deleted = 0
                for item in expired:
                    rec_id = item["id"]
                    chat_id = item["chat_id"]
                    msg_id = item["message_id"]
                    try:
                        await bot.delete_message(chat_id=chat_id, message_id=msg_id)
                        deleted += 1
                        await database.delete_channel_message_record(rec_id)
                    except Exception as del_err:
                        err_str = str(del_err).lower()
                        if "message to delete not found" in err_str or "message can't be deleted" in err_str:
                            await database.delete_channel_message_record(rec_id)
                        else:
                            logging.warning(f"Auto-delete failed for {chat_id}:{msg_id}: {del_err}")
                    await asyncio.sleep(0.1)
                if deleted > 0:
                    logging.info(f"🧹 Auto-delete: cleaned {deleted} messages older than {max_age_days} days from channel.")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.error(f"Auto-delete loop error: {e}")

        # Jalankan pengecekan setiap 1 jam (3600 detik)
        await asyncio.sleep(3600)

async def _maintenance_loop():
    """Maintenance berkala: membersihkan log database lama setiap 12 jam."""
    while True:
        try:
            await asyncio.sleep(43200)
            await database.prune_message_logs(keep_days=30)
            logging.info("🧹 Maintenance: message_logs yang lebih lama dari 30 hari dibersihkan.")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.error(f"Maintenance loop error: {e}")

# --- MAIN FUNCTION ---
async def main():
    # 1. Init Database
    await database.init_db()
    logging.info("💾 Database initialized.")

    # 2. Init HTTP Session
    await services.init_session()

    # 3. Setup Bot & Dispatcher
    bot = Bot(token=BOT_TOKEN or "")
    dp = Dispatcher()

    # Pasang Middleware (CCTV Log)
    dp.message.middleware(UserLoggingMiddleware())

    # 4. Register Router
    dp.include_router(router)

    logging.info("🚀 Bot started! Siap memantau user...")

    bg_tasks = [
        asyncio.create_task(_daily_report_loop(bot), name="daily_report_loop"),
        asyncio.create_task(services.liquidation_ws_loop(), name="liquidation_ws_loop"),
        asyncio.create_task(services.marketfeed_userbot_loop(), name="marketfeed_userbot_loop"),
        asyncio.create_task(services.pump_graduation_scanner_loop(bot, ALPHA_CHANNEL), name="pump_graduation_scanner_loop"),
        asyncio.create_task(_channel_auto_delete_loop(bot, ALPHA_CHANNEL, 7), name="channel_auto_delete_loop"),
        asyncio.create_task(_maintenance_loop(), name="maintenance_loop"),
    ]

    try:
        await dp.start_polling(bot)
    finally:
        for task in bg_tasks:
            task.cancel()
        await asyncio.gather(*bg_tasks, return_exceptions=True)
        await services.close_session()
        await bot.session.close()
        logging.info("🛑 Bot stopped.")

if __name__ == "__main__":
    try:
        if sys.platform == 'win32':
            try:
                sys.stdout.reconfigure(encoding='utf-8', errors='replace')
                sys.stderr.reconfigure(encoding='utf-8', errors='replace')
            except Exception:
                pass
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
