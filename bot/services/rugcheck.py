# bot/services/rugcheck.py
import time
import html
import logging
from typing import Optional, Dict, Any, List
from .coingecko import init_session, _prune_cache

RUGCHECK_CACHE: Dict[str, Dict[str, Any]] = {}
RUGCHECK_CACHE_TTL = 120  # 2 menit

EVM_CHAIN_IDS = {
    "ethereum": "1",
    "bsc": "56",
    "polygon": "137",
    "arbitrum": "42161",
    "optimism": "10",
    "avalanche": "43114",
    "base": "8453",
    "blast": "81457",
    "fantom": "250",
}


async def get_solana_rugcheck(mint: str, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
    """
    Mengambil laporan keamanan kontrak Solana dari API publik RugCheck.xyz.
    Mengembalikan dict terstruktur berisi status authority, LP lock, top holders, insider, & risk flags.
    """
    mint = (mint or "").strip()
    if not mint:
        return None

    cache_key = f"sol:{mint}"
    now = time.time()
    if not force_refresh and cache_key in RUGCHECK_CACHE:
        if now - RUGCHECK_CACHE[cache_key]["time"] < RUGCHECK_CACHE_TTL:
            return RUGCHECK_CACHE[cache_key]["data"]

    url = f"https://api.rugcheck.xyz/v1/tokens/{mint}/report"
    headers = {
        "Accept": "application/json",
        "User-Agent": "CekRego-CryptoBot/2.0"
    }

    session = await init_session()
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                logging.warning(f"[RUGCHECK] HTTP {resp.status} for {mint}")
                return None
            raw = await resp.json()
    except Exception as e:
        logging.error(f"[RUGCHECK] Error fetching report for {mint}: {e}")
        return None

    if not raw or not isinstance(raw, dict):
        return None

    token_info = raw.get("token") or {}
    token_meta = raw.get("tokenMeta") or {}
    known_accounts = raw.get("knownAccounts") or {}

    # 1. Authority & Metadata Checks
    mint_auth = raw.get("mintAuthority") or token_info.get("mintAuthority")
    freeze_auth = raw.get("freezeAuthority") or token_info.get("freezeAuthority")
    is_mutable = bool(token_meta.get("mutable", False))
    is_rugged = bool(raw.get("rugged", False))

    # 2. Score & Risk Level
    score_norm = int(raw.get("score_normalised") if raw.get("score_normalised") is not None else 50)
    raw_score = int(raw.get("score") or 0)

    if is_rugged or score_norm >= 55:
        verdict = "DANGER"
        verdict_badge = "🔴 DANGER (HIGH RISK)"
    elif score_norm >= 25 or mint_auth or freeze_auth:
        verdict = "WARN"
        verdict_badge = "🟡 WARNING (MEDIUM RISK)"
    else:
        verdict = "SAFE"
        verdict_badge = "🟢 GOOD (LOW RISK)"

    # 3. LP Locked / Burned Calculation
    markets = raw.get("markets") or []
    best_lp_locked_pct = 0.0
    for m in markets:
        lp = (m or {}).get("lp") or {}
        locked_pct = float(lp.get("lpLockedPct") or 0.0)
        if locked_pct > best_lp_locked_pct:
            best_lp_locked_pct = locked_pct

    # 4. Top Holders (Exclude known AMM/LP/Exchange pools)
    top_holders_raw = raw.get("topHolders") or []
    real_holders = []
    lp_holders_pct = 0.0

    for h in top_holders_raw:
        addr = str(h.get("address") or "")
        owner = str(h.get("owner") or "")
        pct = float(h.get("pct") or 0.0)
        known = known_accounts.get(addr) or known_accounts.get(owner) or {}
        acc_type = str(known.get("type") or "").upper()
        acc_name = str(known.get("name") or "")

        if acc_type in ("AMM", "LP", "LOCKER", "EXCHANGE") or "pool" in acc_name.lower() or "raydium" in acc_name.lower():
            lp_holders_pct += pct
            continue

        real_holders.append({
            "address": owner or addr,
            "token_account": addr,
            "pct": pct,
            "insider": bool(h.get("insider", False)),
            "label": acc_name
        })

    top10_holders_pct = sum(h["pct"] for h in real_holders[:10])
    top1_holder_pct = real_holders[0]["pct"] if real_holders else 0.0
    insiders_detected = int(raw.get("graphInsidersDetected") or 0) or sum(1 for h in real_holders if h["insider"])

    # 5. Creator info
    creator = raw.get("creator") or ""
    creator_bal = raw.get("creatorBalance")
    supply = float(token_info.get("supply") or 0)
    creator_pct = 0.0
    if creator_bal and supply > 0:
        try:
            creator_pct = (float(creator_bal) / supply) * 100.0
        except Exception:
            creator_pct = 0.0

    # 6. Risk Items
    risks_raw = raw.get("risks") or []
    risks = []
    for r in risks_raw:
        risks.append({
            "name": str(r.get("name") or "Unknown Risk"),
            "value": str(r.get("value") or ""),
            "description": str(r.get("description") or ""),
            "level": str(r.get("level") or "warn").lower(),
            "score": int(r.get("score") or 0)
        })

    parsed = {
        "chain": "solana",
        "source": "RugCheck.xyz",
        "mint": mint,
        "name": token_meta.get("name") or "",
        "symbol": token_meta.get("symbol") or "",
        "verdict": verdict,
        "verdict_badge": verdict_badge,
        "score_normalised": score_norm,
        "raw_score": raw_score,
        "rugged": is_rugged,
        "mint_authority": mint_auth,
        "freeze_authority": freeze_auth,
        "mutable_metadata": is_mutable,
        "lp_locked_pct": best_lp_locked_pct,
        "top10_pct": top10_holders_pct,
        "top1_pct": top1_holder_pct,
        "top_holders": real_holders[:10],
        "total_holders": int(raw.get("totalHolders") or 0),
        "insiders_count": insiders_detected,
        "creator": creator,
        "creator_pct": creator_pct,
        "risks": risks,
        "launchpad": raw.get("launchpad") or raw.get("deployPlatform") or "",
        "url": f"https://rugcheck.xyz/tokens/{mint}"
    }

    _prune_cache(RUGCHECK_CACHE, max_size=300, ttl=RUGCHECK_CACHE_TTL)
    RUGCHECK_CACHE[cache_key] = {"data": parsed, "time": now}
    return parsed


async def get_evm_goplus_security(chain_id: str, contract_address: str, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
    """
    Mengambil laporan keamanan kontrak EVM (ETH, Base, BSC, Arbitrum, dll.) dari GoPlus Security API.
    """
    chain_key = (chain_id or "").strip().lower()
    ca = (contract_address or "").strip().lower()
    num_chain = EVM_CHAIN_IDS.get(chain_key)
    if not num_chain or not ca:
        return None

    cache_key = f"evm:{chain_key}:{ca}"
    now = time.time()
    if not force_refresh and cache_key in RUGCHECK_CACHE:
        if now - RUGCHECK_CACHE[cache_key]["time"] < RUGCHECK_CACHE_TTL:
            return RUGCHECK_CACHE[cache_key]["data"]

    url = f"https://api.gopluslabs.io/api/v1/token_security/{num_chain}"
    params = {"contract_addresses": ca}
    session = await init_session()
    try:
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except Exception as e:
        logging.error(f"[GOPLUS] Error fetching EVM security for {ca}: {e}")
        return None

    result_map = (data or {}).get("result") or {}
    tok = result_map.get(ca) or (list(result_map.values())[0] if result_map else None)
    if not tok or not isinstance(tok, dict):
        return None

    is_honeypot = str(tok.get("is_honeypot", "0")) == "1"
    is_open_source = str(tok.get("is_open_source", "1")) == "1"
    is_mintable = str(tok.get("is_mintable", "0")) == "1"
    is_proxy = str(tok.get("is_proxy", "0")) == "1"
    can_take_back_ownership = str(tok.get("can_take_back_ownership", "0")) == "1"
    owner_address = str(tok.get("owner_address") or "")
    is_renounced = owner_address in ("", "0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead")

    try:
        buy_tax = float(tok.get("buy_tax") or 0) * 100
    except Exception:
        buy_tax = 0.0
    try:
        sell_tax = float(tok.get("sell_tax") or 0) * 100
    except Exception:
        sell_tax = 0.0

    holders = tok.get("holders") or []
    real_holders = []
    for h in holders:
        is_contract = int(h.get("is_contract") or 0) == 1
        tag = str(h.get("tag") or "")
        if "uniswap" in tag.lower() or "pancake" in tag.lower() or "pool" in tag.lower() or "burn" in tag.lower() or "dead" in str(h.get("address", "")).lower():
            continue
        try:
            pct = float(h.get("percent") or 0) * 100
        except Exception:
            pct = 0.0
        real_holders.append({
            "address": str(h.get("address") or ""),
            "pct": pct,
            "insider": False,
            "label": tag or ("Contract" if is_contract else "")
        })

    top10_pct = sum(h["pct"] for h in real_holders[:10])
    risks = []
    risk_score = 0

    if is_honeypot:
        risks.append({"name": "Honeypot Detected", "value": "CRITICAL", "level": "danger"})
        risk_score += 80
    if not is_open_source:
        risks.append({"name": "Contract Unverified (Closed Source)", "value": "", "level": "danger"})
        risk_score += 45
    if buy_tax > 10 or sell_tax > 10:
        risks.append({"name": f"High Tax (Buy {buy_tax:.1f}% / Sell {sell_tax:.1f}%)", "value": "", "level": "danger" if sell_tax > 20 else "warn"})
        risk_score += 35
    if is_mintable:
        risks.append({"name": "Mint Function Active", "value": "", "level": "warn"})
        risk_score += 25
    if can_take_back_ownership:
        risks.append({"name": "Can Reclaim Ownership", "value": "", "level": "danger"})
        risk_score += 35
    if is_proxy:
        risks.append({"name": "Upgradeable Proxy Contract", "value": "", "level": "warn"})
        risk_score += 15

    score_norm = min(100, risk_score)
    if is_honeypot or score_norm >= 55:
        verdict = "DANGER"
        verdict_badge = "🔴 DANGER (HIGH RISK)"
    elif score_norm >= 25:
        verdict = "WARN"
        verdict_badge = "🟡 WARNING (MEDIUM RISK)"
    else:
        verdict = "SAFE"
        verdict_badge = "🟢 GOOD (LOW RISK)"

    parsed = {
        "chain": chain_key,
        "source": "GoPlus Security",
        "mint": contract_address,
        "name": tok.get("token_name") or "",
        "symbol": tok.get("token_symbol") or "",
        "verdict": verdict,
        "verdict_badge": verdict_badge,
        "score_normalised": score_norm,
        "is_honeypot": is_honeypot,
        "is_open_source": is_open_source,
        "is_mintable": is_mintable,
        "is_renounced": is_renounced,
        "buy_tax": buy_tax,
        "sell_tax": sell_tax,
        "top10_pct": top10_pct,
        "top_holders": real_holders[:10],
        "total_holders": int(tok.get("holder_count") or 0),
        "risks": risks,
        "url": f"https://gopluslabs.io/token-security/{num_chain}/{contract_address}"
    }

    _prune_cache(RUGCHECK_CACHE, max_size=300, ttl=RUGCHECK_CACHE_TTL)
    RUGCHECK_CACHE[cache_key] = {"data": parsed, "time": now}
    return parsed


async def get_token_security(chain_id: str, token_address: str, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
    """Router otomatis: RugCheck.xyz untuk Solana, GoPlus untuk EVM."""
    c = (chain_id or "").strip().lower()
    if c == "solana":
        return await get_solana_rugcheck(token_address, force_refresh=force_refresh)
    if c in EVM_CHAIN_IDS:
        return await get_evm_goplus_security(c, token_address, force_refresh=force_refresh)
    return None


def format_security_block_compact(sec: Optional[Dict[str, Any]]) -> str:
    """
    Format ringkas untuk disisipkan langsung ke dalam kartu /dex atau sinyal @cekrego.
    """
    if not sec:
        return ""

    chain = sec.get("chain", "solana")
    verdict_badge = sec.get("verdict_badge", "⚪ UNKNOWN")
    score = sec.get("score_normalised", 0)

    if chain == "solana":
        mint_ok = sec.get("mint_authority") is None
        freeze_ok = sec.get("freeze_authority") is None
        lp_locked = float(sec.get("lp_locked_pct") or 0.0)
        top10 = float(sec.get("top10_pct") or 0.0)
        insiders = int(sec.get("insiders_count") or 0)

        mint_str = "Revoked ✅" if mint_ok else "ACTIVE 🚨"
        freeze_str = "Disabled ✅" if freeze_ok else "ACTIVE 🚨"
        lp_str = f"{lp_locked:.1f}% 🔒" if lp_locked >= 80 else (f"{lp_locked:.1f}% ⚠️" if lp_locked > 0 else "Unlocked/Check Pool")
        top10_icon = "🟢" if top10 <= 25 else ("🟡" if top10 <= 45 else "🔴")
        insider_str = f" | 🚨 {insiders} Insiders" if insiders > 0 else ""

        lines = [
            f"🛡️ <b>RUGCHECK AUDIT:</b> {verdict_badge} <code>(Risk: {score}/100)</code>",
            f"├ Mint / Freeze : <code>{mint_str}</code> | <code>{freeze_str}</code>",
            f"├ LP Locked     : <code>{lp_str}</code>",
            f"└ Top 10 Holder : {top10_icon} <code>{top10:.1f}%</code>{insider_str}",
        ]
        risks = sec.get("risks") or []
        if risks:
            top_risk = html.escape(risks[0].get("name", ""))
            lines.append(f"⚠️ <i>Note: {top_risk}</i>")
        return "\n".join(lines) + "\n"

    # EVM Compact Format
    hp = sec.get("is_honeypot", False)
    os_ok = sec.get("is_open_source", True)
    renounced = sec.get("is_renounced", False)
    buy_tax = float(sec.get("buy_tax") or 0.0)
    sell_tax = float(sec.get("sell_tax") or 0.0)
    top10 = float(sec.get("top10_pct") or 0.0)

    hp_str = "HONEYPOT 🚨" if hp else "Safe ✅"
    os_str = "Verified ✅" if os_ok else "Unverified 🚨"
    own_str = "Renounced ✅" if renounced else "Active ⚠️"
    lines = [
        f"🛡️ <b>SECURITY AUDIT:</b> {verdict_badge} <code>(Risk: {score}/100)</code>",
        f"├ Honeypot / Src: <code>{hp_str}</code> | <code>{os_str}</code>",
        f"├ Tax (B / S)   : <code>{buy_tax:.1f}% / {sell_tax:.1f}%</code> | Owner: <code>{own_str}</code>",
        f"└ Top 10 Holder : <code>{top10:.1f}%</code>",
    ]
    return "\n".join(lines) + "\n"


def format_full_rugcheck_card(sec: Dict[str, Any], pair: Optional[Dict[str, Any]] = None) -> str:
    """
    Format laporan lengkap untuk command /rug atau tombol Detail RugCheck.
    """
    chain = sec.get("chain", "solana").upper()
    source = sec.get("source", "RugCheck")
    mint = sec.get("mint", "-")

    base = (pair or {}).get("baseToken") or {}
    name = html.escape(sec.get("name") or base.get("name") or "Unknown Token")
    symbol = html.escape((sec.get("symbol") or base.get("symbol") or "?").upper())
    verdict_badge = sec.get("verdict_badge", "⚪ UNKNOWN")
    score = sec.get("score_normalised", 0)

    lines = [
        f"🛡️ <b>ON-CHAIN SECURITY AUDIT ({chain})</b>",
        f"<b>💎 {name} (${symbol})</b>",
        "━━━━━━━━━━━━━━━━━━━━━",
        f"📊 <b>Status:</b> {verdict_badge}",
        f"🎯 <b>Risk Score:</b> <code>{score} / 100</code> <i>(Makin rendah makin aman)</i>",
    ]

    if sec.get("chain") == "solana":
        mint_ok = sec.get("mint_authority") is None
        freeze_ok = sec.get("freeze_authority") is None
        mut = sec.get("mutable_metadata", False)
        lp_locked = float(sec.get("lp_locked_pct") or 0.0)
        insiders = int(sec.get("insiders_count") or 0)
        creator_pct = float(sec.get("creator_pct") or 0.0)
        total_holders = int(sec.get("total_holders") or 0)

        lines.extend([
            "",
            "🔒 <b>CONTRACT & AUTHORITY:</b>",
            f"├ Mint Authority  : {'🟢 Revoked (Aman)' if mint_ok else '🔴 ACTIVE (Bisa Cetak Token!)'}",
            f"├ Freeze Authority: {'🟢 Disabled (Aman)' if freeze_ok else '🔴 ACTIVE (Bisa Freeze Wallet!)'}",
            f"├ Metadata Status : {'🟢 Immutable' if not mut else '🟡 Mutable (Bisa Diubah)'}",
            f"├ LP Locked/Burned: <code>{lp_locked:.2f}%</code> {'🔒' if lp_locked >= 80 else '⚠️'}",
            f"└ Insider Networks: {'🟢 0 Detected' if insiders == 0 else f'🔴 {insiders} Insiders Terdeteksi!'}",
            "",
            f"👥 <b>HOLDER DISTRIBUTION ({total_holders:,} Holders):</b>",
            f"├ Top 10 Holders (Non-LP): <code>{sec.get('top10_pct', 0):.2f}%</code>",
            f"├ Top 1 Holder (Non-LP)  : <code>{sec.get('top1_pct', 0):.2f}%</code>",
            f"└ Creator Balance        : <code>{creator_pct:.2f}%</code>",
        ])
    else:
        hp = sec.get("is_honeypot", False)
        os_ok = sec.get("is_open_source", True)
        mintable = sec.get("is_mintable", False)
        renounced = sec.get("is_renounced", False)
        buy_tax = float(sec.get("buy_tax") or 0.0)
        sell_tax = float(sec.get("sell_tax") or 0.0)
        total_holders = int(sec.get("total_holders") or 0)

        lines.extend([
            "",
            "🔒 <b>CONTRACT SECURITY:</b>",
            f"├ Honeypot Check : {'🔴 HONEYPOT!' if hp else '🟢 Aman (Bukan Honeypot)'}",
            f"├ Source Code    : {'🟢 Verified (Open Source)' if os_ok else '🔴 Unverified!'}",
            f"├ Ownership      : {'🟢 Renounced' if renounced else '🟡 Active Owner'}",
            f"├ Mintable       : {'🔴 Ya (Bisa Mint)' if mintable else '🟢 Tidak'}",
            f"└ Tax (Buy/Sell) : <code>{buy_tax:.1f}% / {sell_tax:.1f}%</code>",
            "",
            f"👥 <b>HOLDER DISTRIBUTION ({total_holders:,} Holders):</b>",
            f"└ Top 10 Holders (Non-LP): <code>{sec.get('top10_pct', 0):.2f}%</code>",
        ])

    # Top 5 Non-LP Holders list
    holders = sec.get("top_holders") or []
    if holders:
        lines.append("")
        lines.append("🐋 <b>TOP 5 WALLETS (EXCLUDING LP/DEX):</b>")
        for idx, h in enumerate(holders[:5], 1):
            addr = h.get("address") or ""
            short_addr = f"{addr[:4]}...{addr[-4:]}" if len(addr) > 10 else addr
            pct = float(h.get("pct") or 0.0)
            ins_tag = " 🚨[INSIDER]" if h.get("insider") else ""
            lbl = f" ({html.escape(h['label'])})" if h.get("label") else ""
            lines.append(f"  {idx}. <code>{short_addr}</code> — <b>{pct:.2f}%</b>{lbl}{ins_tag}")

    # Risk Warnings
    risks = sec.get("risks") or []
    if risks:
        lines.append("")
        lines.append("⚠️ <b>DETECTED RISK FLAGS:</b>")
        for r in risks[:5]:
            lvl = r.get("level", "warn")
            icon = "🔴" if lvl in ("danger", "critical", "high") else "🟡"
            r_name = html.escape(r.get("name", ""))
            r_val = f" ({html.escape(r['value'])})" if r.get("value") else ""
            lines.append(f"• {icon} {r_name}{r_val}")
    else:
        lines.append("")
        lines.append("✅ <b>Tidak ditemukan flag risiko kritis pada kontrak ini.</b>")

    lines.extend([
        "",
        "📝 <b>Contract Address:</b>",
        f"<code>{html.escape(mint)}</code>",
        f"<i>Source: {html.escape(source)}</i>"
    ])
    return "\n".join(lines)

