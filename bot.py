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

# ========= CONFIG =========
load_dotenv()
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_PROJECT = os.environ.get("OPENAI_PROJECT", "")

ENDPOINT = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-4o-mini"

print(f"[BOT] endpoint={ENDPOINT} model={MODEL} project='{OPENAI_PROJECT}'")

THREADS = {}

SYSTEM_PROMPT = """Ты — Product Data Assistant. Отвечай кратко, по делу, на русском.
Если тема — данные о товарах или таблицы, задавай уточняющие вопросы.
"""


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
        "Привет! Я готов работать с сообщениями и файлами.\n"
        "Команда: /reset — очистить контекст."
    )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    THREADS.pop(update.effective_user.id, None)
    context.user_data.clear()
    await update.message.reply_text("Контекст очищен ✅")


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


# ========== ФИЛЬТРАЦИЯ EXCEL/CSV ==========
_RULE_RE = re.compile(r"^\s*(.+?)\s*(<=|>=|=|!=|<|>|~)\s*(.+?)\s*$", re.IGNORECASE)


def _coerce_series(s: pd.Series) -> pd.Series:
    # дата
    dt = pd.to_datetime(s, errors="coerce", dayfirst=True, infer_datetime_format=True)
    if dt.notna().sum() >= max(2, int(len(s) * 0.2)):
        return dt

    # число
    num = pd.to_numeric(
        s.astype(str).str.replace(" ", "").str.replace(",", "."),
        errors="coerce",
    )
    if num.notna().sum() >= max(2, int(len(s) * 0.2)):
        return num

    # текст
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
            explain.append(f"⚠️ Колонка «{col_raw}» не найдена — пропустил.")
            continue

        col = colmap[key]
        s = _coerce_series(df[col])

        # значение
        val = val_raw
        if pd.api.types.is_datetime64_any_dtype(s):
            val = pd.to_datetime(val_raw, errors="coerce", dayfirst=True)
        elif pd.api.types.is_numeric_dtype(s):
            try:
                val = float(str(val_raw).replace(" ", "").replace(",", "."))
            except:
                val = None
        else:
            val = str(val_raw).lower()

        # оператор
        m = pd.Series(True, index=df.index)
        if op == "=":
            m = s.eq(val)
        elif op == "!=":
            m = s.ne(val)
        elif op == ">":
            m = s.gt(val)
        elif op == "<":
            m = s.lt(val)
        elif op == ">=":
            m = s.ge(val)
        elif op == "<=":
            m = s.le(val)
        elif op == "~":
            m = s.astype(str).str.contains(re.escape(str(val)), na=False)

        mask &= m
        explain.append(f"✅ {col} {op} {val_raw} — прошло {int(m.sum())}")

    df2 = df[mask].copy()
    explain.insert(0, f"Итого прошло {len(df2)} из {len(df)}")
    return df2, "\n".join(explain)


def _to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="filtered")
    buf.seek(0)
    return buf.read()


# ========== ОБРАБОТКА ТЕКСТА ==========
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    # если ждём фильтры для Excel/CSV
    if context.user_data.get("awaiting_filters") and context.user_data.get("pending_file_path"):
        rules_text = update.message.text.strip()
        local_path = context.user_data["pending_file_path"]
        filename = context.user_data.get("pending_file_name", "filtered.xlsx")
        suffix = Path(filename).suffix.lower()

        try:
            if suffix == ".csv":
                df = pd.read_csv(local_path)
            else:
                df = pd.read_excel(local_path)

            rules = _parse_rules(rules_text)
            df2, explanation = _apply_rules(df, rules)

            out_bytes = _to_excel_bytes(df2)
            await update.message.reply_text("Готово ✅\n" + explanation)
            await update.message.reply_document(
                document=out_bytes,
                filename=f"filtered_{Path(filename).stem}.xlsx",
            )
        except Exception as e:
            await update.message.reply_text(f"Ошибка обработки: {e}")
        finally:
            context.user_data["awaiting_filters"] = False
            try:
                shutil.rmtree(Path(local_path).parent, ignore_errors=True)
            except:
                pass

        return

    # обычный чат → OpenAI
    uid = update.effective_user.id
    user_text = update.message.text.strip()
    hist = get_history(uid)

    hist.append(f"user: {user_text}")
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    reply = await call_openai(hist)
    hist.append(f"assistant: {reply}")
    await update.message.reply_text(reply)


# ========= ПРИЁМ ЛЮБЫХ ФАЙЛОВ =========
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.document:
        return

    doc = update.message.document
    filename = _safe_name(doc.file_name or "file.bin", "file.bin")
    suffix = Path(filename).suffix.lower()

    tg_file = await doc.get_file()
    local_path = await _download_to_tmp(tg_file, filename)

    if suffix in {".xlsx", ".xls", ".csv"}:
        # ждём фильтры
        context.user_data["pending_file_path"] = local_path
        context.user_data["pending_file_name"] = filename
        context.user_data["awaiting_filters"] = True

        try:
            if suffix == ".csv":
                df = pd.read_csv(local_path)
            else:
                df = pd.read_excel(local_path)

            cols = ", ".join(map(str, df.columns[:12]))
            await update.message.reply_text(
                f"✅ Файл получил: {filename}\n"
                f"Колонки: {cols}\n\n"
                "Отправь правила фильтрации одним сообщением.\n"
                "Примеры:\n"
                "  Цена<=100000; Регион~киев; Дедлайн>=2025-01-01\n"
                "  Категория=Охрана; Заказчик~Министерство; Аванс=0\n\n"
                "Операторы: =, !=, >, <, >=, <=, ~ (подстрока)"
            )
        except Exception as e:
            await update.message.reply_text(f"Файл получил, но не смог прочитать таблицу ({e}). "
                                            f"Возвращаю обратно.")
            with open(local_path, "rb") as f:
                await update.message.reply_document(document=f, filename=filename)
            shutil.rmtree(Path(local_path).parent, ignore_errors=True)

    else:
        # просто возвращаем файл
        try:
            with open(local_path, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    filename=filename,
                    caption="Файл получил — возвращаю обратно ✅",
                )
        finally:
            shutil.rmtree(Path(local_path).parent, ignore_errors=True)


# ========= ЗАПУСК =========
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    app.run_polling()


if __name__ == "__main__":
    main()
