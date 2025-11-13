import os
from typing import Dict, List
import traceback

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

# ПРОКСИ ДЛЯ ГАЗПРОМБАНКА (РОССИЙСКИЙ HTTP/HTTPS ПРОКСИ)
# пример: http://user:pass@123.123.123.123:64336
GPB_PROXY_URL = os.environ.get("GPB_PROXY_URL", "").strip()

ENDPOINT = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-4o-mini"

print(f"[BOT] model={MODEL}")
print(f"[BOT] bitrix={BITRIX_WEBHOOK or 'NO BITRIX_WEBHOOK'}")
print(f"[BOT] GPB_PROXY_URL={'SET' if GPB_PROXY_URL else 'NOT SET'}")

THREADS: Dict[int, List[str]] = {}

SYSTEM_PROMPT = """Ты — Product Data Assistant. Отвечай кратко, по делу, на русском."""
# ==========================


# ========= OPENAI CALL =========
async def call_openai(lines: List[str]) -> str:
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


def get_history(uid: int) -> List[str]:
    hist = THREADS.setdefault(uid, [])
    if not hist:
        hist.append(f"system: {SYSTEM_PROMPT}")
    return hist


# ========= БАЗОВЫЕ КОМАНДЫ =========
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я готов работать.\n"
        "Команды:\n"
        "  /tenders — тендеры Газпромбанка (XML API)\n"
        "  /lead Текст лида — создать лид в Битрикс24\n"
        "  лид Текст лида — альтернативный вариант\n"
        "  /reset — очистить контекст\n"
    )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    THREADS.pop(update.effective_user.id, None)
    context.user_data.clear()
    await update.message.reply_text("Контекст очищен ✅")


async def gpb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Бот на связи ✅\nДля получения тендеров используй команду /tenders"
    )


# ========= ТЕНДЕРЫ ГАЗПРОМБАНК (XML) =========
async def fetch_gpb_tenders():
    """
    Тянем XML с тендерами и возвращаем список словарей:
    {"number": ..., "lot": ..., "status": ..., "link": ...}
    """
    # снова идём на https, но уже с follow_redirects=True
    url = "https://etpgaz.gazprombank.ru/api/procedures?late=1"

    proxies = None
    if GPB_PROXY_URL:
        proxies = {
            "http://": GPB_PROXY_URL,
            "https://": GPB_PROXY_URL,
        }

    async with httpx.AsyncClient(
        timeout=30,
        proxies=proxies,
        follow_redirects=True,
    ) as client:
        r = await client.get(url)
        status = r.status_code
        text = r.text

    # Логируем в Railway начало ответа
    print("[GPB_STATUS]", status)
    print("[GPB_BODY_START]", repr(text[:300]))

    if status != 200:
        raise RuntimeError(
            f"API вернул статус {status}. Начало ответа: {text[:200]!r}"
        )

    if not text.strip():
        raise RuntimeError("Пустой ответ от API (через прокси).")

    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise RuntimeError(
            f"Ошибка парсинга XML: {e}. Начало ответа: {text[:200]!r}"
        )

    tenders = []

    for proc in root.findall(".//Procedure"):
        number = (
            proc.findtext("Number")
            or proc.findtext("ProcedureNumber")
            or proc.findtext("Id")
            or "—"
        )
        lot = (
            proc.findtext("LotNumber")
            or proc.findtext("Lot")
            or proc.findtext("LotId")
            or "—"
        )
        status_text = (
            proc.findtext("Status")
            or proc.findtext("State")
            or proc.findtext("ProcedureStatus")
            or "—"
        )

        link = f"https://etpgaz.gazprombank.ru/procedure/{number}"

        tenders.append(
            {
                "number": number,
                "lot": lot,
                "status": status_text,
                "link": link,
            }
        )

    return tenders


async def tenders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Загружаю тендеры Газпромбанка…")

    try:
        items = await fetch_gpb_tenders()
    except Exception as e:
        tb = traceback.format_exc()
        print("[TENDERS_ERROR]", tb)

        msg = f"{type(e).__name__}"
        if str(e):
            msg += f": {e}"

        await update.message.reply_text(f"API ошибка: {msg}")
        return

    if not items:
        await update.message.reply_text("Тендеры не найдены.")
        return

    text = "📄 *Тендеры Газпромбанка*\n\n"

    for t in items[:20]:
        text += (
            f"🔹 *Процедура:* {t['number']}\n"
            f"   *Лот:* {t['lot']}\n"
            f"   *Статус:* {t['status']}\n"
            f"   [Открыть процедуру]({t['link']})\n\n"
        )

    await update.message.reply_markdown(text)


# ========= BITRIX: ЛИД =========
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


# ========= ТЕКСТ / /LEAD =========
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    lower = text.lower()

    # создание лида
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
    app.add_handler(CommandHandler("gpb", gpb_cmd))

    app.add_handler(CommandHandler("tenders", tenders_cmd))
    app.add_handler(CommandHandler("lead", on_text))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling()


if __name__ == "__main__":
    main()
