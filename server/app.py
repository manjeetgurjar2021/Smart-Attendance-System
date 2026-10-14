"""Smart Attendance System - cloud server (Flask + SQLite).

Receives RFID scans (+ optional JPEG photo) from the ESP32-CAM, logs one
attendance record per student per day, and serves a live dashboard.
"""
import csv
import hmac
import io
import os
import re
import sqlite3
from datetime import datetime, timedelta
from functools import wraps
from zoneinfo import ZoneInfo

from flask import (Flask, Response, g, jsonify, render_template, request,
                   send_from_directory)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "attendance.db"))
CAPTURE_DIR = os.environ.get("CAPTURE_DIR", os.path.join(BASE_DIR, "captures"))
API_KEY = os.environ.get("API_KEY", "change-me")          # device -> server
ADMIN_USER = os.environ.get("ADMIN_USER", "")             # dashboard login
ADMIN_PASS = os.environ.get("ADMIN_PASS", "")             # (disabled if empty)
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Kolkata"))
MAX_EPOCH_SKEW = 7 * 24 * 3600   # accept queued offline scans up to 7 days old
UID_RE = re.compile(r"^[0-9A-F]{8,20}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
    uid        TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    roll_no    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attendance (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    uid   TEXT NOT NULL,
    date  TEXT NOT NULL,
    time  TEXT NOT NULL,
    photo TEXT,
    UNIQUE (uid, date)
);
CREATE TABLE IF NOT EXISTS unknown_scans (
    uid       TEXT PRIMARY KEY,
    last_seen TEXT NOT NULL,
    count     INTEGER NOT NULL DEFAULT 1
);
"""

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024   # 2 MB per request
os.makedirs(CAPTURE_DIR, exist_ok=True)


# ---------------------------------------------------------------- database
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.executescript(SCHEMA)


init_db()


# ------------------------------------------------------------------ helpers
def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if ADMIN_USER and ADMIN_PASS:
            auth = request.authorization
            ok = (auth is not None
                  and hmac.compare_digest(auth.username or "", ADMIN_USER)
                  and hmac.compare_digest(auth.password or "", ADMIN_PASS))
            if not ok:
                return Response("Login required", 401,
                                {"WWW-Authenticate": 'Basic realm="Attendance"'})
        return fn(*args, **kwargs)
    return wrapper


def normalize_uid(raw):
    uid = re.sub(r"[^0-9A-Fa-f]", "", raw or "").upper()
    return uid if UID_RE.match(uid) else None


def scan_time():
    """Server time, or the device's NTP time (X-Scan-Epoch) for queued scans."""
    now = datetime.now(TIMEZONE)
    raw = request.headers.get("X-Scan-Epoch", "")
    if raw.isdigit():
        ts = datetime.fromtimestamp(int(raw), TIMEZONE)
        if now - timedelta(seconds=MAX_EPOCH_SKEW) <= ts <= now + timedelta(minutes=5):
            return ts
    return now


def save_photo(data, uid, when):
    if len(data) < 4 or data[:2] != b"\xff\xd8":      # JPEG magic bytes
        return None
    name = f"{when.strftime('%Y%m%d_%H%M%S')}_{uid}.jpg"
    with open(os.path.join(CAPTURE_DIR, name), "wb") as fh:
        fh.write(data)
    return name


def parse_date(value):
    if not value:
        return datetime.now(TIMEZONE).strftime("%Y-%m-%d")
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


# ------------------------------------------------------- device endpoint
@app.post("/api/scan")
def api_scan():
    """Called by the ESP32-CAM. Headers: X-API-Key, X-Card-UID, [X-Scan-Epoch].
    Body: raw JPEG bytes (optional)."""
    if not hmac.compare_digest(request.headers.get("X-API-Key", ""), API_KEY):
        return jsonify(error="unauthorized"), 401
    uid = normalize_uid(request.headers.get("X-Card-UID"))
    if not uid:
        return jsonify(error="invalid card uid"), 400

    db = get_db()
    when = scan_time()
    date, clock = when.strftime("%Y-%m-%d"), when.strftime("%H:%M:%S")

    student = db.execute("SELECT * FROM students WHERE uid=?", (uid,)).fetchone()
    if student is None:
        db.execute(
            "INSERT INTO unknown_scans (uid, last_seen) VALUES (?, ?) "
            "ON CONFLICT(uid) DO UPDATE SET last_seen=excluded.last_seen, count=count+1",
            (uid, f"{date} {clock}"))
        db.commit()
        return jsonify(status="unknown", uid=uid), 404

    existing = db.execute("SELECT time FROM attendance WHERE uid=? AND date=?",
                          (uid, date)).fetchone()
    if existing:
        return jsonify(status="duplicate", name=student["name"],
                       time=existing["time"]), 200

    photo = save_photo(request.get_data(), uid, when)
    try:
        db.execute("INSERT INTO attendance (uid, date, time, photo) VALUES (?,?,?,?)",
                   (uid, date, clock, photo))
        db.commit()
    except sqlite3.IntegrityError:                      # race: double tap
        return jsonify(status="duplicate", name=student["name"]), 200
    return jsonify(status="marked", name=student["name"], time=clock), 201


# ------------------------------------------------------ dashboard + API
@app.get("/")
@admin_required
def dashboard():
    return render_template("dashboard.html")


@app.get("/api/health")
def health():
    return jsonify(status="ok")


@app.get("/api/attendance")
@admin_required
def api_attendance():
    date = parse_date(request.args.get("date"))
    if date is None:
        return jsonify(error="date must be YYYY-MM-DD"), 400
    db = get_db()
    present = db.execute(
        "SELECT a.id, a.uid, COALESCE(s.name, a.uid) AS name, "
        "COALESCE(s.roll_no, '') AS roll_no, a.date, a.time, a.photo "
        "FROM attendance a LEFT JOIN students s ON s.uid = a.uid "
        "WHERE a.date=? ORDER BY a.time DESC", (date,)).fetchall()
    absent = db.execute(
        "SELECT uid, name, roll_no FROM students "
        "WHERE uid NOT IN (SELECT uid FROM attendance WHERE date=?) ORDER BY name",
        (date,)).fetchall()
    total = db.execute("SELECT COUNT(*) FROM students").fetchone()[0]
    return jsonify(date=date, total=total,
                   present=[dict(r) for r in present],
                   absent=[dict(r) for r in absent])


@app.get("/api/history")
@admin_required
def api_history():
    try:
        days = min(max(int(request.args.get("days", 7)), 1), 60)
    except ValueError:
        days = 7
    today = datetime.now(TIMEZONE).date()
    start = today - timedelta(days=days - 1)
    rows = get_db().execute(
        "SELECT a.date, COUNT(*) AS c FROM attendance a "
        "JOIN students s ON s.uid = a.uid WHERE a.date >= ? GROUP BY a.date",
        (start.strftime("%Y-%m-%d"),)).fetchall()
    counts = {r["date"]: r["c"] for r in rows}
    out = []
    for i in range(days):
        d = (start + timedelta(days=i)).strftime("%Y-%m-%d")
        out.append({"date": d, "present": counts.get(d, 0)})
    return jsonify(history=out)


@app.get("/api/students")
@admin_required
def api_students():
    rows = get_db().execute("SELECT uid, name, roll_no FROM students ORDER BY name").fetchall()
    return jsonify(students=[dict(r) for r in rows])


@app.post("/api/students")
@admin_required
def api_add_student():
    data = request.get_json(silent=True) or {}
    uid = normalize_uid(data.get("uid"))
    name = (data.get("name") or "").strip()
    roll_no = (data.get("roll_no") or "").strip()
    if not uid:
        return jsonify(error="card UID must be 8-20 hex characters"), 400
    if not name or len(name) > 80 or len(roll_no) > 30:
        return jsonify(error="name is required (max 80 chars)"), 400
    db = get_db()
    try:
        db.execute("INSERT INTO students (uid, name, roll_no, created_at) VALUES (?,?,?,?)",
                   (uid, name, roll_no, datetime.now(TIMEZONE).isoformat(timespec="seconds")))
    except sqlite3.IntegrityError:
        return jsonify(error="this card is already registered"), 409
    db.execute("DELETE FROM unknown_scans WHERE uid=?", (uid,))
    db.commit()
    return jsonify(status="created", uid=uid), 201


@app.delete("/api/students/<uid>")
@admin_required
def api_delete_student(uid):
    uid = normalize_uid(uid)
    if not uid:
        return jsonify(error="invalid uid"), 400
    db = get_db()
    db.execute("DELETE FROM students WHERE uid=?", (uid,))
    db.commit()
    return jsonify(status="deleted")


@app.get("/api/unknown")
@admin_required
def api_unknown():
    rows = get_db().execute(
        "SELECT uid, last_seen, count FROM unknown_scans ORDER BY last_seen DESC LIMIT 20"
    ).fetchall()
    return jsonify(unknown=[dict(r) for r in rows])


@app.get("/export.csv")
@admin_required
def export_csv():
    db = get_db()
    base = ("SELECT a.date, a.time, a.uid, COALESCE(s.name, '') AS name, "
            "COALESCE(s.roll_no, '') AS roll_no FROM attendance a "
            "LEFT JOIN students s ON s.uid = a.uid ")
    if request.args.get("all"):
        rows = db.execute(base + "ORDER BY a.date DESC, a.time DESC").fetchall()
        fname = "attendance_all.csv"
    else:
        date = parse_date(request.args.get("date"))
        if date is None:
            return jsonify(error="date must be YYYY-MM-DD"), 400
        rows = db.execute(base + "WHERE a.date=? ORDER BY a.time", (date,)).fetchall()
        fname = f"attendance_{date}.csv"
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["date", "time", "card_uid", "name", "roll_no"])
    for r in rows:
        writer.writerow([r["date"], r["time"], r["uid"], r["name"], r["roll_no"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@app.get("/captures/<path:filename>")
@admin_required
def captures(filename):
    return send_from_directory(CAPTURE_DIR, filename)


if __name__ == "__main__":
    # 0.0.0.0 so the ESP32 on your Wi-Fi can reach this PC
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
