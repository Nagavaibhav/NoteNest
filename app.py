from __future__ import annotations

import os
import secrets
import sqlite3
import uuid
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, abort, flash, g, redirect, render_template, request, send_file, send_from_directory, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from services import classify, extract_text, extract_topics, make_excerpt, relevance_score

BASE_DIR = Path(__file__).resolve().parent
ALLOWED_EXTENSIONS = {".pdf", ".ppt", ".pptx", ".docx", ".txt"}


def create_app(test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("NOTENEST_SECRET", "development-change-this-secret"),
        DATABASE=str(BASE_DIR / "instance" / "notenest.db"),
        UPLOAD_FOLDER=str(BASE_DIR / "uploads"),
        MAX_CONTENT_LENGTH=12 * 1024 * 1024,
    )
    if test_config:
        app.config.update(test_config)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    def get_db() -> sqlite3.Connection:
        if "db" not in g:
            g.db = sqlite3.connect(app.config["DATABASE"])
            g.db.row_factory = sqlite3.Row
        return g.db

    @app.teardown_appcontext
    def close_db(_error: BaseException | None = None) -> None:
        db = g.pop("db", None)
        if db is not None:
            db.close()

    def init_db() -> None:
        db = get_db()
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                original_name TEXT NOT NULL,
                stored_name TEXT NOT NULL UNIQUE,
                extension TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                extracted_text TEXT NOT NULL,
                subject TEXT NOT NULL,
                topics TEXT NOT NULL,
                module TEXT NOT NULL DEFAULT 'Unassigned',
                bookmarked INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_notes_user ON notes(user_id);
            CREATE INDEX IF NOT EXISTS idx_notes_subject ON notes(subject);
            CREATE INDEX IF NOT EXISTS idx_notes_module ON notes(module);
            """
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(notes)").fetchall()}
        if "module" not in columns:
            db.execute("ALTER TABLE notes ADD COLUMN module TEXT NOT NULL DEFAULT 'Unassigned'")
        if "bookmarked" not in columns:
            db.execute("ALTER TABLE notes ADD COLUMN bookmarked INTEGER NOT NULL DEFAULT 0")
        db.commit()

    with app.app_context():
        init_db()

    def csrf_token() -> str:
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(24)
        return session["csrf_token"]

    @app.context_processor
    def utility_processor() -> dict[str, Any]:
        return {"csrf_token": csrf_token, "excerpt": make_excerpt}

    def require_login() -> int:
        user_id = session.get("user_id")
        if not user_id:
            flash("Please sign in to access your notes.", "error")
            abort(401)
        return int(user_id)

    def verify_csrf() -> None:
        if not secrets.compare_digest(session.get("csrf_token", ""), request.form.get("csrf_token", "")):
            abort(400, "Invalid form token. Please refresh and try again.")

    def clean_module(value: str) -> str:
        value = " ".join(value.strip().split())
        return value[:60] if value else "Unassigned"

    def fetch_owned_note(note_id: int) -> sqlite3.Row:
        note = get_db().execute("SELECT * FROM notes WHERE id = ? AND user_id = ?", (note_id, require_login())).fetchone()
        if note is None:
            abort(404)
        return note

    @app.errorhandler(401)
    def handle_unauthorized(_error: Exception):
        return redirect(url_for("login"))

    @app.errorhandler(413)
    def handle_too_large(_error: Exception):
        flash("That file is larger than the 12 MB upload limit.", "error")
        return redirect(url_for("upload_note"))

    @app.route("/")
    def home():
        return redirect(url_for("dashboard" if session.get("user_id") else "login"))

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if session.get("user_id"):
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            verify_csrf()
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            if len(name) < 2 or "@" not in email or len(password) < 8:
                flash("Enter a name, a valid email, and a password of at least 8 characters.", "error")
            else:
                try:
                    db = get_db()
                    db.execute(
                        "INSERT INTO users (name, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                        (name, email, generate_password_hash(password), datetime.now(timezone.utc).isoformat()),
                    )
                    db.commit()
                    flash("Your account is ready. Please sign in.", "success")
                    return redirect(url_for("login"))
                except sqlite3.IntegrityError:
                    flash("An account with that email already exists.", "error")
        return render_template("auth.html", mode="register")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("user_id"):
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            verify_csrf()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            user = get_db().execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            if user is None or not check_password_hash(user["password_hash"], password):
                flash("Incorrect email or password.", "error")
            else:
                session.clear()
                session["user_id"] = user["id"]
                session["user_name"] = user["name"]
                csrf_token()
                return redirect(url_for("dashboard"))
        return render_template("auth.html", mode="login")

    @app.post("/logout")
    def logout():
        verify_csrf()
        session.clear()
        flash("You have been signed out.", "success")
        return redirect(url_for("login"))

    @app.get("/dashboard")
    def dashboard():
        user_id = require_login()
        query = request.args.get("q", "").strip()
        subject = request.args.get("subject", "").strip()
        module = request.args.get("module", "").strip()
        file_type = request.args.get("type", "").strip().lower()
        bookmarked = request.args.get("bookmarked") == "1"
        sort = request.args.get("sort", "relevance" if query else "newest")
        db = get_db()
        sql = "SELECT * FROM notes WHERE user_id = ?"
        params: list[Any] = [user_id]
        if query:
            terms = [term for term in query.split() if term]
            for term in terms:
                sql += " AND (original_name LIKE ? OR extracted_text LIKE ? OR subject LIKE ? OR topics LIKE ?)"
                wildcard = f"%{term}%"
                params.extend([wildcard] * 4)
        if subject:
            sql += " AND subject = ?"
            params.append(subject)
        if module:
            sql += " AND module = ?"
            params.append(module)
        if file_type in ALLOWED_EXTENSIONS:
            sql += " AND extension = ?"
            params.append(file_type)
        if bookmarked:
            sql += " AND bookmarked = 1"
        notes = list(db.execute(sql, params).fetchall())
        if sort == "oldest":
            notes.sort(key=lambda note: note["created_at"])
        elif sort == "title":
            notes.sort(key=lambda note: note["original_name"].lower())
        elif sort == "relevance" and query:
            notes.sort(key=lambda note: (relevance_score(note, query), note["created_at"]), reverse=True)
        else:
            notes.sort(key=lambda note: note["created_at"], reverse=True)
        subjects = db.execute("SELECT subject, COUNT(*) AS count FROM notes WHERE user_id = ? GROUP BY subject ORDER BY subject", (user_id,)).fetchall()
        modules = db.execute("SELECT module, COUNT(*) AS count FROM notes WHERE user_id = ? GROUP BY module ORDER BY module", (user_id,)).fetchall()
        stats = db.execute(
            "SELECT COUNT(*) AS total, COUNT(DISTINCT subject) AS subjects, SUM(bookmarked) AS bookmarks, COALESCE(SUM(size_bytes), 0) AS bytes FROM notes WHERE user_id = ?", (user_id,)
        ).fetchone()
        return render_template("dashboard.html", notes=notes, subjects=subjects, modules=modules, stats=stats, query=query, selected_subject=subject, selected_module=module, selected_type=file_type, bookmarked=bookmarked, sort=sort)

    @app.route("/upload", methods=["GET", "POST"])
    def upload_note():
        user_id = require_login()
        if request.method == "POST":
            verify_csrf()
            upload = request.files.get("file")
            if upload is None or not upload.filename:
                flash("Choose a note file to upload.", "error")
                return redirect(url_for("upload_note"))
            original_name = secure_filename(upload.filename)
            extension = Path(original_name).suffix.lower()
            if extension not in ALLOWED_EXTENSIONS:
                flash("Use a PDF, PPT/PPTX, DOCX, or TXT file.", "error")
                return redirect(url_for("upload_note"))
            stored_name = f"{uuid.uuid4().hex}{extension}"
            target = Path(app.config["UPLOAD_FOLDER"]) / stored_name
            upload.save(target)
            try:
                text = extract_text(target, extension).strip()
                if not text:
                    raise ValueError("No selectable text was found in this file. Try a text-based document.")
                subject, _ = classify(text, original_name)
                topics = extract_topics(text, original_name)
                module = clean_module(request.form.get("module", ""))
                db = get_db()
                db.execute(
                    """INSERT INTO notes (user_id, original_name, stored_name, extension, size_bytes, extracted_text, subject, topics, module, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, original_name, stored_name, extension, target.stat().st_size, text, subject, ", ".join(topics), module, datetime.now(timezone.utc).isoformat()),
                )
                db.commit()
                flash(f"{original_name} was organized under {subject}.", "success")
                return redirect(url_for("dashboard"))
            except Exception as error:
                target.unlink(missing_ok=True)
                flash(f"We could not process that file: {error}", "error")
                return redirect(url_for("upload_note"))
        return render_template("upload.html")

    @app.get("/notes/<int:note_id>")
    def note_detail(note_id: int):
        return render_template("note_detail.html", note=fetch_owned_note(note_id))

    @app.get("/notes/<int:note_id>/download")
    def download_note(note_id: int):
        note = fetch_owned_note(note_id)
        return send_from_directory(app.config["UPLOAD_FOLDER"], note["stored_name"], as_attachment=True, download_name=note["original_name"])

    @app.post("/notes/<int:note_id>/delete")
    def delete_note(note_id: int):
        verify_csrf()
        note = fetch_owned_note(note_id)
        get_db().execute("DELETE FROM notes WHERE id = ?", (note_id,))
        get_db().commit()
        (Path(app.config["UPLOAD_FOLDER"]) / note["stored_name"]).unlink(missing_ok=True)
        flash("Note removed from your library.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/notes/<int:note_id>/bookmark")
    def toggle_bookmark(note_id: int):
        verify_csrf()
        note = fetch_owned_note(note_id)
        new_value = 0 if note["bookmarked"] else 1
        get_db().execute("UPDATE notes SET bookmarked = ? WHERE id = ?", (new_value, note_id))
        get_db().commit()
        flash("Bookmark updated.", "success")
        return redirect(request.referrer or url_for("dashboard"))

    @app.post("/notes/<int:note_id>/module")
    def update_module(note_id: int):
        verify_csrf()
        fetch_owned_note(note_id)
        get_db().execute("UPDATE notes SET module = ? WHERE id = ?", (clean_module(request.form.get("module", "")), note_id))
        get_db().commit()
        flash("Module updated.", "success")
        return redirect(url_for("note_detail", note_id=note_id))

    @app.get("/export")
    def export_notes():
        user_id = require_login()
        notes = get_db().execute("SELECT * FROM notes WHERE user_id = ? ORDER BY created_at", (user_id,)).fetchall()
        manifest = {"format": "notenest-export", "version": 1, "notes": []}
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for note in notes:
                file_key = f"files/{note['stored_name']}"
                source = Path(app.config["UPLOAD_FOLDER"]) / note["stored_name"]
                if source.exists():
                    bundle.write(source, file_key)
                manifest["notes"].append({
                    "original_name": note["original_name"], "file": file_key if source.exists() else None,
                    "extension": note["extension"], "extracted_text": note["extracted_text"], "subject": note["subject"],
                    "topics": note["topics"], "module": note["module"], "bookmarked": bool(note["bookmarked"]), "created_at": note["created_at"],
                })
            bundle.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        archive.seek(0)
        return send_file(archive, mimetype="application/zip", as_attachment=True, download_name="notenest-notes.zip")

    @app.route("/import", methods=["GET", "POST"])
    def import_notes():
        user_id = require_login()
        if request.method == "POST":
            verify_csrf()
            upload = request.files.get("archive")
            if upload is None or not upload.filename.lower().endswith(".zip"):
                flash("Choose a NoteNest ZIP export.", "error")
                return redirect(url_for("import_notes"))
            try:
                with zipfile.ZipFile(upload) as bundle:
                    if "manifest.json" not in bundle.namelist():
                        raise ValueError("The ZIP does not contain a manifest file.")
                    manifest = json.loads(bundle.read("manifest.json"))
                    if manifest.get("format") != "notenest-export" or not isinstance(manifest.get("notes"), list):
                        raise ValueError("This is not a valid NoteNest export.")
                    if len(manifest["notes"]) > 500:
                        raise ValueError("Imports are limited to 500 notes at a time.")
                    imported = 0
                    db = get_db()
                    for item in manifest["notes"]:
                        original_name = secure_filename(str(item.get("original_name", "")))
                        extension = Path(original_name).suffix.lower()
                        if not original_name or extension not in ALLOWED_EXTENSIONS:
                            continue
                        extracted_text = str(item.get("extracted_text", "")).strip()
                        if not extracted_text:
                            continue
                        stored_name = f"{uuid.uuid4().hex}{extension}"
                        size_bytes = len(extracted_text.encode("utf-8"))
                        file_key = item.get("file")
                        if isinstance(file_key, str) and file_key.startswith("files/") and file_key in bundle.namelist():
                            if bundle.getinfo(file_key).file_size > app.config["MAX_CONTENT_LENGTH"]:
                                continue
                            payload = bundle.read(file_key)
                            (Path(app.config["UPLOAD_FOLDER"]) / stored_name).write_bytes(payload)
                            size_bytes = len(payload)
                        db.execute(
                            """INSERT INTO notes (user_id, original_name, stored_name, extension, size_bytes, extracted_text, subject, topics, module, bookmarked, created_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (user_id, original_name, stored_name, extension, size_bytes, extracted_text, str(item.get("subject") or "Uncategorized"),
                             str(item.get("topics") or ", ".join(extract_topics(extracted_text, original_name))), clean_module(str(item.get("module", ""))),
                             1 if item.get("bookmarked") else 0, str(item.get("created_at") or datetime.now(timezone.utc).isoformat())),
                        )
                        imported += 1
                    db.commit()
                flash(f"Imported {imported} note{'s' if imported != 1 else ''}.", "success")
                return redirect(url_for("dashboard"))
            except (zipfile.BadZipFile, json.JSONDecodeError, ValueError) as error:
                flash(f"Import failed: {error}", "error")
        return render_template("import.html")

    return app


app = create_app()

if __name__ == "__main__":
    # Port 5000 is commonly occupied by macOS system services over IPv6.
    # A dedicated IPv4 port avoids browser requests being sent to that service.
    app.run(host="127.0.0.1", port=5000, debug=True)
