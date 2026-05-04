import os
import re
from datetime import datetime, timezone
from pathlib import Path

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from models import Document, User, db

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
ALLOWED_EXTENSIONS = {"pdf"}
UNIQUE_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_-]{3,64}$")


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get(
        "SECRET_KEY", "dev-change-this-in-production"
    )
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        f"sqlite:///{BASE_DIR / 'standard_filing.db'}"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB

    db.init_app(app)
    UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

    with app.app_context():
        db.create_all()
        _seed_default_users()

    @app.route("/")
    def index():
        if session.get("user_id"):
            role = session.get("role")
            if role == "admin":
                return redirect(url_for("admin_dashboard"))
            if role == "agent":
                return redirect(url_for("agent_portal"))
        return redirect(url_for("login"))

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("user_id"):
            return redirect(url_for("index"))

        if request.method == "POST":
            username = (request.form.get("username") or "").strip()
            password = request.form.get("password") or ""
            user = User.query.filter_by(username=username).first()
            if user and user.check_password(password):
                session["user_id"] = user.id
                session["username"] = user.username
                session["role"] = user.role
                flash("Signed in.", "success")
                return redirect(url_for("index"))
            flash("Invalid username or password.", "error")

        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        flash("Signed out.", "success")
        return redirect(url_for("login"))

    def require_admin():
        uid = session.get("user_id")
        if not uid or session.get("role") != "admin":
            return None
        return db.session.get(User, uid)

    def require_agent():
        uid = session.get("user_id")
        if not uid or session.get("role") != "agent":
            return None
        return db.session.get(User, uid)

    @app.route("/admin", methods=["GET", "POST"])
    def admin_dashboard():
        admin = require_admin()
        if not admin:
            flash("Admin access only.", "error")
            return redirect(url_for("login"))

        if request.method == "POST":
            unique_id = (request.form.get("unique_id") or "").strip()
            file = request.files.get("pdf")

            if not UNIQUE_ID_PATTERN.match(unique_id):
                flash(
                    "Unique ID must be 3–64 characters: letters, numbers, underscore, hyphen.",
                    "error",
                )
            elif not file or file.filename == "":
                flash("Choose a PDF file.", "error")
            elif not allowed_file(file.filename):
                flash("Only PDF files are allowed.", "error")
            elif Document.query.filter_by(unique_id=unique_id).first():
                flash("That unique ID is already used. Pick another.", "error")
            else:
                safe_original = secure_filename(file.filename) or "document.pdf"
                stored_name = f"{unique_id}_{secure_filename(safe_original)}"
                path = UPLOAD_FOLDER / stored_name
                file.save(path)
                doc = Document(
                    unique_id=unique_id,
                    stored_filename=stored_name,
                    original_filename=safe_original,
                    uploaded_by_id=admin.id,
                    uploaded_at=datetime.now(timezone.utc),
                )
                db.session.add(doc)
                db.session.commit()
                flash(f"Stored PDF as ID «{unique_id}».", "success")

        docs = Document.query.order_by(Document.uploaded_at.desc()).all()
        return render_template("admin.html", documents=docs)

    @app.post("/admin/delete")
    def admin_delete_document():
        admin = require_admin()
        if not admin:
            flash("Admin access only.", "error")
            return redirect(url_for("login"))

        unique_id = (request.form.get("unique_id") or "").strip()
        doc = Document.query.filter_by(unique_id=unique_id).first()
        if not doc:
            flash("Document not found.", "error")
            return redirect(url_for("admin_dashboard"))

        path = (UPLOAD_FOLDER / doc.stored_filename).resolve()
        base = UPLOAD_FOLDER.resolve()
        try:
            path.relative_to(base)
        except ValueError:
            flash("Could not remove file safely.", "error")
            return redirect(url_for("admin_dashboard"))

        if path.is_file():
            try:
                path.unlink()
            except OSError:
                flash("Could not delete the file on disk. Nothing was removed.", "error")
                return redirect(url_for("admin_dashboard"))

        db.session.delete(doc)
        db.session.commit()
        flash(f"Deleted «{unique_id}».", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/agent", methods=["GET", "POST"])
    def agent_portal():
        agent = require_agent()
        if not agent:
            flash("Agent access only.", "error")
            return redirect(url_for("login"))

        doc = None
        lookup_id = None
        if request.method == "POST":
            lookup_id = (request.form.get("unique_id") or "").strip()
            if not lookup_id:
                flash("Enter a unique ID.", "error")
            else:
                doc = Document.query.filter_by(unique_id=lookup_id).first()
                if not doc:
                    flash("No document found for that ID.", "error")

        return render_template("agent.html", document=doc, lookup_id=lookup_id)

    @app.route("/view/<unique_id>")
    def view_pdf(unique_id: str):
        agent = require_agent()
        if not agent:
            flash("Agent access only.", "error")
            return redirect(url_for("login"))

        doc = Document.query.filter_by(unique_id=unique_id).first()
        if not doc:
            flash("Document not found.", "error")
            return redirect(url_for("agent_portal"))

        return send_from_directory(
            UPLOAD_FOLDER,
            doc.stored_filename,
            mimetype="application/pdf",
            as_attachment=False,
            download_name=doc.original_filename,
        )

    return app


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _seed_default_users() -> None:
    if User.query.first():
        return
    admin = User(username="admin", role="admin")
    admin.set_password("admin123")
    agent = User(username="agent", role="agent")
    agent.set_password("agent123")
    db.session.add_all([admin, agent])
    db.session.commit()


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, port=5000)
