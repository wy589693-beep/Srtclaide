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
MODELS = [m.strip() for m in os.environ.get("GEMINI_MODELS", MODEL).split(",") if m.strip()] or [MODEL]
CHUNK_SEC = int(os.environ.get("CHUNK_SECONDS", "600"))  # minimum chunk length (10 min)
MAX_CHUNK_SEC = int(os.environ.get("MAX_CHUNK_SECONDS", "1200"))  # chunks grow up to 20 min on very long files
REQ_BUDGET = int(os.environ.get("REQUEST_BUDGET", "18"))  # Gemini requests per file (free tier ~20/day/model)
PARALLEL = int(os.environ.get("GEMINI_PARALLEL", "3"))    # chunks transcribed at the same time
MAX_CUE = int(os.environ.get("MAX_CUE_CHARS", "36"))     # max visible chars per subtitle
LINE_LEN = int(os.environ.get("LINE_CHARS", "22"))       # wrap to 2nd line after this many
EN_MAX_CUE = int(os.environ.get("EN_MAX_CUE_CHARS", "42"))  # English-only: max chars per subtitle
EN_LINE_LEN = int(os.environ.get("EN_LINE_CHARS", "32"))    # English-only: wrap after this many
SHIFT_MS = int(os.environ.get("SUB_DELAY_MS", "0"))      # +/- manual timing shift (ms)
CONTEXT = os.environ.get("VIDEO_CONTEXT", "").strip()    # optional hint, e.g. "anime recap"
OVERLAP = 5  # seconds (video splitter)
DB = "users.json"

try:
    import imageio_ffmpeg  # bundled ffmpeg (no system install needed)

    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = "ffmpeg"
# ---------- Gemini key pool (keys can be added from the bot with /addkey) ----------
KEYS_DB = "keys.json"
ENV_KEYS = [k for k in re.split(r"[,\s]+", os.environ.get("GEMINI_API_KEYS", "") + "," + GEMINI_KEY) if k]
_clients = {}


def _extra_keys():
    try:
        return [k for k in json.load(open(KEYS_DB)) if isinstance(k, str) and k]
    except Exception:
        return []


def all_keys():
    out = []
    for k in ENV_KEYS + _extra_keys():
        if k not in out:
            out.append(k)
    return out


def get_client(key):
    if key not in _clients:
        _clients[key] = genai.Client(api_key=key)
    return _clients[key]


def mask(k):
    return k[:4] + "…" + k[-4:] if len(k) > 10 else "***"

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
    """Length in seconds, read from ffmpeg's banner (works without ffprobe). 0 if unknown."""
    r = subprocess.run([FFMPEG, "-nostdin", "-i", path], capture_output=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr.decode(errors="ignore"))
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)) if m else 0.0


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
    last_texts = []
    for s, e, body in rows:
        key = " ".join(body.split())
        if len(last_texts) >= 2 and key == last_texts[-1] == last_texts[-2]:
            continue  # model got stuck repeating a line: drop the 3rd+ copy
        last_texts = (last_texts + [key])[-2:]
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


def pick_chunk_sec(total, mode):
    """Longer files -> bigger chunks, to stay inside Gemini's small free daily request quota."""
    cap = min(MAX_CHUNK_SEC, 900) if mode == "both" else MAX_CHUNK_SEC  # bilingual output is twice as long
    if not total:
        return CHUNK_SEC
    return int(max(min(CHUNK_SEC, cap), min(cap, -(-total // REQ_BUDGET))))


def prepare_chunks(src, workdir, job, chunk_sec):
    """Convert to small mono mp3 pieces. Returns (files, start_ms_list)."""
    pattern = os.path.join(workdir, "chunk_%03d.mp3")
    listing = os.path.join(workdir, "list.csv")
    cmd = [
        FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
        "-f", "segment", "-segment_time", str(chunk_sec), "-reset_timestamps", "1",
        "-segment_list", listing, "-segment_list_type", "csv", pattern,
    ]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    job["proc"] = p
    err = p.communicate()[1]
    job["proc"] = None
    if job["cancel"]:
        raise Cancelled()
    if p.returncode != 0:
        raise RuntimeError("ffmpeg error: " + err.decode(errors="ignore")[-300:])
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


_dead = {}  # (key, model) -> time it hit its daily limit
DEAD_SECONDS = 6 * 3600


def _combos():
    now = time.time()
    return [(k, m) for k in all_keys() for m in MODELS if now - _dead.get((k, m), 0) > DEAD_SECONDS]


def _upload(cl, path):
    f = cl.files.upload(file=path)
    while f.state.name == "PROCESSING":
        time.sleep(2)
        f = cl.files.get(name=f.name)
    if f.state.name != "ACTIVE":
        raise RuntimeError(f"Gemini upload failed: {f.state.name}")
    return f


def transcribe_chunk(path, base_prompt=None):
    prompt = (base_prompt or PROMPT) + (f"\nVideo context: {CONTEXT}\n" if CONTEXT else "")
    files = {}  # uploaded copy per key (a file belongs to the key/project that uploaded it)
    last = None
    overloaded = 0
    try:
        for attempt in range(10):
            combos = _combos()
            if not combos:
                raise QuotaExhausted("daily quota used up on all keys/models")
            key, model = combos[min(overloaded // 2, len(combos) - 1)]
            try:
                if key not in files:
                    files[key] = _upload(get_client(key), path)
                resp = get_client(key).models.generate_content(model=model, contents=[prompt, files[key]])
                return resp.text or ""
            except Exception as e:
                last = e
                msg = str(e)
                low = msg.lower()
                if "api_key_invalid" in low or "api key not valid" in low or "api key expired" in low:
                    for m in MODELS:  # bad key: skip it entirely
                        _dead[(key, m)] = time.time()
                    continue
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in low:
                    if "perday" in low.replace(" ", "").replace("_", "") or "per day" in low:
                        _dead[(key, model)] = time.time()  # done for today: next key/model right away
                        continue
                    time.sleep(30 * (attempt + 1))
                elif "503" in msg or "UNAVAILABLE" in msg or "overloaded" in low or "high demand" in low:
                    overloaded += 1
                    time.sleep(min(10 * 2 ** overloaded, 90))
                else:
                    time.sleep(5 * (attempt + 1))
        raise last
    finally:
        for key, f in files.items():
            try:
                get_client(key).files.delete(name=f.name)
            except Exception:
                pass


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
            if not total:
                raise RuntimeError("ဗီဒီယိုအရှည် မဖတ်နိုင်ပါ")
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
    if not all_keys():
        JOBS.pop((status.chat.id, status.id), None)
        return await status.edit_text("❌ GEMINI key မထည့်ရသေးပါ (/addkey နဲ့ ထည့်ပါ)")
    async with lock:
        tmp = tempfile.mkdtemp()
        try:
            check(job)
            media = src_msg.video or src_msg.audio or src_msg.voice or src_msg.document
            base = os.path.splitext(getattr(media, "file_name", None) or "subtitle")[0] or "subtitle"
            await upd(status, "⬇️ Download လုပ်နေပါတယ်...")
            src = await download(src_msg, os.path.join(tmp, "input"), job)
            await upd(status, "🎧 အသံကို ပြင်ဆင်နေပါတယ်... (ဖိုင်ရှည်ရင် ကြာနိုင်ပါတယ်)")
            total = await asyncio.to_thread(get_duration, src)
            chunk_sec = pick_chunk_sec(total, mode)
            chunks, starts = await asyncio.to_thread(prepare_chunks, src, tmp, job, chunk_sec)
            os.remove(src)
            n = len(chunks)
            results = [None] * n
            failed, quota_hit = [], []
            state = {"done": 0, "last": 0.0}
            psem = asyncio.Semaphore(PARALLEL)
            prompt = MODE_PROMPT[mode]

            async def work(i):
                async with psem:
                    if job["cancel"]:
                        return
                    try:
                        raw = await asyncio.to_thread(transcribe_chunk, chunks[i], prompt)
                        length = (starts[i + 1] - starts[i]) if i + 1 < n else None
                        results[i] = (starts[i], parse_srt(raw), length)
                    except Exception as e:
                        print("chunk failed", i, e)
                        failed.append(i)
                        if isinstance(e, QuotaExhausted):
                            quota_hit.append(1)
                    state["done"] += 1
                    if time.time() - state["last"] > 3:
                        state["last"] = time.time()
                        await upd(status, f"🧠 စာသားပြောင်းနေပါတယ် ({state['done']}/{n})...")

            await upd(status, f"🧠 အပိုင်း {n} ပိုင်း ခွဲပြီးပါပြီ၊ စာသားပြောင်းနေပါတယ်...")
            tasks = [asyncio.create_task(work(i)) for i in range(n)]
            while tasks:
                _, pending = await asyncio.wait(tasks, timeout=2)
                if job["cancel"]:
                    for t in pending:
                        t.cancel()
                    raise Cancelled()
                tasks = list(pending)
            check(job)
            if failed and len(failed) == n:
                if quota_hit:
                    raise RuntimeError(
                        "Gemini အခမဲ့ quota (တစ်ရက်စာ) ကုန်သွားပါပြီ။ နောက်ရက် (မြန်မာစံတော်ချိန် ညနေ ၂ နာရီလောက်ကနေ) "
                        "ပြန်စမ်းပါ၊ ဒါမှမဟုတ် GEMINI_MODELS မှာ တခြား model ထည့်ပါ။")
                raise RuntimeError("Gemini က အပိုင်းအားလုံးကို မလုပ်နိုင်ပါ (network / overload ဖြစ်နိုင်)။ ခဏနေမှ ပြန်ကြိုးစားပါ။")
            srt = merge_srt([r for r in results if r], mode)
            if not srt.strip():
                return await status.edit_text("စကားပြောသံ မတွေ့ပါ၊ သို့မဟုတ် subtitle မထွက်ပါ။")
            out = os.path.join(tmp, base + {"my": "_my", "en": "_en", "both": "_my_en"}[mode] + ".srt")
            with open(out, "w", encoding="utf-8") as fh:
                fh.write(srt)
            caption = "ပြီးပါပြီ ✅"
            if failed:
                when = ", ".join(fmt_ts(starts[i])[:8] for i in sorted(failed))
                if quota_hit:
                    caption = (f"⚠️ Gemini တစ်ရက်စာ quota ကုန်လို့ အပိုင်း {len(failed)} ခု မပါပါဘူး။ "
                               f"နောက်ရက်မှာ ဖိုင်ကို ပြန်ပို့ပါ။ မပါတဲ့ စချိန်: {when}")
                else:
                    caption = f"⚠️ အပိုင်း {len(failed)} ခု မရလိုက်ပါ (စချိန်: {when})။ ကျန်တာတွေ ပါပါတယ်။ ထပ်ပို့ကြည့်ပါ။"
            await app.send_document(status.chat.id, out, caption=caption[:900])
            await status.delete()
        except (Cancelled, asyncio.CancelledError):
            await status.edit_text("❌ ရပ်လိုက်ပါပြီ")
        except Exception as e:
            await status.edit_text(f"အမှားတက်သွားပါတယ်: {str(e)[:500]}")
        finally:
            JOBS.pop((status.chat.id, status.id), None)
            shutil.rmtree(tmp, ignore_errors=True)


@app.on_message(filters.command("addkey") & filters.user(ADMIN_ID) & filters.private)
async def addkey(_, m):
    key = m.command[1].strip() if len(m.command) > 1 else ""
    try:
        await m.delete()  # don't leave the secret key sitting in the chat
    except Exception:
        pass
    if not key:
        return await app.send_message(m.chat.id, "သုံးပုံ: /addkey <Gemini API key>")
    if key in all_keys():
        return await app.send_message(m.chat.id, f"ဒီ key ({mask(key)}) ထည့်ထားပြီးသားပါ။")
    try:
        await asyncio.to_thread(lambda: next(iter(get_client(key).models.list()), None))
    except Exception as e:
        _clients.pop(key, None)
        return await app.send_message(m.chat.id, f"❌ ဒီ key အလုပ်မလုပ်ပါ: {str(e)[:200]}")
    extra = _extra_keys() + [key]
    json.dump(extra, open(KEYS_DB, "w"))
    await app.send_message(
        m.chat.id,
        f"✅ key ထည့်ပြီး ({mask(key)})။ စုစုပေါင်း {len(all_keys())} ခု။\n"
        "(message ကို ဖျက်ပြီးပါပြီ။ Render restart/deploy လုပ်ရင် ဒီနည်းနဲ့ထည့်တာ ပျောက်ပါမယ်၊ အမြဲထားချင်ရင် "
        "Render Environment ရဲ့ GEMINI_API_KEYS မှာ ကော်မာခြားထည့်ပါ။)")


@app.on_message(filters.command("keys") & filters.user(ADMIN_ID) & filters.private)
async def keys_cmd(_, m):
    now = time.time()
    lines = []
    for i, k in enumerate(all_keys(), 1):
        left = [mm for mm in MODELS if now - _dead.get((k, mm), 0) > DEAD_SECONDS]
        tag = "env" if k in ENV_KEYS else "bot"
        lines.append(f"{i}. {mask(k)} [{tag}] — " + ("အသုံးပြုလို့ရ ✅" if left else "ဒီနေ့ quota ကုန် ⛔"))
    await m.reply("\n".join(lines) or "key မရှိသေးပါ။ /addkey နဲ့ ထည့်ပါ။")


@app.on_message(filters.command("removekey") & filters.user(ADMIN_ID) & filters.private)
async def removekey(_, m):
    try:
        n = int(m.command[1])
        key = all_keys()[n - 1]
    except Exception:
        return await m.reply("သုံးပုံ: /removekey <နံပါတ်> (နံပါတ်ကို /keys မှာကြည့်ပါ)")
    if key in ENV_KEYS:
        return await m.reply("ဒီ key က Render Environment ထဲက ဖြစ်လို့ Render မှာပဲ ဖျက်ပါ။")
    json.dump([k for k in _extra_keys() if k != key], open(KEYS_DB, "w"))
    _clients.pop(key, None)
    await m.reply(f"🗑 {mask(key)} ဖယ်ပြီး။ ကျန် {len(all_keys())} ခု။")


@app.on_message(filters.command("models") & filters.user(ADMIN_ID))
async def models_cmd(_, m):
    try:
        def _list():
            names = []
            for mm in get_client(all_keys()[0]).models.list():
                acts = getattr(mm, "supported_actions", None) or []
                if "gemini" in mm.name and (not acts or "generateContent" in acts):
                    names.append(mm.name.replace("models/", ""))
            return sorted(names)

        names = await asyncio.to_thread(_list)
        await m.reply("သုံးနေတဲ့ model: " + ", ".join(MODELS) + "\n\nသင့် key နဲ့ သုံးလို့ရတာ:\n" + "\n".join(names)[:3000])
    except Exception as e:
        await m.reply(f"model စာရင်း မရပါ: {str(e)[:300]}")


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
