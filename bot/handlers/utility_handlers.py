# bot/handlers/utility_handlers.py
import re
import ast
import operator
import logging
from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import Command

from bot.utils import escape_md, parse_time_utc_to_wib_label, is_rate_limited

router = Router()

_MATH_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

def safe_math_eval(expr: str):
    clean_expr = expr.strip().replace("x", "*").replace("X", "*").replace("÷", "/").replace(":", "/")
    if len(clean_expr) > 80:
        raise ValueError("Ekspresi terlalu panjang")
    parsed = ast.parse(clean_expr, mode='eval')
    
    def _eval(node):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise TypeError("Hanya angka yang diperbolehkan")
        elif isinstance(node, ast.BinOp):
            left = _eval(node.left)
            right = _eval(node.right)
            op = type(node.op)
            if op in _MATH_OPERATORS:
                if op == ast.Pow and (isinstance(right, (int, float)) and right > 100):
                    raise ValueError("Pangkat terlalu besar")
                return _MATH_OPERATORS[op](left, right)
            raise ValueError("Operator tidak didukung")
        elif isinstance(node, ast.UnaryOp):
            operand = _eval(node.operand)
            op = type(node.op)
            if op in _MATH_OPERATORS:
                return _MATH_OPERATORS[op](operand)
            raise ValueError("Unary operator tidak didukung")
        raise TypeError("Sintaks tidak diizinkan")

    return _eval(parsed.body)

RE_TIME_PATTERN = re.compile(r"^\s*\d{1,2}:\d{2}\s*$")
RE_DATE_PATTERN = re.compile(r"^\s*(\d{1,4}[/-]\d{1,2}[/-]\d{1,4}|\d{1,2}[/-]\d{1,2})\s*$")

# --- CONVERT UTC KE WIB OTOMATIS ---
@router.message(F.text.regexp(r"^\s*(\d{1,2}):(\d{2})\s*[uU][tT][cC]\s*$"))
async def handle_utc_to_wib_convert(message: Message):
    try:
        m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*[uU][tT][cC]\s*$", message.text or "")
        if not m:
            return
        hh = int(m.group(1))
        mm = int(m.group(2))
        if hh < 0 or hh > 23 or mm < 0 or mm > 59:
            return

        wib, note = parse_time_utc_to_wib_label(hh, mm)
        suffix = f" {note}" if note else ""
        await message.reply(f"🕒 {hh:02d}:{mm:02d} UTC = {wib} WIB{suffix}", parse_mode=None)
    except Exception as e:
        logging.error(f"UTC->WIB Convert Error: {e}")

# --- 0. 🧮 FITUR KALKULATOR (COMMAND & TEXT) ---
@router.message(Command("calc", "kalkulator"))
async def cmd_calc(message: Message):
    limited, remain = is_rate_limited(message.from_user.id, "calc")
    if limited:
        return

    content = (message.text or "").strip()
    parts = content.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("`Usage: /calc 100 * 25 / 4`", parse_mode="MarkdownV2")
        return
    expr = parts[1].strip()
    try:
        result = safe_math_eval(expr)
        if isinstance(result, float):
            res_str = f"{int(result):,}" if result.is_integer() else f"{result:,.4f}".rstrip('0').rstrip('.')
        else:
            res_str = f"{result:,}"
        resp = (
            f"```\n"
            f"🧮 Kalkulator\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"{expr} = {res_str}\n"
            f"```"
        )
        await message.reply(resp, parse_mode="MarkdownV2")
    except ZeroDivisionError:
        await message.reply("❌ _Gak bisa dibagi nol atuh bro\\!_", parse_mode="MarkdownV2")
    except Exception as e:
        await message.reply(f"❌ _Ekspresi tidak valid\\: {escape_md(str(e))}_", parse_mode="MarkdownV2")

@router.message(F.text.regexp(r"^\s*\d+(\.\d+)?(\s*[\+\-\*/:]\s*\d+(\.\d+)?)+\s*$"))
async def handle_calculator(message: Message):
    text = (message.text or "").strip()
    # Filter false positive jam (HH:MM) dan tanggal
    if RE_TIME_PATTERN.match(text) or RE_DATE_PATTERN.match(text):
        return

    # Di grup chat, abaikan jika menggunakan ':' tanpa spasi (seringkali skor / ayat / rasio)
    is_group = message.chat.type in ("group", "supergroup")
    if is_group and ":" in text and " : " not in text:
        return

    try:
        result = safe_math_eval(text)
        if isinstance(result, float):
            res_str = f"{int(result):,}" if result.is_integer() else f"{result:,.4f}".rstrip('0').rstrip('.')
        else:
            res_str = f"{result:,}"

        resp = (
            f"```\n"
            f"🧮 Kalkulator\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"{message.text} | Hasil: {res_str}\n"
            f"```"
        )
        await message.reply(resp, parse_mode="MarkdownV2")
    except ZeroDivisionError:
        await message.reply("❌ _Gak bisa dibagi nol atuh bro\\!_", parse_mode="MarkdownV2")
    except Exception:
        pass
