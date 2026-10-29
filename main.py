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
CHUNK_SEC = int(os.environ.get("CHUNK_SECONDS", "600"))  # minimum chunk length (10 min)
MAX_CHUNK_SEC = int(os.environ.get("MAX_CHUNK_SECONDS", "1500"))  # chunks grow up to 25 min on very long files
REQ_BUDGET = int(os.environ.get("REQUEST_BUDGET", "18"))  # Gemini requests we allow ourselves per file (free tier is ~20/day/model)
MODELS = [m.strip() for m in os.environ.get("GEMINI_MODELS", MODEL).split(",") if m.strip()] or [MODEL]
PARALLEL = int(os.environ.get("GEMINI_PARALLEL", "3"))     # chunks transcribed at the same time
MAX_CUE = int(os.environ.get("MAX_CUE_CHARS", "36"))    # max visible characters per subtitle
LINE_LEN = int(os.environ.get("LINE_CHARS", "22"))      # wrap to a 2nd line after this many
SHIFT_MS = int(os.environ.get("SUB_DELAY_MS", "0"))     # +/- manual timing shift (ms), e.g. -300
CONTEXT = os.environ.get("VIDEO_CONTEXT", "").strip()   # optional hint, e.g. "anime recap, narrator voice"
# Private bot: comma-separated Telegram user IDs allowed to use it, e.g. "123456789,987654321"
ALLOWED = {int(x) for x in re.split(r"[,\s]+", os.environ.get("ALLOWED_USER_IDS", "").strip()) if x.isdigit()}

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
client = genai.Client(api_key=GEMINI_KEY)

PROMPT = """You are a professional Burmese subtitle translator and timer.
Listen to this audio clip and write Myanmar (Burmese) subtitles for everything that is spoken.
Output ONLY a valid SRT file (no explanations, no markdown fences).

TRANSLATION QUALITY (most important):
- Translate the MEANING, not word by word. Write the way a native Burmese speaker would naturally say it
  in everyday spoken Burmese (conversational style, natural particles like ပါတယ်/တယ်/လေ/ဟာ), never stiff or literal.
- Keep the same tone as the speaker (funny, serious, dramatic). Keep names consistent through the whole clip.
- Do not add, invent or skip content. Ignore filler sounds. Use Myanmar Unicode (never Zawgyi).

SUBTITLE LAYOUT:
- Each subtitle must be SHORT: one phrase or short sentence, at most about 30 Myanmar characters in total
  (hard limit 36). Split long sentences into several consecutive subtitles at natural phrase breaks.
- Put a normal space between phrases inside a subtitle so it can be wrapped. Never write one huge block.
- Each subtitle should last about 1 to 5 seconds.

TIMING:
- Timestamp format HH:MM:SS,mmm --> HH:MM:SS,mmm, relative to the start of THIS clip (starts at 00:00:00,000).
- A subtitle must appear exactly when its words start being spoken and disappear when they end.
  Do not start early, do not stretch across silence, no overlaps, strictly increasing order.
- Number entries from 1. If there is no speech, output nothing.
"""

# ---------- Web server (keeps Render awake; also serves the video site if videosite.py exists) ----------
try:
    from videosite import app as web  # members video site (same service, same free hours)
except Exception as e:  # not uploaded / not configured: fall back to a tiny keep-alive page
    print("videosite not loaded:", e)
    web = Flask(__name__)

    @web.route("/")
    def home():
        return "Bot is running"

    @web.route("/healthz")
    def healthz():
        return "ok"


def run_web():
    web.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), threaded=True)


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


import unicodedata

_SPLIT_AFTER = "။"


def vlen(t):
    """Visible length: skip combining marks (Myanmar vowel signs/asat) so 1 syllable ~ 1 char."""
    return sum(1 for c in t if not unicodedata.category(c).startswith("M") and c != "\u200b")


def _hard_split(tok, limit):
    """Split a space-less Myanmar string at syllable starts so each piece <= limit visible chars."""
    pieces = []
    while vlen(tok) > limit:
        cut, count = None, 0
        for i, c in enumerate(tok):
            if not unicodedata.category(c).startswith("M"):
                count += 1
            if count > limit:
                break
            starts = (
                i > 0 and "\u1000" <= c <= "\u1021"
                and tok[i - 1] not in "\u1039"
                and not (i + 1 < len(tok) and tok[i + 1] in "\u103a\u1039")
            )
            if starts:
                cut = i
        if not cut:
            break
        pieces.append(tok[:cut])
        tok = tok[cut:]
    pieces.append(tok)
    return pieces


def _pack(text, limit):
    """Cut text into pieces <= limit visible chars, preferring '။' and spaces."""
    toks = []
    for part in re.split(r"(?<=%s)\s*|\s+" % _SPLIT_AFTER, text):
        if part:
            toks.extend(_hard_split(part, limit))
    pieces, cur = [], ""
    for t in toks:
        cand = (cur + " " + t) if cur else t
        if cur and vlen(cand) > limit:
            pieces.append(cur)
            cur = t
        else:
            cur = cand
        if cur.endswith(_SPLIT_AFTER) and vlen(cur) >= limit // 2:
            pieces.append(cur)
            cur = ""
    if cur:
        pieces.append(cur)
    return pieces


def _wrap(text):
    """Break one cue into max 2 balanced lines."""
    if vlen(text) <= LINE_LEN or " " not in text:
        return text
    words, best, total = text.split(" "), None, vlen(text)
    for i in range(1, len(words)):
        left = vlen(" ".join(words[:i]))
        d = abs(total / 2 - left)
        if best is None or d < best[0]:
            best = (d, i)
    i = best[1]
    return " ".join(words[:i]) + "\n" + " ".join(words[i:])


def refine(rows):
    """rows: sorted list of (start, end, text). Split long cues, fix timing/overlaps, wrap lines."""
    out = []
    last_texts = []
    for s, e, body in rows:
        text = " ".join(body.split())
        if len(last_texts) >= 2 and text == last_texts[-1] == last_texts[-2]:
            continue  # model got stuck repeating the same line: drop the 3rd+ copy
        last_texts = (last_texts + [text])[-2:]
        if e <= s:
            e = s + 2000
        pieces = _pack(text, MAX_CUE) or [text]
        weights = [max(1, vlen(p)) for p in pieces]
        total, t = sum(weights), s
        for p, w in zip(pieces, weights):
            dur = (e - s) * w / total
            out.append([int(t), int(t + dur), p])
            t += dur
    # reading-speed cap, no overlaps, minimum length
    for i, r in enumerate(out):
        limit = 1200 + vlen(r[2]) * 140
        r[1] = min(r[1], r[0] + min(limit, 7000))
        if i + 1 < len(out) and r[1] > out[i + 1][0] - 30:
            r[1] = out[i + 1][0] - 30
        if r[1] < r[0] + 700:
            r[1] = r[0] + 700
            if i + 1 < len(out) and r[1] > out[i + 1][0]:
                out[i + 1][0] = r[1] + 30
    return [(a, b, _wrap(c)) for a, b, c in out]


def merge_srt(chunks):
    """chunks: list of (offset_ms, entries[, length_ms]). Returns final SRT text."""
    rows = []
    for ch in chunks:
        offset, entries = ch[0], ch[1]
        length = ch[2] if len(ch) > 2 else None
        for start, end, body in entries:
            if length and start > length + 1000:  # model invented a time beyond this clip
                continue
            if length:
                end = min(end, length + 500)
            s, e = max(0, start + offset + SHIFT_MS), max(0, end + offset + SHIFT_MS)
            if e <= s:
                e = s + 1000
            rows.append((s, e, body))
    rows.sort(key=lambda r: r[0])
    rows = refine(rows)
    lines = []
    for i, (s, e, body) in enumerate(rows, 1):
        lines.append(f"{i}\n{fmt_ts(s)} --> {fmt_ts(e)}\n{body}\n")
    return "\n".join(lines)


# ---------- Media + Gemini ----------
def probe_duration(src):
    """Total length in seconds (from ffmpeg's banner), or 0 if unknown."""
    r = subprocess.run([FFMPEG, "-nostdin", "-i", src], capture_output=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr.decode(errors="ignore"))
    return int(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))) if m else 0


def pick_chunk_sec(total):
    """Longer files -> bigger chunks so we stay inside Gemini's small free daily request quota."""
    if not total:
        return CHUNK_SEC
    return max(CHUNK_SEC, min(MAX_CHUNK_SEC, -(-total // REQ_BUDGET)))


def prepare_chunks(src, workdir, chunk_sec):
    """Convert to small mono mp3 pieces. Returns (files, start_ms_list)."""
    pattern = os.path.join(workdir, "chunk_%03d.mp3")
    listing = os.path.join(workdir, "list.csv")
    cmd = [
        FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
        "-f", "segment", "-segment_time", str(chunk_sec), "-reset_timestamps", "1",
        "-segment_list", listing, "-segment_list_type", "csv", pattern,
    ]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("ffmpeg error: " + r.stderr.decode(errors="ignore")[-300:])
    files, starts = [], []
    try:
        for line in open(listing, encoding="utf-8"):
            parts = line.strip().split(",")
            if len(parts) >= 3 and os.path.exists(os.path.join(workdir, parts[0])):
                files.append(os.path.join(workdir, parts[0]))
                starts.append(int(float(parts[1]) * 1000))
    except OSError:
        pass
    if not files:  # fallback: fixed-length assumption
        files = sorted(glob.glob(os.path.join(workdir, "chunk_*.mp3")))
        starts = [i * chunk_sec * 1000 for i in range(len(files))]
    if not files:
        raise RuntimeError("အသံမတွေ့ပါ (no audio found)")
    return files, starts


class QuotaExhausted(Exception):
    pass


_dead = {}  # model -> time it hit its daily limit
DEAD_SECONDS = 6 * 3600


def _alive_models():
    now = time.time()
    return [m for m in MODELS if now - _dead.get(m, 0) > DEAD_SECONDS]


def transcribe_chunk(path):
    prompt = PROMPT + (f"\nVideo context: {CONTEXT}\n" if CONTEXT else "")
    f = None
    last = None
    try:
        for up in range(3):
            try:
                f = client.files.upload(file=path)
                while f.state.name == "PROCESSING":
                    time.sleep(2)
                    f = client.files.get(name=f.name)
                if f.state.name != "ACTIVE":
                    raise RuntimeError(f"Gemini upload failed: {f.state.name}")
                break
            except Exception as e:
                last = e
                f = None
                time.sleep(5 * (up + 1))
        if f is None:
            raise last
        overloaded = 0
        for attempt in range(8):
            alive = _alive_models()
            if not alive:
                raise QuotaExhausted("daily quota used up on all models")
            model = alive[min(overloaded // 2, len(alive) - 1)]
            try:
                resp = client.models.generate_content(model=model, contents=[prompt, f])
                return resp.text or ""
            except Exception as e:
                last = e
                msg = str(e)
                low = msg.lower()
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in low:
                    if "perday" in low.replace(" ", "").replace("_", "") or "per day" in low:
                        _dead[model] = time.time()  # try the next model right away
                        continue
                    time.sleep(30 * (attempt + 1))
                elif "503" in msg or "UNAVAILABLE" in msg or "overloaded" in low or "high demand" in low:
                    overloaded += 1
                    time.sleep(min(10 * 2 ** overloaded, 90))
                else:
                    time.sleep(5 * (attempt + 1))
        raise last
    finally:
        if f is not None:
            try:
                client.files.delete(name=f.name)
            except Exception:
                pass


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


@bot.on(events.NewMessage(pattern=r"^/models"))
async def models_cmd(event):
    if not await check_allowed(event):
        return
    try:
        def _list():
            names = []
            for m in client.models.list():
                acts = getattr(m, "supported_actions", None) or []
                if "gemini" in m.name and (not acts or "generateContent" in acts):
                    names.append(m.name.replace("models/", ""))
            return sorted(names)

        names = await asyncio.to_thread(_list)
        await event.reply(
            "သုံးနေတဲ့ model: " + ", ".join(MODELS) + "\n\nသင့် key နဲ့ သုံးလို့ရတဲ့ model တွေ:\n" + "\n".join(names)[:3000]
        )
    except Exception as e:
        await event.reply(f"model စာရင်း မရပါ: {str(e)[:300]}")


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
                total = await asyncio.to_thread(probe_duration, src)
                chunk_sec = pick_chunk_sec(total)
                chunks, starts = await asyncio.to_thread(prepare_chunks, src, tmp, chunk_sec)
                os.remove(src)
                n = len(chunks)
                results = [None] * n
                failed = []
                quota_hit = []
                done = 0
                psem = asyncio.Semaphore(PARALLEL)

                async def work(i):
                    nonlocal done
                    async with psem:
                        try:
                            raw = await asyncio.to_thread(transcribe_chunk, chunks[i])
                            length = (starts[i + 1] - starts[i]) if i + 1 < n else None
                            results[i] = (starts[i], parse_srt(raw), length)
                        except Exception as e:
                            print("chunk failed", i, e)
                            failed.append(i)
                            if isinstance(e, QuotaExhausted):
                                quota_hit.append(1)
                        done += 1
                        try:
                            await status.edit(f"စာသားပြောင်းနေပါတယ် ({done}/{n})...")
                        except Exception:
                            pass

                await status.edit(f"အပိုင်း {n} ပိုင်း ခွဲပြီးပါပြီ၊ စာသားပြောင်းနေပါတယ်...")
                await asyncio.gather(*(work(i) for i in range(n)))
                if failed and len(failed) == n:
                    if quota_hit:
                        raise RuntimeError(
                            "Gemini အခမဲ့ quota (တစ်ရက်စာ) ကုန်သွားပါပြီ။ နောက်ရက် (မြန်မာစံတော်ချိန် ညနေ ၂ နာရီလောက်ကနေ) "
                            "ပြန်စမ်းပါ၊ ဒါမှမဟုတ် GEMINI_MODELS မှာ တခြား model ထည့်ပါ။"
                        )
                    raise RuntimeError("Gemini က အပိုင်းအားလုံးကို မလုပ်နိုင်ပါ (network / overload ဖြစ်နိုင်)။ ခဏနေမှ ပြန်ကြိုးစားပါ။")
                srt = merge_srt([r for r in results if r])
                if not srt.strip():
                    await status.edit("စကားပြောသံ မတွေ့ပါ၊ သို့မဟုတ် subtitle မထွက်ပါ။")
                    return
                out = os.path.join(tmp, base + ".srt")
                with open(out, "w", encoding="utf-8") as fh:
                    fh.write(srt)
                caption = "ပြီးပါပြီ ✅"
                if failed:
                    parts = ", ".join(
                        f"{fmt_ts(starts[i])[:8]}" for i in sorted(failed)
                    )
                    if quota_hit:
                        caption = "⚠️ Gemini တစ်ရက်စာ quota ကုန်သွားလို့ အပိုင်း " + str(len(failed)) + " ခု မပါပါဘူး။ နောက်ရက်မှာ ဖိုင်ကို ပြန်ပို့ပါ။ မပါတဲ့ စချိန်: " + parts
                    else:
                        caption = f"⚠️ အပိုင်း {len(failed)} ခု မရလိုက်ပါ (စချိန်: {parts})။ ကျန်တာတွေ ပါပါတယ်။ ထပ်ပို့ကြည့်ပါ။"
                await bot.send_file(event.chat_id, out, caption=caption[:900], reply_to=event.id)
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
