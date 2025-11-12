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
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_PROJECT = os.environ.get("OPENAI_PROJECT", "")

ENDPOINT = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-4o-mini"

print(f"[BOT] endpoint={ENDPOINT} model={MODEL} project='{OPENAI_PROJECT}'")

THREADS = {}

SYSTEM_PROMPT = """Ты — Product Data Assistant. Отвечай кратко, по делу, на русском.
Если тема — таблицы и тендеры, помогай чётко и структурировано.
"""

# ========= OPENAI CALL (для обычного чата) =========
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

# ========= FS UTILS =========
def _safe_name(name: str, fallback: str) -> str:
    base = name or fallback
    base = Path(base).name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base)

async def _download_to_tmp(tg_file, filename: str) -> str:
    tmpdir = tempfile.mkdtemp(prefix="tgbot_")
    local_path = os.path.join(tmpdir, filename)
    await tg_file.download_to_drive(local_path)
    return local_path

def _to_excel_bytes(df: pd.DataFrame, sheet="MCE_scored") -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name=sheet)
    buf.seek(0)
    return buf.read()

# ========= АЛИАСЫ КОЛОНОК =========
NAME_ALIASES = [
    "название","название сделки","название лида","наименование","предмет","тема","лот"
]
COMPANY_ALIASES = [
    "компания","заказчик","покупатель","организация","клиент","контрагент","инициатор"
]
AMOUNT_ALIASES = [
    "сумма","бюджет","нмцк","начальная цена","стоимость","price","amount","total","макс цена"
]

def _find_col(df: pd.DataFrame, aliases) -> str | None:
    cols_map = {str(c).strip().lower(): c for c in df.columns}
    for a in aliases:
        if a in cols_map:
            return cols_map[a]
    for key, c in cols_map.items():
        if any(a in key for a in aliases):
            return c
    return None

# ========= СЕМАНТИКА (расширено по твоему docx) =========
KEYWORDS = [
    # учёт/измерение/узлы
    "сикг","сикн","сикнс","сикк","уирг","ууг","уун","асн","асу тп","асутп",
    "узел учета","узел учёта","узел измерения","узел редуцирования",
    "измеритель","измерительная установка","система измерен","система контроля",
    # агзу и измерительные установки
    "агзу","измерительная установка",
    # налив/слив/эстакады
    "система налива","пункт налива","станция налива","система слива","пункт слива","эстакада",
    "налив нефти","слив нефти","налив метанола","герметичный налив",
    # дозирование реагентов
    "установка дозирования","дозирован","узел ввода реагентов","удх","удхб",
    # блочное/модульное
    "блок-бокс","блочный","модульный блок","технологический блок","блочно-модульная",
    # пробоотбор/аналитика/лаборатории
    "пробоотбор","пробоотборник","сог","хал","лаборатор","химико-аналитичес","газоаналитичес",
    "анализатор","хроматограф","метрологический стенд","поверочный стенд","испытательный стенд",
    # кип/приборка/датчики/уровень/расход/давление
    "кип","контрольно-измерительн","датчик","датчики","манометр","уровнемер","расходомер",
    "термометр","термопара","тсп","ртд","манифольд","диафрагма",
    # смежные термины
    "пнр","шмр","пир","модернизация","проектирование","комплекс поставки"
]

CLIENTS = [
    # группы заказчиков
    "газпром","газпромнефть","лукойл","роснефть","славнефть","самаранефтегаз","няганьнефть",
    "восток-оил","инк","ннк","ритэк","башнефть","метафракс","еврохим","русснефть",
    "томскнефть","мессояханефтегаз","русгаз","бск","козс","татнефть"
]

LEAD_PATTERNS = [
    r"^ткп", r"^ап", r"^com", "запрос", "поставка",
    "система","узел","модернизация","проектирование","комплекс","блок","асутп","асу тп"
]

# ========= СКОРИНГ =========
def _any_in(text: str, bag) -> bool:
    t = str(text).lower()
    return any(k in t for k in bag)

def _any_re(text: str, patterns) -> bool:
    t = str(text)
    return any(re.search(p, t, re.IGNORECASE) for p in patterns)

def _score_keywords(s: str) -> int:   # 0–4
    t = str(s).lower()
    hits = sum(k in t for k in KEYWORDS)
    if hits >= 4: return 4
    if hits == 3: return 3
    if hits == 2: return 2
    if hits == 1: return 1
    return 0

def _score_client(s: str) -> int:     # 0–3
    return 3 if _any_in(s, CLIENTS) else 0

def _score_amount(a: float) -> int:   # 0–2
    try:
        a = float(a)
    except:
        a = 0.0
    if a >= 300_000_000: return 2
    if a >= 50_000_000:  return 1
    return 0

def _score_pattern(s: str) -> int:    # 0–1
    return 1 if _any_re(s, LEAD_PATTERNS) else 0

def _priority(total: int) -> str:
    if total >= 7: return "High"
    if total >= 4: return "Medium"
    return "Low"

def _reason(row) -> str:
    parts = []
    if row["Score_keywords(0-4)"] > 0: parts.append("направления/ключевые слова")
    if row["Score_client(0-3)"] > 0:   parts.append("целевой заказчик")
    if row["Score_amount(0-2)"] > 0:   parts.append("масштаб сделки")
    if row["Score_pattern(0-1)"] > 0:  parts.append("тендерный шаблон")
    return ", ".join(parts) if parts else "значимых совпадений нет"

def mce_filter_scored(df_in: pd.DataFrame) -> pd.DataFrame:
    df = df_in.copy().fillna("")
    # авто-поиск колонок
    name_col    = _find_col(df, NAME_ALIASES)    or "Название"
    company_col = _find_col(df, COMPANY_ALIASES) or "Компания"
    amount_col  = _find_col(df, AMOUNT_ALIASES)  or "Сумма"

    if name_col not in df.columns:    df[name_col] = ""
    if company_col not in df.columns: df[company_col] = ""
    if amount_col not in df.columns:  df[amount_col] = 0

    df[name_col]    = df[name_col].astype(str)
    df[company_col] = df[company_col].astype(str)
    # оставляем строковые суммы как есть — ниже coerce для масок
    # скоринг
    kw  = df[name_col].apply(_score_keywords)
    cl  = df[company_col].apply(_score_client)
    amt = pd.to_numeric(df[amount_col], errors="coerce").fillna(0).apply(_score_amount)
    pat = df[name_col].apply(_score_pattern)

    df["Score_keywords(0-4)"] = kw
    df["Score_client(0-3)"]   = cl
    df["Score_amount(0-2)"]   = amt
    df["Score_pattern(0-1)"]  = pat
    df["Score_total(0-10)"]   = df[[
        "Score_keywords(0-4)","Score_client(0-3)","Score_amount(0-2)","Score_pattern(0-1)"
    ]].sum(axis=1)

    # базовый смысловой проход
    base_mask = (kw >= 1) | (cl >= 1) | (pat >= 1)
    # сумма: >=1 млн, но допускаем 0 если сильно наши по предмету+клиенту
    amounts = pd.to_numeric(df[amount_col], errors="coerce").fillna(0)
    sum_mask = (amounts >= 1_000_000) | ((kw >= 2) & (cl >= 1))

    out = df[base_mask & sum_mask].copy()
    out["Priority"] = out["Score_total(0-10)"].apply(_priority)
    out["Причина"] = out.apply(_reason, axis=1)

    # сортировка: High → Medium → Low, затем по Score, затем по сумме
    prio_order = {"High":0, "Medium":1, "Low":2}
    out = out.sort_values(
        by=["Priority","Score_total(0-10)", amount_col],
        key=lambda s: s.map(prio_order) if s.name=="Priority" else pd.to_numeric(s, errors="coerce"),
        ascending=[True, False, False]
    )
    return out

# ========= КОМАНДЫ =========
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Кидай Excel/CSV — я отфильтрую тендеры, выставлю приоритеты и объясню, почему выбрал.\n"
        "Команда: /reset — очистить контекст."
    )

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    THREADS.pop(update.effective_user.id, None)
    context.user_data.clear()
    await update.message.reply_text("Контекст очищен ✅")

# ========= ТЕКСТ (чат с LLM) =========
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    uid = update.effective_user.id
    user_text = update.message.text.strip()
    hist = get_history(uid)

    hist.append(f"user: {user_text}")
    await context.bot.send_chat_action(update.effective_chat.id, "typing")
    reply = await call_openai(hist)
    hist.append(f"assistant: {reply}")
    await update.message.reply_text(reply)

# ========= ПРИЁМ ФАЙЛОВ =========
def _read_table_any(local_path: str, suffix: str) -> pd.DataFrame:
    if suffix == ".csv":
        # пробуем utf-8 → cp1251
        try:
            return pd.read_csv(local_path, sep=None, engine="python")
        except Exception:
            return pd.read_csv(local_path, sep=None, engine="python", encoding="cp1251", on_bad_lines="skip")
    else:
        try:
            return pd.read_excel(local_path)
        except Exception:
            return pd.read_excel(local_path, engine="openpyxl")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.document:
        return

    doc = update.message.document
    filename = _safe_name(doc.file_name or "file.bin", "file.bin")
    suffix = Path(filename).suffix.lower()

    tg_file = await doc.get_file()
    local_path = await _download_to_tmp(tg_file, filename)

    if suffix in {".xlsx", ".xls", ".csv"}:
        try:
            df = _read_table_any(local_path, suffix)
            scored = mce_filter_scored(df)

            total_in  = len(df)
            total_out = len(scored)
            by_prio = scored["Priority"].value_counts().to_dict()
            high = by_prio.get("High", 0)
            med  = by_prio.get("Medium", 0)
            low  = by_prio.get("Low", 0)

            # превью 5 строк с причинами
            name_col = _find_col(scored, NAME_ALIASES) or "Название"
            preview_rows = []
            for _, row in scored.head(5).iterrows():
                title = str(row.get(name_col, ""))[:140]
                prio  = row["Priority"]
                sc    = row["Score_total(0-10)"]
                why   = row["Причина"]
                preview_rows.append(f"• [{prio} | {sc}] {title}\n   — {why}")

            out_bytes = _to_excel_bytes(scored, sheet="MCE_scored")

            await update.message.reply_text(
                "Готово ✅\n"
                f"Вход: {total_in} строк • Отобрано: {total_out}\n"
                f"Приоритеты — High: {high}, Medium: {med}, Low: {low}\n\n"
                + ("\n".join(preview_rows) if preview_rows else "Совпадений с нашими направлениями не найдено")
            )
            await update.message.reply_document(
                document=out_bytes,
                filename=f"MCE_filtered_scored_{Path(filename).stem}.xlsx",
            )

        except Exception as e:
            await update.message.reply_text(f"Ошибка обработки таблицы: {e}")
        finally:
            shutil.rmtree(Path(local_path).parent, ignore_errors=True)
        return

    # Прочие файлы — эхо
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
