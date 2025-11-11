import os, re, tempfile, shutil
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters
)
import httpx

# ====== CONFIG & BANNER ======
load_dotenv()
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
OPENAI_PROJECT = os.environ.get("OPENAI_PROJECT", "")

ENDPOINT = "https://api.openai.com/v1/chat/completions"
MODEL = "gpt-4o-mini"

print(f"[BOT v3] endpoint={ENDPOINT} model={MODEL} project='{OPENAI_PROJECT}'")

THREADS: dict[int, list[str]] = {}

SYSTEM_PROMPT = """Ты — Product Data Assistant. Отвечай кратко, по делу, на русском.
Если тема — данные о товарах, задавай уточняющие вопросы.
"""

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

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! Я готов. Можешь писать текст или присылать файлы/медиа.\n"
        "Команда: /reset — очистка контекста."
    )

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    THREADS.pop(uid, None)
    await update.message.reply_text("Контекст очищен. Поехали заново ✨")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    uid = update.effective_user.id
    user_text = update.message.text.strip()
    hist = get_history(uid)
    hist.append(f"user: {user_text}")
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    reply = await call_openai(hist)
    hist.append(f"assistant: {reply}")
    await update.message.reply_text(reply)

# ---------- универсальные помощники для файлов ----------
def _safe_name(name: str, fallback: str) -> str:
    base = name or fallback
    base = Path(base).name
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base)

async def _download_to_tmp(tg_file, filename: str) -> str:
    tmpdir = tempfile.mkdtemp(prefix="tgbot_")
    local_path = os.path.join(tmpdir, filename)
    await tg_file.download_to_drive(local_path)
    return local_path

# --- 1) Документы ---
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.document:
        return
    doc = update.message.document
    filename = _safe_name(doc.file_name or "file.bin", "file.bin")
    tg_file = await doc.get_file()
    local_path = await _download_to_tmp(tg_file, filename)
    try:
        with open(local_path, "rb") as f:
            await update.message.reply_document(
                document=f,
                filename=filename,
                caption=update.message.caption or "Файл получил — возвращаю обратно ✅",
            )
    finally:
        shutil.rmtree(Path(local_path).parent, ignore_errors=True)

# --- 2) Фото ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return
    ph = update.message.photo[-1]  # самое большое
    tg_file = await ph.get_file()
    local_path = await _download_to_tmp(tg_file, "photo.jpg")
    try:
        with open(local_path, "rb") as f:
            await update.message.reply_photo(
                photo=f,
                caption=update.message.caption or "Фото получил — возвращаю ✅",
            )
    finally:
        shutil.rmtree(Path(local_path).parent, ignore_errors=True)

# --- 3) Видео ---
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.video:
        return
    v = update.message.video
    filename = _safe_name(getattr(v, "file_name", None) or "video.mp4", "video.mp4")
    tg_file = await v.get_file()
    local_path = await _download_to_tmp(tg_file, filename)
    try:
        with open(local_path, "rb") as f:
            await update.message.reply_video(
                video=f,
                caption=update.message.caption or "Видео получил — возвращаю ✅",
            )
    finally:
        shutil.rmtree(Path(local_path).parent, ignore_errors=True)

# --- 4) Аудио ---
async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.audio:
        return
    a = update.message.audio
    filename = _safe_name(getattr(a, "file_name", None) or "audio.mp3", "audio.mp3")
    tg_file = await a.get_file()
    local_path = await _download_to_tmp(tg_file, filename)
    try:
        with open(local_path, "rb") as f:
            await update.message.reply_audio(
                audio=f,
                caption=update.message.caption or "Аудио получил — возвращаю ✅",
            )
    finally:
        shutil.rmtree(Path(local_path).parent, ignore_errors=True)

# --- 5) Голосовые ---
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.voice:
        return
    v = update.message.voice
    tg_file = await v.get_file()
    local_path = await _download_to_tmp(tg_file, "voice.ogg")
    try:
        with open(local_path, "rb") as f:
            await update.message.reply_voice(
                voice=f,
                caption="Войс получил — возвращаю ✅",
            )
    finally:
        shutil.rmtree(Path(local_path).parent, ignore_errors=True)

# --- 6) Анимации (GIF/MP4) ---
async def handle_animation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.animation:
        return
    a = update.message.animation
    filename = _safe_name(getattr(a, "file_name", None) or "animation.mp4", "animation.mp4")
    tg_file = await a.get_file()
    local_path = await _download_to_tmp(tg_file, filename)
    try:
        with open(local_path, "rb") as f:
            await update.message.reply_animation(
                animation=f,
                caption=update.message.caption or "Анимацию получил — возвращаю ✅",
            )
    finally:
        shutil.rmtree(Path(local_path).parent, ignore_errors=True)

# --- 7) Видео-заметки ---
async def handle_video_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.video_note:
        return
    vn = update.message.video_note
    tg_file = await vn.get_file()
    local_path = await _download_to_tmp(tg_file, "video_note.mp4")
    try:
        with open(local_path, "rb") as f:
            await update.message.reply_video_note(video_note=f)
    finally:
        shutil.rmtree(Path(local_path).parent, ignore_errors=True)

# --- 8) Стикеры ---
async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.sticker:
        return
    st = update.message.sticker
    tg_file = await st.get_file()
    local_path = await _download_to_tmp(tg_file, "sticker.webp")
    try:
        with open(local_path, "rb") as f:
            await update.message.reply_sticker(f)
    finally:
        shutil.rmtree(Path(local_path).parent, ignore_errors=True)

def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    # команды
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))

    # медиа/файлы
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.AUDIO, handle_audio))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.ANIMATION, handle_animation))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, handle_video_note))
    app.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))

    # текст — в самом конце, чтобы не перехватывал команды/медиа
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling()

if __name__ == "__main__":
    main()
