# bot/database/db.py
import aiosqlite
import time
from config import DB_NAME

def get_db():
    """Mengembalikan koneksi aiosqlite dengan timeout dan optimasi concurrency."""
    return aiosqlite.connect(DB_NAME, timeout=15.0)

async def init_db():
    """Inisialisasi tabel SQLite."""
    async with get_db() as db:
        # Improve concurrency for many readers/writers
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("PRAGMA busy_timeout=5000;")

        # Tabel Preferred IDs (Shortcuts)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS preferred_ids (
                symbol TEXT PRIMARY KEY,
                api_id TEXT
            )
        ''')
        # Tabel User/Chat Profile & Subscription
        await db.execute('''
            CREATE TABLE IF NOT EXISTS user_profiles (
                chat_id INTEGER PRIMARY KEY,
                user_id INTEGER,
                username TEXT,
                first_name TEXT,
                chat_type TEXT,
                daily_report_enabled INTEGER DEFAULT 1,
                created_at INTEGER,
                last_seen INTEGER,
                message_count INTEGER DEFAULT 0
            )
        ''')
        # Tabel Log Aktivitas Message/Command (untuk analytics)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS message_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER,
                user_id INTEGER,
                is_command INTEGER DEFAULT 0,
                command TEXT,
                created_at INTEGER
            )
        ''')
        # Tabel Auto-Delete Channel Message Logs
        await db.execute('''
            CREATE TABLE IF NOT EXISTS channel_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT,
                message_id INTEGER,
                created_at INTEGER
            )
        ''')
        # Tabel Deduplikasi DEX Graduation Scanner
        await db.execute('''
            CREATE TABLE IF NOT EXISTS seen_pairs (
                pair_address TEXT PRIMARY KEY,
                created_at INTEGER
            )
        ''')
        # Tabel Market Feed (Telethon Userbot Context)
        await db.execute('''
            CREATE TABLE IF NOT EXISTS market_feed (
                channel TEXT,
                message_id INTEGER,
                date_ts INTEGER,
                text TEXT,
                permalink TEXT,
                PRIMARY KEY (channel, message_id)
            )
        ''')

        # Bersihkan tabel MFP lama jika masih ada
        await db.execute("DROP TABLE IF EXISTS user_mfp_keys;")
        await db.execute("DROP TABLE IF EXISTS user_autotrade_settings;")
        await db.execute("DROP TABLE IF EXISTS autotrade_logs;")

        # Indexing untuk performa kueri
        await db.execute("CREATE INDEX IF NOT EXISTS idx_logs_chat ON message_logs(chat_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_logs_user ON message_logs(user_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_logs_created ON message_logs(created_at);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_chan_created ON channel_messages(created_at);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_chan_chat ON channel_messages(chat_id);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_seen_pairs_created ON seen_pairs(created_at);")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_market_feed_date ON market_feed(date_ts);")

        # Indeks pencarian teks username
        await db.execute("CREATE INDEX IF NOT EXISTS idx_profiles_username ON user_profiles(username);")

        await db.commit()

async def prune_message_logs(keep_days: int = 30) -> int:
    """Hapus logs aktivitas yang lebih tua dari keep_days untuk mencegah database membengkak."""
    cutoff = int(time.time()) - (keep_days * 86400)
    async with get_db() as db:
        cursor = await db.execute("DELETE FROM message_logs WHERE created_at < ?", (cutoff,))
        deleted_count = cursor.rowcount
        await db.commit()
        return deleted_count
