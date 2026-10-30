# config.py
import os
from dotenv import load_dotenv

load_dotenv()

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return float(default)

def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return int(default)

BOT_TOKEN = os.getenv("TELEGRAM_API_TOKEN")
CG_API_KEY = os.getenv("COINGECKO_API_KEY", "")
COINGECKO_API_KEY = CG_API_KEY

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash").strip() or "gemini-2.0-flash"
GEMINI_MODEL_ASK = os.getenv("GEMINI_MODEL_ASK", "gemini-2.0-flash").strip() or "gemini-2.0-flash"
GEMINI_ASK_PERSONA = (os.getenv("GEMINI_ASK_PERSONA", "analyst").strip() or "analyst").lower()
GEMINI_ASK_STYLE = (os.getenv("GEMINI_ASK_STYLE", "natural").strip() or "natural").lower()
GEMINI_ASK_TEMPERATURE = max(0.0, min(1.5, _env_float("GEMINI_ASK_TEMPERATURE", 0.6)))

# OpenRouter Configuration
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b").strip() or "nvidia/nemotron-3-ultra-550b-a55b"
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1/chat/completions").strip() or "https://openrouter.ai/api/v1/chat/completions"

# Trading Signals Quant API (signals.x70.ai / roman-rr/trading-skills)
X70_SIGNALS_API_KEY = os.getenv("X70_SIGNALS_API_KEY", "").strip()
X70_SIGNALS_BASE_URL = os.getenv("X70_SIGNALS_BASE_URL", "https://signals.x70.ai").strip().rstrip("/")


ADMIN_ID = int(os.getenv("ADMIN_USER_ID", 0))

WHALE_ALERT_API_KEY = os.getenv("WHALE_ALERT_API_KEY", "").strip()

# Telethon Userbot Config
TELETHON_API_ID = _env_int("TELETHON_API_ID", 0)
TELETHON_API_HASH = os.getenv("TELETHON_API_HASH", "").strip()
TELETHON_SESSION_NAME = os.getenv("TELETHON_SESSION_NAME", "userbot").strip() or "userbot"
TELETHON_CHANNELS = os.getenv("TELETHON_CHANNELS", "marketfeed").strip() or "marketfeed"
TELETHON_FETCH_LIMIT = max(1, min(200, _env_int("TELETHON_FETCH_LIMIT", 50)))
TELETHON_POLL_SEC = max(15, min(3600, _env_int("TELETHON_POLL_SEC", 120)))
TELETHON_ENABLED = str(os.getenv("TELETHON_ENABLED", "1")).strip().lower() in ("1", "true", "yes", "y", "on")

# Aliases for Telethon
TELEGRAM_API_ID = TELETHON_API_ID
TELEGRAM_API_HASH = TELETHON_API_HASH
TELEGRAM_SESSION_NAME = TELETHON_SESSION_NAME
MARKETFEED_CHANNELS = TELETHON_CHANNELS

MARKETFEED_CONTEXT_MAX_ITEMS = max(1, min(30, _env_int("MARKETFEED_CONTEXT_MAX_ITEMS", 8)))
MARKETFEED_CONTEXT_MAX_AGE_HOURS = max(1, min(168, _env_int("MARKETFEED_CONTEXT_MAX_AGE_HOURS", 24)))

# Liquidation & Websockets
BINANCE_LIQ_WS_URL = os.getenv("BINANCE_LIQ_WS_URL", "wss://fstream.binance.com/ws/!forceOrder@arr")
LIQUIDATION_SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "DOGEUSDT", "XRPUSDT"]
PUMPPORTAL_WS_URL = os.getenv("PUMPPORTAL_WS_URL", "wss://pumpportal.fun/api/data")

if not BOT_TOKEN:
    raise ValueError("Token Telegram gak ketemu di file .env!")

# Database
DB_NAME = "crypto_bot.db"

# Alpha Token Graduation Radar Config
ALPHA_CHANNEL = os.getenv("ALPHA_CHANNEL", "@cekrego").strip() or "@cekrego"
ALPHA_SCANNER_ENABLED = str(os.getenv("ALPHA_SCANNER_ENABLED", "1")).strip().lower() in ("1", "true", "yes", "y", "on")

