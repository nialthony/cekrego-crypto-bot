# bot/services/telethon_feed.py
import re
import os
import time
import asyncio
import logging
import unicodedata
from datetime import datetime, timezone

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from config import (
    TELETHON_API_ID, TELETHON_API_HASH, TELETHON_SESSION_NAME,
    TELETHON_CHANNELS, TELETHON_FETCH_LIMIT, TELETHON_POLL_SEC,
    TELETHON_ENABLED, MARKETFEED_CHANNELS,
    MARKETFEED_CONTEXT_MAX_ITEMS, MARKETFEED_CONTEXT_MAX_AGE_HOURS
)
import bot.database as database


def _normalize_search_text(text):
    s = str(text or "").strip().lower().replace("đ", "d")
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")

def _parse_marketfeed_channels(raw: str):
    out = []
    for part in (raw or "").split(","):
        s = part.strip()
        if not s:
            continue
        if "t.me/" in s:
            s = s.split("t.me/", 1)[1]
            s = s.split("/", 1)[0]
        if s.startswith("@"):
            s = s[1:]
        if s:
            out.append(s)
    uniq = []
    seen = set()
    for s in out:
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(s)
    return uniq

def _compact_text(txt: str, max_len: int):
    t = (txt or "").replace("\r", " ").replace("\n", " ").strip()
    if len(t) <= max_len:
        return t
    return t[:max_len].rstrip() + "..."

async def _store_marketfeed_message(channel: str, msg):
    if not msg or not msg.id:
        return False
    text = msg.message or msg.raw_text or ""
    text = _compact_text(text, 2000)
    if not text:
        return False
    ts = int(msg.date.timestamp()) if msg.date else int(time.time())
    permalink = f"https://t.me/{channel}/{msg.id}" if channel else ""
    return await database.upsert_market_feed(channel, msg.id, ts, text, permalink)

async def get_marketfeed_context(max_items=None, max_age_hours=None):
    if not TELETHON_ENABLED:
        return ""
    max_items = max_items or MARKETFEED_CONTEXT_MAX_ITEMS
    max_age_hours = max_age_hours or MARKETFEED_CONTEXT_MAX_AGE_HOURS
    rows = await database.get_market_feed_recent(max_items, max_age_hours)
    if not rows:
        return ""
    lines = []
    for row in rows:
        ts = int(row.get("date_ts") or 0)
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        text = _compact_text(row.get("text") or "", 180)
        if text:
            lines.append(f"- [{dt}] {text}")
    return "\n".join(lines)

async def marketfeed_userbot_loop():
    if not TELETHON_ENABLED:
        logging.info("[MF] Telethon disabled via TELETHON_ENABLED.")
        return
    if not TELETHON_API_ID or not TELETHON_API_HASH:
        logging.info("[MF] Telethon credentials missing; skipping userbot.")
        return
    if TelegramClient is None:
        logging.warning("[MF] Telethon not installed; skipping userbot.")
        return

    channels = _parse_marketfeed_channels(TELETHON_CHANNELS)
    if not channels:
        logging.info("[MF] No channels configured; skipping userbot.")
        return

    client = TelegramClient(TELETHON_SESSION_NAME, TELETHON_API_ID, TELETHON_API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            logging.warning("[MF] Telethon session belum ter-otorisasi / login. Userbot dilewati agar bot utama tetap berjalan lancar tanpa terhenti.")
            await client.disconnect()
            return
    except Exception as e:
        logging.error(f"[MF] Telethon start failed: {e}")
        return

    logging.info(f"[MF] Userbot started. Channels: {', '.join(channels)}")

    last_ids = {}
    for ch in channels:
        last_ids[ch] = await database.get_market_feed_latest_id(ch)

    try:
        while True:
            for ch in channels:
                try:
                    messages = await client.get_messages(ch, limit=TELETHON_FETCH_LIMIT)
                    if not messages:
                        continue
                    new_count = 0
                    for msg in reversed(messages):
                        if not msg or not msg.id:
                            continue
                        if msg.id <= last_ids.get(ch, 0):
                            continue
                        if await _store_marketfeed_message(ch, msg):
                            new_count += 1
                        if msg.id > last_ids.get(ch, 0):
                            last_ids[ch] = msg.id
                    if new_count:
                        logging.info(f"[MF] {ch}: stored {new_count} new posts")
                except FloodWaitError as e:
                    wait_sec = int(getattr(e, "seconds", 30))
                    logging.warning(f"[MF] Flood wait {wait_sec}s")
                    await asyncio.sleep(min(max(wait_sec, 5), 900))
                except Exception as e:
                    logging.warning(f"[MF] {ch} fetch error: {e}")

            await asyncio.sleep(TELETHON_POLL_SEC)
    except asyncio.CancelledError:
        raise
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

