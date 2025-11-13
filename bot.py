import os, re, io, tempfile, shutil
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    CommandHandler,
    ContextTypes,
    filters,
)

import httpx
import json

# ========= CONFIG =========
load_dotenv()

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_PROJECT = os.environ.get("OPENAI_PROJECT", "")
BITRIX_WEBHOOK = os.environ.get("BITRIX_WEBHOOK", "").rstrip("/")

ENDPOINT = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-4o-mini"

print(f"[BOT] model={MODEL}")

THREADS = {}

SYSTEM_PROMPT = """Ты — Product Data Assistant. Отвечай кратко и по делу."""


# ========= OPENAI CALL =========
async def call_openai(lines: list[str]) -> str:
    msgs = []
    for ln in lines:
        if ": " in ln:
            role, content = ln.split(": ", 1)
            if role in ("system", "user", "assistant"):
                msgs.append({"role": role, "content": content})

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    if OPENAI_PROJECT:
        headers["OpenAI-Project"] = OPENAI_PROJECT

    payload = {"model": MODEL, "messages": msgs, "temperature": 0.2}

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(ENDPOINT, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
            return data["choices"][0]["message"]["content"].strip()

    except Exception as e:
        return f"⚠️ Ошибка OpenAI: {e}"


def get_history(uid: int) -> list[str]:
    hist = THREADS.setdefault(uid, [])
    if not hist:
        hist.append(f"system: {SYSTEM_PROMPT}")
    return hist


# ========= BITRIX24 — СОЗДАНИЕ ЛИДА =========

async def send_lead_to_bitrix(title: str, description: str, phone: str = "", name: str = ""):
    if not BITRIX_WEBHOOK:
        return "❌ Переменная BITRIX_WEBHOOK не установлена!"

    url = f"{BITRIX_WEBHOOK}/crm.lead.add.json"

    payload = {
        "fields": {
            "TITLE": title,
            "NAME": name if name else "Telegram",
            "COMMENTS": description,
            "PHONE": [{"VALUE": phone, "VALUE_TYPE": "WORK"}] if phone else [],
        },
        "params": {"REGISTER_SONET_EVENT": "Y"}
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()

        if "result" in data:
            return f"✅ Лид создан: ID {data['result']}"
        else:
            return f"⚠️ Ошибка Bitrix: {data}"

    except Exception as e:
        return f"⚠️ Bitrix ошибка: {e}"


# ========= ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =========

def _safe_name(name: str, fallback: str) -> str:
    base = name or fallback
    base = Path(base).name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base)


async def _download_to_tmp(tg_file, filename: str) -> str:
    tmpdir = tempfile.mkdtemp(prefix="tgbot_")
    local_path = os.path.join(tmpdir, filename)
    await tg_file.download_to_drive(local_path)
    return local_path


# ========== EXCEL/CSV ФИЛЬТРАЦИЯ ==========

_RULE_RE = re.compile(r"^\s*(.+?)\s*(<=|>=|=|!=|<|>|~)\s*(.+?)\s*$", re.IGNORECASE)

def _coerce_series(s: pd.Series) -> pd.Series:
    dt = pd.to_datetime(s, errors="ignore", dayfirst=True)
    if dt.notna().sum() >= len(s) * 0.3:
        return dt
    num = pd.to_numeric(s.astype(str).str.replace(" ", "").str.replace(",", "."), errors="ignore")
    if pd.to_numeric(num, errors="coerce").notna().sum() >= len(s) * 0.3:
        return num
    return s.astype(str).str.lower()


def _parse_rules(text: str):
    parts = [p for p in re.split(r"[;\n]+", text) if p.strip()]
    rules = []
    for p in parts:
        m = _RULE_RE.match(p)
        if m:
            rules.append((m.group(1).strip(), m.group(2), m.group(3).strip()))
    return rules


def _apply_rules(df: pd.DataFrame, rules):
    if df.empty or not rules:
        return df, "Правил нет — ничего не фильтровал."

    explain = []
    mask = pd.Series(True, index=df.index)
    colmap = {str(c).strip().lower(): c for c in df.columns}

    for col_raw, op, val_raw in rules:
        key = col_raw.lower()
        if key not in colmap:
            explain.append(f"⚠️ Колонка «{col_raw}» не найдена.")
            continue

        col = colmap[key]
        s = _coerce_series(df[col])

        val = val_raw
        if pd.api.types.is_numeric_dtype(s):
            try: val = float(val_raw)
            except: pass
        elif pd.api.types.is_datetime64_any_dtype(s):
            val = pd.to_datetime(val_raw, errors="coerce")

        m = pd.Series(True, index=df.index)
        if op == "=":   m = s == val
        if op == "!=":  m = s != val
        if op == ">":   m = s > val
        if op == "<":   m = s < val
        if op == ">=":  m = s >= val
        if op == "<=":  m = s <= val
        if op == "~":   m = s.astype(str).str.contains(str(val_raw).lower(), na=False)

        mask &= m
        explain.append(f"{col}: {op} {val_raw} — прошло {m.sum()}")

    df2 = df[mask].copy()
    explain.insert(0, f"Итого прошло {len(df2)} из {len(df)}")
    return df2, "\n".join(explain)


def _to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="filtered")
    buf.seek(0)
    return buf.read()


# ========= ОБРАБОТКА ТЕКСТА =========

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    uid = update.effective_user.id
    text = update.message.text.strip()

    # ******* СОЗДАНИЕ ЛИДА ********
    if text.lower().startswith("лид "):
        title = "Лид из Telegram"
        desc = text[4:].strip()
        ans = await send_lead_to_bitrix(title, desc)
        await update.message.reply_text(ans)
        return

    # ******* OPENAI ЧАТ ********
    hist = get_history(uid)
    hist.append(f"user: {text}")

    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    reply = await call_openai(hist)
    hist.append(f"assistant: {reply}")

    await update.message.reply_text(reply)


# ========= ПРИЁМ ФАЙЛОВ =========

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    filename = _safe_name(doc.file_name, "file.bin")
    suffix = Path(filename).suffix.lower()

    tg_file = await doc.get_file()
    local_path = await _download_to_tmp(tg_file, filename)

    # EXCEL/CSV
    if suffix in {".xlsx", ".xls", ".csv"}:
        try:
            df = pd.read_excel(local_path) if suffix != ".csv" else pd.read_csv(local_path)
        except Exception as e:
            await update.message.reply_text(f"Ошибка чтения файла: {e}")
            return

        cols = ", ".join(map(str, df.columns))
        context.user_data["pending_file"] = local_path
        await update.message.reply_text(
            f"Файл получен: {filename}\n"
            f"Колонки: {cols}\n\n"
            f"Отправь правила фильтрации. Пример:\n"
            f"Название~насос; Сумма>1000000\n"
        )
        context.user_data["awaiting_filters"] = True
        return

    # Обычный файл → просто вернуть обратно
    with open(local_path, "rb") as f:
        await update.message.reply_document(f, filename=filename)


# ========= ПРИМЕНЕНИЕ ФИЛЬТРОВ =========

async def apply_filters(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_filters"):
        return

    rules_text = update.message.text.strip()
    local_path = context.user_data["pending_file"]
    suffix = Path(local_path).suffix.lower()

    df = pd.read_excel(local_path) if suffix != ".csv" else pd.read_csv(local_path)

    rules = _parse_rules(rules_text)
    df2, explanation = _apply_rules(df, rules)

    out_bytes = _to_excel_bytes(df2)
    await update.message.reply_text(explanation)
    await update.message.reply_document(out_bytes, filename="filtered.xlsx")

    context.user_data["awaiting_filters"] = False


# ========= КОМАНДЫ =========

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Бот готов. Чтобы создать лид — напиши:\n\nлид текст лида")


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    THREADS.pop(update.effective_user.id, None)
    context.user_data.clear()
    await update.message.reply_text("Контекст очищен.")


# ========= ЗАПУСК =========

def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))

    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, apply_filters))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling()


if __name__ == "__main__":
    main()
