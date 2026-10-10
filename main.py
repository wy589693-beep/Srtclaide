"""Telegram bot (MTProto via Telethon): send big mp3/mp4 -> get Myanmar .srt (Gemini API).

Long media is converted to small mono mp3, split into chunks, each chunk is
transcribed separately, then timestamps are shifted and merged into one SRT.
"""
import asyncio
import glob
import os
import re
import subprocess
import tempfile
import time
from threading import Thread

import imageio_ffmpeg
from flask import Flask
from google import genai
from telethon import TelegramClient, events

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
MAX_MB = int(os.environ.get("MAX_FILE_MB", "1000"))
CHUNK_SEC = int(os.environ.get("CHUNK_SECONDS", "600"))  # 10 minutes
# Private bot: comma-separated Telegram user IDs allowed to use it, e.g. "123456789,987654321"
ALLOWED = {int(x) for x in re.split(r"[,\s]+", os.environ.get("ALLOWED_USER_IDS", "").strip()) if x.isdigit()}

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
client = genai.Client(api_key=GEMINI_KEY)

PROMPT = """Listen to this audio clip carefully and transcribe all speech into Myanmar (Burmese).
Output ONLY a valid SRT subtitle file, nothing else (no explanations, no markdown code fences).
Rules:
- Number entries from 1.
- Timestamp format: HH:MM:SS,mmm --> HH:MM:SS,mmm
- Timestamps are relative to the start of THIS clip (the clip starts at 00:00:00,000).
- Each entry is one short sentence/phrase (max 2 lines, about 42 characters per line).
- Timestamps must match when words are actually spoken, in order, no overlaps.
- If the speech is another language, translate into natural Myanmar.
- If there is no speech, output nothing.
- Use Myanmar Unicode (not Zawgyi).
"""

# ---------- Flask keep-alive (for Render) ----------
web = Flask(__name__)


@web.route("/")
def home():
    return "Bot is running"


def run_web():
    web.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))


# ---------- SRT helpers ----------
_TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})"
_BLOCK = re.compile(rf"{_TS}\s*-->\s*{_TS}[^\n]*\n(.*?)(?=\n\s*\n|\Z)", re.S)


def _ms(h, m, s, ms):
    return ((int(h or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms.ljust(3, "0"))


def parse_srt(text):
    """Return list of (start_ms, end_ms, text)."""
    text = text.replace("\r\n", "\n")
    out = []
    for m in _BLOCK.finditer(text):
        g = m.groups()
        start, end = _ms(*g[0:4]), _ms(*g[4:8])
        body = g[8].strip()
        if body:
            out.append((start, end, body))
    return out


def fmt_ts(ms):
    ms = max(0, int(ms))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def merge_srt(chunks):
    """chunks: list of (offset_ms, entries). Returns final SRT text."""
    rows = []
    for offset, entries in chunks:
        for start, end, body in entries:
            s, e = start + offset, end + offset
            if e <= s:
                e = s + 1000
            rows.append((s, e, body))
    rows.sort(key=lambda r: r[0])
    lines = []
    for i, (s, e, body) in enumerate(rows, 1):
        lines.append(f"{i}\n{fmt_ts(s)} --> {fmt_ts(e)}\n{body}\n")
    return "\n".join(lines)


# ---------- Media + Gemini ----------
def prepare_chunks(src, workdir):
    pattern = os.path.join(workdir, "chunk_%03d.mp3")
    cmd = [
        FFMPEG, "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
        "-f", "segment", "-segment_time", str(CHUNK_SEC), "-reset_timestamps", "1", pattern,
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg error: " + r.stderr.decode(errors="ignore")[-300:])
    files = sorted(glob.glob(os.path.join(workdir, "chunk_*.mp3")))
    if not files:
        raise RuntimeError("အသံမတွေ့ပါ (no audio found)")
    return files


def transcribe_chunk(path):
    last = None
    for attempt in range(3):
        f = None
        try:
            f = client.files.upload(file=path)
            while f.state.name == "PROCESSING":
                time.sleep(2)
                f = client.files.get(name=f.name)
            if f.state.name != "ACTIVE":
                raise RuntimeError(f"Gemini upload failed: {f.state.name}")
            resp = client.models.generate_content(model=MODEL, contents=[PROMPT, f])
            return resp.text or ""
        except Exception as e:  # retry a couple of times
            last = e
            time.sleep(5 * (attempt + 1))
        finally:
            if f is not None:
                try:
                    client.files.delete(name=f.name)
                except Exception:
                    pass
    raise last


# ---------- Telegram (Telethon) ----------
bot = TelegramClient(None, API_ID, API_HASH)  # in-memory session
sem = asyncio.Semaphore(1)  # one job at a time (small Render instance)


async def check_allowed(event):
    """Only allow-listed users. Everyone else just gets their own ID (private chats only)."""
    if event.sender_id in ALLOWED:
        return True
    if event.is_private:
        await event.reply(f"ဒီ bot က private ဖြစ်ပါတယ်။\nသင့် Telegram ID: {event.sender_id}")
    return False


@bot.on(events.NewMessage(pattern=r"^/start"))
async def start(event):
    if not await check_allowed(event):
        return
    await event.reply(
        "မင်္ဂလာပါ! mp3 သို့မဟုတ် mp4 ဖိုင် ပို့ပေးပါ။ မြန်မာ .srt subtitle ပြန်ပို့ပေးပါမယ်။ "
        f"(ဖိုင်ကြီးလည်း ရပါတယ်၊ {MAX_MB}MB အထိ)"
    )


def _is_media(e):
    f = e.file
    if f is None or e.photo:
        return False
    mime = (f.mime_type or "").lower()
    return mime.startswith("audio/") or mime.startswith("video/")


@bot.on(events.NewMessage(func=_is_media))
async def handle(event):
    if not await check_allowed(event):
        return
    f = event.file
    if f.size and f.size > MAX_MB * 1024 * 1024:
        await event.reply(f"ဖိုင်က {MAX_MB}MB ထက်ကြီးနေပါတယ်။")
        return
    if sem.locked():
        await event.reply("တခြားဖိုင်တစ်ခု လုပ်နေလို့ တန်းစီစောင့်နေပါတယ်...")
    async with sem:
        status = await event.reply("ဖိုင်ကို ဒေါင်းလုဒ်လုပ်နေပါတယ်...")
        base = os.path.splitext(f.name or "subtitle")[0] or "subtitle"
        ext = f.ext or ".mp4"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                src = await event.download_media(file=os.path.join(tmp, "input" + ext))
                await status.edit("အသံကို ပြင်ဆင်နေပါတယ်...")
                chunks = await asyncio.to_thread(prepare_chunks, src, tmp)
                os.remove(src)
                results = []
                for i, ch in enumerate(chunks):
                    await status.edit(f"စာသားပြောင်းနေပါတယ် ({i + 1}/{len(chunks)})...")
                    raw = await asyncio.to_thread(transcribe_chunk, ch)
                    results.append((i * CHUNK_SEC * 1000, parse_srt(raw)))
                srt = merge_srt(results)
                if not srt.strip():
                    await status.edit("စကားပြောသံ မတွေ့ပါ၊ သို့မဟုတ် subtitle မထွက်ပါ။")
                    return
                out = os.path.join(tmp, base + ".srt")
                with open(out, "w", encoding="utf-8") as fh:
                    fh.write(srt)
                await bot.send_file(event.chat_id, out, caption="ပြီးပါပြီ ✅", reply_to=event.id)
            await status.delete()
        except Exception as e:
            await status.edit(f"အမှားတက်သွားပါတယ်: {str(e)[:500]}")


def main():
    Thread(target=run_web, daemon=True).start()
    bot.start(bot_token=BOT_TOKEN)
    print("Bot started")
    bot.run_until_disconnected()


if __name__ == "__main__":
    main()
