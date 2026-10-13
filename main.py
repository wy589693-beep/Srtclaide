"""Telegram bot (Pyrogram/MTProto): 1) split videos into 1-5 min parts with 5s overlap,
2) Myanmar .srt subtitles via Gemini (same SRT engine as the old SRT bot).
Admin-approved users only.
"""
import asyncio
import glob
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata

from flask import Flask
from google import genai
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
CHUNK_SEC = int(os.environ.get("CHUNK_SECONDS", "300"))  # 5 min (shorter = less timing drift)
MAX_CUE = int(os.environ.get("MAX_CUE_CHARS", "36"))     # max visible chars per subtitle
LINE_LEN = int(os.environ.get("LINE_CHARS", "22"))       # wrap to 2nd line after this many
EN_MAX_CUE = int(os.environ.get("EN_MAX_CUE_CHARS", "42"))  # English-only: max chars per subtitle
EN_LINE_LEN = int(os.environ.get("EN_LINE_CHARS", "32"))    # English-only: wrap after this many
SHIFT_MS = int(os.environ.get("SUB_DELAY_MS", "0"))      # +/- manual timing shift (ms)
CONTEXT = os.environ.get("VIDEO_CONTEXT", "").strip()    # optional hint, e.g. "anime recap"
OVERLAP = 5  # seconds (video splitter)
DB = "users.json"

FFMPEG = "ffmpeg"
client = genai.Client(api_key=GEMINI_KEY) if GEMINI_KEY else None
app = Client("splitbot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
lock = asyncio.Lock()  # one job at a time (free hosting is weak)


# ---------- approved users ----------
def load():
    try:
        return set(json.load(open(DB)))
    except Exception:
        return set()


def save(s):
    json.dump(list(s), open(DB, "w"))


def allowed(uid):
    return uid == ADMIN_ID or uid in load()


# =====================================================================
#  VIDEO SPLITTER
# =====================================================================
def get_duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", path])
    return float(out.strip())


def plan(total, L):
    """Part1 = L+5s. Next parts start 5s before previous end, length L."""
    parts, start, end = [], 0.0, min(L + OVERLAP, total)
    parts.append((start, end - start))
    while end < total - 1:
        start = end - OVERLAP
        end = min(start + L, total)
        parts.append((start, end - start))
    return parts


class Cancelled(Exception):
    pass


JOBS = {}  # (chat_id, status message id) -> {"cancel": bool, "proc": Popen | None}


def cut(src, dst, start, dur, job):
    p = subprocess.Popen(
        [FFMPEG, "-y", "-ss", str(start), "-i", src, "-t", str(dur),
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
         "-c:a", "aac", "-movflags", "+faststart", dst],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    job["proc"] = p
    rc = p.wait()
    job["proc"] = None
    if job["cancel"]:
        raise Cancelled()
    if rc != 0:
        raise RuntimeError("ffmpeg failed")


# =====================================================================
#  MYANMAR SRT  (ported from the old SRT bot)
# =====================================================================
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

PROMPT_EN = """You are a professional English subtitle translator and timer.
Listen to this audio clip and write English subtitles for everything that is spoken.
Output ONLY a valid SRT file (no explanations, no markdown fences).

TRANSLATION QUALITY (most important):
- Translate the MEANING, not word by word. Write natural, fluent, conversational English the way a native speaker would say it.
- Keep the same tone as the speaker (funny, serious, dramatic). Keep names consistent through the whole clip.
- Do not add, invent or skip content. Ignore filler sounds.

SUBTITLE LAYOUT:
- Each subtitle must be SHORT: one phrase or short sentence, at most about 38 characters (hard limit 42).
  Split long sentences into several consecutive subtitles at natural phrase breaks.
- Each subtitle should last about 1 to 5 seconds.

TIMING:
- Timestamp format HH:MM:SS,mmm --> HH:MM:SS,mmm, relative to the start of THIS clip (starts at 00:00:00,000).
- A subtitle must appear exactly when its words start being spoken and disappear when they end.
  Do not start early, do not stretch across silence, no overlaps, strictly increasing order.
- Number entries from 1. If there is no speech, output nothing.
"""

PROMPT_BOTH = """You are a professional bilingual subtitle translator and timer (Burmese + English).
Listen to this audio clip and write subtitles for everything that is spoken.
Output ONLY a valid SRT file (no explanations, no markdown fences).

EVERY subtitle has EXACTLY TWO lines, with no blank line between them:
  line 1 = Myanmar (Burmese) subtitle
  line 2 = English subtitle with the same meaning

TRANSLATION QUALITY (most important):
- Translate the MEANING, not word by word. Myanmar line: everyday spoken Burmese a native speaker would naturally say
  (natural particles like ပါတယ်/တယ်/လေ/ဟာ), Myanmar Unicode only (never Zawgyi). English line: natural, fluent, conversational English.
- Keep the same tone as the speaker (funny, serious, dramatic). Keep names consistent through the whole clip.
- Do not add, invent or skip content. Ignore filler sounds.

SUBTITLE LAYOUT:
- Each subtitle must be SHORT: Myanmar line at most about 30 Myanmar characters (hard limit 36), English line at most
  about 42 characters. Split long sentences into several consecutive subtitles at natural phrase breaks.
- Each subtitle should last about 1 to 5 seconds.

TIMING:
- Timestamp format HH:MM:SS,mmm --> HH:MM:SS,mmm, relative to the start of THIS clip (starts at 00:00:00,000).
- A subtitle must appear exactly when its words start being spoken and disappear when they end.
  Do not start early, do not stretch across silence, no overlaps, strictly increasing order.
- Number entries from 1. If there is no speech, output nothing.
"""

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


_SPLIT_AFTER = "။"


def vlen(t):
    """Visible length: skip combining marks (Myanmar vowel signs/asat) so 1 syllable ~ 1 char."""
    return sum(1 for c in t if not unicodedata.category(c).startswith("M") and c != "​")


def _hard_split(tok, limit):
    """Split a space-less Myanmar string at syllable starts so each piece <= limit visible chars."""
    pieces = []
    while vlen(tok) > limit:
        cut_at, count = None, 0
        for i, c in enumerate(tok):
            if not unicodedata.category(c).startswith("M"):
                count += 1
            if count > limit:
                break
            starts = (
                i > 0 and "က" <= c <= "အ"
                and tok[i - 1] not in "္"
                and not (i + 1 < len(tok) and tok[i + 1] in "်္")
            )
            if starts:
                cut_at = i
        if not cut_at:
            break
        pieces.append(tok[:cut_at])
        tok = tok[cut_at:]
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


def _wrap(text, line_len=None):
    """Break one cue into max 2 balanced lines."""
    if vlen(text) <= (line_len or LINE_LEN) or " " not in text:
        return text
    words, best, total = text.split(" "), None, vlen(text)
    for i in range(1, len(words)):
        left = vlen(" ".join(words[:i]))
        d = abs(total / 2 - left)
        if best is None or d < best[0]:
            best = (d, i)
    i = best[1]
    return " ".join(words[:i]) + "\n" + " ".join(words[i:])


def refine(rows, max_cue=None, line_len=None, split=True):
    """rows: sorted list of (start, end, text). Split long cues, fix timing/overlaps, wrap lines."""
    max_cue = max_cue or MAX_CUE
    line_len = line_len or LINE_LEN
    out = []
    for s, e, body in rows:
        if not split:  # bilingual: keep the 2 lines exactly as written, only fix timing
            if e <= s:
                e = s + 2000
            out.append([int(s), int(e), "\n".join(l.strip() for l in body.split("\n") if l.strip())])
            continue
        text = " ".join(body.split())
        if e <= s:
            e = s + 2000
        pieces = _pack(text, max_cue) or [text]
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
    return [(a, b, _wrap(c, line_len) if split else c) for a, b, c in out]


def merge_srt(chunks, mode="my"):
    """chunks: list of (offset_ms, entries). Returns final SRT text."""
    rows = []
    for offset, entries in chunks:
        for start, end, body in entries:
            s, e = max(0, start + offset + SHIFT_MS), max(0, end + offset + SHIFT_MS)
            if e <= s:
                e = s + 1000
            rows.append((s, e, body))
    rows.sort(key=lambda r: r[0])
    if mode == "en":
        rows = refine(rows, EN_MAX_CUE, EN_LINE_LEN)
    elif mode == "both":
        rows = refine(rows, split=False)
    else:
        rows = refine(rows)
    lines = []
    for i, (s, e, body) in enumerate(rows, 1):
        lines.append(f"{i}\n{fmt_ts(s)} --> {fmt_ts(e)}\n{body}\n")
    return "\n".join(lines)


def prepare_chunks(src, workdir, job):
    pattern = os.path.join(workdir, "chunk_%03d.mp3")
    cmd = [
        FFMPEG, "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
        "-f", "segment", "-segment_time", str(CHUNK_SEC), "-reset_timestamps", "1", pattern,
    ]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    job["proc"] = p
    err = p.communicate()[1]
    job["proc"] = None
    if job["cancel"]:
        raise Cancelled()
    if p.returncode != 0:
        raise RuntimeError("ffmpeg error: " + err.decode(errors="ignore")[-300:])
    files = sorted(glob.glob(os.path.join(workdir, "chunk_*.mp3")))
    if not files:
        raise RuntimeError("အသံမတွေ့ပါ (no audio found)")
    return files


def transcribe_chunk(path, base_prompt=None):
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
            prompt = (base_prompt or PROMPT) + (f"\nVideo context: {CONTEXT}\n" if CONTEXT else "")
            resp = client.models.generate_content(model=MODEL, contents=[prompt, f])
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


# =====================================================================
#  TELEGRAM HANDLERS
# =====================================================================
async def ask_admin(u):
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"ap:{u.id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"rj:{u.id}")]])
    await app.send_message(
        ADMIN_ID, f"ခွင့်ပြုချက်တောင်းနေသူ: {u.first_name} (@{u.username}) id={u.id}",
        reply_markup=kb)


@app.on_message(filters.command("start") & filters.private)
async def start(_, m):
    if allowed(m.from_user.id):
        await m.reply("Video/mp3 ပို့ပါ။ ✂️ အပုဒ်ခွဲမလား 📝 မြန်မာ SRT ထုတ်မလား ရွေးနိုင်ပါတယ်။")
    else:
        await ask_admin(m.from_user)
        await m.reply("Admin ခွင့်ပြုချက် စောင့်ပါ။ ခွင့်ပြုပြီးရင် အသိပေးပါမယ်။")


@app.on_message(filters.command("approve") & filters.user(ADMIN_ID))
async def approve(_, m):
    s = load(); s.add(int(m.command[1])); save(s)
    await m.reply("✅ ခွင့်ပြုပြီး")


@app.on_message(filters.command("remove") & filters.user(ADMIN_ID))
async def remove(_, m):
    s = load(); s.discard(int(m.command[1])); save(s)
    await m.reply("🗑 ဖယ်ပြီး")


@app.on_message(filters.command("users") & filters.user(ADMIN_ID))
async def users(_, m):
    await m.reply("Approved: " + (", ".join(map(str, load())) or "မရှိသေး"))


@app.on_message(filters.private & (filters.video | filters.audio | filters.voice | filters.document))
async def got_media(_, m):
    mime = ((m.document.mime_type if m.document else "") or "").lower()
    is_video = bool(m.video) or mime.startswith("video")
    is_audio = bool(m.audio or m.voice) or mime.startswith("audio")
    if not (is_video or is_audio):
        return
    if not allowed(m.from_user.id):
        await ask_admin(m.from_user)
        return await m.reply("Admin ခွင့်ပြုချက် လိုပါတယ်။ Admin ကို တောင်းခံပြီးပါပြီ။")
    rows = []
    if is_video:
        rows.append([InlineKeyboardButton(f"✂️ {n} min", callback_data=f"len:{n}:{m.id}")
                     for n in (1, 2, 3, 4, 5)])
    rows.append([InlineKeyboardButton("📝 မြန်မာ SRT", callback_data=f"srt:{m.id}"),
                 InlineKeyboardButton("🇬🇧 English SRT", callback_data=f"sre:{m.id}")])
    rows.append([InlineKeyboardButton("🇲🇲+🇬🇧 မြန်မာ + English (နှစ်ဘာသာ)", callback_data=f"srb:{m.id}")])
    await m.reply("ဘာလုပ်မလဲ?", reply_markup=InlineKeyboardMarkup(rows), quote=True)


@app.on_callback_query()
async def cb(_, q):
    d = q.data.split(":")
    if d[0] in ("ap", "rj") and q.from_user.id == ADMIN_ID:
        uid = int(d[1])
        if d[0] == "ap":
            s = load(); s.add(uid); save(s)
            await app.send_message(uid, "✅ ခွင့်ပြုပြီးပါပြီ။ Video ပို့လို့ရပါပြီ။")
        await q.message.edit_text(("✅ Approved " if d[0] == "ap" else "❌ Rejected ") + str(uid))
    elif d[0] == "len" and allowed(q.from_user.id):
        L = int(d[1]) * 60
        src_msg = await app.get_messages(q.message.chat.id, int(d[2]))
        await q.message.edit_text("⏳ တန်းစီစောင့်နေပါတယ်...", reply_markup=cancel_kb(q.message.id))
        asyncio.create_task(process(q.message, src_msg, L))
    elif d[0] in ("srt", "sre", "srb") and allowed(q.from_user.id):
        mode = {"srt": "my", "sre": "en", "srb": "both"}[d[0]]
        src_msg = await app.get_messages(q.message.chat.id, int(d[1]))
        await q.message.edit_text("⏳ တန်းစီစောင့်နေပါတယ်...", reply_markup=cancel_kb(q.message.id))
        asyncio.create_task(process_srt(q.message, src_msg, mode))
    elif d[0] == "cx":
        job = JOBS.get((q.message.chat.id, int(d[1])))
        if job:
            job["cancel"] = True
            p = job["proc"]
            if p:
                try:
                    p.terminate()
                except Exception:
                    pass
            return await q.answer("ရပ်နေပါတယ်...")
        return await q.answer("ဒီ job ပြီးသွားပါပြီ")
    await q.answer()


def cancel_kb(mid):
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"cx:{mid}")]])


async def upd(status, text):
    try:
        await status.edit_text(text, reply_markup=cancel_kb(status.id))
    except Exception:
        pass


def new_job(status):
    job = {"cancel": False, "proc": None}
    JOBS[(status.chat.id, status.id)] = job
    return job


def check(job):
    if job["cancel"]:
        raise Cancelled()


async def dl_progress(current, total, job):
    if job["cancel"]:
        app.stop_transmission()


async def download(src_msg, path, job):
    f = await app.download_media(src_msg, file_name=path, progress=dl_progress, progress_args=(job,))
    check(job)
    if not f:
        raise RuntimeError("Download မအောင်မြင်ပါ")
    return f


async def process(status, src_msg, L):
    job = new_job(status)
    async with lock:
        tmp = tempfile.mkdtemp()
        try:
            check(job)
            await upd(status, "⬇️ Download လုပ်နေပါတယ်...")
            src = await download(src_msg, os.path.join(tmp, "in.mp4"), job)
            total = await asyncio.to_thread(get_duration, src)
            parts = plan(total, L)
            n = len(parts)
            for i, (st, du) in enumerate(parts, 1):
                check(job)
                await upd(status, f"✂️ ဖြတ်/ပို့နေပါတယ် {i}/{n}")
                out = os.path.join(tmp, f"p{i}.mp4")
                await asyncio.to_thread(cut, src, out, st, du, job)
                check(job)
                await app.send_video(status.chat.id, out, caption=f"Part {i}/{n}",
                                     duration=int(du), supports_streaming=True)
                os.remove(out)
            await status.edit_text(f"✅ ပြီးပါပြီ — စုစုပေါင်း {n} ပုဒ်")
        except Cancelled:
            await status.edit_text("❌ ရပ်လိုက်ပါပြီ")
        except Exception as e:
            await status.edit_text(f"❌ အမှားဖြစ်သွားတယ်: {str(e)[:500]}")
        finally:
            JOBS.pop((status.chat.id, status.id), None)
            shutil.rmtree(tmp, ignore_errors=True)


MODE_PROMPT = {"my": None, "en": PROMPT_EN, "both": PROMPT_BOTH}


async def process_srt(status, src_msg, mode="my"):
    job = new_job(status)
    if client is None:
        JOBS.pop((status.chat.id, status.id), None)
        return await status.edit_text("❌ GEMINI_API_KEY မထည့်ရသေးပါ")
    async with lock:
        tmp = tempfile.mkdtemp()
        try:
            check(job)
            media = src_msg.video or src_msg.audio or src_msg.voice or src_msg.document
            base = os.path.splitext(getattr(media, "file_name", None) or "subtitle")[0] or "subtitle"
            await upd(status, "⬇️ Download လုပ်နေပါတယ်...")
            src = await download(src_msg, os.path.join(tmp, "input"), job)
            await upd(status, "🎧 အသံကို ပြင်ဆင်နေပါတယ်...")
            chunks = await asyncio.to_thread(prepare_chunks, src, tmp, job)
            os.remove(src)
            results = []
            for i, ch in enumerate(chunks):
                check(job)
                await upd(status, f"🧠 စာသားပြောင်းနေပါတယ် ({i + 1}/{len(chunks)})...")
                raw = await asyncio.to_thread(transcribe_chunk, ch, MODE_PROMPT[mode])
                results.append((i * CHUNK_SEC * 1000, parse_srt(raw)))
            check(job)
            srt = merge_srt(results, mode)
            if not srt.strip():
                return await status.edit_text("စကားပြောသံ မတွေ့ပါ၊ သို့မဟုတ် subtitle မထွက်ပါ။")
            out = os.path.join(tmp, base + {"my": "_my", "en": "_en", "both": "_my_en"}[mode] + ".srt")
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(srt)
            await app.send_document(status.chat.id, out, caption="ပြီးပါပြီ ✅")
            await status.delete()
        except Cancelled:
            await status.edit_text("❌ ရပ်လိုက်ပါပြီ")
        except Exception as e:
            await status.edit_text(f"အမှားတက်သွားပါတယ်: {str(e)[:500]}")
        finally:
            JOBS.pop((status.chat.id, status.id), None)
            shutil.rmtree(tmp, ignore_errors=True)


# ---------- web server (keeps Render awake; serves videosite.py if present) ----------
try:
    from videosite import app as web
except Exception as e:
    print("videosite not loaded:", e)
    web = Flask(__name__)

    @web.route("/")
    def home():
        return "Bot is running"

    @web.route("/healthz")
    def healthz():
        return "ok"


threading.Thread(
    target=lambda: web.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), threaded=True),
    daemon=True).start()

app.run()
