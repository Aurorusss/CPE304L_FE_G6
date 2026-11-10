#!/usr/bin/env python3
"""BulSU Meeting Room Reservation - zero-dependency backend (Python stdlib + SQLite).
Run:  python server.py   ->  http://localhost:8000"""
import sqlite3, json, os, hashlib, secrets, re, random
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(DIR, "reservations.db")
OPEN_H, CLOSE_H, GRACE_MIN, MAX_PENALTY = 7, 20, 15, 3
SESSIONS = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS student_org(
 org_id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(100) NOT NULL UNIQUE,
 email VARCHAR(100) NOT NULL UNIQUE, rep_name VARCHAR(100) NOT NULL,
 password VARCHAR(200) NOT NULL,                       -- changed: stores salted hash, not plain text
 penalty_count INTEGER NOT NULL DEFAULT 0 CHECK(penalty_count>=0),
 is_suspended BOOLEAN NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS admin(
 admin_id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(100) NOT NULL UNIQUE,
 email VARCHAR(100) NOT NULL UNIQUE, user_name VARCHAR(100) NOT NULL, password VARCHAR(200) NOT NULL);
CREATE TABLE IF NOT EXISTS meeting_room(
 room_id INTEGER PRIMARY KEY AUTOINCREMENT, room_name VARCHAR(50) NOT NULL UNIQUE,
 location VARCHAR(50) NOT NULL,                        -- changed: no longer UNIQUE (rooms can share a building)
 building VARCHAR(50) NOT NULL DEFAULT '',             -- added: UI shows "Learning Hub - Science Building"
 capacity INTEGER NOT NULL CHECK(capacity>0),
 amenities VARCHAR(100) NOT NULL DEFAULT '',           -- added: UI shows "Airconditioned, Whiteboard"
 is_available BOOLEAN NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS reservation(
 reservation_id INTEGER PRIMARY KEY AUTOINCREMENT,
 org_id INTEGER NOT NULL REFERENCES student_org(org_id),
 admin_id INTEGER REFERENCES admin(admin_id),          -- changed: nullable until an admin acts on it
 room_id INTEGER NOT NULL REFERENCES meeting_room(room_id),
 date DATE NOT NULL, start_time TIME NOT NULL, end_time TIME NOT NULL,
 group_size INTEGER NOT NULL CHECK(group_size>0),      -- added: UI "Group size"
 status VARCHAR(15) NOT NULL CHECK(status IN('Pending','Approved','Rejected','Cancelled','Released','Completed')),
 check_in_code VARCHAR(20) UNIQUE, is_checked_in BOOLEAN NOT NULL DEFAULT 0,
 created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
 CHECK(end_time>start_time));
"""

def hp(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    return salt + "$" + hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 50000).hex()
def vp(pw, stored): return hp(pw, stored.split("$")[0]) == stored

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); return c

def init():
    c = db(); c.executescript(SCHEMA)
    if not c.execute("SELECT 1 FROM meeting_room").fetchone():
        c.executemany("INSERT INTO meeting_room(room_name,location,building,capacity,amenities) VALUES(?,?,?,?,?)", [
            ("Student Center", "Campus Grounds", "Floor 2", 10, "Airconditioned,Whiteboard"),
            ("Room 308", "Learning Hub", "Science Building", 15, "TV,Whiteboard"),
            ("Library Nook", "Main Library", "Ground Floor", 6, "Airconditioned")])
        c.execute("INSERT INTO student_org(name,email,rep_name,password) VALUES(?,?,?,?)",
                  ("CPE Organization", "cpe@bulsu.edu.ph", "Juan Dela Cruz", hp("cpe12345")))
        c.execute("INSERT INTO admin(name,email,user_name,password) VALUES(?,?,?,?)",
                  ("Room Administrator", "admin@bulsu.edu.ph", "admin", hp("admin123")))
    c.commit(); c.close()

def release_sweep(c):
    """FR-004: auto-release approved bookings not checked in within 15 min of start; add a penalty."""
    now = datetime.now(); changed = False
    for r in c.execute("SELECT * FROM reservation WHERE status='Approved' AND is_checked_in=0").fetchall():
        start = datetime.fromisoformat(f"{r['date']} {r['start_time']}")
        if now > start + timedelta(minutes=GRACE_MIN):
            c.execute("UPDATE reservation SET status='Released',updated_at=CURRENT_TIMESTAMP WHERE reservation_id=?", (r["reservation_id"],))
            c.execute("UPDATE student_org SET penalty_count=penalty_count+1, is_suspended=(penalty_count+1>=?) WHERE org_id=?", (MAX_PENALTY, r["org_id"]))
            changed = True
    for r in c.execute("SELECT * FROM reservation WHERE status='Approved' AND is_checked_in=1").fetchall():
        if now > datetime.fromisoformat(f"{r['date']} {r['end_time']}"):
            c.execute("UPDATE reservation SET status='Completed' WHERE reservation_id=?", (r["reservation_id"],)); changed = True
    if changed: c.commit()

SELECT_RES = """SELECT r.*, m.room_name, m.location, m.building, m.capacity, m.amenities, o.name AS org_name
 FROM reservation r JOIN meeting_room m USING(room_id) JOIN student_org o USING(org_id)"""

def conflict(c, room, date, st, et, ignore=None):
    return c.execute("""SELECT 1 FROM reservation WHERE room_id=? AND date=? AND status IN('Pending','Approved')
        AND start_time<? AND end_time>? AND reservation_id IS NOT ?""", (room, date, et, st, ignore)).fetchone() is not None

class Err(Exception):
    def __init__(s, msg, code=400): s.msg, s.code = msg, code

def validate_slot(date, st, et):
    if not (re.fullmatch(r"\d{4}-\d\d-\d\d", date or "") and re.fullmatch(r"\d\d:\d\d", st or "") and re.fullmatch(r"\d\d:\d\d", et or "")):
        raise Err("Invalid date or time.")
    if et <= st: raise Err("End time must be after start time.")
    if not (f"{OPEN_H:02d}:00" <= st and et <= f"{CLOSE_H:02d}:00"): raise Err(f"Rooms are open {OPEN_H}:00-{CLOSE_H}:00.")
    if datetime.fromisoformat(f"{date} {st}") < datetime.now() - timedelta(minutes=1): raise Err("That time has already passed.")

def api(method, path, q, body, user):
    c = db(); release_sweep(c)
    try:
        if path == "/api/register" and method == "POST":
            e = (body.get("email") or "").strip().lower()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e): raise Err("Enter a valid email.")
            if len(body.get("password") or "") < 6: raise Err("Password must be at least 6 characters.")
            if not (body.get("name") or "").strip() or not (body.get("rep_name") or "").strip(): raise Err("Organization and representative names are required.")
            try:
                c.execute("INSERT INTO student_org(name,email,rep_name,password) VALUES(?,?,?,?)", (body["name"].strip(), e, body["rep_name"].strip(), hp(body["password"]))); c.commit()
            except sqlite3.IntegrityError: raise Err("Organization name or email already registered.")
            return {"ok": True}
        if path == "/api/login" and method == "POST":
            e = (body.get("email") or "").strip().lower(); pw = body.get("password") or ""
            for role, tbl in (("org", "student_org"), ("admin", "admin")):
                u = c.execute(f"SELECT * FROM {tbl} WHERE email=?", (e,)).fetchone()
                if u and vp(pw, u["password"]):
                    t = secrets.token_hex(16); SESSIONS[t] = (role, u[0]); return {"token": t, "role": role}
            raise Err("Incorrect email or password.", 401)
        if not user: raise Err("Please log in.", 401)
        role, uid = user
        if path == "/api/me":
            if role == "org":
                u = dict(c.execute("SELECT org_id,name,email,rep_name,penalty_count,is_suspended FROM student_org WHERE org_id=?", (uid,)).fetchone())
                return {"role": role, "max_penalty": MAX_PENALTY, "grace": GRACE_MIN, **u}
            return {"role": role, **dict(c.execute("SELECT admin_id,name,email FROM admin WHERE admin_id=?", (uid,)).fetchone())}
        if path == "/api/rooms/available":
            date, st, et, size = q.get("date"), q.get("start"), q.get("end"), int(q.get("size", 1))
            validate_slot(date, st, et)
            rows = c.execute("SELECT * FROM meeting_room WHERE is_available=1 AND capacity>=? ORDER BY capacity", (size,)).fetchall()
            return [dict(r) for r in rows if not conflict(c, r["room_id"], date, st, et)]
        if path == "/api/reservations" and method == "GET":
            if role == "admin": rows = c.execute(SELECT_RES + " ORDER BY date DESC,start_time DESC").fetchall()
            else: rows = c.execute(SELECT_RES + " WHERE org_id=? ORDER BY date DESC,start_time DESC", (uid,)).fetchall()
            return [dict(r) for r in rows]
        if path == "/api/reservations" and method == "POST":
            if role != "org": raise Err("Only organizations can book.", 403)
            org = c.execute("SELECT * FROM student_org WHERE org_id=?", (uid,)).fetchone()
            if org["is_suspended"]: raise Err("Your organization is suspended due to penalties. Contact the administrator.", 403)
            room = c.execute("SELECT * FROM meeting_room WHERE room_id=?", (body.get("room_id"),)).fetchone()
            date, st, et, size = body.get("date"), body.get("start"), body.get("end"), int(body.get("group_size", 0))
            validate_slot(date, st, et)
            if not room or not room["is_available"]: raise Err("Room not found.", 404)
            if size < 1 or size > room["capacity"]: raise Err(f"Group size must be 1-{room['capacity']} for this room.")
            c.execute("BEGIN IMMEDIATE")                 # FR-002: check + insert atomically, no double-booking
            if conflict(c, room["room_id"], date, st, et): c.rollback(); raise Err("Sorry, that slot was just taken.", 409)
            code = f"MR-{date[2:4]}{date[5:7]}{date[8:10]}-{random.randint(0, 999):03d}"
            while c.execute("SELECT 1 FROM reservation WHERE check_in_code=?", (code,)).fetchone(): code = code[:-3] + f"{random.randint(0,999):03d}"
            cur = c.execute("""INSERT INTO reservation(org_id,room_id,date,start_time,end_time,group_size,status,check_in_code)
                VALUES(?,?,?,?,?,?, 'Approved', ?)""", (uid, room["room_id"], date, st, et, size, code))   # auto-approved to match UI; admin can still reject/cancel
            c.commit()
            return dict(c.execute(SELECT_RES + " WHERE reservation_id=?", (cur.lastrowid,)).fetchone())
        m = re.fullmatch(r"/api/reservations/(\d+)/(cancel|checkin|approve|reject)", path)
        if m and method == "POST":
            rid, act = int(m[1]), m[2]
            r = c.execute("SELECT * FROM reservation WHERE reservation_id=?", (rid,)).fetchone()
            if not r: raise Err("Not found.", 404)
            if act in ("cancel", "checkin") and (role != "org" or r["org_id"] != uid) and not (act == "cancel" and role == "admin"):
                raise Err("Not allowed.", 403)                 # NFR-002
            if act in ("approve", "reject") and role != "admin": raise Err("Admins only.", 403)
            if act == "cancel":
                if r["status"] not in ("Pending", "Approved"): raise Err("This reservation can't be cancelled.")
                c.execute("UPDATE reservation SET status='Cancelled',updated_at=CURRENT_TIMESTAMP WHERE reservation_id=?", (rid,))
            elif act == "checkin":
                if r["status"] != "Approved": raise Err("Only approved reservations can be checked in.")
                if (body.get("code") or "").strip().upper() != r["check_in_code"]: raise Err("Wrong check-in code.")
                start = datetime.fromisoformat(f"{r['date']} {r['start_time']}")
                if datetime.now() < start - timedelta(minutes=30): raise Err("Check-in opens 30 minutes before the start time.")
                c.execute("UPDATE reservation SET is_checked_in=1,updated_at=CURRENT_TIMESTAMP WHERE reservation_id=?", (rid,))
            else:
                if act == "approve" and conflict(c, r["room_id"], r["date"], r["start_time"], r["end_time"], rid): raise Err("Conflicts with another booking.", 409)
                st = "Approved" if act == "approve" else "Rejected"
                code = r["check_in_code"] or f"MR-{rid:04d}"
                c.execute("UPDATE reservation SET status=?,admin_id=?,check_in_code=?,updated_at=CURRENT_TIMESTAMP WHERE reservation_id=?", (st, uid, code, rid))
            c.commit()
            return dict(c.execute(SELECT_RES + " WHERE reservation_id=?", (rid,)).fetchone())
        if path == "/api/admin/orgs" and role == "admin":
            return [dict(r) for r in c.execute("SELECT org_id,name,email,rep_name,penalty_count,is_suspended FROM student_org")]
        m = re.fullmatch(r"/api/admin/orgs/(\d+)/reset", path)
        if m and role == "admin" and method == "POST":
            c.execute("UPDATE student_org SET penalty_count=0,is_suspended=0 WHERE org_id=?", (m[1],)); c.commit(); return {"ok": True}
        raise Err("Not found.", 404)
    finally: c.close()

class H(BaseHTTPRequestHandler):
    def send(self, code, data, ctype="application/json"):
        b = data if isinstance(data, bytes) else json.dumps(data).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def handle_any(self, method):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        if not u.path.startswith("/api/"):
            return self.send(200, open(os.path.join(DIR, "index.html"), "rb").read(), "text/html; charset=utf-8")
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            user = SESSIONS.get((self.headers.get("Authorization") or "").replace("Bearer ", ""))
            self.send(200, api(method, u.path, {k: v[0] for k, v in parse_qs(u.query).items()}, body, user))
        except Err as e: self.send(e.code, {"error": e.msg})
        except (ValueError, KeyError, TypeError): self.send(400, {"error": "Bad request."})
    def do_GET(self): self.handle_any("GET")
    def do_POST(self): self.handle_any("POST")
    def log_message(self, *a): pass

if __name__ == "__main__":
    init(); port = int(os.environ.get("PORT", 8000))
    print(f"Running at http://localhost:{port}\n  Org login:   cpe@bulsu.edu.ph / cpe12345\n  Admin login: admin@bulsu.edu.ph / admin123")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
