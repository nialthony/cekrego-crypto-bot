# bot/handlers/dex_handlers.py
import re
import time
import hashlib
import logging

from aiogram import Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.filters import Command

from bot.utils import (
    escape_md, fmt_usd_raw, fmt_pct_raw, is_rate_limited
)
import bot.services as services

router = Router()

DEX_REFRESH_CACHE = {}
DEX_REFRESH_TTL = 1800  # detik

DEX_KEYWORDS = (
    "uniswap", "pancake", "sushi", "curve", "balancer", "raydium", "orca",
    "jupiter", "meteora", "camelot", "quickswap", "spooky", "traderjoe",
    "kyberswap", "1inch", "matcha", "woofi", "aerodrome", "syncswap",
    "lifinity", "swap", "amm"
)





