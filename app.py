import os
import re
import json
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlparse

import click
from flask import (
    Flask, render_template, request, redirect, url_for, flash, session,
    send_from_directory, abort, jsonify
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

from resume_screening.screening import screen_resume


app = Flask(__name__)

# Secure Flask secret key
app.secret_key = os.environ.get("SECRET_KEY")

if not app.secret_key:
    raise RuntimeError(
        "SECRET_KEY environment variable is not set! "
        "Please configure it before starting SmartRecruit."
    )
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
STAGING_FOLDER = os.path.join(UPLOAD_FOLDER, "staging")
SCREENED_FOLDER = os.path.join(UPLOAD_FOLDER, "screened")
DOCUMENT_FOLDER = os.path.join(BASE_DIR, "documents")
DATABASE = os.path.join(BASE_DIR, "smartrecruit.db")

for folder in (UPLOAD_FOLDER, STAGING_FOLDER, SCREENED_FOLDER, DOCUMENT_FOLDER):
    os.makedirs(folder, exist_ok=True)

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024

ALLOWED_RESUMES = {"pdf", "docx"}
ALLOWED_DOCS = {"pdf", "docx", "txt", "png", "jpg", "jpeg"}
VALID_CANDIDATE_STATUSES = {
    "New", "Shortlisted", "Interview", "Selected", "Rejected"
}


# DATABASE CONNECTION
def db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


# SAFE JSON READER
def read_json(value):
    if not value:
        return []
    try:
        result = json.loads(value)
        return result if isinstance(result, (list, dict)) else []
    except (TypeError, ValueError):
        return []


# AUTHENTICATION HELPERS
def current_recruiter():
    recruiter_id = session.get("recruiter_id")
    if not recruiter_id:
        return None

    with db() as con:
        recruiter = con.execute(
            "SELECT * FROM recruiters WHERE id = ? AND is_active = 1",
            (recruiter_id,)
        ).fetchone()

    return recruiter


@app.context_processor
def inject_recruiter():
    return {"current_recruiter": current_recruiter()}


def safe_next_url(target):
    if not target:
        return None
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return None
    if not target.startswith("/") or target.startswith("//"):
        return None
    return target


@app.before_request
def require_login():
    if request.endpoint in {"login", "static"}:
        return None

    recruiter = current_recruiter()
    if recruiter is None:
        session.clear()
        return redirect(url_for("login", next=request.url))

    return None


def admin_required(function):
    @wraps(function)
    def decorated(*args, **kwargs):
        recruiter = current_recruiter()
        if not recruiter or recruiter["role"] != "admin":
            abort(403)
        return function(*args, **kwargs)
    return decorated


# INITIALIZE DATABASE
def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT,
            role TEXT,
            experience TEXT,
            location TEXT,
            status TEXT DEFAULT 'New',
            created_at TEXT,
            resume_filename TEXT,
            match_score REAL,
            matched_skills TEXT,
            missing_skills TEXT,
            score_breakdown TEXT,
            resume_stored_name TEXT
        );

        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            department TEXT,
            location TEXT,
            status TEXT DEFAULT 'Active',
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient TEXT NOT NULL,
            subject TEXT,
            body TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            uploaded_at TEXT
        );

        CREATE TABLE IF NOT EXISTS staged_resumes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            filename TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            uploaded_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS recruiters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            email TEXT,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'recruiter',
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS login_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE,
            ip_address TEXT,
            attempted_at TEXT NOT NULL,
            successful INTEGER NOT NULL DEFAULT 0
        );
        """)

        existing_columns = {
            row["name"] for row in con.execute(
                "PRAGMA table_info(candidates)"
            ).fetchall()
        }

        new_columns = {
            "resume_filename": "TEXT",
            "match_score": "REAL",
            "matched_skills": "TEXT",
            "missing_skills": "TEXT",
            "score_breakdown": "TEXT",
            "resume_stored_name": "TEXT",
        }

        for column, data_type in new_columns.items():
            if column not in existing_columns:
                con.execute(
                    f"ALTER TABLE candidates ADD COLUMN {column} {data_type}"
                )


# LOGIN
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_recruiter():
        return redirect(url_for("dashboard"))

    error = None
    next_url = safe_next_url(
        request.args.get("next") or request.form.get("next")
    )

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        ip_address = request.remote_addr or "unknown"
        now = datetime.now()
        cutoff = (
            now - timedelta(minutes=15)
        ).strftime("%Y-%m-%d %H:%M:%S")

        with db() as con:
            con.execute(
                "DELETE FROM login_attempts WHERE attempted_at < ?",
                (
                    (now - timedelta(days=7)).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                )
            )

            recent_failures = con.execute("""
                SELECT COUNT(*) FROM login_attempts
                WHERE username = ? COLLATE NOCASE
                  AND ip_address = ?
                  AND successful = 0
                  AND attempted_at >= ?
            """, (username, ip_address, cutoff)).fetchone()[0]

            if recent_failures >= 5:
                error = (
                    "Too many failed attempts. "
                    "Please try again in 15 minutes."
                )
            else:
                recruiter = con.execute("""
                    SELECT * FROM recruiters
                    WHERE username = ? COLLATE NOCASE
                      AND is_active = 1
                """, (username,)).fetchone()

                valid = bool(
                    recruiter and check_password_hash(
                        recruiter["password_hash"], password
                    )
                )

                con.execute("""
                    INSERT INTO login_attempts
                    (username, ip_address, attempted_at, successful)
                    VALUES (?, ?, ?, ?)
                """, (
                    username,
                    ip_address,
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    1 if valid else 0
                ))

                if valid:
                    session.clear()
                    session["recruiter_id"] = recruiter["id"]
                    session.permanent = True
                    return redirect(
                        next_url or url_for("dashboard")
                    )

                error = (
                    "Invalid username or password, "
                    "or account is inactive."
                )

    return render_template(
        "login.html",
        error=error,
        next=next_url or ""
    )


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    flash("You have been logged out.")
    return redirect(url_for("login"))


# ADMIN: RECRUITER ACCOUNT MANAGEMENT
@app.route("/admin/recruiters", methods=["GET", "POST"])
@admin_required
def admin_recruiters():
    error = None

    if request.method == "POST":
        action = request.form.get("action", "create")

        if action == "create":
            name = request.form.get("name", "").strip()
            username = request.form.get("username", "").strip()
            email = request.form.get("email", "").strip()
            password = request.form.get("password", "")

            if not name or len(username) < 3:
                error = (
                    "Enter a name and a username "
                    "with at least 3 characters."
                )
            elif len(password) < 8:
                error = "Password must contain at least 8 characters."
            else:
                try:
                    with db() as con:
                        con.execute("""
                            INSERT INTO recruiters
                            (name, username, email, password_hash, role,
                             is_active, created_at)
                            VALUES (?, ?, ?, ?, 'recruiter', 1, ?)
                        """, (
                            name,
                            username,
                            email,
                            generate_password_hash(password),
                            datetime.now().strftime("%d %b %Y %H:%M")
                        ))

                    flash("Recruiter account created successfully.")
                    return redirect(url_for("admin_recruiters"))

                except sqlite3.IntegrityError:
                    error = "That username is already in use."

        elif action == "toggle":
            recruiter_id = request.form.get(
                "recruiter_id", type=int
            )

            if not recruiter_id:
                error = "Invalid recruiter account."
            else:
                with db() as con:
                    target = con.execute(
                        "SELECT * FROM recruiters WHERE id = ?",
                        (recruiter_id,)
                    ).fetchone()

                    if not target:
                        error = "Recruiter account not found."
                    elif target["role"] == "admin":
                        error = (
                            "Admin accounts cannot be changed here."
                        )
                    else:
                        new_status = (
                            0 if target["is_active"] else 1
                        )

                        con.execute(
                            "UPDATE recruiters SET is_active = ? WHERE id = ?",
                            (new_status, recruiter_id)
                        )

                        flash(
                            "Recruiter account deactivated."
                            if not new_status
                            else "Recruiter account activated."
                        )

                        return redirect(url_for("admin_recruiters"))

    with db() as con:
        recruiters = con.execute("""
            SELECT id, name, username, email, role, is_active, created_at
            FROM recruiters
            ORDER BY id DESC
        """).fetchall()

    return render_template(
        "admin_users.html",
        recruiters=recruiters,
        error=error
    )


# CREATE FIRST ADMIN FROM THE COMMAND LINE
@app.cli.command("create-admin")
def create_admin():
    """Create the first SmartRecruit administrator account."""

    with db() as con:
        admin_exists = con.execute(
            "SELECT id FROM recruiters WHERE role = 'admin' LIMIT 1"
        ).fetchone()

    if admin_exists:
        click.echo(
            "An admin account already exists. No account was created."
        )
        return

    name = click.prompt("Full name")
    username = click.prompt("Username")
    email = click.prompt(
        "Email", default="", show_default=False
    )
    password = click.prompt(
        "Password",
        hide_input=True,
        confirmation_prompt=True
    )

    if len(username.strip()) < 3:
        raise click.ClickException(
            "Username must contain at least 3 characters."
        )

    if len(password) < 8:
        raise click.ClickException(
            "Password must contain at least 8 characters."
        )

    try:
        with db() as con:
            con.execute("""
                INSERT INTO recruiters
                (name, username, email, password_hash, role,
                 is_active, created_at)
                VALUES (?, ?, ?, ?, 'admin', 1, ?)
            """, (
                name.strip(),
                username.strip(),
                email.strip(),
                generate_password_hash(password),
                datetime.now().strftime("%d %b %Y %H:%M")
            ))

        click.echo(
            "Admin account created. You can now sign in at /login."
        )

    except sqlite3.IntegrityError:
        raise click.ClickException(
            "That username is already in use."
        )


# EXTRACT JOB TITLE
def get_job_title(description):
    match = re.search(
        r"(?im)^\s*Job\s*Title\s*:\s*(.+)$",
        description
    )

    return (
        match.group(1).strip()
        if match else "AI Screened Candidate"
    )


# CREATE CANDIDATE NAME FROM FILENAME
def get_candidate_name(filename):
    name = os.path.splitext(filename)[0]
    name = re.sub(r"[_-]+", " ", name)
    name = re.sub(r"\s+", " ", name)

    return name.strip().title() or "Unknown Candidate"


# SAVE SCREENING RESULT
def save_screening_candidate(filename, result, role, file_path):
    candidate_name = get_candidate_name(filename)

    extension = os.path.splitext(filename)[1].lower()
    stored_name = f"{uuid.uuid4().hex}{extension}"

    permanent_path = os.path.join(
        SCREENED_FOLDER, stored_name
    )

    shutil.copy2(file_path, permanent_path)

    try:
        with db() as con:
            con.execute("""
                INSERT INTO candidates (
                    name, email, role, experience, location, status,
                    created_at, resume_filename, match_score,
                    matched_skills, missing_skills, score_breakdown,
                    resume_stored_name
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                candidate_name,
                "",
                role,
                "",
                "",
                "New",
                datetime.now().strftime("%d %b %Y %H:%M"),
                filename,
                result["match_score"],
                json.dumps(result["matched_skills"]),
                json.dumps(result["missing_skills"]),
                json.dumps(result["score_breakdown"]),
                stored_name
            ))

    except Exception:
        if os.path.exists(permanent_path):
            os.remove(permanent_path)
        raise


# DASHBOARD STATISTICS
def get_counts():
    with db() as con:
        return {
            "total_candidates": con.execute(
                "SELECT COUNT(*) FROM candidates"
            ).fetchone()[0],

            "active_jobs": con.execute(
                "SELECT COUNT(*) FROM jobs WHERE status='Active'"
            ).fetchone()[0],

            "shortlisted": con.execute(
                "SELECT COUNT(*) FROM candidates WHERE status='Shortlisted'"
            ).fetchone()[0],

            "interviews": con.execute(
                "SELECT COUNT(*) FROM candidates WHERE status='Interview'"
            ).fetchone()[0],
        }
# DASHBOARD
@app.route("/")
def dashboard():
    with db() as con:
        candidates = con.execute(
            "SELECT * FROM candidates ORDER BY id DESC LIMIT 5"
        ).fetchall()

        jobs = con.execute("""
            SELECT jobs.*,
            (SELECT COUNT(*) FROM candidates) AS candidates
            FROM jobs ORDER BY id DESC LIMIT 5
        """).fetchall()

    return render_template(
        "index.html",
        stats=get_counts(),
        candidates=candidates,
        jobs=jobs
    )


# CANDIDATES
@app.route("/candidates", methods=["GET", "POST"])
def candidates_page():
    if request.method == "POST":
        action = request.form.get("action", "add")

        # UPDATE STATUS
        if action == "update_status":
            candidate_id = request.form.get(
                "candidate_id", type=int
            )
            status = request.form.get("status", "").strip()

            if candidate_id and status in VALID_CANDIDATE_STATUSES:
                with db() as con:
                    cursor = con.execute(
                        "UPDATE candidates SET status = ? WHERE id = ?",
                        (status, candidate_id)
                    )

                flash(
                    "Candidate status updated."
                    if cursor.rowcount else "Candidate not found."
                )
            else:
                flash("Invalid candidate status.")

            return redirect(url_for("candidates_page"))

        # ADD CANDIDATE
        name = request.form.get("name", "").strip()

        if name:
            status = request.form.get("status", "New").strip()

            if status not in VALID_CANDIDATE_STATUSES:
                status = "New"

            with db() as con:
                con.execute("""
                    INSERT INTO candidates
                    (name, email, role, experience, location, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    name,
                    request.form.get("email", "").strip(),
                    request.form.get("role", "").strip(),
                    request.form.get("experience", "").strip(),
                    request.form.get("location", "").strip(),
                    status,
                    datetime.now().strftime("%d %b %Y %H:%M")
                ))

            flash("Candidate added successfully.")

        return redirect(url_for("candidates_page"))

    with db() as con:
        rows = con.execute(
            "SELECT * FROM candidates ORDER BY id DESC"
        ).fetchall()

    candidates = []

    for row in rows:
        candidate = dict(row)

        candidate["matched_skills_list"] = read_json(
            candidate.get("matched_skills")
        )

        candidate["missing_skills_list"] = read_json(
            candidate.get("missing_skills")
        )

        candidate["score_breakdown_dict"] = read_json(
            candidate.get("score_breakdown")
        )

        candidates.append(candidate)

    return render_template(
        "module.html",
        section="candidates",
        title="Candidates",
        subtitle="Manage candidate profiles and recruitment progress.",
        rows=candidates
    )


# DOWNLOAD SCREENED RESUME
@app.route("/candidate-resume/<int:candidate_id>")
def download_candidate_resume(candidate_id):
    with db() as con:
        candidate = con.execute("""
            SELECT resume_filename, resume_stored_name
            FROM candidates WHERE id = ?
        """, (candidate_id,)).fetchone()

    if not candidate or not candidate["resume_stored_name"]:
        abort(404)

    return send_from_directory(
        SCREENED_FOLDER,
        candidate["resume_stored_name"],
        as_attachment=True,
        download_name=candidate["resume_filename"] or "resume"
    )


# CANDIDATE PROFILE
@app.route("/candidate/<int:candidate_id>", methods=["GET", "POST"])
def candidate_profile(candidate_id):
    if request.method == "POST":
        status = request.form.get("status", "").strip()

        if status not in VALID_CANDIDATE_STATUSES:
            flash("Invalid candidate status.")
            return redirect(
                url_for("candidate_profile", candidate_id=candidate_id)
            )

        with db() as con:
            cursor = con.execute(
                "UPDATE candidates SET status = ? WHERE id = ?",
                (status, candidate_id)
            )

        if cursor.rowcount == 0:
            abort(404)

        flash("Candidate status updated.")

        return redirect(
            url_for("candidate_profile", candidate_id=candidate_id)
        )

    with db() as con:
        row = con.execute(
            "SELECT * FROM candidates WHERE id = ?",
            (candidate_id,)
        ).fetchone()

    if not row:
        abort(404)

    candidate = dict(row)

    candidate["matched_skills_list"] = read_json(
        candidate.get("matched_skills")
    )

    candidate["missing_skills_list"] = read_json(
        candidate.get("missing_skills")
    )

    candidate["score_breakdown_dict"] = read_json(
        candidate.get("score_breakdown")
    )

    return render_template(
        "candidate_profile.html",
        candidate=candidate,
        title="Candidate Profile"
    )


# RECRUITMENT PIPELINE
@app.route("/pipeline")
def recruitment_pipeline():
    pipeline_statuses = [
        "New",
        "Shortlisted",
        "Interview",
        "Selected",
        "Rejected"
    ]

    with db() as con:
        rows = con.execute(
            "SELECT * FROM candidates ORDER BY id DESC"
        ).fetchall()

    candidates = [dict(row) for row in rows]

    pipeline = {
        status: [
            candidate
            for candidate in candidates
            if candidate.get("status") == status
        ]
        for status in pipeline_statuses
    }

    return render_template(
        "pipeline.html",
        pipeline=pipeline,
        pipeline_statuses=pipeline_statuses,
        title="Recruitment Pipeline"
    )


# UPDATE PIPELINE STATUS
@app.route("/pipeline/update-status", methods=["POST"])
def update_pipeline_status():
    data = request.get_json(silent=True) or {}

    candidate_id = data.get("candidate_id")
    new_status = data.get("status", "").strip()

    if new_status not in VALID_CANDIDATE_STATUSES:
        return jsonify({
            "success": False,
            "message": "Invalid recruitment stage."
        }), 400

    try:
        candidate_id = int(candidate_id)
    except (TypeError, ValueError):
        return jsonify({
            "success": False,
            "message": "Invalid candidate ID."
        }), 400

    with db() as con:
        cursor = con.execute(
            "UPDATE candidates SET status = ? WHERE id = ?",
            (new_status, candidate_id)
        )

    if cursor.rowcount == 0:
        return jsonify({
            "success": False,
            "message": "Candidate not found."
        }), 404

    return jsonify({
        "success": True,
        "message": "Candidate moved successfully.",
        "status": new_status
    })


# JOB OPENINGS
@app.route("/jobs", methods=["GET", "POST"])
def jobs_page():
    if request.method == "POST":
        title = request.form.get("title", "").strip()

        if title:
            with db() as con:
                con.execute("""
                    INSERT INTO jobs
                    (title, department, location, status, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (
                    title,
                    request.form.get("department", "").strip(),
                    request.form.get("location", "").strip(),
                    request.form.get("status", "Active"),
                    datetime.now().strftime("%d %b %Y %H:%M")
                ))

            flash("Job opening created.")

        return redirect(url_for("jobs_page"))

    with db() as con:
        rows = con.execute(
            "SELECT * FROM jobs ORDER BY id DESC"
        ).fetchall()

    return render_template(
        "module.html",
        section="jobs",
        title="Job Openings",
        subtitle="Create vacancies and track active hiring needs.",
        rows=rows
    )
# AI RESUME SCREENING
@app.route("/resume-screening", methods=["GET", "POST"])
def resume_screening():
    results = []
    error = None

    if "screening_id" not in session:
        session["screening_id"] = uuid.uuid4().hex

    screening_id = session["screening_id"]

    description = session.get(
        "screening_job_description", ""
    )

    con = db()

    try:
        if request.method == "POST":
            action = request.form.get("action", "add")

            submitted_description = request.form.get(
                "job_description", ""
            ).strip()

            if submitted_description:
                description = submitted_description
                session["screening_job_description"] = description

            # ADD RESUME
            if action == "add":
                resume = request.files.get("resume")

                if not description:
                    error = "Please enter the job description first."

                elif not resume or not resume.filename:
                    error = "Please choose a resume to upload."

                else:
                    safe_name = secure_filename(resume.filename)

                    extension = (
                        safe_name.rsplit(".", 1)[-1].lower()
                        if "." in safe_name else ""
                    )

                    if extension not in ALLOWED_RESUMES:
                        error = "Only PDF and DOCX files are supported."

                    else:
                        stored_name = f"{uuid.uuid4().hex}.{extension}"

                        file_path = os.path.join(
                            STAGING_FOLDER, stored_name
                        )

                        try:
                            resume.save(file_path)

                            con.execute("""
                                INSERT INTO staged_resumes
                                (session_id, filename, stored_name, uploaded_at)
                                VALUES (?, ?, ?, ?)
                            """, (
                                screening_id,
                                safe_name,
                                stored_name,
                                datetime.now().strftime("%d %b %Y %H:%M")
                            ))

                            con.commit()
                            flash(f"{safe_name} added successfully.")

                        except Exception as exc:
                            print("Upload error:", exc)

                            if os.path.exists(file_path):
                                os.remove(file_path)

                            error = "Unable to upload this resume."

            # REMOVE ONE RESUME
            elif action == "remove":
                resume_id = request.form.get(
                    "resume_id", type=int
                )

                if resume_id:
                    row = con.execute("""
                        SELECT stored_name
                        FROM staged_resumes
                        WHERE id = ? AND session_id = ?
                    """, (resume_id, screening_id)).fetchone()

                    if row:
                        file_path = os.path.join(
                            STAGING_FOLDER,
                            row["stored_name"]
                        )

                        if os.path.exists(file_path):
                            os.remove(file_path)

                        con.execute("""
                            DELETE FROM staged_resumes
                            WHERE id = ? AND session_id = ?
                        """, (resume_id, screening_id))

                        con.commit()
                        flash("Resume removed.")

            # CLEAR ALL RESUMES
            elif action == "clear":
                rows = con.execute(
                    "SELECT stored_name FROM staged_resumes WHERE session_id = ?",
                    (screening_id,)
                ).fetchall()

                for row in rows:
                    file_path = os.path.join(
                        STAGING_FOLDER,
                        row["stored_name"]
                    )

                    if os.path.exists(file_path):
                        os.remove(file_path)

                con.execute(
                    "DELETE FROM staged_resumes WHERE session_id = ?",
                    (screening_id,)
                )

                con.commit()
                flash("All staged resumes cleared.")

            # ANALYSE ALL RESUMES
            elif action == "analyse":
                if not description:
                    error = "Please enter the job description."

                else:
                    rows = con.execute("""
                        SELECT id, filename, stored_name
                        FROM staged_resumes
                        WHERE session_id = ?
                        ORDER BY id
                    """, (screening_id,)).fetchall()

                    if not rows:
                        error = "Please add at least one resume first."

                    else:
                        role = get_job_title(description)

                        for row in rows:
                            filename = row["filename"]

                            file_path = os.path.join(
                                STAGING_FOLDER,
                                row["stored_name"]
                            )

                            try:
                                if not os.path.exists(file_path):
                                    raise FileNotFoundError(
                                        "Staged resume not found."
                                    )

                                screening_result = screen_resume(
                                    file_path,
                                    description
                                )

                                save_screening_candidate(
                                    filename,
                                    screening_result,
                                    role,
                                    file_path
                                )

                                results.append({
                                    "filename": filename,
                                    "score": screening_result["match_score"],
                                    "score_breakdown": screening_result["score_breakdown"],
                                    "matched_skills": screening_result["matched_skills"],
                                    "missing_skills": screening_result["missing_skills"],
                                    "error": None
                                })

                            except Exception as exc:
                                print(
                                    "Screening error:",
                                    filename,
                                    exc
                                )

                                results.append({
                                    "filename": filename,
                                    "error": (
                                        "Unable to read this resume. "
                                        "Check the file."
                                    )
                                })

                            finally:
                                if os.path.exists(file_path):
                                    os.remove(file_path)

                        con.execute(
                            "DELETE FROM staged_resumes WHERE session_id = ?",
                            (screening_id,)
                        )

                        con.commit()

                        results.sort(
                            key=lambda item: item.get("score", -1),
                            reverse=True
                        )

    finally:
        con.close()

    with db() as con:
        staged_resumes = con.execute("""
            SELECT id, filename, uploaded_at
            FROM staged_resumes
            WHERE session_id = ?
            ORDER BY id
        """, (screening_id,)).fetchall()

    return render_template(
        "screening.html",
        results=results,
        error=error,
        staged_resumes=staged_resumes,
        job_description=description
    )


# RESUME STUDIO
@app.route("/resume-studio", methods=["GET", "POST"])
def resume_studio():
    generated = None

    if request.method == "POST":
        name = request.form.get(
            "candidate_name", ""
        ).strip()

        role = request.form.get(
            "target_role", ""
        ).strip()

        skills = request.form.get(
            "skills", ""
        ).strip()

        experience = request.form.get(
            "experience", ""
        ).strip()

        generated = (
            f"{name}\n"
            f"Target role: {role}\n\n"
            f"Professional summary\n{experience}\n\n"
            f"Key skills\n{skills}"
        )

    return render_template(
        "module.html",
        section="resume-studio",
        title="Resume Studio",
        subtitle="Create a basic professional summary draft.",
        generated=generated
    )


# DOCUMENTS
@app.route("/documents", methods=["GET", "POST"])
def documents_page():
    error = None

    if request.method == "POST":
        file = request.files.get("document")

        if not file or not file.filename:
            error = "Choose a document to upload."

        else:
            safe_name = secure_filename(file.filename)

            extension = (
                safe_name.rsplit(".", 1)[-1].lower()
                if "." in safe_name else ""
            )

            if extension not in ALLOWED_DOCS:
                error = "Allowed formats: PDF, DOCX, TXT, PNG, JPG."

            else:
                stored_name = f"{uuid.uuid4().hex}_{safe_name}"

                file.save(
                    os.path.join(DOCUMENT_FOLDER, stored_name)
                )

                with db() as con:
                    con.execute("""
                        INSERT INTO documents
                        (filename, stored_name, uploaded_at)
                        VALUES (?, ?, ?)
                    """, (
                        safe_name,
                        stored_name,
                        datetime.now().strftime("%d %b %Y %H:%M")
                    ))

                flash("Document uploaded.")
                return redirect(url_for("documents_page"))

    with db() as con:
        rows = con.execute(
            "SELECT * FROM documents ORDER BY id DESC"
        ).fetchall()

    return render_template(
        "module.html",
        section="documents",
        title="Documents",
        subtitle="Upload and manage recruitment files.",
        rows=rows,
        error=error
    )


# MESSAGES
@app.route("/messages", methods=["GET", "POST"])
def messages_page():
    if request.method == "POST":
        recipient = request.form.get(
            "recipient", ""
        ).strip()

        if recipient:
            with db() as con:
                con.execute("""
                    INSERT INTO messages
                    (recipient, subject, body, created_at)
                    VALUES (?, ?, ?, ?)
                """, (
                    recipient,
                    request.form.get("subject", "").strip(),
                    request.form.get("body", "").strip(),
                    datetime.now().strftime("%d %b %Y %H:%M")
                ))

            flash("Message saved in the local message log.")

        return redirect(url_for("messages_page"))

    with db() as con:
        rows = con.execute(
            "SELECT * FROM messages ORDER BY id DESC"
        ).fetchall()

    return render_template(
        "module.html",
        section="messages",
        title="Messages",
        subtitle="Draft and save candidate communications.",
        rows=rows
    )


# ERROR HANDLERS
@app.errorhandler(403)
def forbidden(error):
    return (
        "Access denied. Admin permission is required for this page.",
        403
    )


@app.errorhandler(413)
def too_large(error):
    return (
        "Upload too large. Please keep the request below 10 MB.",
        413
    )


# INITIALIZE DATABASE
init_db()


# RUN APPLICATION
if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5001,
        debug=False,
        use_reloader=False,
        threaded=True
    )