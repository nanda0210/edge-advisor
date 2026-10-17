"""
User accounts for NandaEdge Advisor.

Mirrors the Resume Hub (myjobandcareer.com) auth design: email as username,
salted PBKDF2-SHA256 passwords, email-verification links, password reset, and
server-side sessions.

Storage: Postgres when DATABASE_URL is set (Neon/Supabase), SQLite otherwise.
Render's free tier has an ephemeral filesystem, so SQLite there would lose every
account on restart — Postgres is the production path and SQLite exists so the
logic can be exercised locally and in tests without a live database.

No secrets are logged. Passwords are never stored, only salt + hash.
"""
import os
import re
import secrets
import hashlib
import hmac
import smtplib
import sqlite3
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

# ── Policy ────────────────────────────────────────────
PASSWORD_RULE = "At least 8 characters, including at least one letter and one number."
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
LINK_TTL_MIN = 60          # verification / reset link lifetime
SESSION_TTL_MIN = int(os.environ.get("USER_SESSION_MINUTES", "15") or "15")
PBKDF2_ROUNDS = 100_000    # matches the Resume Hub

DATABASE_URL = (os.environ.get("DATABASE_URL", "") or "").strip()
SQLITE_PATH = os.environ.get(
    "ACCOUNTS_SQLITE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.db"),
)


def _utcnow():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Storage adapter ───────────────────────────────────
# Postgres uses %s placeholders, SQLite uses ?. The SQL is otherwise kept to a
# common subset so one set of statements serves both.
class _Db:
    def __init__(self):
        self.is_postgres = bool(DATABASE_URL)
        if self.is_postgres:
            import psycopg          # imported lazily; only needed in production
            self._connect = lambda: psycopg.connect(DATABASE_URL)
        else:
            def _sqlite():
                conn = sqlite3.connect(SQLITE_PATH, timeout=10)
                conn.execute("PRAGMA journal_mode=WAL")
                return conn
            self._connect = _sqlite

    def q(self, sql):
        """Translate ? placeholders to %s for Postgres."""
        return sql.replace("?", "%s") if self.is_postgres else sql

    def connect(self):
        return self._connect()


_db = _Db()


def using_postgres():
    return _db.is_postgres


def storage_label():
    return "postgres" if _db.is_postgres else f"sqlite ({SQLITE_PATH})"


SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id           SERIAL PRIMARY KEY,
        email        TEXT UNIQUE NOT NULL,
        pw_salt      TEXT NOT NULL,
        pw_hash      TEXT NOT NULL,
        verified     INTEGER NOT NULL DEFAULT 0,
        token        TEXT,
        token_purpose TEXT,
        token_expires TEXT,
        created_at   TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS sessions (
        token      TEXT PRIMARY KEY,
        user_id    INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )""",
]


def create_tables():
    sql = SCHEMA
    if not _db.is_postgres:
        # SQLite has no SERIAL.
        sql = [s.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
               for s in sql]
    conn = _db.connect()
    try:
        cur = conn.cursor()
        for stmt in sql:
            cur.execute(stmt)
        conn.commit()
    finally:
        conn.close()


# ── Validation ────────────────────────────────────────
def valid_email(email):
    return bool(_EMAIL_RE.match((email or "").strip()))


def password_problem(pw):
    """Returns a human-readable problem, or None when the password is fine."""
    pw = pw or ""
    if len(pw) < 8:
        return PASSWORD_RULE
    if not re.search(r"[A-Za-z]", pw) or not re.search(r"\d", pw):
        return PASSWORD_RULE
    return None


# ── Password hashing ──────────────────────────────────
def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", (pw or "").encode(), salt.encode(),
                            PBKDF2_ROUNDS).hex()
    return salt, h


def verify_pw(pw, salt, expected):
    if not salt or not expected:
        return False
    _, h = hash_pw(pw, salt)
    return hmac.compare_digest(h, expected)      # constant-time


# ── Link tokens ───────────────────────────────────────
def new_token():
    return secrets.token_urlsafe(32)


def token_expiry(minutes=LINK_TTL_MIN):
    return _iso(_utcnow() + timedelta(minutes=minutes))


def token_expired(expires_at):
    if not expires_at:
        return True
    try:
        exp = datetime.strptime(expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return True
    return _utcnow() > exp


# ── User operations ───────────────────────────────────
def _row_to_user(row):
    if not row:
        return None
    keys = ("id", "email", "pw_salt", "pw_hash", "verified",
            "token", "token_purpose", "token_expires", "created_at")
    return dict(zip(keys, row))


def get_user(email):
    conn = _db.connect()
    try:
        cur = conn.cursor()
        cur.execute(_db.q(
            "SELECT id,email,pw_salt,pw_hash,verified,token,token_purpose,"
            "token_expires,created_at FROM users WHERE email=?"),
            ((email or "").strip().lower(),))
        return _row_to_user(cur.fetchone())
    finally:
        conn.close()


def create_user(email, password):
    """Returns (user_id, verify_token) or raises ValueError."""
    email = (email or "").strip().lower()
    if not valid_email(email):
        raise ValueError("Enter a valid email address.")
    problem = password_problem(password)
    if problem:
        raise ValueError(problem)
    if get_user(email):
        raise ValueError("An account with that email already exists.")
    salt, h = hash_pw(password)
    tok, exp = new_token(), token_expiry()
    conn = _db.connect()
    try:
        cur = conn.cursor()
        cur.execute(_db.q(
            "INSERT INTO users (email,pw_salt,pw_hash,verified,token,token_purpose,"
            "token_expires,created_at) VALUES (?,?,?,0,?,'verify',?,?)"),
            (email, salt, h, tok, exp, _iso(_utcnow())))
        conn.commit()
        cur.execute(_db.q("SELECT id FROM users WHERE email=?"), (email,))
        return cur.fetchone()[0], tok
    finally:
        conn.close()


def consume_token(token, purpose):
    """Validate a verify/reset token and clear it. Returns the user or None."""
    if not token:
        return None
    conn = _db.connect()
    try:
        cur = conn.cursor()
        cur.execute(_db.q(
            "SELECT id,email,pw_salt,pw_hash,verified,token,token_purpose,"
            "token_expires,created_at FROM users WHERE token=? AND token_purpose=?"),
            (token, purpose))
        user = _row_to_user(cur.fetchone())
        if not user or token_expired(user["token_expires"]):
            return None
        if purpose == "verify":
            cur.execute(_db.q(
                "UPDATE users SET verified=1,token=NULL,token_purpose=NULL,"
                "token_expires=NULL WHERE id=?"), (user["id"],))
            conn.commit()
            user["verified"] = 1
        return user
    finally:
        conn.close()


def set_reset_token(email):
    """Returns a reset token, or None if no such account. Does not reveal which."""
    user = get_user(email)
    if not user:
        return None
    tok, exp = new_token(), token_expiry()
    conn = _db.connect()
    try:
        cur = conn.cursor()
        cur.execute(_db.q(
            "UPDATE users SET token=?,token_purpose='reset',token_expires=? WHERE id=?"),
            (tok, exp, user["id"]))
        conn.commit()
        return tok
    finally:
        conn.close()


def reset_password(token, new_password):
    problem = password_problem(new_password)
    if problem:
        raise ValueError(problem)
    user = consume_token(token, "reset")
    if not user:
        raise ValueError("That reset link is invalid or has expired.")
    salt, h = hash_pw(new_password)
    conn = _db.connect()
    try:
        cur = conn.cursor()
        # Verifying by email also confirms the address, so mark it verified.
        cur.execute(_db.q(
            "UPDATE users SET pw_salt=?,pw_hash=?,verified=1,token=NULL,"
            "token_purpose=NULL,token_expires=NULL WHERE id=?"),
            (salt, h, user["id"]))
        cur.execute(_db.q("DELETE FROM sessions WHERE user_id=?"), (user["id"],))
        conn.commit()
        return user["id"]
    finally:
        conn.close()


def authenticate(email, password):
    """Returns (user, error). Error messages never reveal whether the email exists."""
    user = get_user(email)
    generic = "Incorrect email or password."
    if not user or not verify_pw(password, user["pw_salt"], user["pw_hash"]):
        return None, generic
    if not user["verified"]:
        return None, "Please verify your email address first — check your inbox."
    return user, None


# ── Sessions ──────────────────────────────────────────
def start_session(user_id, minutes=None):
    minutes = SESSION_TTL_MIN if minutes is None else minutes
    token = secrets.token_urlsafe(32)
    now = _utcnow()
    conn = _db.connect()
    try:
        cur = conn.cursor()
        cur.execute(_db.q(
            "INSERT INTO sessions (token,user_id,created_at,expires_at) VALUES (?,?,?,?)"),
            (token, user_id, _iso(now), _iso(now + timedelta(minutes=minutes))))
        conn.commit()
        return token
    finally:
        conn.close()


def user_for_session(token):
    """Returns the user for a live session, or None. Expired rows are deleted.

    This is the real 15-minute expiry: the server rejects the session itself,
    rather than trusting the browser to forget it.
    """
    if not token:
        return None
    conn = _db.connect()
    try:
        cur = conn.cursor()
        cur.execute(_db.q("SELECT user_id,expires_at FROM sessions WHERE token=?"),
                    (token,))
        row = cur.fetchone()
        if not row:
            return None
        user_id, expires_at = row
        if token_expired(expires_at):
            cur.execute(_db.q("DELETE FROM sessions WHERE token=?"), (token,))
            conn.commit()
            return None
        cur.execute(_db.q(
            "SELECT id,email,pw_salt,pw_hash,verified,token,token_purpose,"
            "token_expires,created_at FROM users WHERE id=?"), (user_id,))
        return _row_to_user(cur.fetchone())
    finally:
        conn.close()


def touch_session(token, minutes=None):
    """Extend a live session — idle timeout rather than a hard cap."""
    minutes = SESSION_TTL_MIN if minutes is None else minutes
    conn = _db.connect()
    try:
        cur = conn.cursor()
        cur.execute(_db.q("UPDATE sessions SET expires_at=? WHERE token=?"),
                    (_iso(_utcnow() + timedelta(minutes=minutes)), token))
        conn.commit()
    finally:
        conn.close()


def end_session(token):
    conn = _db.connect()
    try:
        cur = conn.cursor()
        cur.execute(_db.q("DELETE FROM sessions WHERE token=?"), (token,))
        conn.commit()
    finally:
        conn.close()


def purge_expired_sessions():
    conn = _db.connect()
    try:
        cur = conn.cursor()
        cur.execute(_db.q("DELETE FROM sessions WHERE expires_at < ?"), (_iso(_utcnow()),))
        conn.commit()
    finally:
        conn.close()


# ── Email ─────────────────────────────────────────────
def send_link(email, link, purpose):
    """Send a verification or reset link.

    Returns (sent, fallback_link). When SMTP is not configured or fails, the
    link is returned so the flow still completes in development instead of
    stranding the user — matching the Resume Hub's behaviour.
    """
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    host = os.environ.get("SMTP_HOST", "smtp.resend.com")
    port = int(os.environ.get("SMTP_PORT", "587") or "587")
    sender = os.environ.get("SMTP_FROM", user or "noreply@localhost")
    if not user or not pw:
        return False, link

    if purpose == "reset":
        subject = "Reset your NandaEdge Advisor password"
        intro = "Use the link below to choose a new password."
    else:
        subject = "Verify your NandaEdge Advisor account"
        intro = "Use the link below to confirm your email address."

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"NandaEdge Advisor <{sender}>"
    msg["To"] = email
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(
        f"{intro}\n\n{link}\n\n"
        f"This link expires in {LINK_TTL_MIN} minutes.\n"
        "If you did not request it, you can ignore this email.\n"
    )
    msg.add_alternative(
        f"<p>{intro}</p><p><a href=\"{link}\">{link}</a></p>"
        f"<p style=\"color:#666;font-size:12px\">This link expires in "
        f"{LINK_TTL_MIN} minutes. If you did not request it, ignore this email.</p>",
        subtype="html",
    )
    try:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(msg)
        return True, None
    except Exception as e:
        # Never log credentials; the exception text is enough to diagnose.
        print(f"  WARN account mail failed: {type(e).__name__}: {e}", flush=True)
        return False, link
