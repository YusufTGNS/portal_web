import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_socketio import SocketIO, emit, join_room
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
APP_ENV = os.getenv("APP_ENV", "development").strip().lower()
IS_PROD = APP_ENV == "production"
default_db_path = str(BASE_DIR / "data.db")
DB_PATH = Path(os.getenv("DB_PATH", default_db_path))
DEFAULT_ADMIN_USERNAME = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.getenv("DEFAULT_ADMIN_PASSWORD")
DEFAULT_USER_INITIAL_PASSWORD = os.getenv("DEFAULT_USER_INITIAL_PASSWORD")
LOCK_MINUTES = 5
MAX_FAILED_ATTEMPTS = 5
MAX_IP_ATTEMPTS_PER_MINUTE = 25
ALLOWED_ATTACHMENT_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "pdf", "txt", "doc", "docx"}
MESSAGE_ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024
DELETED_MESSAGE_TEXT = "Mesaj silindi"
REMEMBER_ME_DAYS = int(os.getenv("REMEMBER_ME_DAYS", "30"))

ip_attempt_store = {}
ip_attempt_lock = threading.Lock()
default_upload_dir = str(BASE_DIR / "uploads" / "messages")
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", default_upload_dir))
online_user_sids = {}
sid_to_user = {}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("APP_SECRET_KEY", "CHANGE_ME_FOR_PRODUCTION")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = IS_PROD
app.config["MAX_CONTENT_LENGTH"] = MESSAGE_ATTACHMENT_MAX_BYTES + (1024 * 1024)
app.config["PREFERRED_URL_SCHEME"] = "https" if IS_PROD else "http"
app.permanent_session_lifetime = timedelta(days=REMEMBER_ME_DAYS)
socketio = SocketIO(app, cors_allowed_origins="*")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
            must_change_password INTEGER NOT NULL DEFAULT 0,
            failed_attempts INTEGER NOT NULL DEFAULT 0,
            lock_until TEXT NULL,
            created_at TEXT NOT NULL,
            last_login TEXT NULL
        );

        CREATE TABLE IF NOT EXISTS login_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NULL,
            username TEXT NOT NULL,
            ip_address TEXT NOT NULL,
            user_agent TEXT NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS click_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            portal_item_id INTEGER NULL,
            target_name TEXT NOT NULL,
            target_url TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS admin_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER NOT NULL,
            admin_username TEXT NOT NULL,
            action_type TEXT NOT NULL,
            detail TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS portal_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            preview TEXT NOT NULL,
            redirect_url TEXT NOT NULL,
            image_url TEXT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_by_admin_id INTEGER NOT NULL,
            created_by_admin_username TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS portal_widgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            icon TEXT NOT NULL DEFAULT '✨',
            accent_color TEXT NOT NULL DEFAULT '#6366f1',
            widget_type TEXT NOT NULL DEFAULT 'static',
            api_url TEXT NULL,
            link_url TEXT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_by_user_id INTEGER NOT NULL,
            created_by_username TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            rating INTEGER NOT NULL,
            category TEXT NOT NULL,
            message TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER NOT NULL,
            sender_username TEXT NOT NULL,
            recipient_id INTEGER NOT NULL,
            recipient_username TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            attachment_original_name TEXT NULL,
            attachment_stored_name TEXT NULL,
            attachment_mime TEXT NULL,
            attachment_size INTEGER NULL,
            is_read INTEGER NOT NULL DEFAULT 0,
            deleted_by_sender INTEGER NOT NULL DEFAULT 0,
            deleted_by_recipient INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_blocks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            blocker_id INTEGER NOT NULL,
            blocked_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chat_rooms (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            created_by_user_id INTEGER NOT NULL,
            created_by_username TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS room_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL,
            joined_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS room_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            room_id INTEGER NOT NULL,
            sender_id INTEGER NOT NULL,
            sender_username TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    click_cols = db.execute("PRAGMA table_info(click_logs)").fetchall()
    click_col_names = {col["name"] for col in click_cols}
    if "portal_item_id" not in click_col_names:
        db.execute("ALTER TABLE click_logs ADD COLUMN portal_item_id INTEGER NULL")

    message_cols = db.execute("PRAGMA table_info(user_messages)").fetchall()
    message_col_names = {col["name"] for col in message_cols}
    if "attachment_original_name" not in message_col_names:
        db.execute("ALTER TABLE user_messages ADD COLUMN attachment_original_name TEXT NULL")
    if "attachment_stored_name" not in message_col_names:
        db.execute("ALTER TABLE user_messages ADD COLUMN attachment_stored_name TEXT NULL")
    if "attachment_mime" not in message_col_names:
        db.execute("ALTER TABLE user_messages ADD COLUMN attachment_mime TEXT NULL")
    if "attachment_size" not in message_col_names:
        db.execute("ALTER TABLE user_messages ADD COLUMN attachment_size INTEGER NULL")
    if "deleted_by_sender" not in message_col_names:
        db.execute("ALTER TABLE user_messages ADD COLUMN deleted_by_sender INTEGER NOT NULL DEFAULT 0")
    if "deleted_by_recipient" not in message_col_names:
        db.execute("ALTER TABLE user_messages ADD COLUMN deleted_by_recipient INTEGER NOT NULL DEFAULT 0")

    db.execute("CREATE INDEX IF NOT EXISTS idx_room_members_room_user ON room_members(room_id, user_id)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_user_blocks_pair ON user_blocks(blocker_id, blocked_id)")

    widget_cols = db.execute("PRAGMA table_info(portal_widgets)").fetchall()
    widget_col_names = {col["name"] for col in widget_cols}
    if "widget_type" not in widget_col_names:
        db.execute("ALTER TABLE portal_widgets ADD COLUMN widget_type TEXT NOT NULL DEFAULT 'static'")
    if "api_url" not in widget_col_names:
        db.execute("ALTER TABLE portal_widgets ADD COLUMN api_url TEXT NULL")
    db.commit()


def ensure_default_admin():
    if not DEFAULT_ADMIN_PASSWORD:
        raise RuntimeError("DEFAULT_ADMIN_PASSWORD tanimli degil. Lutfen .env dosyasinda ayarlayin.")
    if not DEFAULT_USER_INITIAL_PASSWORD:
        raise RuntimeError("DEFAULT_USER_INITIAL_PASSWORD tanimli degil. Lutfen .env dosyasinda ayarlayin.")

    db = get_db()
    exists = db.execute(
        "SELECT id FROM users WHERE username = ? LIMIT 1",
        (DEFAULT_ADMIN_USERNAME,),
    ).fetchone()
    if exists:
        db.execute("UPDATE users SET must_change_password = 0 WHERE id = ?", (exists["id"],))
        db.commit()
        return

    db.execute(
        """
        INSERT INTO users (username, password_hash, role, must_change_password, created_at)
        VALUES (?, ?, 'admin', 0, ?)
        """,
        (
            DEFAULT_ADMIN_USERNAME,
            generate_password_hash(DEFAULT_ADMIN_PASSWORD),
            utcnow_iso(),
        ),
    )
    db.commit()


def ensure_default_portal_items():
    db = get_db()
    count = db.execute("SELECT COUNT(*) AS c FROM portal_items").fetchone()["c"]
    if count > 0:
        return

    admin = db.execute("SELECT id, username FROM users WHERE role = 'admin' ORDER BY id ASC LIMIT 1").fetchone()
    if not admin:
        return

    defaults = [
        ("Flask Resmi Dokumantasyon", "Python tabanli web uygulamalari icin resmi Flask kaynagi.", "https://flask.palletsprojects.com/", ""),
        ("OWASP Top 10", "Web guvenligi riskleri ve korunma yontemleri.", "https://owasp.org/www-project-top-ten/", ""),
        ("MDN Web Docs", "HTML, CSS ve JavaScript rehberleri.", "https://developer.mozilla.org/", ""),
    ]
    for title, preview, url, image in defaults:
        db.execute(
            """
            INSERT INTO portal_items (title, preview, redirect_url, image_url, is_active, created_by_admin_id, created_by_admin_username, created_at)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (title, preview, url, image, admin["id"], admin["username"], utcnow_iso()),
        )
    db.commit()


def ensure_default_portal_widgets():
    db = get_db()
    count = db.execute("SELECT COUNT(*) AS c FROM portal_widgets").fetchone()["c"]
    if count > 0:
        return
    owner = db.execute("SELECT id, username FROM users ORDER BY id ASC LIMIT 1").fetchone()
    if not owner:
        return
    defaults = [
        ("Hızlı Erişim", "Sık kullanılan portal bağlantılarına tek tıkla erişin.", "🚀", "#06b6d4", ""),
        ("Duyuru Alanı", "Sistem ve ekip duyurularını burada takip edin.", "📢", "#8b5cf6", ""),
        ("Destek Merkezi", "Sorun bildirimi ve yardım süreçlerini başlatın.", "🛠️", "#22c55e", ""),
    ]
    for title, body, icon, color, link in defaults:
        db.execute(
            """
            INSERT INTO portal_widgets (title, body, icon, accent_color, widget_type, api_url, link_url, is_active, created_by_user_id, created_by_username, created_at)
            VALUES (?, ?, ?, ?, 'static', NULL, ?, 1, ?, ?, ?)
            """,
            (title, body, icon, color, link, owner["id"], owner["username"], utcnow_iso()),
        )
    db.commit()


def ensure_default_chat_rooms():
    db = get_db()
    admin = db.execute(
        "SELECT id, username FROM users WHERE role = 'admin' ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if not admin:
        return

    defaults = ["Genel Sohbet", "Teknik Yardim", "Duyurular"]
    for room_name in defaults:
        exists = db.execute("SELECT id FROM chat_rooms WHERE name = ? LIMIT 1", (room_name,)).fetchone()
        if not exists:
            db.execute(
                """
                INSERT INTO chat_rooms (name, created_by_user_id, created_by_username, is_active, created_at)
                VALUES (?, ?, ?, 1, ?)
                """,
                (room_name, admin["id"], admin["username"], utcnow_iso()),
            )
    db.commit()


def utcnow_iso():
    return datetime.utcnow().isoformat(timespec="seconds")


def parse_iso(value):
    if not value:
        return None
    return datetime.fromisoformat(value)


def allowed_attachment(filename):
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_ATTACHMENT_EXTENSIONS


def is_blocked_between(sender_id, recipient_id):
    block_row = get_db().execute(
        """
        SELECT id
        FROM user_blocks
        WHERE (blocker_id = ? AND blocked_id = ?)
           OR (blocker_id = ? AND blocked_id = ?)
        LIMIT 1
        """,
        (sender_id, recipient_id, recipient_id, sender_id),
    ).fetchone()
    return block_row is not None


def get_unread_count(user_id):
    row = get_db().execute(
        """
        SELECT COUNT(*) AS c
        FROM user_messages
        WHERE recipient_id = ?
          AND is_read = 0
          AND deleted_by_recipient = 0
        """,
        (user_id,),
    ).fetchone()
    return row["c"]


def active_online_user_ids():
    return sorted(int(uid) for uid in online_user_sids.keys())


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def login_required(role=None):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user:
                flash("Bu sayfaya erişmek için giriş yapmalısınız.", "error")
                return redirect(url_for("login"))

            if role and user["role"] != role:
                abort(403)
            return view(*args, **kwargs)

        return wrapped

    return decorator


def generate_csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def verify_csrf():
    form_token = request.form.get("csrf_token", "")
    if not form_token or form_token != session.get("csrf_token"):
        abort(400, "Geçersiz CSRF token.")


def ip_rate_limited(ip):
    now = time.time()
    with ip_attempt_lock:
        bucket = ip_attempt_store.get(ip, [])
        bucket = [t for t in bucket if now - t < 60]
        limited = len(bucket) >= MAX_IP_ATTEMPTS_PER_MINUTE
        bucket.append(now)
        ip_attempt_store[ip] = bucket
        return limited


def client_ip():
    xf = request.headers.get("X-Forwarded-For")
    if xf:
        return xf.split(",")[0].strip()
    return request.remote_addr or "unknown"


def add_login_log(username, status, reason, user_id=None):
    get_db().execute(
        """
        INSERT INTO login_logs (user_id, username, ip_address, user_agent, status, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            username,
            client_ip(),
            request.headers.get("User-Agent", "unknown")[:250],
            status,
            reason,
            utcnow_iso(),
        ),
    )
    get_db().commit()


def add_click_log(user, target):
    get_db().execute(
        """
        INSERT INTO click_logs (user_id, username, portal_item_id, target_name, target_url, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            user["id"],
            user["username"],
            target["id"],
            target["title"],
            target["redirect_url"],
            utcnow_iso(),
        ),
    )
    get_db().commit()


def add_admin_action(user, action_type, detail):
    get_db().execute(
        """
        INSERT INTO admin_actions (admin_id, admin_username, action_type, detail, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (user["id"], user["username"], action_type, detail, utcnow_iso()),
    )
    get_db().commit()


@app.context_processor
def inject_shared_context():
    user = current_user()
    return {
        "csrf_token": generate_csrf_token(),
        "current_user": user,
    }


@app.route("/")
def home():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    if user["must_change_password"]:
        return redirect(url_for("set_password"))
    if user["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("portal"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", remember_me=False)

    verify_csrf()
    ip = client_ip()
    remember_me = request.form.get("remember_me") == "1"
    if ip_rate_limited(ip):
        flash("Çok fazla deneme yaptınız. 1 dakika sonra tekrar deneyin.", "error")
        return render_template("login.html", remember_me=remember_me), 429

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    user = get_db().execute(
        "SELECT * FROM users WHERE username = ? LIMIT 1",
        (username,),
    ).fetchone()

    if not user:
        flash("Kullanıcı adı veya şifre hatalı.", "error")
        add_login_log(username or "unknown", "failed", "user-not-found")
        return render_template("login.html", remember_me=remember_me), 401

    lock_until = parse_iso(user["lock_until"])
    if lock_until and lock_until > datetime.utcnow():
        flash("Hesap geçici olarak kilitlendi. Lütfen daha sonra deneyin.", "error")
        add_login_log(username, "blocked", "account-locked", user["id"])
        return render_template("login.html", remember_me=remember_me), 423

    if not check_password_hash(user["password_hash"], password):
        failed = user["failed_attempts"] + 1
        lock_str = None
        if failed >= MAX_FAILED_ATTEMPTS:
            lock_str = (datetime.utcnow() + timedelta(minutes=LOCK_MINUTES)).isoformat(timespec="seconds")
            failed = 0
        get_db().execute(
            "UPDATE users SET failed_attempts = ?, lock_until = ? WHERE id = ?",
            (failed, lock_str, user["id"]),
        )
        get_db().commit()
        add_login_log(username, "failed", "wrong-password", user["id"])
        flash("Kullanıcı adı veya şifre hatalı.", "error")
        return render_template("login.html", remember_me=remember_me), 401

    get_db().execute(
        "UPDATE users SET failed_attempts = 0, lock_until = NULL, last_login = ? WHERE id = ?",
        (utcnow_iso(), user["id"]),
    )
    get_db().commit()
    add_login_log(username, "success", "login-ok", user["id"])

    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["role"] = user["role"]
    session.permanent = remember_me

    if user["must_change_password"]:
        return redirect(url_for("set_password"))
    if user["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("portal"))


@app.route("/logout", methods=["POST"])
def logout():
    verify_csrf()
    session.clear()
    flash("Çıkış yapıldı.", "ok")
    return redirect(url_for("login"))


@app.route("/set-password", methods=["GET", "POST"])
@login_required()
def set_password():
    user = current_user()
    if request.method == "GET":
        return render_template("set_password.html")

    verify_csrf()
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not check_password_hash(user["password_hash"], current_password):
        flash("Mevcut şifre doğrulanamadı.", "error")
        return render_template("set_password.html"), 400

    if new_password != confirm_password:
        flash("Yeni şifreler aynı değil.", "error")
        return render_template("set_password.html"), 400

    get_db().execute(
        "UPDATE users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
        (generate_password_hash(new_password), user["id"]),
    )
    get_db().commit()

    flash("Şifreniz güncellendi.", "ok")
    if user["role"] == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("portal"))


@app.route("/portal")
@login_required()
def portal():
    links = get_db().execute(
        """
        SELECT id, title, preview, redirect_url, image_url
        FROM portal_items
        WHERE is_active = 1
        ORDER BY id DESC
        """
    ).fetchall()
    widgets = get_db().execute(
        """
        SELECT id, title, body, icon, accent_color, link_url
        FROM portal_widgets
        WHERE is_active = 1
        ORDER BY id DESC
        """
    ).fetchall()
    return render_template("portal.html", links=links, widgets=widgets)


@app.route("/portal/widgets/add", methods=["POST"])
@login_required(role="admin")
def portal_add_widget():
    verify_csrf()
    admin = current_user()
    title = request.form.get("title", "").strip()
    body = request.form.get("body", "").strip()
    icon = request.form.get("icon", "").strip() or "✨"
    accent_color = request.form.get("accent_color", "").strip() or "#6366f1"
    link_url = request.form.get("link_url", "").strip()

    if len(title) < 2:
        flash("Widget başlığı en az 2 karakter olmalı.", "error")
        return redirect(url_for("portal"))
    if len(body) < 4:
        flash("Widget içeriği en az 4 karakter olmalı.", "error")
        return redirect(url_for("portal"))
    if len(icon) > 3:
        icon = "✨"
    if not accent_color.startswith("#") or len(accent_color) not in (4, 7):
        accent_color = "#6366f1"
    if link_url:
        parsed = urlparse(link_url)
        if parsed.scheme not in ("http", "https"):
            flash("Widget linki http/https olmalı.", "error")
            return redirect(url_for("portal"))

    get_db().execute(
        """
        INSERT INTO portal_widgets (title, body, icon, accent_color, widget_type, api_url, link_url, is_active, created_by_user_id, created_by_username, created_at)
        VALUES (?, ?, ?, ?, 'static', NULL, ?, 1, ?, ?, ?)
        """,
        (
            title,
            body,
            icon,
            accent_color,
            link_url,
            admin["id"],
            admin["username"],
            utcnow_iso(),
        ),
    )
    get_db().commit()
    add_admin_action(admin, "add-widget", f"Portal widget eklendi: {title}")
    flash("Widget eklendi.", "ok")
    return redirect(url_for("portal"))


@app.route("/portal/widgets/<int:widget_id>/delete", methods=["POST"])
@login_required(role="admin")
def portal_delete_widget(widget_id):
    verify_csrf()
    admin = current_user()
    widget = get_db().execute(
        "SELECT id, title FROM portal_widgets WHERE id = ? LIMIT 1",
        (widget_id,),
    ).fetchone()
    if not widget:
        abort(404)

    get_db().execute("DELETE FROM portal_widgets WHERE id = ?", (widget_id,))
    get_db().commit()
    add_admin_action(admin, "delete-widget", f"Portal widget silindi: {widget['title']}")
    flash("Widget silindi.", "ok")
    return redirect(url_for("portal"))


@app.route("/feedback")
@login_required()
def feedback_page():
    user = current_user()
    feedback_recent = get_db().execute(
        """
        SELECT id, rating, category, message, status, created_at
        FROM user_feedback
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT 12
        """,
        (user["id"],),
    ).fetchall()
    return render_template("feedback.html", feedback_recent=feedback_recent)


@app.route("/chat")
@login_required()
def chat_page():
    user = current_user()
    db = get_db()
    recipients = db.execute(
        """
        SELECT id, username
        FROM users
        WHERE id != ?
          AND id NOT IN (SELECT blocked_id FROM user_blocks WHERE blocker_id = ?)
          AND id NOT IN (SELECT blocker_id FROM user_blocks WHERE blocked_id = ?)
        ORDER BY username ASC
        """,
        (user["id"], user["id"], user["id"]),
    ).fetchall()

    unread_by_sender_rows = db.execute(
        """
        SELECT sender_id, COUNT(*) AS c
        FROM user_messages
        WHERE recipient_id = ?
          AND is_read = 0
          AND deleted_by_recipient = 0
        GROUP BY sender_id
        """,
        (user["id"],),
    ).fetchall()
    unread_by_sender = {int(r["sender_id"]): int(r["c"]) for r in unread_by_sender_rows}

    peer_raw = request.args.get("peer_id", "").strip()
    selected_peer_id = int(peer_raw) if peer_raw.isdigit() else None
    selected_peer = None
    if selected_peer_id is not None:
        selected_peer = db.execute(
            """
            SELECT id, username
            FROM users
            WHERE id = ?
              AND id != ?
            LIMIT 1
            """,
            (selected_peer_id, user["id"]),
        ).fetchone()

    thread_messages = []
    if selected_peer:
        thread_messages = db.execute(
            """
            SELECT id, sender_id, sender_username, recipient_id, recipient_username, body, created_at, attachment_original_name,
                   CASE WHEN sender_id = ? THEN 1 ELSE 0 END AS can_delete
            FROM user_messages
            WHERE (
                sender_id = ? AND recipient_id = ? AND deleted_by_sender = 0
            ) OR (
                sender_id = ? AND recipient_id = ? AND deleted_by_recipient = 0
            )
            ORDER BY id ASC
            LIMIT 150
            """,
            (user["id"], user["id"], selected_peer["id"], selected_peer["id"], user["id"]),
        ).fetchall()

        db.execute(
            """
            UPDATE user_messages
            SET is_read = 1
            WHERE recipient_id = ?
              AND sender_id = ?
              AND is_read = 0
              AND deleted_by_recipient = 0
            """,
            (user["id"], selected_peer["id"]),
        )
        db.commit()
        socketio.emit("unread_count", {"count": get_unread_count(user["id"])}, room=f"user:{user['id']}")

    blocked_users = db.execute(
        """
        SELECT u.id, u.username
        FROM user_blocks b
        JOIN users u ON u.id = b.blocked_id
        WHERE b.blocker_id = ?
        ORDER BY u.username ASC
        """,
        (user["id"],),
    ).fetchall()

    return render_template(
        "chat.html",
        recipients=recipients,
        blocked_users=blocked_users,
        unread_count=get_unread_count(user["id"]),
        selected_peer=selected_peer,
        selected_peer_id=selected_peer["id"] if selected_peer else None,
        thread_messages=thread_messages,
        unread_by_sender=unread_by_sender,
    )


@app.route("/portal/open/<int:link_id>", methods=["POST"])
@login_required()
def portal_open(link_id):
    verify_csrf()
    target = get_db().execute(
        """
        SELECT id, title, redirect_url
        FROM portal_items
        WHERE id = ? AND is_active = 1
        LIMIT 1
        """,
        (link_id,),
    ).fetchone()
    if not target:
        abort(404)

    parsed = urlparse(target["redirect_url"])
    if parsed.scheme != "https":
        abort(400, "Yalnız HTTPS linklerine izin verilir.")

    user = current_user()
    add_click_log(user, target)
    return redirect(target["redirect_url"])


@app.route("/feedback/submit", methods=["POST"])
@login_required()
def portal_feedback():
    verify_csrf()
    user = current_user()
    category = request.form.get("category", "").strip()
    rating_raw = request.form.get("rating", "").strip()
    message = request.form.get("message", "").strip()

    allowed_categories = {"Genel", "Portal", "Hata", "Öneri"}
    if category not in allowed_categories:
        flash("Geçersiz geri bildirim kategorisi.", "error")
        return redirect(url_for("feedback_page"))
    if not rating_raw.isdigit():
        flash("Puanlama değeri geçersiz.", "error")
        return redirect(url_for("feedback_page"))
    rating = int(rating_raw)
    if rating < 1 or rating > 5:
        flash("Puanlama 1 ile 5 arasında olmalı.", "error")
        return redirect(url_for("feedback_page"))
    if len(message) < 8:
        flash("Geri bildirim metni en az 8 karakter olmalı.", "error")
        return redirect(url_for("feedback_page"))

    get_db().execute(
        """
        INSERT INTO user_feedback (user_id, username, rating, category, message, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (user["id"], user["username"], rating, category, message, utcnow_iso()),
    )
    get_db().commit()
    flash("Geri bildiriminiz kaydedildi. Teşekkürler.", "ok")
    return redirect(url_for("feedback_page"))


@app.route("/chat/rooms/create", methods=["POST"])
@login_required()
def portal_create_room():
    verify_csrf()
    user = current_user()
    name = request.form.get("room_name", "").strip()
    if len(name) < 3:
        flash("Sohbet odası adı en az 3 karakter olmalı.", "error")
        return redirect(url_for("chat_page"))
    if len(name) > 60:
        flash("Sohbet odası adı en fazla 60 karakter olmalı.", "error")
        return redirect(url_for("chat_page"))
    existing = get_db().execute("SELECT id FROM chat_rooms WHERE name = ? LIMIT 1", (name,)).fetchone()
    if existing:
        flash("Bu oda adı zaten kullanılıyor.", "error")
        return redirect(url_for("chat_page", room_id=existing["id"]))

    cursor = get_db().execute(
        """
        INSERT INTO chat_rooms (name, created_by_user_id, created_by_username, is_active, created_at)
        VALUES (?, ?, ?, 1, ?)
        """,
        (name, user["id"], user["username"], utcnow_iso()),
    )
    room_id = cursor.lastrowid
    existing_member = get_db().execute(
        "SELECT id FROM room_members WHERE room_id = ? AND user_id = ? LIMIT 1",
        (room_id, user["id"]),
    ).fetchone()
    if not existing_member:
        get_db().execute(
            """
            INSERT INTO room_members (room_id, user_id, username, joined_at)
            VALUES (?, ?, ?, ?)
            """,
            (room_id, user["id"], user["username"], utcnow_iso()),
        )
    get_db().commit()
    flash("Yeni sohbet odası oluşturuldu.", "ok")
    return redirect(url_for("chat_page", room_id=room_id))


@app.route("/chat/messages/send", methods=["POST"])
@login_required()
def portal_send_message():
    verify_csrf()
    sender = current_user()
    peer_id_raw = request.form.get("peer_id", "").strip()
    recipient_raw = request.form.get("recipient_id", "").strip()
    subject = request.form.get("subject", "").strip() or "DM"
    body = request.form.get("body", "").strip()

    if not recipient_raw.isdigit():
        flash("Mesaj alıcısı geçersiz.", "error")
        if peer_id_raw.isdigit():
            return redirect(url_for("chat_page", peer_id=int(peer_id_raw)))
        return redirect(url_for("chat_page"))
    recipient_id = int(recipient_raw)
    recipient = get_db().execute(
        "SELECT id, username, role FROM users WHERE id = ? LIMIT 1",
        (recipient_id,),
    ).fetchone()
    if not recipient or recipient["id"] == sender["id"]:
        flash("Mesaj alıcısı bulunamadı.", "error")
        return redirect(url_for("chat_page"))
    if is_blocked_between(sender["id"], recipient["id"]):
        flash("Mesaj gönderimi engellendi. Taraflardan biri diğerini engellemiş.", "error")
        return redirect(url_for("chat_page", peer_id=recipient["id"]))
    if len(body) < 1:
        flash("Mesaj boş olamaz.", "error")
        return redirect(url_for("chat_page", peer_id=recipient["id"]))

    attachment = request.files.get("attachment")
    original_name = None
    stored_name = None
    attachment_mime = None
    attachment_size = None

    if attachment and attachment.filename:
        original_name = attachment.filename
        if not allowed_attachment(original_name):
            flash("Dosya tipi desteklenmiyor.", "error")
            return redirect(url_for("chat_page", peer_id=recipient["id"]))
        attachment.stream.seek(0, os.SEEK_END)
        attachment_size = attachment.stream.tell()
        attachment.stream.seek(0)
        if attachment_size > MESSAGE_ATTACHMENT_MAX_BYTES:
            flash("Dosya boyutu 5MB üzerinde olamaz.", "error")
            return redirect(url_for("chat_page", peer_id=recipient["id"]))
        safe_name = secure_filename(original_name)
        stored_name = f"{int(time.time())}_{secrets.token_hex(6)}_{safe_name}"
        attachment_mime = (attachment.mimetype or "application/octet-stream")[:120]
        attachment.save(UPLOAD_DIR / stored_name)

    cursor = get_db().execute(
        """
        INSERT INTO user_messages
        (sender_id, sender_username, recipient_id, recipient_username, subject, body, attachment_original_name, attachment_stored_name, attachment_mime, attachment_size, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            sender["id"],
            sender["username"],
            recipient["id"],
            recipient["username"],
            subject,
            body,
            original_name,
            stored_name,
            attachment_mime,
            attachment_size,
            utcnow_iso(),
        ),
    )
    msg_id = cursor.lastrowid
    get_db().commit()
    payload = {
        "id": msg_id,
        "sender_id": int(sender["id"]),
        "sender_username": sender["username"],
        "recipient_id": int(recipient["id"]),
        "recipient_username": recipient["username"],
        "subject": subject,
        "body": body,
        "created_at": utcnow_iso(),
        "has_attachment": bool(original_name),
    }
    socketio.emit("dm_message", payload, room=f"user:{recipient['id']}")
    socketio.emit("dm_message", payload, room=f"user:{sender['id']}")
    socketio.emit("unread_count", {"count": get_unread_count(recipient["id"])}, room=f"user:{recipient['id']}")
    flash("Mesaj gönderildi.", "ok")
    return redirect(url_for("chat_page", peer_id=recipient["id"]))


@app.route("/chat/messages/<int:message_id>/read", methods=["POST"])
@login_required()
def portal_mark_message_read(message_id):
    verify_csrf()
    user = current_user()
    row = get_db().execute(
        "SELECT sender_id FROM user_messages WHERE id = ? AND recipient_id = ? LIMIT 1",
        (message_id, user["id"]),
    ).fetchone()
    get_db().execute(
        "UPDATE user_messages SET is_read = 1 WHERE id = ? AND recipient_id = ?",
        (message_id, user["id"]),
    )
    get_db().commit()
    socketio.emit("unread_count", {"count": get_unread_count(user["id"])}, room=f"user:{user['id']}")
    if row:
        socketio.emit("dm_read", {"message_id": message_id, "reader_id": int(user["id"])}, room=f"user:{row['sender_id']}")
    return redirect(url_for("chat_page"))


@app.route("/chat/messages/<int:message_id>/delete", methods=["POST"])
@login_required()
def portal_delete_message(message_id):
    verify_csrf()
    user = current_user()
    message = get_db().execute(
        """
        SELECT id, sender_id, recipient_id, attachment_stored_name
        FROM user_messages
        WHERE id = ?
        LIMIT 1
        """,
        (message_id,),
    ).fetchone()
    if not message:
        abort(404)
    if user["id"] not in (message["sender_id"], message["recipient_id"]):
        abort(403)
    if user["id"] != message["sender_id"]:
        abort(403)

    stored_name = message["attachment_stored_name"]
    get_db().execute(
        """
        UPDATE user_messages
        SET body = ?, subject = 'DM', attachment_original_name = NULL, attachment_stored_name = NULL,
            attachment_mime = NULL, attachment_size = NULL
        WHERE id = ?
        """,
        (DELETED_MESSAGE_TEXT, message_id),
    )
    if stored_name:
        file_path = UPLOAD_DIR / stored_name
        if file_path.exists():
            file_path.unlink()
    get_db().commit()
    payload = {"message_id": message_id, "body": DELETED_MESSAGE_TEXT}
    socketio.emit("dm_message_deleted", payload, room=f"user:{message['sender_id']}")
    socketio.emit("dm_message_deleted", payload, room=f"user:{message['recipient_id']}")
    flash("Mesaj silindi.", "ok")
    return redirect(url_for("chat_page", peer_id=message["recipient_id"]))


@app.route("/chat/messages/<int:message_id>/attachment")
@login_required()
def portal_message_attachment(message_id):
    user = current_user()
    row = get_db().execute(
        """
        SELECT sender_id, recipient_id, attachment_stored_name, attachment_original_name, attachment_mime
        FROM user_messages
        WHERE id = ?
        LIMIT 1
        """,
        (message_id,),
    ).fetchone()
    if not row or not row["attachment_stored_name"]:
        abort(404)
    if user["id"] not in (row["sender_id"], row["recipient_id"]):
        abort(403)

    path = UPLOAD_DIR / row["attachment_stored_name"]
    if not path.exists():
        abort(404)
    return send_file(
        path,
        as_attachment=True,
        download_name=row["attachment_original_name"] or row["attachment_stored_name"],
        mimetype=row["attachment_mime"] or "application/octet-stream",
    )


@app.route("/chat/blocks", methods=["POST"])
@login_required()
def portal_block_user():
    verify_csrf()
    user = current_user()
    target_raw = request.form.get("recipient_id", "").strip()
    if not target_raw.isdigit():
        flash("Geçersiz engelleme hedefi.", "error")
        return redirect(url_for("chat_page"))
    target_id = int(target_raw)
    target = get_db().execute(
        "SELECT id, username, role FROM users WHERE id = ? LIMIT 1",
        (target_id,),
    ).fetchone()
    if not target or target["id"] == user["id"]:
        flash("Engellenecek kullanıcı bulunamadı.", "error")
        return redirect(url_for("chat_page"))
    exists = get_db().execute(
        "SELECT id FROM user_blocks WHERE blocker_id = ? AND blocked_id = ? LIMIT 1",
        (user["id"], target_id),
    ).fetchone()
    if not exists:
        get_db().execute(
            "INSERT INTO user_blocks (blocker_id, blocked_id, created_at) VALUES (?, ?, ?)",
            (user["id"], target_id, utcnow_iso()),
        )
        get_db().commit()
    flash(f"{target['username']} engellendi.", "ok")
    return redirect(url_for("chat_page"))


@app.route("/chat/blocks/<int:blocked_id>/remove", methods=["POST"])
@login_required()
def portal_unblock_user(blocked_id):
    verify_csrf()
    user = current_user()
    get_db().execute(
        "DELETE FROM user_blocks WHERE blocker_id = ? AND blocked_id = ?",
        (user["id"], blocked_id),
    )
    get_db().commit()
    flash("Kullanıcı engeli kaldırıldı.", "ok")
    return redirect(url_for("chat_page"))


@app.route("/admin")
@login_required(role="admin")
def admin_dashboard():
    db = get_db()
    stats = {
        "users": db.execute("SELECT COUNT(*) AS c FROM users WHERE role = 'user'").fetchone()["c"],
        "login_logs": db.execute("SELECT COUNT(*) AS c FROM login_logs").fetchone()["c"],
        "click_logs": db.execute("SELECT COUNT(*) AS c FROM click_logs").fetchone()["c"],
        "actions": db.execute("SELECT COUNT(*) AS c FROM admin_actions").fetchone()["c"],
        "portal_items": db.execute("SELECT COUNT(*) AS c FROM portal_items WHERE is_active = 1").fetchone()["c"],
        "feedback": db.execute("SELECT COUNT(*) AS c FROM user_feedback").fetchone()["c"],
        "messages": db.execute("SELECT COUNT(*) AS c FROM user_messages").fetchone()["c"],
    }
    return render_template("admin_dashboard.html", stats=stats)


@app.route("/admin/create-user", methods=["GET", "POST"])
@login_required(role="admin")
def admin_create_user():
    if request.method == "GET":
        return redirect(url_for("admin_users"))

    verify_csrf()
    username = request.form.get("username", "").strip()
    if not username or len(username) < 3:
        flash("Kullanıcı adı en az 3 karakter olmalı.", "error")
        return redirect(url_for("admin_users"))

    existing = get_db().execute(
        "SELECT id FROM users WHERE username = ? LIMIT 1",
        (username,),
    ).fetchone()
    if existing:
        flash("Bu kullanıcı adı zaten mevcut.", "error")
        return redirect(url_for("admin_users"))

    get_db().execute(
        """
        INSERT INTO users (username, password_hash, role, must_change_password, created_at)
        VALUES (?, ?, 'user', 1, ?)
        """,
        (username, generate_password_hash(DEFAULT_USER_INITIAL_PASSWORD), utcnow_iso()),
    )
    get_db().commit()

    admin = current_user()
    add_admin_action(admin, "create-user", f"{username} oluşturuldu. Varsayılan başlangıç şifresi atandı.")
    flash("Kullanıcı oluşturuldu. Başlangıç şifresi atandı ve ilk girişte şifre değiştirilecek.", "ok")
    return redirect(url_for("admin_users"))


@app.route("/admin/users")
@login_required(role="admin")
def admin_users():
    search_query = request.args.get("q", "").strip()
    filter_mode = request.args.get("filter", "all")
    admin = current_user()

    sql = """
        SELECT
            id,
            username,
            role,
            must_change_password,
            failed_attempts,
            lock_until,
            created_at,
            last_login,
            CASE
                WHEN lock_until IS NOT NULL AND lock_until > ? THEN 1
                ELSE 0
            END AS is_locked
        FROM users
        WHERE 1 = 1
    """
    now_value = utcnow_iso()
    params = [now_value]

    if search_query:
        sql += " AND username LIKE ?"
        params.append(f"%{search_query}%")

    if filter_mode == "locked":
        sql += " AND lock_until IS NOT NULL AND lock_until > ?"
        params.append(now_value)
    elif filter_mode == "must_change":
        sql += " AND must_change_password = 1"

    sql += " ORDER BY id DESC"

    users = get_db().execute(sql, tuple(params)).fetchall()
    admin_count = get_db().execute("SELECT COUNT(*) AS c FROM users WHERE role = 'admin'").fetchone()["c"]
    return render_template(
        "admin_users.html",
        users=users,
        q=search_query,
        filter_mode=filter_mode,
        me_id=int(admin["id"]),
        admin_count=int(admin_count),
    )


@app.route("/admin/users/<int:user_id>/action", methods=["POST"])
@login_required(role="admin")
def admin_user_action(user_id):
    verify_csrf()
    action = request.form.get("action", "").strip()
    admin = current_user()

    user = get_db().execute(
        """
        SELECT id, username, role, must_change_password, lock_until
        FROM users
        WHERE id = ?
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()
    if not user:
        abort(404)

    if action == "unlock":
        get_db().execute(
            "UPDATE users SET failed_attempts = 0, lock_until = NULL WHERE id = ?",
            (user_id,),
        )
        get_db().commit()
        add_admin_action(admin, "unlock-user", f"{user['username']} hesabinin kilidi acildi.")
        flash("Kullanıcı hesabının kilidi açıldı.", "ok")
    elif action == "lock":
        lock_until = (datetime.utcnow() + timedelta(minutes=LOCK_MINUTES)).isoformat(timespec="seconds")
        get_db().execute(
            "UPDATE users SET lock_until = ?, failed_attempts = 0 WHERE id = ?",
            (lock_until, user_id),
        )
        get_db().commit()
        add_admin_action(admin, "lock-user", f"{user['username']} hesabi gecici olarak kilitlendi.")
        flash("Kullanıcı hesabı geçici olarak kilitlendi.", "ok")
    elif action == "force-password-change":
        get_db().execute(
            "UPDATE users SET must_change_password = 1 WHERE id = ?",
            (user_id,),
        )
        get_db().commit()
        add_admin_action(admin, "force-password-change", f"{user['username']} icin zorunlu sifre degisimi acildi.")
        flash("Kullanıcı için zorunlu şifre değişimi aktif edildi.", "ok")
    elif action == "clear-password-change":
        get_db().execute(
            "UPDATE users SET must_change_password = 0 WHERE id = ?",
            (user_id,),
        )
        get_db().commit()
        add_admin_action(admin, "clear-password-change", f"{user['username']} icin zorunlu sifre degisimi kapatildi.")
        flash("Kullanıcı için zorunlu şifre değişimi kapatıldı.", "ok")
    elif action == "reset-password":
        if not DEFAULT_USER_INITIAL_PASSWORD:
            flash("Başlangıç şifresi tanımlı değil. .env dosyasını kontrol edin.", "error")
            return redirect(url_for("admin_users"))
        get_db().execute(
            """
            UPDATE users
            SET password_hash = ?, must_change_password = 1, failed_attempts = 0, lock_until = NULL
            WHERE id = ?
            """,
            (generate_password_hash(DEFAULT_USER_INITIAL_PASSWORD), user_id),
        )
        get_db().commit()
        add_admin_action(admin, "reset-user-password", f"{user['username']} parolasi sifirlandi ve ilk giriste degisim zorunlu.")
        flash("Kullanıcı parolası sıfırlandı. İlk girişte şifre değişimi zorunlu.", "ok")
    elif action == "make-admin":
        if user["role"] == "admin":
            flash("Kullanıcı zaten admin.", "error")
            return redirect(url_for("admin_users"))
        get_db().execute("UPDATE users SET role = 'admin' WHERE id = ?", (user_id,))
        get_db().commit()
        add_admin_action(admin, "promote-admin", f"{user['username']} admin yapildi.")
        flash("Kullanıcı admin rolüne geçirildi.", "ok")
    elif action == "make-user":
        if user["role"] == "user":
            flash("Kullanıcı zaten user rolünde.", "error")
            return redirect(url_for("admin_users"))
        if int(user["id"]) == int(admin["id"]):
            flash("Kendi hesabınızı user rolüne düşüremezsiniz.", "error")
            return redirect(url_for("admin_users"))
        admin_count = get_db().execute("SELECT COUNT(*) AS c FROM users WHERE role = 'admin'").fetchone()["c"]
        if int(admin_count) <= 1:
            flash("Sistemde en az bir admin kalmalıdır.", "error")
            return redirect(url_for("admin_users"))
        get_db().execute("UPDATE users SET role = 'user' WHERE id = ?", (user_id,))
        get_db().commit()
        add_admin_action(admin, "demote-admin", f"{user['username']} user rolune dusuruldu.")
        flash("Kullanıcı user rolüne geçirildi.", "ok")
    else:
        flash("Geçersiz kullanıcı işlemi.", "error")

    return redirect(url_for("admin_users"))


@app.route("/admin/logins")
@login_required(role="admin")
def admin_login_logs():
    logs = get_db().execute(
        """
        SELECT id, username, ip_address, status, reason, created_at
        FROM login_logs
        ORDER BY id DESC
        LIMIT 500
        """
    ).fetchall()
    return render_template("admin_login_logs.html", logs=logs)


@app.route("/admin/clicks")
@login_required(role="admin")
def admin_click_logs():
    logs = get_db().execute(
        """
        SELECT id, username, portal_item_id, target_name, target_url, created_at
        FROM click_logs
        ORDER BY id DESC
        LIMIT 500
        """
    ).fetchall()
    return render_template("admin_click_logs.html", logs=logs)


@app.route("/admin/feedback")
@login_required(role="admin")
def admin_feedback():
    rows = get_db().execute(
        """
        SELECT id, username, rating, category, message, status, created_at
        FROM user_feedback
        ORDER BY id DESC
        LIMIT 500
        """
    ).fetchall()
    return render_template("admin_feedback.html", rows=rows)


@app.route("/admin/messages")
@login_required(role="admin")
def admin_messages():
    rows = get_db().execute(
        """
        SELECT id, sender_username, recipient_username, subject, body, attachment_original_name, is_read, deleted_by_sender, deleted_by_recipient, created_at
        FROM user_messages
        ORDER BY id DESC
        LIMIT 500
        """
    ).fetchall()
    return render_template("admin_messages.html", rows=rows)


@app.route("/admin/portal-items", methods=["GET", "POST"])
@login_required(role="admin")
def admin_portal_items():
    admin = current_user()
    if request.method == "POST":
        verify_csrf()
        title = request.form.get("title", "").strip()
        preview = request.form.get("preview", "").strip()
        redirect_url = request.form.get("redirect_url", "").strip()
        image_url = request.form.get("image_url", "").strip()

        if len(title) < 3:
            flash("Portal başlığı en az 3 karakter olmalı.", "error")
            return redirect(url_for("admin_portal_items"))
        if len(preview) < 8:
            flash("Portal önizlemesi en az 8 karakter olmalı.", "error")
            return redirect(url_for("admin_portal_items"))

        parsed_redirect = urlparse(redirect_url)
        if parsed_redirect.scheme != "https":
            flash("Yönlendirme linki HTTPS olmalı.", "error")
            return redirect(url_for("admin_portal_items"))

        if image_url:
            parsed_image = urlparse(image_url)
            if parsed_image.scheme not in ("http", "https"):
                flash("Görsel linki HTTP veya HTTPS olmalı.", "error")
                return redirect(url_for("admin_portal_items"))

        get_db().execute(
            """
            INSERT INTO portal_items (title, preview, redirect_url, image_url, is_active, created_by_admin_id, created_by_admin_username, created_at)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                title,
                preview,
                redirect_url,
                image_url,
                admin["id"],
                admin["username"],
                utcnow_iso(),
            ),
        )
        get_db().commit()
        add_admin_action(admin, "add-portal-item", f"Portal öğe eklendi: {title}")
        flash("Portal öğesi başarıyla eklendi.", "ok")
        return redirect(url_for("admin_portal_items"))

    items = get_db().execute(
        """
        SELECT id, title, preview, redirect_url, image_url, is_active, created_by_admin_username, created_at
        FROM portal_items
        ORDER BY id DESC
        """
    ).fetchall()
    return render_template("admin_portal_items.html", items=items)


@app.route("/admin/portal-items/<int:item_id>/toggle", methods=["POST"])
@login_required(role="admin")
def admin_toggle_portal_item(item_id):
    verify_csrf()
    admin = current_user()
    item = get_db().execute("SELECT id, title, is_active FROM portal_items WHERE id = ?", (item_id,)).fetchone()
    if not item:
        abort(404)

    next_state = 0 if item["is_active"] else 1
    get_db().execute("UPDATE portal_items SET is_active = ? WHERE id = ?", (next_state, item_id))
    get_db().commit()
    action = "portal-item-disabled" if next_state == 0 else "portal-item-enabled"
    add_admin_action(admin, action, f"Portal öğe durumu değişti: {item['title']}")
    flash("Portal öğe durumu güncellendi.", "ok")
    return redirect(url_for("admin_portal_items"))


@app.route("/admin/portal-items/<int:item_id>/delete", methods=["POST"])
@login_required(role="admin")
def admin_delete_portal_item(item_id):
    verify_csrf()
    admin = current_user()
    item = get_db().execute(
        "SELECT id, title FROM portal_items WHERE id = ? LIMIT 1",
        (item_id,),
    ).fetchone()
    if not item:
        abort(404)

    get_db().execute("DELETE FROM portal_items WHERE id = ?", (item_id,))
    get_db().commit()
    add_admin_action(admin, "delete-portal-item", f"Portal öğe silindi: {item['title']}")
    flash("Portal öğesi silindi.", "ok")
    return redirect(url_for("admin_portal_items"))


@app.route("/admin/actions")
@login_required(role="admin")
def admin_action_logs():
    logs = get_db().execute(
        """
        SELECT id, admin_username, action_type, detail, created_at
        FROM admin_actions
        ORDER BY id DESC
        LIMIT 500
        """
    ).fetchall()
    return render_template("admin_action_logs.html", logs=logs)


@app.route("/admin/db-reset", methods=["GET", "POST"])
@login_required(role="admin")
def admin_db_reset():
    available_scopes = ("users", "portal", "messages", "logs", "all_except_admin")
    default_scopes = ("users", "portal", "messages", "logs")

    def reset_preview_counts():
        db = get_db()
        return {
            "users": db.execute("SELECT COUNT(*) AS c FROM users WHERE role = 'user'").fetchone()["c"],
            "portal_items": db.execute("SELECT COUNT(*) AS c FROM portal_items").fetchone()["c"],
            "portal_widgets": db.execute("SELECT COUNT(*) AS c FROM portal_widgets").fetchone()["c"],
            "messages": db.execute("SELECT COUNT(*) AS c FROM user_messages").fetchone()["c"],
            "feedback": db.execute("SELECT COUNT(*) AS c FROM user_feedback").fetchone()["c"],
            "rooms": db.execute("SELECT COUNT(*) AS c FROM chat_rooms").fetchone()["c"],
            "logs": (
                db.execute("SELECT COUNT(*) AS c FROM login_logs").fetchone()["c"]
                + db.execute("SELECT COUNT(*) AS c FROM click_logs").fetchone()["c"]
                + db.execute("SELECT COUNT(*) AS c FROM admin_actions").fetchone()["c"]
            ),
        }

    def render_reset_page(selected_scopes=None):
        return render_template(
            "admin_db_reset.html",
            db_reset_code=session.get("db_reset_code"),
            preview=reset_preview_counts(),
            selected_scopes=selected_scopes or list(default_scopes),
        )

    user = current_user()
    if request.method == "GET":
        session["db_reset_code"] = secrets.token_hex(3).upper()
        return render_reset_page()

    verify_csrf()
    phrase = request.form.get("confirm_phrase", "").strip()
    code = request.form.get("reset_code", "").strip().upper()
    password = request.form.get("admin_password", "")
    selected_scopes = [s for s in request.form.getlist("reset_scopes") if s in available_scopes]

    if not selected_scopes:
        flash("En az bir veri alanı seçmelisiniz.", "error")
        return render_reset_page(selected_scopes=[]), 400

    if phrase != "VERITABANI_SIFIRLA":
        flash("Onay cümlesi hatalı.", "error")
        return render_reset_page(selected_scopes), 400

    if code != session.get("db_reset_code"):
        flash("Doğrulama kodu hatalı.", "error")
        return render_reset_page(selected_scopes), 400

    fresh_user = get_db().execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    if not check_password_hash(fresh_user["password_hash"], password):
        flash("Admin şifresi doğrulanamadı.", "error")
        return render_reset_page(selected_scopes), 400

    db = get_db()
    if "all_except_admin" in selected_scopes:
        selected_scopes = ["all_except_admin"]

    if "messages" in selected_scopes or "all_except_admin" in selected_scopes:
        attachment_rows = db.execute(
            """
            SELECT attachment_stored_name
            FROM user_messages
            WHERE attachment_stored_name IS NOT NULL
            """
        ).fetchall()
        for row in attachment_rows:
            stored_name = row["attachment_stored_name"]
            if not stored_name:
                continue
            file_path = UPLOAD_DIR / stored_name
            if file_path.exists():
                try:
                    file_path.unlink()
                except OSError:
                    pass
        db.execute("DELETE FROM room_messages")
        db.execute("DELETE FROM room_members")
        db.execute("DELETE FROM chat_rooms")
        db.execute("DELETE FROM user_blocks")
        db.execute("DELETE FROM user_messages")
        db.execute("DELETE FROM user_feedback")

    if "portal" in selected_scopes or "all_except_admin" in selected_scopes:
        db.execute("DELETE FROM portal_widgets")
        db.execute("DELETE FROM portal_items")

    if "logs" in selected_scopes or "all_except_admin" in selected_scopes:
        db.execute("DELETE FROM click_logs")
        db.execute("DELETE FROM login_logs")
        db.execute("DELETE FROM admin_actions")

    if "users" in selected_scopes or "all_except_admin" in selected_scopes:
        db.execute("DELETE FROM users WHERE id != ?", (user["id"],))

    db.commit()

    if "messages" in selected_scopes:
        ensure_default_chat_rooms()

    scope_labels = {
        "users": "Kullanıcılar",
        "portal": "Portal/Widget",
        "messages": "Mesaj/Chat/Geri Bildirim",
        "logs": "Loglar",
        "all_except_admin": "Admin Hariç Tam Sıfırlama",
    }
    selected_text = ", ".join(scope_labels[s] for s in selected_scopes)
    add_admin_action(user, "db-reset", f"Secimli reset uygulandi: {selected_text}")
    flash(f"DB reset tamamlandı. Temizlenen alanlar: {selected_text}.", "ok")
    session["db_reset_code"] = secrets.token_hex(3).upper()
    return redirect(url_for("admin_dashboard"))


@socketio.on("connect")
def socket_connect():
    user = current_user()
    if not user:
        return False

    uid = int(user["id"])
    sid_to_user[request.sid] = uid
    online_user_sids.setdefault(uid, set()).add(request.sid)
    join_room(f"user:{uid}")

    emit("presence_snapshot", {"online_user_ids": active_online_user_ids()})
    emit(
        "user_presence",
        {"user_id": uid, "username": user["username"], "online": True},
        broadcast=True,
        include_self=False,
    )


@socketio.on("disconnect")
def socket_disconnect():
    uid = sid_to_user.pop(request.sid, None)
    if not uid:
        return
    sid_set = online_user_sids.get(uid, set())
    sid_set.discard(request.sid)
    if sid_set:
        online_user_sids[uid] = sid_set
        return
    online_user_sids.pop(uid, None)
    emit("user_presence", {"user_id": int(uid), "online": False}, broadcast=True)


@socketio.on("typing_dm")
def socket_typing_dm(data):
    data = data or {}
    user = current_user()
    if not user:
        return
    recipient_raw = str(data.get("recipient_id", "0")).strip()
    if not recipient_raw.isdigit():
        return
    recipient_id = int(recipient_raw)
    is_typing = bool(data.get("is_typing", False))
    if recipient_id <= 0 or recipient_id == int(user["id"]):
        return
    if is_blocked_between(int(user["id"]), recipient_id):
        return
    emit(
        "typing_dm",
        {"from_user_id": int(user["id"]), "from_username": user["username"], "is_typing": is_typing},
        room=f"user:{recipient_id}",
    )


@socketio.on("send_dm")
def socket_send_dm(data):
    data = data or {}
    user = current_user()
    if not user:
        return
    recipient_raw = str(data.get("recipient_id", "0")).strip()
    if not recipient_raw.isdigit():
        emit("dm_error", {"message": "Geçersiz alıcı."})
        return
    recipient_id = int(recipient_raw)
    subject = str(data.get("subject", "")).strip() or "DM"
    body = str(data.get("body", "")).strip()

    if recipient_id <= 0 or recipient_id == int(user["id"]):
        emit("dm_error", {"message": "Geçersiz alıcı."})
        return
    if len(body) < 1:
        emit("dm_error", {"message": "Mesaj boş olamaz."})
        return
    recipient = get_db().execute(
        "SELECT id, username, role FROM users WHERE id = ? LIMIT 1",
        (recipient_id,),
    ).fetchone()
    if not recipient:
        emit("dm_error", {"message": "Alıcı bulunamadı."})
        return
    if is_blocked_between(int(user["id"]), recipient_id):
        emit("dm_error", {"message": "Mesaj gönderimi engellendi."})
        return

    cursor = get_db().execute(
        """
        INSERT INTO user_messages
        (sender_id, sender_username, recipient_id, recipient_username, subject, body, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (user["id"], user["username"], recipient["id"], recipient["username"], subject, body, utcnow_iso()),
    )
    msg_id = cursor.lastrowid
    get_db().commit()

    payload = {
        "id": msg_id,
        "sender_id": int(user["id"]),
        "sender_username": user["username"],
        "recipient_id": int(recipient["id"]),
        "recipient_username": recipient["username"],
        "subject": subject,
        "body": body,
        "created_at": utcnow_iso(),
    }
    emit("dm_message", payload, room=f"user:{recipient_id}")
    emit("dm_message", payload, room=f"user:{int(user['id'])}")
    emit("unread_count", {"count": get_unread_count(recipient_id)}, room=f"user:{recipient_id}")


@socketio.on("join_chat_room")
def socket_join_chat_room(data):
    data = data or {}
    user = current_user()
    if not user:
        return
    room_raw = str(data.get("room_id", "0")).strip()
    if not room_raw.isdigit():
        return
    room_id = int(room_raw)
    if room_id <= 0:
        return
    room_row = get_db().execute(
        "SELECT id, name FROM chat_rooms WHERE id = ? AND is_active = 1 LIMIT 1",
        (room_id,),
    ).fetchone()
    if not room_row:
        return

    existing_member = get_db().execute(
        "SELECT id FROM room_members WHERE room_id = ? AND user_id = ? LIMIT 1",
        (room_id, user["id"]),
    ).fetchone()
    if not existing_member:
        get_db().execute(
            """
            INSERT INTO room_members (room_id, user_id, username, joined_at)
            VALUES (?, ?, ?, ?)
            """,
            (room_id, user["id"], user["username"], utcnow_iso()),
        )
    get_db().commit()
    join_room(f"chat:{room_id}")
    rows = get_db().execute(
        """
        SELECT id, sender_username, body, created_at
        FROM room_messages
        WHERE room_id = ?
        ORDER BY id DESC
        LIMIT 25
        """,
        (room_id,),
    ).fetchall()
    emit(
        "room_history",
        {
            "room_id": room_id,
            "messages": [
                {"id": r["id"], "sender_username": r["sender_username"], "body": r["body"], "created_at": r["created_at"]}
                for r in rows
            ],
        },
    )


@socketio.on("typing_room")
def socket_typing_room(data):
    data = data or {}
    user = current_user()
    if not user:
        return
    room_raw = str(data.get("room_id", "0")).strip()
    if not room_raw.isdigit():
        return
    room_id = int(room_raw)
    if room_id <= 0:
        return
    emit(
        "typing_room",
        {
            "room_id": room_id,
            "username": user["username"],
            "is_typing": bool(data.get("is_typing", False)),
        },
        room=f"chat:{room_id}",
        include_self=False,
    )


@socketio.on("send_room_message")
def socket_send_room_message(data):
    data = data or {}
    user = current_user()
    if not user:
        return
    room_raw = str(data.get("room_id", "0")).strip()
    if not room_raw.isdigit():
        return
    room_id = int(room_raw)
    body = str(data.get("body", "")).strip()
    if room_id <= 0 or len(body) < 1:
        return
    room_row = get_db().execute(
        "SELECT id FROM chat_rooms WHERE id = ? AND is_active = 1 LIMIT 1",
        (room_id,),
    ).fetchone()
    if not room_row:
        return
    existing_member = get_db().execute(
        "SELECT id FROM room_members WHERE room_id = ? AND user_id = ? LIMIT 1",
        (room_id, user["id"]),
    ).fetchone()
    if not existing_member:
        get_db().execute(
            """
            INSERT INTO room_members (room_id, user_id, username, joined_at)
            VALUES (?, ?, ?, ?)
            """,
            (room_id, user["id"], user["username"], utcnow_iso()),
        )
    cursor = get_db().execute(
        """
        INSERT INTO room_messages (room_id, sender_id, sender_username, body, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (room_id, user["id"], user["username"], body, utcnow_iso()),
    )
    msg_id = cursor.lastrowid
    get_db().commit()
    emit(
        "room_message",
        {
            "room_id": room_id,
            "id": msg_id,
            "sender_username": user["username"],
            "body": body,
            "created_at": utcnow_iso(),
        },
        room=f"chat:{room_id}",
    )


def bootstrap():
    global DB_PATH, UPLOAD_DIR
    if IS_PROD and app.config["SECRET_KEY"] == "CHANGE_ME_FOR_PRODUCTION":
        raise RuntimeError("APP_SECRET_KEY zorunlu. Production ortaminda varsayilan key kullanilamaz.")
    with app.app_context():
        candidate_paths = [(DB_PATH, UPLOAD_DIR), (Path("/tmp/data.db"), Path("/tmp/uploads/messages"))]
        selected = None
        for db_candidate, upload_candidate in candidate_paths:
            try:
                db_candidate.parent.mkdir(parents=True, exist_ok=True)
                upload_candidate.mkdir(parents=True, exist_ok=True)
                probe = upload_candidate / ".write_probe"
                probe.write_text("ok", encoding="utf-8")
                probe.unlink(missing_ok=True)
                selected = (db_candidate, upload_candidate)
                break
            except OSError:
                continue
        if not selected:
            raise RuntimeError("Yazilabilir depolama dizini bulunamadi.")
        DB_PATH, UPLOAD_DIR = selected
        init_db()
        ensure_default_admin()
        ensure_default_chat_rooms()


bootstrap()


if __name__ == "__main__":
    socketio.run(app, debug=not IS_PROD)
