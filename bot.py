import os
import re
from pathlib import Path

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
import xml.etree.ElementTree as ET


# ========= CONFIG =========
load_dotenv()

OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_PROJECT = os.environ.get("OPENAI_PROJECT", "")

# Bitrix
BITRIX_WEBHOOK = os.environ.get("BITRIX_WEBHOOK", "").rstrip("/")
BITRIX_METHOD_LEAD_ADD = "crm.lead.add.json"

ENDPOINT = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-4o-mini"

print(f"[BOT] model={MODEL}")
print(f"[BOT] bitrix={BITRIX_WEBHOOK or 'NO BITRIX_WEBHOOK'}")

THREADS: dict[int, list[str]] = {}

SYSTEM_PROMPT = """Ты — Product Data Assistant. Отвечай кратко, по делу, на русском."""


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

    except httpx.HTTPStatusError as e:
        return f"⚠️ OpenAI {e.response.status_code}: {e.response.text}"
    except Exception as e:
        return f"⚠️ Локальная ошибка: {e}"


def get_history(uid: int) -> list[str]:
    hist = THREADS.setdefault(uid, [])
    if not hist:
        hist.append(f"system: {SYSTEM_PROMPT}")
    return hist


# ========= БАЗОВЫЕ КОМАНДЫ =========
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я готов работать.\n"
        "Команды:\n"
        "  /tenders — тендеры Газпромбанк XML\n"
        "  /lead текст — создать лид в Битрикс24\n"
        "  /reset — очистить контекст"
    )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    THREADS.pop(update.effective_user.id, None)
    context.user_data.clear()
    await update.message.reply_text("Контекст очищен ✅")


async def gpb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Команда /gpb работает.")


# ========= 🚀 НОВОЕ! ЗАГРУЗКА XML ТЕНДЕРОВ =========
async def fetch_gpb_tenders():
    url = "https://etpgaz.gazprombank.ru/api/procedures?late=1"

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(url)
        r.raise_for_status()
        xml_text = r.text

    root = ET.fromstring(xml_text)

    tenders = []

    for proc in root.findall(".//Procedure"):
        number = proc.findtext("Number", "—")
        lot = proc.findtext("LotNumber", "—")
        status = proc.findtext("Status", "—")

        link = f"https://etpgaz.gazprombank.ru/Procedure/{number}"

        tenders.append({
            "number": number,
            "lot": lot,
            "status": status,
            "link": link
        })

    return tenders


# ========= 🚀 КОМАНДА /tenders =========
async def tenders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Загружаю XML тендеры…")

    try:
        items = await fetch_gpb_tenders()

        if not items:
            await update.message.reply_text("❌ Нет данных.")
            return

        text = "📄 *Тендеры Газпромбанк (последние)*\n\n"

        for t in items[:20]:
            text += (
                f"🔹 *Процедура:* {t['number']}\n"
                f"   *Лот:* {t['lot']}\n"
                f"   *Статус:* {t['status']}\n"
                f"   [Открыть]({t['link']})\n\n"
            )

        await update.message.reply_markdown(text)

    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


# ========= BITRIX =========
async def create_bitrix_lead(title: str, comment: str, tg_user) -> str:

    if not BITRIX_WEBHOOK:
        return "❌ BITRIX_WEBHOOK не задан."

    url = f"{BITRIX_WEBHOOK}/{BITRIX_METHOD_LEAD_ADD}"

    fields = {
        "TITLE": title,
        "COMMENTS": comment,
        "SOURCE_ID": "WEB",
        "STATUS_ID": "NEW",
        "NAME": tg_user.first_name or "",
        "LAST_NAME": tg_user.last_name or "",
    }

    payload = {"fields": fields, "params": {"REGISTER_SONET_EVENT": "Y"}}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()

        if "error" in data:
            return f"❌ Bitrix: {data.get('error_description', data['error'])}"

        return f"Лид создан в Б24 ✅ (ID: {data.get('result')})"

    except Exception as e:
        return f"❌ Ошибка Bitrix: {e}"


# ========= ТЕКСТ =========
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()

    # создание лида
    lower = text.lower()
    if lower.startswith("лид ") or lower.startswith("lead ") or text.startswith("/lead"):
        parts = text.split(maxsplit=1)
        title = parts[1].strip() if len(parts) > 1 else "Лид из Telegram"

        comment = (
            f"Сообщение из Telegram: {text}\n\n"
            f"Username: @{update.effective_user.username or ''}"
        )

        await update.message.reply_text("Создаю лид в Битрикс24…")
        result_msg = await create_bitrix_lead(
            title=title,
            comment=comment,
            tg_user=update.effective_user,
        )
        await update.message.reply_text(result_msg)
        return

    # обычный чат → OpenAI
    uid = update.effective_user.id
    hist = get_history(uid)

    hist.append(f"user: {text}")
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    reply = await call_openai(hist)
    hist.append(f"assistant: {reply}")
    await update.message.reply_text(reply)


# ========= MAIN =========
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("lead", on_text))
    app.add_handler(CommandHandler("gpb", gpb_cmd))
    app.add_handler(CommandHandler("tenders", tenders_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling()


if __name__ == "__main__":
    main()
