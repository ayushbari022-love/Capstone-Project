"""EventRegister - College Event Registration System (Flask + MySQL).

Supports solo and team registrations, and an optional "pay by QR, admin
verifies" flow for paid events.
"""
import csv
import io
import os
import re
from datetime import date, datetime
from functools import wraps

import mysql.connector
from flask import (Flask, Response, abort, flash, jsonify, redirect,
                   render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from config import (ALLOWED_IMAGE_EXT, COLLEGE_DOMAIN, DB_CONFIG,
                    DEFAULT_ADMIN, MAX_CONTENT_LENGTH, MAX_TEAM_SIZE,
                    MIN_TEAM_SIZE, PROOF_FOLDER, QR_FOLDER, SECRET_KEY,
                    UPLOAD_FOLDER)
from ds import (Trie, UndoStack, WaitlistQueue, as_time, event_datetime,
                events_on_or_after, next_events, top_n)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH

for folder in (QR_FOLDER, PROOF_FOLDER):
    os.makedirs(folder, exist_ok=True)

CATEGORIES = ["Technical", "Cultural", "Sports", "Workshop", "Literary", "Other"]
DEPARTMENTS = ["Computer Engineering", "Information Technology",
               "Electronics & Telecom", "Mechanical", "Civil", "Electrical",
               "AI & Data Science", "Other"]

# Gamification rules (the "unique feature")
REG_XP = 10                                  # for every confirmed registration
PLACE_BONUS = {1: 100, 2: 60, 3: 40}         # bonus XP for winning
LEVELS = [(0, "Rookie", "🌱"), (50, "Explorer", "🧭"), (150, "Achiever", "🚀"),
          (300, "Champion", "🏆"), (500, "Legend", "👑")]

EMAIL_RE = re.compile(r"^[a-z0-9._%+-]+@" + re.escape(COLLEGE_DOMAIN) + r"$")

undo_stacks = {}      # student_id -> UndoStack   (in-memory, per running server)


# =============================================================== DB helpers
def get_db():
    return mysql.connector.connect(**DB_CONFIG)


def query(sql, params=(), one=False):
    """Run a SELECT and return dict rows (or a single row when one=True)."""
    conn = get_db()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(sql, params)
        rows = cur.fetchall()
        return (rows[0] if rows else None) if one else rows
    finally:
        cur.close()
        conn.close()


def execute(sql, params=()):
    """Run INSERT / UPDATE / DELETE and commit. Returns lastrowid."""
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        conn.commit()
        return cur.lastrowid
    finally:
        cur.close()
        conn.close()


def events_with_counts(where="", params=(), order="e.event_date, e.event_time"):
    rows = query(f"""
        SELECT e.*,
          (SELECT COUNT(*) FROM registrations r
            WHERE r.event_id = e.event_id AND r.status = 'confirmed')          AS confirmed_students,
          (SELECT COUNT(DISTINCT r.team_id) FROM registrations r
            WHERE r.event_id = e.event_id AND r.status = 'confirmed')          AS confirmed_teams,
          (SELECT COUNT(*) FROM registrations r
            WHERE r.event_id = e.event_id AND r.status = 'waitlisted')         AS waitlisted_students,
          (SELECT COUNT(DISTINCT r.team_id) FROM registrations r
            WHERE r.event_id = e.event_id AND r.status = 'waitlisted')         AS waitlisted_teams
        FROM events e {where} ORDER BY {order}""", params)
    for e in rows:
        e["confirmed"] = e["confirmed_teams"] if e["is_team_event"] else e["confirmed_students"]
        e["waitlisted"] = e["waitlisted_teams"] if e["is_team_event"] else e["waitlisted_students"]
    return rows


# =============================================================== uploads
def save_upload(file_storage, folder_abs, folder_rel, prefix):
    """Save an uploaded image; return its path relative to /static, or None."""
    if not file_storage or not file_storage.filename:
        return None
    ext = file_storage.filename.rsplit(".", 1)[-1].lower() if "." in file_storage.filename else ""
    if ext not in ALLOWED_IMAGE_EXT:
        raise ValueError("Please upload a PNG, JPG or WEBP image.")
    fname = f"{prefix}_{int(datetime.now().timestamp() * 1000)}.{ext}"
    file_storage.save(os.path.join(folder_abs, fname))
    return f"uploads/{folder_rel}/{fname}"


# =============================================================== template helpers
@app.template_filter("t12")
def t12(t):
    t = as_time(t)
    return t.strftime("%I:%M %p").lstrip("0") if t and not isinstance(t, str) else (t or "")


@app.template_filter("hhmm")
def hhmm(t):
    t = as_time(t)
    if isinstance(t, str):
        return t[:5]
    return t.strftime("%H:%M") if t else ""


@app.template_filter("dfmt")
def dfmt(d):
    return d.strftime("%d %b %Y") if hasattr(d, "strftime") else d


@app.template_filter("medal")
def medal(place):
    return {1: "🥇 1st", 2: "🥈 2nd", 3: "🥉 3rd"}.get(place, str(place))


@app.template_filter("inr")
def inr(amount):
    if amount in (None, ""):
        return ""
    amount = float(amount)
    return f"₹{amount:.0f}" if amount == int(amount) else f"₹{amount:.2f}"


@app.context_processor
def inject_globals():
    return {"today": date.today(), "college_domain": COLLEGE_DOMAIN,
             "min_team": MIN_TEAM_SIZE, "max_team": MAX_TEAM_SIZE}


# =============================================================== auth guards
def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("role") != "admin":
            flash("Please log in as admin first.", "error")
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapper


def student_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get("role") != "student":
            flash("Please log in with your college email first.", "error")
            return redirect(url_for("student_login"))
        return f(*args, **kwargs)
    return wrapper


# =============================================================== gamification
def xp_scores():
    """Hash map {student_id: xp} built from registrations + results."""
    scores = {s["student_id"]: 0 for s in query("SELECT student_id FROM students")}
    for r in query("""SELECT r.student_id, r.status, r.attended, e.points
                      FROM registrations r JOIN events e ON e.event_id = r.event_id"""):
        if r["status"] == "confirmed":
            scores[r["student_id"]] += REG_XP
        if r["attended"]:
            scores[r["student_id"]] += r["points"]
    for w in query("SELECT student_id, place FROM results"):
        scores[w["student_id"]] += PLACE_BONUS.get(w["place"], 0)
    return scores


def level_info(xp):
    idx = max(i for i, (need, _, _) in enumerate(LEVELS) if xp >= need)
    _, name, icon = LEVELS[idx]
    nxt = LEVELS[idx + 1] if idx + 1 < len(LEVELS) else None
    if nxt:
        pct = int((xp - LEVELS[idx][0]) / (nxt[0] - LEVELS[idx][0]) * 100)
        to_next = nxt[0] - xp
    else:
        pct, to_next = 100, 0
    return {"name": name, "icon": icon, "no": idx + 1, "pct": pct,
            "next": nxt[1] if nxt else None, "to_next": to_next}


def build_badges(regs, wins):
    attended = [r for r in regs if r["attended"]]
    categories = {r["category"] for r in attended}
    return [
        {"icon": "👣", "name": "First Step", "desc": "Register for your first event", "earned": len(regs) >= 1},
        {"icon": "🔥", "name": "Regular", "desc": "Register for 5 events", "earned": len(regs) >= 5},
        {"icon": "🎯", "name": "Showed Up", "desc": "Attend an event", "earned": len(attended) >= 1},
        {"icon": "🌈", "name": "All-Rounder", "desc": "Attend events in 3 categories", "earned": len(categories) >= 3},
        {"icon": "🤝", "name": "Team Player", "desc": "Register for a team event", "earned": any(r.get("team_id") for r in regs)},
        {"icon": "🥉", "name": "On the Podium", "desc": "Finish in the top 3", "earned": len(wins) >= 1},
        {"icon": "🥇", "name": "Champion", "desc": "Win 1st place", "earned": any(w["place"] == 1 for w in wins)},
    ]


def student_progress(sid):
    regs = query("""
        SELECT r.reg_id, r.event_id, r.status, r.attended, r.team_id,
               r.payment_status, r.payment_ref, r.paid_amount,
               e.title, e.category, e.venue, e.event_date, e.event_time, e.points,
               e.is_team_event, e.requires_payment, t.team_name, t.leader_id
        FROM registrations r
        JOIN events e ON e.event_id = r.event_id
        LEFT JOIN teams t ON t.team_id = r.team_id
        WHERE r.student_id = %s ORDER BY e.event_date DESC, e.event_time DESC""", (sid,))
    wins = query("""
        SELECT res.result_id, res.event_id, res.place, e.title, e.category, e.event_date
        FROM results res JOIN events e ON e.event_id = res.event_id
        WHERE res.student_id = %s ORDER BY e.event_date DESC""", (sid,))
    scores = xp_scores()
    xp = scores.get(sid, 0)
    win_map = {w["event_id"]: w for w in wins}
    today = date.today()
    is_past = lambda r: r["event_date"] < today or r["event_id"] in win_map
    upcoming = sorted([r for r in regs if not is_past(r)], key=lambda r: r["event_date"])
    past = [r for r in regs if is_past(r)]
    for r in upcoming:
        if r["status"] == "waitlisted":
            ev = {"is_team_event": r["is_team_event"]}
            r["wait_pos"] = waitlist_position(r["event_id"], ev, sid=sid, team_id=r["team_id"])
    for r in past:
        r["win"] = win_map.get(r["event_id"])
    return {
        "xp": xp,
        "level": level_info(xp),
        "rank": 1 + sum(1 for v in scores.values() if v > xp),
        "total_students": len(scores),
        "regs": regs, "upcoming": upcoming, "past": past, "wins": wins,
        "attended": sum(1 for r in regs if r["attended"]),
        "badges": build_badges(regs, wins),
    }


# =============================================================== waitlist (Queue) - team aware
def waiting_units(event_id, ev):
    """Ordered list of waiting units: one row per team (team events) or
    one row per student (solo events), earliest registration first."""
    if ev["is_team_event"]:
        return query("""SELECT r.team_id AS team_id, MIN(r.registered_at) AS first_at
                        FROM registrations r
                        WHERE r.event_id = %s AND r.status = 'waitlisted'
                        GROUP BY r.team_id ORDER BY first_at, r.team_id""", (event_id,))
    return query("""SELECT reg_id, student_id, registered_at FROM registrations
                    WHERE event_id = %s AND status = 'waitlisted'
                    ORDER BY registered_at, reg_id""", (event_id,))


def waitlist_position(event_id, ev, sid=None, team_id=None):
    q = WaitlistQueue(waiting_units(event_id, ev))
    if ev["is_team_event"]:
        return q.position_of(team_id, field="team_id")
    return q.position_of(sid, field="student_id")


def promote_waitlist(event_id):
    """Fill free seats/team-slots from the front of the FIFO waitlist."""
    ev = query("SELECT * FROM events WHERE event_id = %s", (event_id,), one=True)
    if not ev:
        return
    if ev["is_team_event"]:
        confirmed = query("""SELECT COUNT(DISTINCT team_id) AS c FROM registrations
                             WHERE event_id = %s AND status = 'confirmed'""", (event_id,), one=True)["c"]
        for unit in waiting_units(event_id, ev):
            if confirmed >= ev["capacity"]:
                break
            execute("UPDATE registrations SET status = 'confirmed' WHERE event_id = %s AND team_id = %s",
                    (event_id, unit["team_id"]))
            confirmed += 1
    else:
        confirmed = query("""SELECT COUNT(*) AS c FROM registrations
                             WHERE event_id = %s AND status = 'confirmed'""", (event_id,), one=True)["c"]
        for unit in waiting_units(event_id, ev):
            if confirmed >= ev["capacity"]:
                break
            execute("UPDATE registrations SET status = 'confirmed' WHERE reg_id = %s", (unit["reg_id"],))
            confirmed += 1


# =============================================================== registration core (solo + team)
def do_register_core(sid, event_id, team_name=None, teammate_emails=None,
                     payment_ref=None, proof_path=None):
    """Shared by the one-click button, the team/payment form, and Undo.
    teammate_emails: list of emails (team events only), not including sid's own.
    Returns (ok, message, flash_category)."""
    ev = query("SELECT * FROM events WHERE event_id = %s", (event_id,), one=True)
    if not ev:
        return False, "That event no longer exists.", "error"
    if ev["event_date"] < date.today():
        return False, "Registration is closed - this event is over.", "error"

    team_size = ev["team_size"] if ev["is_team_event"] else 1
    member_ids = [sid]

    if ev["is_team_event"]:
        teammate_emails = [e.strip().lower() for e in (teammate_emails or []) if e.strip()]
        me = query("SELECT email FROM students WHERE student_id = %s", (sid,), one=True)
        if me["email"].lower() in teammate_emails:
            return False, "Don't include your own email in the teammates list.", "error"
        if len(set(teammate_emails)) != len(teammate_emails):
            return False, "You listed the same teammate email more than once.", "error"
        if len(teammate_emails) != team_size - 1:
            return False, f"This is a team of {team_size} - enter exactly {team_size - 1} teammate email(s).", "error"
        for mail in teammate_emails:
            mate = query("SELECT student_id, name FROM students WHERE email = %s", (mail,), one=True)
            if not mate:
                return False, f"No EventRegister account found for {mail}. They need to sign up first.", "error"
            member_ids.append(mate["student_id"])
        if not team_name or len(team_name.strip()) < 2:
            return False, "Give your team a name.", "error"

    # No member may already be registered for this event
    for mid in member_ids:
        existing = query("SELECT 1 AS x FROM registrations WHERE student_id = %s AND event_id = %s",
                         (mid, event_id), one=True)
        if existing:
            who = "You are" if mid == sid else "A teammate is"
            return False, f"{who} already registered for this event.", "error"

    # Clash Shield: same date + same start time as another registration, for every member
    for mid in member_ids:
        clash = query("""
            SELECT e.title, s.name FROM registrations r
            JOIN events e ON e.event_id = r.event_id
            JOIN students s ON s.student_id = r.student_id
            WHERE r.student_id = %s AND e.event_id <> %s AND e.event_date = %s
              AND e.event_time = (SELECT event_time FROM events WHERE event_id = %s)""",
            (mid, event_id, ev["event_date"], event_id), one=True)
        if clash:
            who = "You already have" if mid == sid else f"{clash['name']} already has"
            return False, f"Clash Shield: {who} '{clash['title']}' at the same time.", "error"

    if ev["requires_payment"] and not proof_path and not (payment_ref and payment_ref.strip()):
        return False, "Attach a payment screenshot or enter a transaction reference.", "error"

    # Capacity: teams count as one unit, solo registrations count as one unit each
    if ev["is_team_event"]:
        confirmed_units = query("""SELECT COUNT(DISTINCT team_id) AS c FROM registrations
                                   WHERE event_id = %s AND status = 'confirmed'""", (event_id,), one=True)["c"]
    else:
        confirmed_units = query("""SELECT COUNT(*) AS c FROM registrations
                                   WHERE event_id = %s AND status = 'confirmed'""", (event_id,), one=True)["c"]
    status = "confirmed" if confirmed_units < ev["capacity"] else "waitlisted"
    payment_status = "pending" if ev["requires_payment"] else "not_required"

    team_id = None
    if ev["is_team_event"]:
        team_id = execute("INSERT INTO teams (event_id, team_name, leader_id) VALUES (%s, %s, %s)",
                          (event_id, team_name.strip(), sid))

    for mid in member_ids:
        execute("""INSERT INTO registrations
                    (student_id, event_id, status, team_id, payment_status, payment_ref,
                     payment_proof_path, paid_amount)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (mid, event_id, status, team_id, payment_status, (payment_ref or "").strip() or None,
                 proof_path, ev["fee_amount"] if ev["requires_payment"] else None))

    unit_label = f"Team '{team_name.strip()}'" if ev["is_team_event"] else "You're"
    if status == "confirmed":
        msg = f"{unit_label} in! Seat booked for {ev['title']} (+{REG_XP} XP each)."
        if ev["requires_payment"]:
            msg += " Your payment is pending admin verification."
        return True, msg, "success"
    pos = waitlist_position(event_id, ev, sid=sid, team_id=team_id)
    return True, f"{unit_label} #{pos} on the waitlist for {ev['title']}. We'll promote you automatically.", "info"


def cancel_registration_core(sid, event_id, next_page):
    """Solo: cancels the student's own registration. Team: only the leader
    can cancel, and cancelling removes the whole team."""
    reg = query("""SELECT r.reg_id, r.team_id, e.title, e.event_date, e.is_team_event
                   FROM registrations r JOIN events e ON e.event_id = r.event_id
                   WHERE r.student_id = %s AND r.event_id = %s""", (sid, event_id), one=True)
    if not reg:
        flash("You are not registered for that event.", "info")
        return redirect(url_for(next_page))
    if reg["event_date"] < date.today():
        flash("Past events can't be cancelled.", "error")
        return redirect(url_for(next_page))

    if reg["team_id"]:
        team = query("SELECT leader_id, team_name FROM teams WHERE team_id = %s", (reg["team_id"],), one=True)
        if not team or team["leader_id"] != sid:
            flash(f"Only your team leader can cancel this registration.", "error")
            return redirect(url_for(next_page))
        execute("DELETE FROM registrations WHERE team_id = %s", (reg["team_id"],))
        execute("DELETE FROM teams WHERE team_id = %s", (reg["team_id"],))
        promote_waitlist(event_id)
        flash(f"Team '{team['team_name']}' was removed from '{reg['title']}'.", "success")
    else:
        execute("DELETE FROM registrations WHERE reg_id = %s", (reg["reg_id"],))
        undo_stacks.setdefault(sid, UndoStack()).push({"event_id": event_id, "title": reg["title"]})
        promote_waitlist(event_id)
        flash(f"Registration for '{reg['title']}' cancelled. Changed your mind? Use Undo.", "success")
    return redirect(url_for(next_page))


# =============================================================== public pages
@app.route("/")
def home():
    upcoming = events_with_counts("WHERE e.event_date >= CURDATE()")
    soon = next_events(upcoming, 1, now=datetime.now())
    hero = None
    if soon:
        hero = soon[0]
        hero["iso"] = event_datetime(hero).isoformat()
    counts = {
        "events": query("SELECT COUNT(*) AS c FROM events WHERE event_date >= CURDATE()", one=True)["c"],
        "students": query("SELECT COUNT(*) AS c FROM students", one=True)["c"],
        "registrations": query("SELECT COUNT(*) AS c FROM registrations", one=True)["c"],
        "winners": query("SELECT COUNT(*) AS c FROM results", one=True)["c"],
    }
    return render_template("index.html", events=upcoming[:6], hero=hero, counts=counts)


@app.route("/api/search")
def api_search():
    """Trie-powered autocomplete."""
    q = request.args.get("q", "").strip()
    trie = Trie()
    for e in query("SELECT title FROM events WHERE event_date >= CURDATE()"):
        trie.insert(e["title"], e["title"])
        for word in re.split(r"\W+", e["title"]):
            if word:
                trie.insert(word, e["title"])
    return jsonify(trie.search_prefix(q, 6) if q else [])


@app.route("/leaderboard")
def leaderboard_page():
    scores = xp_scores()
    info = {s["student_id"]: s for s in query(
        "SELECT student_id, name, department, study_year FROM students")}
    rows = []
    for i, (sid, xp) in enumerate(top_n(scores, 20), start=1):
        if xp <= 0:
            break
        rows.append({"rank": i, "xp": xp, "level": level_info(xp), **info[sid]})
    return render_template("leaderboard.html", rows=rows)


# =============================================================== auth
@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        user = query("SELECT * FROM admins WHERE username = %s",
                     (request.form.get("username", "").strip(),), one=True)
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session.update(role="admin", admin_name=user["username"])
            return redirect(url_for("admin_dashboard"))
        flash("Wrong username or password.", "error")
    return render_template("admin_login.html")


@app.route("/student/login", methods=["GET", "POST"])
def student_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = query("SELECT * FROM students WHERE email = %s", (email,), one=True)
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            session.clear()
            session.update(role="student", student_id=user["student_id"], student_name=user["name"])
            return redirect(url_for("student_dashboard"))
        flash("Wrong email or password.", "error")
    return render_template("student_login.html")


@app.route("/student/signup", methods=["GET", "POST"])
def student_signup():
    if request.method == "POST":
        f = request.form
        name = f.get("name", "").strip()
        email = f.get("email", "").strip().lower()
        password = f.get("password", "")
        errors = []
        if len(name) < 3:
            errors.append("Enter your full name.")
        if not EMAIL_RE.match(email):
            errors.append(f"Use your college email, for example rahul.sharma@{COLLEGE_DOMAIN}.")
        elif query("SELECT 1 AS x FROM students WHERE email = %s", (email,), one=True):
            errors.append("That email is already registered. Try logging in.")
        if len(password) < 6:
            errors.append("Password must be at least 6 characters.")
        if password != f.get("confirm", ""):
            errors.append("Passwords do not match.")
        if f.get("department") not in DEPARTMENTS:
            errors.append("Choose your department.")
        if f.get("study_year") not in ("1", "2", "3", "4"):
            errors.append("Choose your year of study.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("student_signup.html", form=f, departments=DEPARTMENTS)
        execute("""INSERT INTO students (name, email, password_hash, department, study_year, phone)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (name, email, generate_password_hash(password), f["department"],
                 int(f["study_year"]), f.get("phone", "").strip() or None))
        flash("Account created. Log in to start registering for events.", "success")
        return redirect(url_for("student_login"))
    return render_template("student_signup.html", form={}, departments=DEPARTMENTS)


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("home"))


# =============================================================== admin: events
def read_event_form(f):
    data = {k: f.get(k, "").strip() for k in
            ("title", "category", "description", "venue", "event_date", "event_time", "capacity")}
    errors = []
    if len(data["title"]) < 3:
        errors.append("Event title is required.")
    if data["category"] not in CATEGORIES:
        errors.append("Pick a category.")
    if not data["venue"]:
        errors.append("Venue is required.")
    try:
        datetime.strptime(data["event_date"], "%Y-%m-%d")
    except ValueError:
        errors.append("Pick a valid date.")
    try:
        datetime.strptime(data["event_time"], "%H:%M")
    except ValueError:
        errors.append("Pick a valid start time.")
    try:
        data["capacity"] = int(data["capacity"])
        if data["capacity"] < 1:
            raise ValueError
    except ValueError:
        errors.append("Capacity must be a whole number (minimum 1).")
    try:
        data["points"] = int(f.get("points", "").strip())
        if data["points"] < 0:
            raise ValueError
    except ValueError:
        errors.append("XP points must be a number (minimum 0).")

    data["is_team_event"] = bool(f.get("is_team_event"))
    if data["is_team_event"]:
        try:
            data["team_size"] = int(f.get("team_size", "").strip())
            if not (MIN_TEAM_SIZE <= data["team_size"] <= MAX_TEAM_SIZE):
                raise ValueError
        except ValueError:
            errors.append(f"Team size must be between {MIN_TEAM_SIZE} and {MAX_TEAM_SIZE}.")
            data["team_size"] = None
    else:
        data["team_size"] = None

    data["requires_payment"] = bool(f.get("requires_payment"))
    if data["requires_payment"]:
        try:
            data["fee_amount"] = float(f.get("fee_amount", "").strip())
            if data["fee_amount"] <= 0:
                raise ValueError
        except ValueError:
            errors.append("Enter a valid registration fee.")
            data["fee_amount"] = None
    else:
        data["fee_amount"] = None

    return data, errors


@app.route("/admin")
@admin_required
def admin_dashboard():
    stats = {
        "events": query("SELECT COUNT(*) AS c FROM events", one=True)["c"],
        "upcoming": query("SELECT COUNT(*) AS c FROM events WHERE event_date >= CURDATE()", one=True)["c"],
        "students": query("SELECT COUNT(*) AS c FROM students", one=True)["c"],
        "registrations": query("SELECT COUNT(*) AS c FROM registrations", one=True)["c"],
        "winners": query("SELECT COUNT(*) AS c FROM results", one=True)["c"],
        "pending_payments": query("""SELECT COUNT(*) AS c FROM registrations
                                     WHERE payment_status = 'pending'""", one=True)["c"],
    }
    events = events_with_counts(order="e.event_date DESC, e.event_time DESC")
    return render_template("admin_dashboard.html", stats=stats, events=events)


@app.route("/admin/event/new", methods=["GET", "POST"])
@admin_required
def admin_event_new():
    ev = {"capacity": 100, "points": 50, "event_date": date.today().isoformat(),
          "team_size": MIN_TEAM_SIZE}
    if request.method == "POST":
        ev, errors = read_event_form(request.form)
        qr_path = None
        if not errors and ev["requires_payment"]:
            file = request.files.get("qr_image")
            if not file or not file.filename:
                errors.append("Upload a QR code image so students can pay for this event.")
            else:
                try:
                    qr_path = save_upload(file, QR_FOLDER, "qr", "event")
                except ValueError as exc:
                    errors.append(str(exc))
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            execute("""INSERT INTO events (title, category, description, venue, event_date,
                                           event_time, capacity, points, is_team_event, team_size,
                                           requires_payment, fee_amount, qr_image_path)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (ev["title"], ev["category"], ev["description"], ev["venue"],
                     ev["event_date"], ev["event_time"], ev["capacity"], ev["points"],
                     ev["is_team_event"], ev["team_size"], ev["requires_payment"],
                     ev["fee_amount"], qr_path))
            flash(f"'{ev['title']}' is live on the event board.", "success")
            return redirect(url_for("admin_dashboard"))
    return render_template("admin_event_form.html", ev=ev, categories=CATEGORIES, mode="new")


@app.route("/admin/event/<int:event_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_event_edit(event_id):
    ev = query("SELECT * FROM events WHERE event_id = %s", (event_id,), one=True)
    if not ev:
        abort(404)
    if request.method == "POST":
        qr_path = ev.get("qr_image_path")
        form_ev, errors = read_event_form(request.form)
        if not errors and form_ev["requires_payment"]:
            file = request.files.get("qr_image")
            if file and file.filename:
                try:
                    qr_path = save_upload(file, QR_FOLDER, "qr", "event")
                except ValueError as exc:
                    errors.append(str(exc))
            elif not qr_path:
                errors.append("Upload a QR code image so students can pay for this event.")
        form_ev["event_id"] = event_id
        if errors:
            for e in errors:
                flash(e, "error")
            ev = form_ev
        else:
            execute("""UPDATE events SET title=%s, category=%s, description=%s, venue=%s,
                              event_date=%s, event_time=%s, capacity=%s, points=%s,
                              is_team_event=%s, team_size=%s, requires_payment=%s,
                              fee_amount=%s, qr_image_path=%s
                       WHERE event_id=%s""",
                    (form_ev["title"], form_ev["category"], form_ev["description"], form_ev["venue"],
                     form_ev["event_date"], form_ev["event_time"], form_ev["capacity"], form_ev["points"],
                     form_ev["is_team_event"], form_ev["team_size"], form_ev["requires_payment"],
                     form_ev["fee_amount"], qr_path, event_id))
            promote_waitlist(event_id)          # capacity may have increased
            flash("Event updated.", "success")
            return redirect(url_for("admin_dashboard"))
    return render_template("admin_event_form.html", ev=ev, categories=CATEGORIES, mode="edit")


@app.route("/admin/event/<int:event_id>/delete", methods=["POST"])
@admin_required
def admin_event_delete(event_id):
    execute("DELETE FROM events WHERE event_id = %s", (event_id,))   # cascades to registrations/teams/results
    flash("Event deleted along with its registrations.", "success")
    return redirect(url_for("admin_dashboard"))


# =============================================================== admin: registrations & winners
def registration_groups(event_id, ev):
    """One row per registration, grouped into teams for team events so the
    admin sees one card per team rather than one row per member."""
    rows = query("""
        SELECT r.reg_id, r.status, r.attended, r.registered_at, r.team_id,
               r.payment_status, r.payment_ref, r.payment_proof_path, r.paid_amount,
               s.student_id, s.name, s.email, s.department, s.study_year,
               t.team_name, t.leader_id
        FROM registrations r
        JOIN students s ON s.student_id = r.student_id
        LEFT JOIN teams t ON t.team_id = r.team_id
        WHERE r.event_id = %s
        ORDER BY r.status, r.registered_at, r.reg_id""", (event_id,))
    if not ev["is_team_event"]:
        return [{"team_id": None, "team_name": None, "status": r["status"],
                 "payment_status": r["payment_status"], "payment_ref": r["payment_ref"],
                 "payment_proof_path": r["payment_proof_path"], "paid_amount": r["paid_amount"],
                 "registered_at": r["registered_at"], "members": [r]} for r in rows]
    groups, order = {}, []
    for r in rows:
        key = r["team_id"]
        if key not in groups:
            groups[key] = {"team_id": key, "team_name": r["team_name"], "leader_id": r["leader_id"],
                           "status": r["status"], "payment_status": r["payment_status"],
                           "payment_ref": r["payment_ref"], "payment_proof_path": r["payment_proof_path"],
                           "paid_amount": r["paid_amount"], "registered_at": r["registered_at"], "members": []}
            order.append(key)
        groups[key]["members"].append(r)
    return [groups[k] for k in order]


@app.route("/admin/event/<int:event_id>/registrations")
@admin_required
def admin_registrations(event_id):
    ev = events_with_counts("WHERE e.event_id = %s", (event_id,))
    if not ev:
        abort(404)
    ev = ev[0]
    groups = registration_groups(event_id, ev)
    winners = {w["place"]: w["student_id"] for w in
               query("SELECT place, student_id FROM results WHERE event_id = %s", (event_id,))}
    team_choices = []
    if ev["is_team_event"]:
        for g in groups:
            if g["status"] == "confirmed":
                names = ", ".join(m["name"] for m in g["members"])
                team_choices.append({"team_id": g["team_id"], "label": f"{g['team_name']} ({names})",
                                     "members": g["members"]})
    return render_template("admin_registrations.html", ev=ev, groups=groups, winners=winners,
                           team_choices=team_choices)


@app.route("/admin/registration/<int:reg_id>/attend", methods=["POST"])
@admin_required
def admin_toggle_attend(reg_id):
    reg = query("SELECT event_id, status FROM registrations WHERE reg_id = %s", (reg_id,), one=True)
    if not reg:
        abort(404)
    if reg["status"] == "confirmed":
        execute("UPDATE registrations SET attended = 1 - attended WHERE reg_id = %s", (reg_id,))
    return redirect(url_for("admin_registrations", event_id=reg["event_id"]))


@app.route("/admin/registration/<int:reg_id>/remove", methods=["POST"])
@admin_required
def admin_remove_registration(reg_id):
    reg = query("SELECT event_id, student_id, team_id FROM registrations WHERE reg_id = %s", (reg_id,), one=True)
    if not reg:
        abort(404)
    if reg["team_id"]:
        execute("DELETE FROM registrations WHERE team_id = %s", (reg["team_id"],))
        execute("DELETE FROM teams WHERE team_id = %s", (reg["team_id"],))
    else:
        execute("DELETE FROM registrations WHERE reg_id = %s", (reg_id,))
    execute("DELETE FROM results WHERE event_id = %s AND student_id = %s", (reg["event_id"], reg["student_id"]))
    promote_waitlist(reg["event_id"])
    flash("Registration removed. The next student or team on the waitlist (if any) was promoted.", "success")
    return redirect(url_for("admin_registrations", event_id=reg["event_id"]))


@app.route("/admin/payment/<action>/<kind>/<int:obj_id>", methods=["POST"])
@admin_required
def admin_payment_action(action, kind, obj_id):
    if action not in ("verify", "reject") or kind not in ("reg", "team"):
        abort(404)
    where = "team_id = %s" if kind == "team" else "reg_id = %s"
    rows = query(f"SELECT reg_id, event_id, student_id, team_id FROM registrations WHERE {where}", (obj_id,))
    if not rows:
        abort(404)
    event_id = rows[0]["event_id"]
    if action == "verify":
        execute(f"UPDATE registrations SET payment_status = 'verified' WHERE {where}", (obj_id,))
        flash("Payment verified.", "success")
    else:
        if kind == "team":
            execute("DELETE FROM registrations WHERE team_id = %s", (obj_id,))
            execute("DELETE FROM teams WHERE team_id = %s", (obj_id,))
        else:
            execute("DELETE FROM registrations WHERE reg_id = %s", (obj_id,))
        promote_waitlist(event_id)
        flash("Payment rejected and the seat was released.", "info")
    return redirect(url_for("admin_registrations", event_id=event_id))


@app.route("/admin/event/<int:event_id>/winners", methods=["POST"])
@admin_required
def admin_set_winners(event_id):
    ev = query("SELECT * FROM events WHERE event_id = %s", (event_id,), one=True)
    if not ev:
        abort(404)
    if ev["event_date"] > date.today():
        flash("Winners can be declared on or after the event date.", "error")
        return redirect(url_for("admin_registrations", event_id=event_id))
    picks = {p: request.form.get(f"p{p}", "") for p in (1, 2, 3)}
    chosen = [v for v in picks.values() if v]
    if len(chosen) != len(set(chosen)):
        flash("A student or team cannot hold two positions.", "error")
        return redirect(url_for("admin_registrations", event_id=event_id))

    place_members = {}  # place -> list of student_ids
    for place, pick in picks.items():
        if not pick:
            continue
        if ev["is_team_event"]:
            team = query("""SELECT t.team_id FROM teams t
                            JOIN registrations r ON r.team_id = t.team_id AND r.status = 'confirmed'
                            WHERE t.team_id = %s AND t.event_id = %s LIMIT 1""", (pick, event_id), one=True)
            if not team:
                flash("Winners must be a confirmed team registered for this event.", "error")
                return redirect(url_for("admin_registrations", event_id=event_id))
            members = query("SELECT student_id FROM registrations WHERE team_id = %s", (pick,))
            place_members[place] = [m["student_id"] for m in members]
        else:
            ok = query("""SELECT 1 AS x FROM registrations
                          WHERE event_id = %s AND student_id = %s AND status = 'confirmed'""",
                       (event_id, pick), one=True)
            if not ok:
                flash("Winners must be confirmed registrants of this event.", "error")
                return redirect(url_for("admin_registrations", event_id=event_id))
            place_members[place] = [pick]

    execute("DELETE FROM results WHERE event_id = %s", (event_id,))
    for place, student_ids in place_members.items():
        for sid in student_ids:
            execute("INSERT INTO results (event_id, student_id, place) VALUES (%s, %s, %s)",
                    (event_id, sid, place))
    flash("Results saved. XP and badges are updated for the winners.", "success")
    return redirect(url_for("admin_registrations", event_id=event_id))


# =============================================================== admin: reports
def winners_report(event_id=None, dept=""):
    sql = """SELECT res.result_id, res.place, e.title, e.category, e.event_date,
                    s.name, s.email, s.department, s.study_year
             FROM results res
             JOIN events e   ON e.event_id   = res.event_id
             JOIN students s ON s.student_id = res.student_id
             WHERE 1 = 1"""
    params = []
    if event_id:
        sql += " AND e.event_id = %s"
        params.append(event_id)
    if dept:
        sql += " AND s.department = %s"
        params.append(dept)
    sql += " ORDER BY e.event_date DESC, e.title, res.place"
    return query(sql, tuple(params))


@app.route("/admin/reports")
@admin_required
def admin_reports():
    event_id = request.args.get("event_id", type=int)
    dept = request.args.get("department", "")
    by_event = query("""
        SELECT e.title, COUNT(r.reg_id) AS total,
               COALESCE(SUM(r.attended), 0) AS attended
        FROM events e LEFT JOIN registrations r ON r.event_id = e.event_id
        GROUP BY e.event_id, e.title, e.event_date
        ORDER BY e.event_date DESC LIMIT 8""")
    by_dept = query("""
        SELECT s.department, COUNT(*) AS wins
        FROM results res JOIN students s ON s.student_id = res.student_id
        GROUP BY s.department ORDER BY wins DESC""")
    info = {s["student_id"]: s for s in query("SELECT student_id, name, department FROM students")}
    top = [{"xp": xp, **info[sid]} for sid, xp in top_n(xp_scores(), 5) if xp > 0]
    return render_template(
        "admin_reports.html",
        winners=winners_report(event_id, dept),
        events=query("SELECT event_id, title FROM events ORDER BY event_date DESC"),
        departments=query("SELECT DISTINCT department FROM students ORDER BY department"),
        by_event=by_event, by_dept=by_dept, top=top,
        sel_event=event_id, sel_dept=dept)


@app.route("/admin/reports/export")
@admin_required
def admin_reports_export():
    rows = winners_report(request.args.get("event_id", type=int), request.args.get("department", ""))
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Event", "Category", "Date", "Position", "Student", "Email", "Department", "Year"])
    for r in rows:
        writer.writerow([r["title"], r["category"], r["event_date"], r["place"],
                         r["name"], r["email"], r["department"], r["study_year"]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=winners_report.csv"})


# =============================================================== student
@app.route("/student")
@student_required
def student_dashboard():
    sid = session["student_id"]
    q = request.args.get("q", "").strip().lower()
    from_day = request.args.get("from", "")
    events = events_with_counts("WHERE e.event_date >= CURDATE()")     # already sorted by date
    if from_day:
        try:
            events = events_on_or_after(events, datetime.strptime(from_day, "%Y-%m-%d").date())
        except ValueError:
            flash("That date filter was not valid.", "error")
    if q:
        events = [e for e in events if q in e["title"].lower() or q in e["category"].lower()]
    mine = {r["event_id"]: r for r in query(
        """SELECT r.event_id, r.status, r.team_id, r.payment_status, t.team_name, t.leader_id
           FROM registrations r LEFT JOIN teams t ON t.team_id = r.team_id
           WHERE r.student_id = %s""", (sid,))}
    for e in events:
        mr = mine.get(e["event_id"])
        e["my_status"] = mr["status"] if mr else None
        e["my_team"] = mr if mr else None
        e["i_am_leader"] = bool(mr and (mr["leader_id"] is None or mr["leader_id"] == sid))
        if mr and mr["status"] == "waitlisted":
            e["wait_pos"] = waitlist_position(e["event_id"], e, sid=sid, team_id=mr["team_id"])
    xp = xp_scores().get(sid, 0)
    stack = undo_stacks.get(sid)
    return render_template("student_dashboard.html", events=events, xp=xp, level=level_info(xp),
                           undo_item=stack.peek() if stack else None, q=q, from_day=from_day)


@app.route("/student/register/<int:event_id>", methods=["POST"])
@student_required
def register_event(event_id):
    """Instant one-click registration - only valid for solo, free events.
    Team events and paid events go through the dedicated registration form."""
    ev = query("SELECT is_team_event, requires_payment FROM events WHERE event_id = %s", (event_id,), one=True)
    if ev and (ev["is_team_event"] or ev["requires_payment"]):
        return redirect(url_for("event_register", event_id=event_id))
    ok, msg, cat = do_register_core(session["student_id"], event_id)
    flash(msg, cat)
    return redirect(url_for("student_dashboard"))


@app.route("/student/event/<int:event_id>/register", methods=["GET", "POST"])
@student_required
def event_register(event_id):
    sid = session["student_id"]
    ev = query("SELECT * FROM events WHERE event_id = %s", (event_id,), one=True)
    if not ev:
        abort(404)
    if request.method == "POST":
        teammate_emails = re.split(r"[,\n]+", request.form.get("teammates", "")) if ev["is_team_event"] else None
        proof_path = None
        if ev["requires_payment"]:
            file = request.files.get("proof")
            try:
                proof_path = save_upload(file, PROOF_FOLDER, "payments", f"reg_s{sid}_e{event_id}")
            except ValueError as exc:
                flash(str(exc), "error")
                return render_template("event_register.html", ev=ev, form=request.form)
        ok, msg, cat = do_register_core(
            sid, event_id,
            team_name=request.form.get("team_name", ""),
            teammate_emails=teammate_emails,
            payment_ref=request.form.get("payment_ref", ""),
            proof_path=proof_path)
        flash(msg, cat)
        if ok:
            return redirect(url_for("student_dashboard"))
        return render_template("event_register.html", ev=ev, form=request.form)
    return render_template("event_register.html", ev=ev, form={})


@app.route("/student/cancel/<int:event_id>", methods=["POST"])
@student_required
def cancel_registration(event_id):
    back = "student_profile" if request.form.get("next") == "profile" else "student_dashboard"
    return cancel_registration_core(session["student_id"], event_id, back)


@app.route("/student/undo", methods=["POST"])
@student_required
def undo_cancel():
    stack = undo_stacks.get(session["student_id"])
    item = stack.pop() if stack else None
    if not item:
        flash("Nothing to undo.", "info")
    else:
        _, msg, cat = do_register_core(session["student_id"], item["event_id"])
        flash(msg, cat)
    return redirect(url_for("student_dashboard"))


@app.route("/student/profile", methods=["GET", "POST"])
@student_required
def student_profile():
    sid = session["student_id"]
    if request.method == "POST":
        f = request.form
        name = f.get("name", "").strip()
        errors = []
        if len(name) < 3:
            errors.append("Enter your full name.")
        if f.get("department") not in DEPARTMENTS:
            errors.append("Choose your department.")
        if f.get("study_year") not in ("1", "2", "3", "4"):
            errors.append("Choose your year of study.")
        new_pw = f.get("new_password", "")
        if new_pw:
            me = query("SELECT password_hash FROM students WHERE student_id = %s", (sid,), one=True)
            if not check_password_hash(me["password_hash"], f.get("current_password", "")):
                errors.append("Current password is incorrect.")
            elif len(new_pw) < 6:
                errors.append("New password must be at least 6 characters.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            execute("""UPDATE students SET name=%s, department=%s, study_year=%s, phone=%s
                       WHERE student_id=%s""",
                    (name, f["department"], int(f["study_year"]), f.get("phone", "").strip() or None, sid))
            if new_pw:
                execute("UPDATE students SET password_hash=%s WHERE student_id=%s",
                        (generate_password_hash(new_pw), sid))
            session["student_name"] = name
            flash("Profile updated.", "success")
            return redirect(url_for("student_profile"))
    me = query("SELECT * FROM students WHERE student_id = %s", (sid,), one=True)
    return render_template("student_profile.html", me=me, p=student_progress(sid), departments=DEPARTMENTS)


@app.route("/student/certificate/<int:result_id>")
@student_required
def certificate(result_id):
    row = query("""SELECT res.place, e.title, e.category, e.event_date, s.name, s.department
                   FROM results res
                   JOIN events e   ON e.event_id   = res.event_id
                   JOIN students s ON s.student_id = res.student_id
                   WHERE res.result_id = %s AND res.student_id = %s""",
                (result_id, session["student_id"]), one=True)
    if not row:
        abort(404)
    return render_template("certificate.html", c=row)


@app.errorhandler(404)
def not_found(_):
    return render_template("404.html"), 404


@app.errorhandler(413)
def too_large(_):
    flash("That file is too large. Please upload an image under 5 MB.", "error")
    return redirect(request.referrer or url_for("home"))


# =============================================================== start-up
def bootstrap():
    """Create the default admin the first time the app runs."""
    try:
        if not query("SELECT admin_id FROM admins LIMIT 1", one=True):
            execute("INSERT INTO admins (username, password_hash) VALUES (%s, %s)",
                    (DEFAULT_ADMIN["username"], generate_password_hash(DEFAULT_ADMIN["password"])))
            print(f"[setup] Default admin created -> {DEFAULT_ADMIN['username']} / {DEFAULT_ADMIN['password']}")
    except mysql.connector.Error as err:
        print("[setup] Could not use the database. Check config.py and run the SQL setup in Workbench.")
        print("        MySQL said:", err)


if __name__ == "__main__":
    bootstrap()
    app.run(debug=True)
