"""Members video site: categories, Free / VIP videos, owner approval, no-download player.

Runs standalone:   gunicorn videosite:app --workers 1 --threads 8 --timeout 0
or inside another service (the Telegram bot's main.py imports `app` from here).

Environment variables
  DATABASE_URL    postgres://...  (use a persistent free Postgres, e.g. Neon/Supabase).
                  If missing, a local SQLite file is used (LOST on every Render restart).
  SECRET_KEY      long random string (keeps logins valid across restarts)
  ADMIN_USERNAME  owner login name
  ADMIN_PASSWORD  owner password (the owner account is created/updated at startup)
  SITE_NAME       optional, default "Apex Video"
"""
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, stream_with_context, url_for)
from jinja2 import DictLoader
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

SITE_NAME = os.environ.get("SITE_NAME", "Apex Video")
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
IS_PG = DATABASE_URL.startswith("postgresql")
SQLITE_PATH = os.environ.get("SQLITE_PATH", "videosite.db")
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "").strip().lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
MM_TZ = timezone(timedelta(hours=6, minutes=30))

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    MAX_CONTENT_LENGTH=64 * 1024,
)
if not os.environ.get("SECRET_KEY"):
    print("WARNING: SECRET_KEY not set - users will be logged out on every restart")
if not IS_PG:
    print("WARNING: DATABASE_URL not set - using local SQLite (data is lost on Render restarts)")


# ------------------------------------------------------------------ database
def connect():
    if IS_PG:
        import psycopg2
        import psycopg2.extras
        return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor,
                                connect_timeout=15)
    con = sqlite3.connect(SQLITE_PATH, timeout=15)
    con.row_factory = lambda cur, row: {d[0]: row[i] for i, d in enumerate(cur.description)}
    return con


def _sql(sql):
    return sql.replace("?", "%s") if IS_PG else sql


def db():
    if "db" not in g:
        g.db = connect()
    return g.db


def _run(sql, args=()):
    cur = db().cursor()
    cur.execute(_sql(sql), tuple(args))
    return cur


def one(sql, args=()):
    cur = _run(sql, args)
    row = cur.fetchone()
    cur.close()
    return row


def many(sql, args=()):
    cur = _run(sql, args)
    rows = cur.fetchall()
    cur.close()
    return rows


def write(sql, args=()):
    cur = _run(sql, args)
    cur.close()
    db().commit()


def insert(sql, args=()):
    cur = _run(sql + " RETURNING id", args)
    new_id = cur.fetchone()["id"]
    cur.close()
    db().commit()
    return new_id


@app.teardown_appcontext
def close_db(_exc):
    con = g.pop("db", None)
    if con is not None:
        try:
            con.close()
        except Exception:
            pass


def init_db():
    con = connect()
    cur = con.cursor()
    pk = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    for stmt in (
        f"""CREATE TABLE IF NOT EXISTS users (
            id {pk}, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0, vip_until BIGINT NOT NULL DEFAULT 0,
            vip_requested INTEGER NOT NULL DEFAULT 0, vip_note TEXT NOT NULL DEFAULT '',
            created_at BIGINT NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS categories (
            id {pk}, name TEXT UNIQUE NOT NULL, sort INTEGER NOT NULL DEFAULT 0)""",
        f"""CREATE TABLE IF NOT EXISTS videos (
            id {pk}, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            category_id INTEGER, access TEXT NOT NULL DEFAULT 'free',
            src_type TEXT NOT NULL, src TEXT NOT NULL, thumb TEXT NOT NULL DEFAULT '',
            views INTEGER NOT NULL DEFAULT 0, published INTEGER NOT NULL DEFAULT 1,
            created_at BIGINT NOT NULL)""",
    ):
        cur.execute(stmt)
    cur.execute("SELECT COUNT(*) AS n FROM categories")
    if cur.fetchone()["n"] == 0:
        for i, name in enumerate(("Cartoon", "Anime", "AI Recap")):
            cur.execute(_sql("INSERT INTO categories (name, sort) VALUES (?, ?)"), (name, i))
    if ADMIN_USERNAME and ADMIN_PASSWORD:
        cur.execute(_sql("SELECT * FROM users WHERE username = ?"), (ADMIN_USERNAME,))
        row = cur.fetchone()
        if row is None:
            cur.execute(_sql("INSERT INTO users (username, pw_hash, is_admin, created_at) "
                             "VALUES (?, ?, 1, ?)"),
                        (ADMIN_USERNAME, generate_password_hash(ADMIN_PASSWORD, "pbkdf2:sha256"),
                         int(time.time())))
        elif not row["is_admin"] or not check_password_hash(row["pw_hash"], ADMIN_PASSWORD):
            cur.execute(_sql("UPDATE users SET pw_hash = ?, is_admin = 1 WHERE id = ?"),
                        (generate_password_hash(ADMIN_PASSWORD, "pbkdf2:sha256"), row["id"]))
    elif not ADMIN_USERNAME or not ADMIN_PASSWORD:
        print("WARNING: ADMIN_USERNAME / ADMIN_PASSWORD not set - nobody can open the admin panel")
    con.commit()
    con.close()


# ------------------------------------------------------------------ helpers
def vip_active(user):
    if not user:
        return False
    if user["is_admin"]:
        return True
    vu = user["vip_until"]
    return vu == -1 or vu > time.time()


def fmt_date(ts):
    return datetime.fromtimestamp(ts, MM_TZ).strftime("%Y-%m-%d")


def vip_label(user):
    if user["is_admin"]:
        return "Owner"
    vu = user["vip_until"]
    if vu == -1:
        return "VIP (ကန့်သတ်ချက်မရှိ)"
    if vu > time.time():
        return "VIP (" + fmt_date(vu) + " အထိ)"
    return "VIP ကုန်ဆုံးပြီး" if vu else "အခမဲ့အကောင့်"


_YT = re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/))([A-Za-z0-9_-]{11})")


def youtube_id(text):
    text = (text or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text
    m = _YT.search(text)
    return m.group(1) if m else None


def thumb_of(v):
    if v["thumb"]:
        return v["thumb"]
    if v["src_type"] == "youtube":
        return f"https://i.ytimg.com/vi/{v['src']}/hqdefault.jpg"
    return ""


def csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def safe_next(target):
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return url_for("home")


_hits = {}


def throttled(key, limit, window):
    now = time.time()
    hits = [t for t in _hits.get(key, []) if now - t < window]
    _hits[key] = hits
    if len(hits) >= limit:
        return True
    hits.append(now)
    return False


def login_required(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*a, **k):
        if not g.user:
            flash("အရင် ဝင်ရောက်ပါ။", "err")
            return redirect(url_for("login", next=request.path))
        return fn(*a, **k)
    return wrapper


def admin_required(fn):
    from functools import wraps

    @wraps(fn)
    def wrapper(*a, **k):
        if not g.user or not g.user["is_admin"]:
            abort(404)
        return fn(*a, **k)
    return wrapper


@app.before_request
def load_user():
    g.user = None
    if request.endpoint in ("healthz", "static"):
        return
    uid = session.get("uid")
    if uid:
        g.user = one("SELECT * FROM users WHERE id = ?", (uid,))
        if g.user is None:
            session.clear()
    if request.method == "POST":
        sent = request.form.get("csrf", "")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400)


@app.after_request
def headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


app.jinja_env.globals.update(site_name=SITE_NAME, csrf=csrf, thumb_of=thumb_of,
                             vip_active=vip_active, vip_label=vip_label)
app.jinja_env.filters["date"] = fmt_date


# ------------------------------------------------------------------ public pages
@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/")
def home():
    cat = request.args.get("cat", type=int)
    acc = request.args.get("a", "")
    q = request.args.get("q", "").strip()[:60]
    sql = ("SELECT v.*, c.name AS cat_name FROM videos v "
           "LEFT JOIN categories c ON c.id = v.category_id WHERE v.published = 1")
    args = []
    if cat:
        sql += " AND v.category_id = ?"
        args.append(cat)
    if acc in ("free", "vip"):
        sql += " AND v.access = ?"
        args.append(acc)
    if q:
        sql += " AND LOWER(v.title) LIKE ?"
        args.append("%" + q.lower() + "%")
    sql += " ORDER BY v.id DESC LIMIT 120"
    return render_template("home.html", videos=many(sql, args),
                           cats=many("SELECT * FROM categories ORDER BY sort, name"),
                           cat=cat, acc=acc, q=q)


@app.route("/watch/<int:vid>")
def watch(vid):
    v = one("SELECT v.*, c.name AS cat_name FROM videos v "
            "LEFT JOIN categories c ON c.id = v.category_id WHERE v.id = ?", (vid,))
    if not v or (not v["published"] and not (g.user and g.user["is_admin"])):
        abort(404)
    allowed = v["access"] == "free" or vip_active(g.user)
    embed = None
    if allowed:
        write("UPDATE videos SET views = views + 1 WHERE id = ?", (vid,))
        if v["src_type"] == "youtube":
            embed = (f"https://www.youtube-nocookie.com/embed/{v['src']}"
                     "?rel=0&modestbranding=1&playsinline=1")
    related = many("SELECT v.*, c.name AS cat_name FROM videos v "
                   "LEFT JOIN categories c ON c.id = v.category_id "
                   "WHERE v.published = 1 AND v.id != ? "
                   + ("AND v.category_id = ? " if v["category_id"] else "")
                   + "ORDER BY v.id DESC LIMIT 6",
                   (vid, v["category_id"]) if v["category_id"] else (vid,))
    return render_template("watch.html", v=v, allowed=allowed, embed=embed, related=related)


@app.route("/stream/<int:vid>")
def stream(vid):
    """Proxy the real video file so its address is never shown to the viewer."""
    # A direct tap on the address bar / "open in new tab" sends Sec-Fetch-Dest: document.
    if request.headers.get("Sec-Fetch-Dest") == "document":
        abort(403)
    v = one("SELECT * FROM videos WHERE id = ?", (vid,))
    if not v or v["src_type"] != "url" or not v["published"] and not (g.user and g.user["is_admin"]):
        abort(404)
    if v["access"] != "free" and not vip_active(g.user):
        abort(403)
    fwd = {}
    if request.headers.get("Range"):
        fwd["Range"] = request.headers["Range"]
    try:
        up = requests.get(v["src"], headers=fwd, stream=True, timeout=(10, 30))
    except requests.RequestException:
        abort(502)
    if up.status_code >= 400:
        up.close()
        abort(502)
    out = {"Content-Type": up.headers.get("Content-Type", "video/mp4"),
           "Accept-Ranges": "bytes", "Cache-Control": "private, no-store",
           "Content-Disposition": "inline"}
    for h in ("Content-Length", "Content-Range"):
        if up.headers.get(h):
            out[h] = up.headers[h]

    def body():
        try:
            for chunk in up.iter_content(64 * 1024):
                if chunk:
                    yield chunk
        finally:
            up.close()

    return Response(stream_with_context(body()), status=up.status_code, headers=out)


# ------------------------------------------------------------------ accounts
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("username", "").strip().lower()
        pw = request.form.get("password", "")
        if throttled("reg:" + (request.remote_addr or ""), 8, 3600):
            flash("အကောင့်များစွာ ဖွင့်နေပါတယ်။ ခဏနေမှ ပြန်စမ်းပါ။", "err")
        elif not re.fullmatch(r"[a-z0-9_.-]{3,30}", name):
            flash("နာမည်က အင်္ဂလိပ်စာလုံး၊ ဂဏန်း၊ _ . - ၃ လုံးမှ ၃၀ အတွင်း ဖြစ်ရပါမယ်။", "err")
        elif len(pw) < 6:
            flash("Password က အနည်းဆုံး ၆ လုံး ရှိရပါမယ်။", "err")
        elif one("SELECT id FROM users WHERE username = ?", (name,)):
            flash("ဒီနာမည် သုံးပြီးသားပါ။", "err")
        else:
            uid = insert("INSERT INTO users (username, pw_hash, created_at) VALUES (?, ?, ?)",
                         (name, generate_password_hash(pw, "pbkdf2:sha256"), int(time.time())))
            session.clear()
            session["uid"] = uid
            session.permanent = True
            return redirect(url_for("home"))
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = request.values.get("next", "")
    if request.method == "POST":
        name = request.form.get("username", "").strip().lower()
        pw = request.form.get("password", "")
        if throttled("log:" + (request.remote_addr or "") + name, 6, 600):
            flash("မှားများလွန်းပါတယ်။ ၁၀ မိနစ်နေမှ ပြန်စမ်းပါ။", "err")
        else:
            u = one("SELECT * FROM users WHERE username = ?", (name,))
            if u and check_password_hash(u["pw_hash"], pw):
                session.clear()
                session["uid"] = u["id"]
                session.permanent = True
                return redirect(safe_next(nxt))
            flash("နာမည် သို့မဟုတ် password မှားနေပါတယ်။", "err")
    return render_template("login.html", nxt=nxt)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/account")
@login_required
def account():
    return render_template("account.html")


@app.route("/vip/request", methods=["POST"])
@login_required
def vip_request():
    note = request.form.get("note", "").strip()[:200]
    write("UPDATE users SET vip_requested = 1, vip_note = ? WHERE id = ?", (note, g.user["id"]))
    flash("တောင်းဆိုပြီးပါပြီ။ Owner ခွင့်ပြုချက် စောင့်ပါ။", "ok")
    return redirect(request.form.get("back") and safe_next(request.form["back"]) or url_for("account"))


# ------------------------------------------------------------------ admin
@app.route("/admin")
@admin_required
def admin():
    users = many("SELECT * FROM users ORDER BY vip_requested DESC, id DESC LIMIT 300")
    return render_template("admin.html", users=users,
                           pending=[u for u in users if u["vip_requested"]],
                           n_videos=one("SELECT COUNT(*) AS n FROM videos")["n"])


@app.route("/admin/user/<int:uid>/vip", methods=["POST"])
@admin_required
def admin_vip(uid):
    u = one("SELECT * FROM users WHERE id = ?", (uid,))
    if not u:
        abort(404)
    act = request.form.get("action", "")
    now = int(time.time())
    days = {"grant_30": 30, "grant_90": 90, "grant_365": 365}
    if act in days:
        base = u["vip_until"] if u["vip_until"] > now else now
        if u["vip_until"] == -1:
            new = -1
        else:
            new = base + days[act] * 86400
        write("UPDATE users SET vip_until = ?, vip_requested = 0 WHERE id = ?", (new, uid))
        flash(f"{u['username']} ကို VIP {days[act]} ရက် ပေးပြီးပါပြီ။", "ok")
    elif act == "grant_forever":
        write("UPDATE users SET vip_until = -1, vip_requested = 0 WHERE id = ?", (uid,))
        flash(f"{u['username']} ကို VIP အမြဲတမ်း ပေးပြီးပါပြီ။", "ok")
    elif act == "revoke":
        write("UPDATE users SET vip_until = 0, vip_requested = 0 WHERE id = ?", (uid,))
        flash(f"{u['username']} ရဲ့ VIP ကို ရုပ်သိမ်းပြီးပါပြီ။", "ok")
    elif act == "reject":
        write("UPDATE users SET vip_requested = 0 WHERE id = ?", (uid,))
        flash("တောင်းဆိုမှုကို ပယ်ပြီးပါပြီ။", "ok")
    return redirect(url_for("admin"))


def _video_form():
    f = request.form
    title = f.get("title", "").strip()[:120]
    src_type = f.get("src_type", "youtube")
    src = f.get("src", "").strip()
    err = None
    if not title:
        err = "ခေါင်းစဉ် ထည့်ပါ။"
    elif src_type == "youtube":
        src = youtube_id(src)
        if not src:
            err = "YouTube link သို့မဟုတ် video ID မှန်ကန်မှု မရှိပါ။"
    elif src_type == "url":
        p = urlparse(src)
        if p.scheme not in ("http", "https") or not p.netloc:
            err = "Video URL က http:// သို့မဟုတ် https:// နဲ့ စရပါမယ်။"
    else:
        err = "Source အမျိုးအစား မှားနေပါတယ်။"
    data = dict(title=title, description=f.get("description", "").strip()[:2000],
                category_id=f.get("category_id", type=int) or None,
                access="vip" if f.get("access") == "vip" else "free",
                src_type=src_type, src=src or "", thumb=f.get("thumb", "").strip()[:500],
                published=1 if f.get("published") else 0)
    return data, err


@app.route("/admin/videos")
@admin_required
def admin_videos():
    vids = many("SELECT v.*, c.name AS cat_name FROM videos v "
                "LEFT JOIN categories c ON c.id = v.category_id ORDER BY v.id DESC LIMIT 300")
    return render_template("admin_videos.html", videos=vids)


@app.route("/admin/videos/new", methods=["GET", "POST"])
@app.route("/admin/videos/<int:vid>", methods=["GET", "POST"])
@admin_required
def admin_video_edit(vid=None):
    v = one("SELECT * FROM videos WHERE id = ?", (vid,)) if vid else None
    if vid and not v:
        abort(404)
    cats = many("SELECT * FROM categories ORDER BY sort, name")
    if request.method == "POST":
        data, err = _video_form()
        if err:
            flash(err, "err")
            v = dict(data, id=vid)
        elif vid:
            write("UPDATE videos SET title=?, description=?, category_id=?, access=?, src_type=?, "
                  "src=?, thumb=?, published=? WHERE id=?",
                  (data["title"], data["description"], data["category_id"], data["access"],
                   data["src_type"], data["src"], data["thumb"], data["published"], vid))
            flash("သိမ်းပြီးပါပြီ။", "ok")
            return redirect(url_for("admin_videos"))
        else:
            insert("INSERT INTO videos (title, description, category_id, access, src_type, src, "
                   "thumb, published, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (data["title"], data["description"], data["category_id"], data["access"],
                    data["src_type"], data["src"], data["thumb"], data["published"],
                    int(time.time())))
            flash("ဗီဒီယို ထည့်ပြီးပါပြီ။", "ok")
            return redirect(url_for("admin_videos"))
    return render_template("admin_video_form.html", v=v, cats=cats)


@app.route("/admin/videos/<int:vid>/delete", methods=["POST"])
@admin_required
def admin_video_delete(vid):
    write("DELETE FROM videos WHERE id = ?", (vid,))
    flash("ဖျက်ပြီးပါပြီ။", "ok")
    return redirect(url_for("admin_videos"))


@app.route("/admin/categories", methods=["GET", "POST"])
@admin_required
def admin_categories():
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:40]
        if not name:
            flash("အမျိုးအစားနာမည် ထည့်ပါ။", "err")
        elif one("SELECT id FROM categories WHERE name = ?", (name,)):
            flash("ဒီနာမည် ရှိပြီးသားပါ။", "err")
        else:
            n = one("SELECT COALESCE(MAX(sort), 0) + 1 AS n FROM categories")["n"]
            write("INSERT INTO categories (name, sort) VALUES (?, ?)", (name, n))
            flash("ထည့်ပြီးပါပြီ။", "ok")
        return redirect(url_for("admin_categories"))
    return render_template("admin_cats.html", cats=many(
        "SELECT c.*, (SELECT COUNT(*) FROM videos v WHERE v.category_id = c.id) AS n "
        "FROM categories c ORDER BY sort, name"))


@app.route("/admin/categories/<int:cid>/delete", methods=["POST"])
@admin_required
def admin_category_delete(cid):
    write("UPDATE videos SET category_id = NULL WHERE category_id = ?", (cid,))
    write("DELETE FROM categories WHERE id = ?", (cid,))
    flash("ဖျက်ပြီးပါပြီ။", "ok")
    return redirect(url_for("admin_categories"))


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(502)
def error_page(e):
    msgs = {400: "တောင်းဆိုမှု မမှန်ပါ။ စာမျက်နှာကို refresh လုပ်ပြီး ပြန်စမ်းပါ။",
            403: "ဒီအကြောင်းအရာကို ကြည့်ခွင့် မရှိပါ။",
            404: "ရှာမတွေ့ပါ။",
            502: "ဗီဒီယိုရင်းမြစ်ကို ချိတ်ဆက်လို့ မရပါ။"}
    return render_template("error.html", msg=msgs.get(e.code, "အမှားတစ်ခု ဖြစ်သွားပါတယ်။")), e.code


# ------------------------------------------------------------------ templates
CSS = """
:root{--bg:#0d0e12;--panel:#16181f;--panel2:#1d2029;--line:#282c38;--fg:#ecebe6;--muted:#9b9ca8;
--vip:#e9b44c;--free:#5fc9a8;--bad:#e5646a;--accent:#e9b44c;color-scheme:dark}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 "Noto Sans Myanmar",system-ui,sans-serif}
a{color:inherit}h1,h2,h3{font-family:Sora,"Noto Sans Myanmar",system-ui,sans-serif;line-height:1.25;margin:0 0 .5em;text-wrap:balance}
.top{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:12px 16px;
border-bottom:1px solid var(--line);position:sticky;top:0;background:rgba(13,14,18,.94);z-index:5}
.brand{font:700 20px Sora,sans-serif;text-decoration:none;letter-spacing:.2px}
.brand b{color:var(--accent)}nav{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
nav a,nav button{font:inherit;font-size:14px;color:var(--fg);text-decoration:none;background:none;border:1px solid var(--line);
padding:6px 12px;border-radius:8px;cursor:pointer}nav a:hover,nav button:hover{border-color:var(--muted)}
.wrap{max-width:1100px;margin:0 auto;padding:20px 16px 48px}
.msg{padding:10px 14px;border-radius:8px;margin-bottom:14px;border:1px solid var(--line);background:var(--panel)}
.msg.ok{border-color:var(--free)}.msg.err{border-color:var(--bad)}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px}
.chip{font-size:14px;padding:5px 14px;border-radius:99px;border:1px solid var(--line);text-decoration:none;color:var(--muted)}
.chip.on{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.search{display:flex;gap:8px;margin:0 0 14px}.search input{flex:1;min-width:0}
input,select,textarea,button.btn,a.btn{font:inherit;font-size:16px}
input,select,textarea{background:var(--panel);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:10px 12px;width:100%}
input[type=checkbox]{width:auto}textarea{min-height:90px}
input:focus,select:focus,textarea:focus,a:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.btn{display:inline-block;background:var(--accent);color:#17130a;border:0;border-radius:8px;padding:10px 18px;font-weight:600;cursor:pointer;text-decoration:none}
.btn.ghost{background:none;color:var(--fg);border:1px solid var(--line)}.btn.bad{background:var(--bad);color:#fff}
.btn.sm{padding:6px 12px;font-size:14px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:16px}
.card{text-decoration:none;display:block;min-width:0}
.th{position:relative;aspect-ratio:16/9;border-radius:10px;overflow:hidden;background:linear-gradient(135deg,#222634,#12141a);border:1px solid var(--line)}
.th img{width:100%;height:100%;object-fit:cover;display:block}
.th .ph{position:absolute;inset:0;display:grid;place-items:center;font:700 34px Sora,sans-serif;color:#3a3f52}
.tag{position:absolute;top:8px;left:8px;font:700 11px Sora,sans-serif;letter-spacing:.8px;padding:3px 8px;border-radius:5px}
.tag.vip{background:var(--vip);color:#17130a}.tag.free{background:rgba(13,14,18,.78);color:var(--free);border:1px solid var(--free)}
.card.lock .th img{filter:brightness(.45)}
.card h3{font-size:15px;margin:8px 0 2px;font-family:"Noto Sans Myanmar",sans-serif;font-weight:600}
.meta{font-size:13px;color:var(--muted)}
.player{position:relative;aspect-ratio:16/9;background:#000;border-radius:10px;overflow:hidden;max-width:100%}
.player video,.player iframe{width:100%;height:100%;border:0;display:block;background:#000}
.player:fullscreen{border-radius:0;aspect-ratio:auto}
.wm{position:absolute;pointer-events:none;font:600 13px Sora,sans-serif;color:rgba(255,255,255,.32);text-shadow:0 0 3px rgba(0,0,0,.6);
animation:wm 40s linear infinite;z-index:2}
@keyframes wm{0%{left:4%;top:8%}25%{left:62%;top:20%}50%{left:30%;top:78%}75%{left:70%;top:62%}100%{left:4%;top:8%}}
.fs{position:absolute;right:8px;top:8px;z-index:3;background:rgba(0,0,0,.55);color:#fff;border:1px solid rgba(255,255,255,.3);
border-radius:6px;padding:4px 10px;font-size:13px;cursor:pointer}
.locked{border:1px solid var(--line);background:var(--panel);border-radius:12px;padding:24px;max-width:560px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:18px;max-width:640px}
.form{display:grid;gap:12px;max-width:520px}label{font-size:14px;color:var(--muted);display:block;margin-bottom:4px}
table{border-collapse:collapse;width:100%;min-width:560px}th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top;font-size:14px}
.tbl{overflow-x:auto;border:1px solid var(--line);border-radius:10px;margin-bottom:20px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.pill{font-size:12px;padding:2px 9px;border-radius:99px;border:1px solid var(--line);color:var(--muted)}
.pill.vip{border-color:var(--vip);color:var(--vip)}.pill.req{border-color:var(--bad);color:var(--bad)}
.sec{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin:26px 0 12px}
footer{border-top:1px solid var(--line);color:var(--muted);font-size:13px;padding:16px;text-align:center}
@media (prefers-reduced-motion:reduce){.wm{animation:none;left:4%;top:8%}}
"""

TEMPLATES = {
"macros.html": """
{% macro card(v, user_vip) %}
<a class="card {% if v.access == 'vip' and not user_vip %}lock{% endif %}" href="{{ url_for('watch', vid=v.id) }}">
  <div class="th">
    {% set t = thumb_of(v) %}
    {% if t %}<img src="{{ t }}" alt="" loading="lazy" referrerpolicy="no-referrer">{% else %}<div class="ph">{{ v.title[:1] }}</div>{% endif %}
    {% if v.access == 'vip' %}<span class="tag vip">VIP</span>{% else %}<span class="tag free">FREE</span>{% endif %}
  </div>
  <h3>{{ v.title }}</h3>
  <div class="meta">{{ v.cat_name or 'အခြား' }} · {{ v.views }} ကြည့်ပြီး</div>
</a>
{% endmacro %}
""",
"base.html": """<!doctype html>
<html lang="my"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{% block title %}{{ site_name }}{% endblock %}</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Sans+Myanmar:wght@400;600&family=Sora:wght@600;700&display=swap">
<style>""" + CSS + """</style></head><body>
<header class="top">
  <a class="brand" href="{{ url_for('home') }}">{{ site_name }}</a>
  <nav>
    {% if g.user %}
      <a href="{{ url_for('account') }}">{{ g.user.username }}</a>
      {% if g.user.is_admin %}<a href="{{ url_for('admin') }}">စီမံခန့်ခွဲရန်</a>{% endif %}
      <form method="post" action="{{ url_for('logout') }}" style="margin:0">
        <input type="hidden" name="csrf" value="{{ csrf() }}"><button>ထွက်ရန်</button></form>
    {% else %}
      <a href="{{ url_for('login') }}">ဝင်ရောက်ရန်</a>
      <a href="{{ url_for('register') }}">အကောင့်ဖွင့်ရန်</a>
    {% endif %}
  </nav>
</header>
<main class="wrap">
  {% for cat, m in get_flashed_messages(with_categories=true) %}<div class="msg {{ cat }}">{{ m }}</div>{% endfor %}
  {% block body %}{% endblock %}
</main>
<footer>{{ site_name }}</footer>
</body></html>""",
"home.html": """{% extends "base.html" %}{% from "macros.html" import card %}
{% block body %}
<form class="search" method="get" action="{{ url_for('home') }}">
  <input type="search" name="q" value="{{ q }}" placeholder="ဗီဒီယို ရှာရန်">
  {% if cat %}<input type="hidden" name="cat" value="{{ cat }}">{% endif %}
  {% if acc %}<input type="hidden" name="a" value="{{ acc }}">{% endif %}
  <button class="btn">ရှာရန်</button>
</form>
<div class="chips">
  <a class="chip {% if not cat %}on{% endif %}" href="{{ url_for('home', a=acc or None, q=q or None) }}">အားလုံး</a>
  {% for c in cats %}<a class="chip {% if cat == c.id %}on{% endif %}" href="{{ url_for('home', cat=c.id, a=acc or None, q=q or None) }}">{{ c.name }}</a>{% endfor %}
</div>
<div class="chips">
  <a class="chip {% if not acc %}on{% endif %}" href="{{ url_for('home', cat=cat, q=q or None) }}">Free + VIP</a>
  <a class="chip {% if acc == 'free' %}on{% endif %}" href="{{ url_for('home', cat=cat, a='free', q=q or None) }}">အခမဲ့</a>
  <a class="chip {% if acc == 'vip' %}on{% endif %}" href="{{ url_for('home', cat=cat, a='vip', q=q or None) }}">VIP</a>
</div>
{% if videos %}
<div class="grid">{% for v in videos %}{{ card(v, vip_active(g.user)) }}{% endfor %}</div>
{% else %}
<div class="panel"><h2>ဗီဒီယို မရှိသေးပါ</h2>
<p class="meta">ရွေးထားတဲ့ အမျိုးအစားထဲမှာ ဗီဒီယို မရှိသေးပါ။{% if g.user and g.user.is_admin %} <a href="{{ url_for('admin_video_edit') }}">ပထမဆုံး ဗီဒီယို ထည့်ရန်</a>{% endif %}</p></div>
{% endif %}
{% endblock %}""",
"watch.html": """{% extends "base.html" %}{% from "macros.html" import card %}
{% block title %}{{ v.title }} - {{ site_name }}{% endblock %}
{% block body %}
{% if allowed %}
  <div class="player" id="pl">
    {% if embed %}
      <iframe src="{{ embed }}" allow="accelerometer; encrypted-media; gyroscope; picture-in-picture" allowfullscreen referrerpolicy="strict-origin-when-cross-origin"></iframe>
    {% else %}
      <video id="vd" src="{{ url_for('stream', vid=v.id) }}" controls playsinline preload="metadata"
        controlsList="nodownload nofullscreen noremoteplayback" disablePictureInPicture oncontextmenu="return false"></video>
      <button class="fs" type="button" id="fs">မျက်နှာပြင်အပြည့်</button>
    {% endif %}
    {% if v.access == 'vip' and g.user %}<div class="wm">{{ g.user.username }}</div>{% endif %}
  </div>
  <script>
  (function(){var b=document.getElementById('fs');if(!b)return;var p=document.getElementById('pl'),v=document.getElementById('vd');
  b.onclick=function(){if(document.fullscreenElement){document.exitFullscreen();}
  else if(p.requestFullscreen){p.requestFullscreen();}else if(v.webkitEnterFullscreen){v.webkitEnterFullscreen();}};})();
  </script>
{% else %}
  <div class="locked">
    <span class="pill vip">VIP</span>
    <h2 style="margin-top:10px">{{ v.title }}</h2>
    <p>ဒီဗီဒီယိုက VIP အဖွဲ့ဝင်တွေအတွက် ဖြစ်ပါတယ်။</p>
    {% if not g.user %}
      <div class="row"><a class="btn" href="{{ url_for('login', next=request.path) }}">ဝင်ရောက်ရန်</a>
      <a class="btn ghost" href="{{ url_for('register') }}">အကောင့်ဖွင့်ရန်</a></div>
    {% elif g.user.vip_requested %}
      <p class="msg ok">VIP တောင်းဆိုထားပါတယ်။ Owner ခွင့်ပြုတာကို စောင့်ပါ။</p>
    {% else %}
      <form class="form" method="post" action="{{ url_for('vip_request') }}">
        <input type="hidden" name="csrf" value="{{ csrf() }}"><input type="hidden" name="back" value="{{ request.path }}">
        <div><label for="note">ဆက်သွယ်ရန် (ဥပမာ Telegram username)</label><input id="note" name="note" maxlength="200"></div>
        <button class="btn">VIP ခွင့်ပြုချက် တောင်းရန်</button>
      </form>
    {% endif %}
  </div>
{% endif %}
<h1 style="margin-top:18px">{{ v.title }}</h1>
<p class="meta">{{ v.cat_name or 'အခြား' }} · {{ v.views }} ကြည့်ပြီး · {{ v.created_at|date }}</p>
{% if v.description %}<p style="white-space:pre-line;max-width:70ch">{{ v.description }}</p>{% endif %}
{% if related %}<div class="sec"><h2 style="margin:0">ဆက်ကြည့်ရန်</h2></div>
<div class="grid">{% for r in related %}{{ card(r, vip_active(g.user)) }}{% endfor %}</div>{% endif %}
{% endblock %}""",
"login.html": """{% extends "base.html" %}{% block body %}
<h1>ဝင်ရောက်ရန်</h1>
<form class="form" method="post"><input type="hidden" name="csrf" value="{{ csrf() }}">
<input type="hidden" name="next" value="{{ nxt }}">
<div><label for="u">နာမည်</label><input id="u" name="username" autocomplete="username" required autofocus></div>
<div><label for="p">Password</label><input id="p" name="password" type="password" autocomplete="current-password" required></div>
<button class="btn">ဝင်ရောက်ရန်</button>
<p class="meta">အကောင့်မရှိဘူးလား? <a href="{{ url_for('register') }}">အကောင့်ဖွင့်ရန်</a></p></form>
{% endblock %}""",
"register.html": """{% extends "base.html" %}{% block body %}
<h1>အကောင့်ဖွင့်ရန်</h1>
<form class="form" method="post"><input type="hidden" name="csrf" value="{{ csrf() }}">
<div><label for="u">နာမည် (အင်္ဂလိပ်စာလုံး၊ ဂဏန်း)</label><input id="u" name="username" autocomplete="username" required minlength="3" maxlength="30" autofocus></div>
<div><label for="p">Password (အနည်းဆုံး ၆ လုံး)</label><input id="p" name="password" type="password" autocomplete="new-password" required minlength="6"></div>
<button class="btn">အကောင့်ဖွင့်ရန်</button>
<p class="meta">အကောင့်ရှိပြီးသားလား? <a href="{{ url_for('login') }}">ဝင်ရောက်ရန်</a></p></form>
{% endblock %}""",
"account.html": """{% extends "base.html" %}{% block body %}
<h1>{{ g.user.username }}</h1>
<div class="panel"><div class="meta">အခြေအနေ</div>
<h2>{{ vip_label(g.user) }}</h2>
{% if g.user.is_admin %}<p class="meta">သင်က Owner ဖြစ်လို့ ဗီဒီယိုအားလုံး ကြည့်လို့ရပါတယ်။</p>
{% elif vip_active(g.user) %}<p class="meta">VIP ဗီဒီယိုတွေ အားလုံး ကြည့်လို့ရပါတယ်။</p>
{% elif g.user.vip_requested %}<p class="msg ok">VIP တောင်းဆိုထားပါတယ်။ Owner ခွင့်ပြုတာကို စောင့်ပါ။</p>
{% else %}
<form class="form" method="post" action="{{ url_for('vip_request') }}"><input type="hidden" name="csrf" value="{{ csrf() }}">
<div><label for="note">ဆက်သွယ်ရန် (ဥပမာ Telegram username)</label><input id="note" name="note" maxlength="200"></div>
<button class="btn">VIP ခွင့်ပြုချက် တောင်းရန်</button></form>
{% endif %}</div>
{% endblock %}""",
"admin.html": """{% extends "base.html" %}{% block body %}
<h1>စီမံခန့်ခွဲရန်</h1>
<div class="row" style="margin-bottom:20px">
  <a class="btn" href="{{ url_for('admin_video_edit') }}">ဗီဒီယိုအသစ် ထည့်ရန်</a>
  <a class="btn ghost" href="{{ url_for('admin_videos') }}">ဗီဒီယို {{ n_videos }} ခု</a>
  <a class="btn ghost" href="{{ url_for('admin_categories') }}">အမျိုးအစားများ</a>
</div>
{% macro actions(u) %}
<form method="post" action="{{ url_for('admin_vip', uid=u.id) }}" class="row"><input type="hidden" name="csrf" value="{{ csrf() }}">
  <button class="btn sm" name="action" value="grant_30">၃၀ ရက်</button>
  <button class="btn sm" name="action" value="grant_90">၉၀ ရက်</button>
  <button class="btn sm" name="action" value="grant_365">၁ နှစ်</button>
  <button class="btn sm" name="action" value="grant_forever">အမြဲတမ်း</button>
  {% if u.vip_requested %}<button class="btn sm ghost" name="action" value="reject">ပယ်ရန်</button>{% endif %}
  {% if u.vip_until != 0 %}<button class="btn sm bad" name="action" value="revoke">ရုပ်သိမ်းရန်</button>{% endif %}
</form>
{% endmacro %}
<div class="sec"><h2 style="margin:0">VIP တောင်းဆိုမှုများ ({{ pending|length }})</h2></div>
{% if pending %}
<div class="tbl"><table><tr><th>User</th><th>ဆက်သွယ်ရန်</th><th>ခွင့်ပြုရန်</th></tr>
{% for u in pending %}<tr><td>{{ u.username }}</td><td>{{ u.vip_note or '-' }}</td><td>{{ actions(u) }}</td></tr>{% endfor %}</table></div>
{% else %}<p class="meta">တောင်းဆိုမှု မရှိပါ။</p>{% endif %}
<div class="sec"><h2 style="margin:0">Users အားလုံး ({{ users|length }})</h2></div>
<div class="tbl"><table><tr><th>User</th><th>အခြေအနေ</th><th>စတင်ရက်</th><th>VIP စီမံရန်</th></tr>
{% for u in users %}<tr><td>{{ u.username }}{% if u.vip_requested %} <span class="pill req">တောင်းဆိုထား</span>{% endif %}</td>
<td>{% if u.is_admin %}<span class="pill vip">Owner</span>{% else %}<span class="pill {% if vip_active(u) %}vip{% endif %}">{{ vip_label(u) }}</span>{% endif %}</td>
<td>{{ u.created_at|date }}</td><td>{% if not u.is_admin %}{{ actions(u) }}{% endif %}</td></tr>{% endfor %}</table></div>
{% endblock %}""",
"admin_videos.html": """{% extends "base.html" %}{% block body %}
<div class="sec"><h1 style="margin:0">ဗီဒီယိုများ</h1><a class="btn" href="{{ url_for('admin_video_edit') }}">ဗီဒီယိုအသစ်</a></div>
<p><a href="{{ url_for('admin') }}">စီမံခန့်ခွဲရန်သို့ ပြန်သွားရန်</a></p>
{% if videos %}<div class="tbl"><table><tr><th>ခေါင်းစဉ်</th><th>အမျိုးအစား</th><th>Access</th><th>ကြည့်</th><th></th></tr>
{% for v in videos %}<tr><td><a href="{{ url_for('watch', vid=v.id) }}">{{ v.title }}</a>{% if not v.published %} <span class="pill">ဖျောက်ထား</span>{% endif %}</td>
<td>{{ v.cat_name or '-' }}</td><td><span class="pill {% if v.access == 'vip' %}vip{% endif %}">{{ 'VIP' if v.access == 'vip' else 'FREE' }}</span></td><td>{{ v.views }}</td>
<td class="row"><a class="btn sm ghost" href="{{ url_for('admin_video_edit', vid=v.id) }}">ပြင်ရန်</a>
<form method="post" action="{{ url_for('admin_video_delete', vid=v.id) }}" onsubmit="return confirm('ဖျက်မလား?')" style="margin:0">
<input type="hidden" name="csrf" value="{{ csrf() }}"><button class="btn sm bad">ဖျက်ရန်</button></form></td></tr>{% endfor %}</table></div>
{% else %}<p class="meta">ဗီဒီယို မရှိသေးပါ။</p>{% endif %}
{% endblock %}""",
"admin_video_form.html": """{% extends "base.html" %}{% block body %}
<h1>{{ 'ဗီဒီယို ပြင်ရန်' if v and v.id else 'ဗီဒီယိုအသစ်' }}</h1>
<form class="form" method="post"><input type="hidden" name="csrf" value="{{ csrf() }}">
<div><label for="title">ခေါင်းစဉ်</label><input id="title" name="title" required maxlength="120" value="{{ v.title if v else '' }}"></div>
<div><label for="description">ဖော်ပြချက်</label><textarea id="description" name="description" maxlength="2000">{{ v.description if v else '' }}</textarea></div>
<div><label for="category_id">အမျိုးအစား</label><select id="category_id" name="category_id"><option value="">- မရွေးပါ -</option>
{% for c in cats %}<option value="{{ c.id }}" {% if v and v.category_id == c.id %}selected{% endif %}>{{ c.name }}</option>{% endfor %}</select></div>
<div><label for="access">ဘယ်သူ ကြည့်လို့ရမလဲ</label><select id="access" name="access">
<option value="free" {% if v and v.access == 'free' %}selected{% endif %}>အားလုံး (Free)</option>
<option value="vip" {% if v and v.access == 'vip' %}selected{% endif %}>VIP သာ</option></select></div>
<div><label for="src_type">ဗီဒီယို ရင်းမြစ်</label><select id="src_type" name="src_type">
<option value="youtube" {% if not v or v.src_type == 'youtube' %}selected{% endif %}>YouTube (unlisted)</option>
<option value="url" {% if v and v.src_type == 'url' %}selected{% endif %}>mp4 link (တိုက်ရိုက် ဖိုင်လင့်ခ်)</option></select></div>
<div><label for="src">YouTube link / video ID သို့မဟုတ် mp4 URL</label>
<input id="src" name="src" required value="{{ v.src if v else '' }}" placeholder="https://youtu.be/xxxxxxxxxxx"></div>
<div><label for="thumb">ပုံငယ် (thumbnail) URL - YouTube အတွက် မလိုပါ</label><input id="thumb" name="thumb" value="{{ v.thumb if v else '' }}"></div>
<div><label><input type="checkbox" name="published" value="1" {% if not v or v.published %}checked{% endif %}> ပြသမယ် (ဖျောက်ချင်ရင် မှန်ကွက်ဖြုတ်ပါ)</label></div>
<div class="row"><button class="btn">သိမ်းရန်</button><a class="btn ghost" href="{{ url_for('admin_videos') }}">မလုပ်တော့ပါ</a></div>
</form>{% endblock %}""",
"admin_cats.html": """{% extends "base.html" %}{% block body %}
<h1>အမျိုးအစားများ</h1><p><a href="{{ url_for('admin') }}">စီမံခန့်ခွဲရန်သို့ ပြန်သွားရန်</a></p>
<form class="form" method="post" style="margin-bottom:20px"><input type="hidden" name="csrf" value="{{ csrf() }}">
<div><label for="name">အမျိုးအစားအသစ်</label><input id="name" name="name" required maxlength="40"></div><button class="btn">ထည့်ရန်</button></form>
<div class="tbl"><table><tr><th>နာမည်</th><th>ဗီဒီယို</th><th></th></tr>
{% for c in cats %}<tr><td>{{ c.name }}</td><td>{{ c.n }}</td><td>
<form method="post" action="{{ url_for('admin_category_delete', cid=c.id) }}" onsubmit="return confirm('ဖျက်မလား? ဗီဒီယိုတွေက အမျိုးအစားမဲ့ ဖြစ်သွားမယ်။')" style="margin:0">
<input type="hidden" name="csrf" value="{{ csrf() }}"><button class="btn sm bad">ဖျက်ရန်</button></form></td></tr>{% endfor %}</table></div>
{% endblock %}""",
"error.html": """{% extends "base.html" %}{% block body %}
<div class="panel"><h2>{{ msg }}</h2><p><a class="btn" href="{{ url_for('home') }}">ပင်မစာမျက်နှာ</a></p></div>{% endblock %}""",
}
app.jinja_env.loader = DictLoader(TEMPLATES)

init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), threaded=True)
