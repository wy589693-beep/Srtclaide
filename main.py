"""Telegram bot: send mp3/mp4 -> get Myanmar .srt subtitles (Gemini API)."""
import asyncio
import os
import re
import tempfile
import time
from threading import Thread

from flask import Flask
from google import genai
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, CommandHandler, filters

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
MAX_BYTES = 20 * 1024 * 1024  # Telegram Bot API download limit

client = genai.Client(api_key=GEMINI_KEY)

PROMPT = """Listen to this media carefully and transcribe all speech into Myanmar (Burmese).
Output ONLY a valid SRT subtitle file, nothing else (no explanations, no markdown code fences).
Rules:
- Number entries from 1.
- Timestamp format: HH:MM:SS,mmm --> HH:MM:SS,mmm
- Each entry is one short sentence/phrase (max 2 lines, about 42 characters per line).
- Timestamps must match when words are actually spoken, in order, no overlaps.
- If the speech is another language, translate into natural Myanmar.
- Use Myanmar Unicode (not Zawgyi).
"""

# ---------- Flask keep-alive (for Render) ----------
web = Flask(__name__)


@web.route("/")
def home():
    return "Bot is running"


def run_web():
    web.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))


# ---------- Gemini ----------
def make_srt(path: str) -> str:
    f = client.files.upload(file=path)
    try:
        while f.state.name == "PROCESSING":
            time.sleep(2)
            f = client.files.get(name=f.name)
        if f.state.name != "ACTIVE":
            raise RuntimeError(f"Gemini upload failed: {f.state.name}")
        resp = client.models.generate_content(model=MODEL, contents=[PROMPT, f])
        text = resp.text.strip()
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
        return text.strip() + "\n"
    finally:
        try:
            client.files.delete(name=f.name)
        except Exception:
            pass


# ---------- Telegram handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "မင်္ဂလာပါ! mp3 သို့မဟုတ် mp4 ဖိုင် ပို့ပေးပါ။ မြန်မာ .srt subtitle ပြန်ပို့ပေးပါမယ်။ (၂၀MB အောက်)"
    )


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = update.message
    media = m.audio or m.video or m.voice or m.document
    if media is None:
        return
    if media.file_size and media.file_size > MAX_BYTES:
        await m.reply_text("ဖိုင်က ၂၀MB ထက်ကြီးနေပါတယ်။ ဖိုင်သေးအောင် ချုံ့ပြီး ပြန်ပို့ပါ။")
        return

    name = getattr(media, "file_name", None) or "media"
    base = os.path.splitext(name)[0] or "subtitle"
    ext = os.path.splitext(name)[1] or (".ogg" if m.voice else ".mp4" if m.video else ".mp3")

    status = await m.reply_text("လုပ်ဆောင်နေပါတယ်၊ ခဏစောင့်ပါ...")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "input" + ext)
        tg_file = await context.bot.get_file(media.file_id)
        await tg_file.download_to_drive(src)
        try:
            srt = await asyncio.to_thread(make_srt, src)
        except Exception as e:
            await status.edit_text(f"အမှားတက်သွားပါတယ်: {e}")
            return
        out = os.path.join(tmp, base + ".srt")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(srt)
        with open(out, "rb") as fh:
            await m.reply_document(fh, filename=base + ".srt", caption="ပြီးပါပြီ ✅")
    await status.delete()


def main():
    Thread(target=run_web, daemon=True).start()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        filters.AUDIO | filters.VIDEO | filters.VOICE | filters.Document.ALL, handle_media))
    app.run_polling()


if __name__ == "__main__":
    main()
