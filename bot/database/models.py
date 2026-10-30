# bot/database/models.py
import time
import re
from config import ADMIN_ID
from .db import get_db

async def add_preferred_id(symbol, api_id):
    async with get_db() as db:
        await db.execute(
            'INSERT OR REPLACE INTO preferred_ids (symbol, api_id) VALUES (?, ?)',
            (symbol.lower(), api_id)
        )
        await db.commit()

async def get_preferred_id(symbol):
    async with get_db() as db:
        async with db.execute('SELECT api_id FROM preferred_ids WHERE symbol = ?', (symbol.lower(),)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else None

async def search_preferred_ids(query, limit=10):
    q = (query or "").strip().lower()
    if not q:
        return []

    tokens = [t for t in re.split(r"[^a-z0-9]+", q) if t]

    async with get_db() as db:
        async with db.execute('SELECT symbol, api_id FROM preferred_ids') as cursor:
            rows = await cursor.fetchall()

    scored = []
    for symbol, api_id in rows:
        symbol_l = (symbol or "").lower()
        api_id_l = (api_id or "").lower()

        score = 0
        if symbol_l == q:
            score += 1000
        if api_id_l == q:
            score += 900
        if symbol_l.startswith(q):
            score += 300
        if api_id_l.startswith(q):
            score += 220
        if q in symbol_l:
            score += 180
        if q in api_id_l:
            score += 140

        for tok in tokens:
            if tok == symbol_l:
                score += 150
            if tok == api_id_l:
                score += 120
            if tok in symbol_l:
                score += 70
            if tok in api_id_l:
                score += 50

        if score > 0:
            scored.append((score, symbol, api_id))

    scored.sort(key=lambda item: (-item[0], item[1], item[2]))

    return [
        {"symbol": symbol, "api_id": api_id}
        for _, symbol, api_id in scored[:max(1, int(limit))]
    ]

# --- TRACKING USER / ANALYTICS ---
def _extract_command(text: str):
    if not text:
        return None
    txt = text.strip()
    if not txt.startswith("/"):
        return None
    first = txt.split()[0].lower()
    if "@" in first:
        first = first.split("@", 1)[0]
    return first

async def track_user_activity(chat_id, user_id, username, first_name, chat_type, text):
    now = int(time.time())
    cmd = _extract_command(text or "")
    is_command = 1 if cmd else 0

    async with get_db() as db:
        await db.execute(
            '''
            INSERT INTO user_profiles (
                chat_id, user_id, username, first_name, chat_type, created_at, last_seen, message_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(chat_id) DO UPDATE SET
                user_id = excluded.user_id,
                username = excluded.username,
                first_name = excluded.first_name,
                chat_type = excluded.chat_type,
                last_seen = excluded.last_seen,
                message_count = message_count + 1
            ''',
            (chat_id, user_id, username, first_name, chat_type, now, now)
        )
        await db.execute(
            '''
            INSERT INTO message_logs (chat_id, user_id, is_command, command, created_at)
            VALUES (?, ?, ?, ?, ?)
            ''',
            (chat_id, user_id, is_command, cmd, now)
        )
        await db.commit()

async def prune_message_logs(keep_days: int = 30):
    """Membersihkan log pesan yang lebih lama dari keep_days agar database tidak membengkak."""
    cutoff = int(time.time()) - (keep_days * 86400)
    async with get_db() as db:
        await db.execute("DELETE FROM message_logs WHERE created_at < ?", (cutoff,))
        await db.commit()

async def set_subscription(chat_id, feature: str, enabled: bool):
    field_map = {
        "daily": "daily_report_enabled",
    }
    field = field_map.get((feature or "").lower())
    if not field:
        return False

    now = int(time.time())
    async with get_db() as db:
        await db.execute(
            '''
            INSERT INTO user_profiles (
                chat_id, user_id, username, first_name, chat_type, created_at, last_seen, message_count
            ) VALUES (?, 0, '', '', 'unknown', ?, ?, 0)
            ON CONFLICT(chat_id) DO UPDATE SET last_seen = excluded.last_seen
            ''',
            (chat_id, now, now)
        )
        await db.execute(
            f"UPDATE user_profiles SET {field} = ? WHERE chat_id = ?",
            (1 if enabled else 0, chat_id)
        )
        await db.commit()
        return True

async def get_subscription(chat_id):
    async with get_db() as db:
        async with db.execute(
            "SELECT daily_report_enabled FROM user_profiles WHERE chat_id = ?",
            (chat_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return {"daily": 1}
            return {"daily": int(row[0] or 0)}

async def get_broadcast_chat_ids(feature: str, active_days: int = 45):
    field_map = {
        "daily": "daily_report_enabled",
    }
    field = field_map.get((feature or "").lower())
    if not field:
        return []

    min_seen = int(time.time()) - (max(1, active_days) * 86400)

    async with get_db() as db:
        async with db.execute(
            f"SELECT chat_id FROM user_profiles WHERE {field} = 1 AND last_seen >= ?",
            (min_seen,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [int(r[0]) for r in rows]

async def get_admin_stats():
    now = int(time.time())
    d1 = now - 86400
    d7 = now - (7 * 86400)
    d30 = now - (30 * 86400)

    async with get_db() as db:
        async with db.execute("SELECT COUNT(*) FROM user_profiles") as c:
            row = await c.fetchone()
            total_chats = row[0] if row else 0
        async with db.execute("SELECT COUNT(*) FROM user_profiles WHERE chat_type = 'private'") as c:
            row = await c.fetchone()
            private_chats = row[0] if row else 0
        async with db.execute("SELECT COUNT(*) FROM user_profiles WHERE chat_type != 'private'") as c:
            row = await c.fetchone()
            group_chats = row[0] if row else 0
        async with db.execute("SELECT COUNT(*) FROM user_profiles WHERE last_seen >= ?", (d1,)) as c:
            row = await c.fetchone()
            active_24h = row[0] if row else 0
        async with db.execute("SELECT COUNT(*) FROM user_profiles WHERE last_seen >= ?", (d7,)) as c:
            row = await c.fetchone()
            active_7d = row[0] if row else 0
        async with db.execute("SELECT COUNT(*) FROM user_profiles WHERE last_seen >= ?", (d30,)) as c:
            row = await c.fetchone()
            active_30d = row[0] if row else 0
        async with db.execute("SELECT COALESCE(SUM(message_count),0) FROM user_profiles") as c:
            row = await c.fetchone()
            total_messages = row[0] if row else 0

        async with db.execute("SELECT COUNT(*) FROM message_logs WHERE created_at >= ?", (d1,)) as c:
            row = await c.fetchone()
            msg_24h = row[0] if row else 0
        async with db.execute("SELECT COUNT(*) FROM message_logs WHERE created_at >= ?", (d7,)) as c:
            row = await c.fetchone()
            msg_7d = row[0] if row else 0
        async with db.execute("SELECT COUNT(*) FROM message_logs WHERE is_command = 1 AND created_at >= ?", (d1,)) as c:
            row = await c.fetchone()
            cmd_24h = row[0] if row else 0
        async with db.execute("SELECT COUNT(*) FROM message_logs WHERE is_command = 1 AND created_at >= ?", (d7,)) as c:
            row = await c.fetchone()
            cmd_7d = row[0] if row else 0

        async with db.execute(
            '''
            SELECT command, COUNT(*) AS c
            FROM message_logs
            WHERE is_command = 1 AND command IS NOT NULL AND created_at >= ?
            GROUP BY command
            ORDER BY c DESC
            LIMIT 10
            ''',
            (d7,)
        ) as c:
            top_commands = await c.fetchall()

        async with db.execute(
            "SELECT COUNT(*) FROM user_profiles WHERE daily_report_enabled = 1"
        ) as c:
            row = await c.fetchone()
            daily_subscribers = row[0] if row else 0
    return {
        "total_chats": total_chats,
        "private_chats": private_chats,
        "group_chats": group_chats,
        "active_24h": active_24h,
        "active_7d": active_7d,
        "active_30d": active_30d,
        "total_messages": total_messages,
        "msg_24h": msg_24h,
        "msg_7d": msg_7d,
        "cmd_24h": cmd_24h,
        "cmd_7d": cmd_7d,
        "top_commands": top_commands,
        "daily_subscribers": daily_subscribers,
    }

# --- MARKET FEED ---
async def upsert_market_feed(channel, message_id, date_ts, text, permalink):
    if not channel or not message_id:
        return False
    async with get_db() as db:
        await db.execute(
            '''
            INSERT INTO market_feed (channel, message_id, date_ts, text, permalink)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(channel, message_id) DO UPDATE SET
                date_ts = excluded.date_ts,
                text = excluded.text,
                permalink = excluded.permalink
            ''',
            (str(channel), int(message_id), int(date_ts or 0), text or "", permalink or "")
        )
        await db.commit()
        return True

async def get_market_feed_latest_id(channel):
    if not channel:
        return 0
    async with get_db() as db:
        async with db.execute(
            "SELECT MAX(message_id) FROM market_feed WHERE channel = ?",
            (str(channel),)
        ) as cursor:
            row = await cursor.fetchone()
            return int(row[0] or 0) if row else 0

async def get_market_feed_recent(limit=10, max_age_hours=24):
    limit = max(1, int(limit or 1))
    max_age_hours = max(1, int(max_age_hours or 1))
    min_ts = int(time.time()) - (max_age_hours * 3600)
    async with get_db() as db:
        async with db.execute(
            '''
            SELECT channel, message_id, date_ts, text, permalink
            FROM market_feed
            WHERE date_ts >= ?
            ORDER BY date_ts DESC
            LIMIT ?
            ''',
            (min_ts, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            return [
                {
                    "channel": r[0],
                    "message_id": r[1],
                    "date_ts": r[2],
                    "text": r[3],
                    "permalink": r[4],
                }
                for r in rows
            ]

# --- CHANNEL MESSAGES AUTO-DELETE ---
async def save_channel_message(chat_id: str, message_id: int):
    """Simpan message_id yang dikirim ke channel agar bisa di-auto-delete setelah 7 hari."""
    now = int(time.time())
    async with get_db() as db:
        await db.execute(
            "INSERT INTO channel_messages (chat_id, message_id, created_at) VALUES (?, ?, ?)",
            (str(chat_id), int(message_id), now)
        )
        await db.commit()

async def get_expired_channel_messages(max_age_days: int = 7):
    """Ambil daftar pesan channel yang umurnya sudah melewati batas hari (default 7 hari)."""
    cutoff = int(time.time()) - (int(max_age_days) * 86400)
    async with get_db() as db:
        async with db.execute(
            "SELECT id, chat_id, message_id FROM channel_messages WHERE created_at < ? ORDER BY id ASC LIMIT 100",
            (cutoff,)
        ) as cursor:
            rows = await cursor.fetchall()
            return [{"id": r[0], "chat_id": r[1], "message_id": r[2]} for r in rows]

async def delete_channel_message_record(record_id: int):
    """Hapus record pesan channel dari database."""
    async with get_db() as db:
        await db.execute("DELETE FROM channel_messages WHERE id = ?", (int(record_id),))
        await db.commit()
