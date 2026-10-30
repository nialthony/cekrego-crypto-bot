# bot/utils/symbols.py
import re
import bot.database as database
import bot.services as services

def _clean_symbol_key(raw: str) -> str:
    s = (raw or "").strip().lower()
    return re.sub(r"[^a-z0-9]", "", s)

def _rank_sort_key(coin: dict):
    rank = coin.get("market_cap_rank")
    try:
        if rank is None:
            return 999999
        return int(rank)
    except:
        return 999999

async def resolve_symbol_candidates(raw_symbol: str, limit: int = 5):
    raw = (raw_symbol or "").strip()
    norm = _clean_symbol_key(raw)
    if not norm:
        return {"status": "not_found", "coin": None, "matches": []}

    pref_id = await database.get_preferred_id(norm)
    if pref_id:
        return {"status": "direct", "coin": {"id": pref_id, "symbol": raw.upper(), "name": pref_id.replace('-', ' ').title()}}

    results = await services.search_coin(raw)
    if not results:
        results = await services.search_coin(norm)
    if not results:
        return {"status": "not_found", "coin": None, "matches": []}

    ranked = sorted(results, key=_rank_sort_key)
    exact_symbol = [c for c in ranked if _clean_symbol_key(c.get("symbol")) == norm]
    exact_id = [c for c in ranked if _clean_symbol_key(c.get("id")) == norm]
    exact_name = [c for c in ranked if _clean_symbol_key(c.get("name")) == norm]

    chosen = None
    if exact_id:
        chosen = exact_id[0]
    elif exact_symbol:
        chosen = exact_symbol[0]
    elif exact_name:
        chosen = exact_name[0]

    if chosen:
        top_rank = chosen.get("market_cap_rank")
        try:
            top_rank_val = int(top_rank) if top_rank is not None else 999999
        except:
            top_rank_val = 999999

        competitors = [
            c for c in exact_symbol
            if c.get("id") != chosen.get("id")
        ]
        if competitors and top_rank_val > 150:
            top_matches = [chosen] + competitors[:max(1, limit - 1)]
            return {
                "status": "ambiguous",
                "matches": [{k: item[k] for k in ("id", "symbol", "name")} for item in top_matches]
            }

        return {"status": "direct", "coin": {k: chosen[k] for k in ("id", "symbol", "name")}}

    top = ranked[:max(2, limit)]
    return {"status": "ambiguous", "matches": [{k: item[k] for k in ("id", "symbol", "name")} for item in top]}
