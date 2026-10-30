# bot/services/dexscreener.py
import time
import logging
from .coingecko import get_json_url, _prune_cache

DEX_CACHE = {}
DEX_CACHE_TTL = 30  # 30 detik agar harga & likuiditas tetap realtime


async def get_dex_token(token_address: str, force_refresh: bool = False):
    """Ambil data pair dari token address (CA)."""
    token_address = (token_address or "").strip()
    if not token_address:
        return None
    cache_key = f"tok:{token_address.lower()}"
    now = time.time()
    if not force_refresh and cache_key in DEX_CACHE and now - DEX_CACHE[cache_key]["time"] < DEX_CACHE_TTL:
        return DEX_CACHE[cache_key]["data"]

    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
    data = await get_json_url(url)
    if not data or not data.get("pairs"):
        return None

    pairs = data.get("pairs") or []
    # Urutkan berdasarkan likuiditas USD tertinggi
    pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
    _prune_cache(DEX_CACHE, max_size=300, ttl=DEX_CACHE_TTL)
    DEX_CACHE[cache_key] = {"data": pairs, "time": now}
    return pairs


async def search_dex_pairs(query: str, force_refresh: bool = False):
    """Cari pair DEX berdasarkan nama atau ticker."""
    query = (query or "").strip()
    if not query:
        return None
    cache_key = f"search:{query.lower()}"
    now = time.time()
    if not force_refresh and cache_key in DEX_CACHE and now - DEX_CACHE[cache_key]["time"] < DEX_CACHE_TTL:
        return DEX_CACHE[cache_key]["data"]

    url = "https://api.dexscreener.com/latest/dex/search"
    data = await get_json_url(url, params={"q": query})
    if not data or not data.get("pairs"):
        return None

    pairs = data.get("pairs") or []
    pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
    _prune_cache(DEX_CACHE, max_size=300, ttl=DEX_CACHE_TTL)
    DEX_CACHE[cache_key] = {"data": pairs, "time": now}
    return pairs


async def get_dex_pair(chain_id: str, pair_address: str, force_refresh: bool = False):
    """Ambil data single pair (untuk refresh)."""
    chain_id = (chain_id or "").strip().lower()
    pair_address = (pair_address or "").strip()
    if not chain_id or not pair_address:
        return None

    cache_key = f"pair:{chain_id}:{pair_address.lower()}"
    now = time.time()
    if not force_refresh and cache_key in DEX_CACHE and now - DEX_CACHE[cache_key]["time"] < DEX_CACHE_TTL:
        return DEX_CACHE[cache_key]["data"]

    url = f"https://api.dexscreener.com/latest/dex/pairs/{chain_id}/{pair_address}"
    data = await get_json_url(url)
    if not data:
        return None

    pair = data.get("pair") or (data.get("pairs") and data["pairs"][0])
    if pair:
        _prune_cache(DEX_CACHE, max_size=300, ttl=DEX_CACHE_TTL)
        DEX_CACHE[cache_key] = {"data": pair, "time": now}
    return pair
