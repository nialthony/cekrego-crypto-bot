# bot/utils/rate_limit.py
import time
from collections import defaultdict

COOLDOWN = {
    "price": 2,
    "ai": 8,
    "chart": 5,
    "trend": 3,
    "gnr": 3,
    "lsr": 3,
    "fiat": 2,
    "calc": 1,
    "ask": 5,
    "liq": 3,
    "calendar": 3,
    "news": 10,
    "thesis": 12,
    "dex": 3,
    "rug": 3,
    "graduated": 5,
    "alpha": 5,
    "signals": 4,
}

_last_call = defaultdict(lambda: defaultdict(float))

def is_rate_limited(user_id: int, key: str):
    now = time.time()
    if len(_last_call) > 1000:
        inactive = [u for u, v in list(_last_call.items()) if all(now - t > 1800 for t in v.values())]
        for u in inactive[:300]:
            _last_call.pop(u, None)
    cd = COOLDOWN.get(key, 0)
    last = _last_call[user_id][key]
    if cd and now - last < cd:
        remain = int(cd - (now - last))
        return True, max(remain, 1)
    _last_call[user_id][key] = now
    return False, 0
