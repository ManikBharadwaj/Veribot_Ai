"""
main.py - The WEB SERVER of VeriBot AI.

If you are new to FastAPI, read this first
-------------------------------------------
A "route" is a Python function that runs when the browser visits an address.
You mark a function as a route with a line above it, called a decorator:

        @app.get("/login")        <- runs when the browser OPENS /login
        @app.post("/login")       <- runs when a FORM is SUBMITTED to /login

HOW DATA FLOWS (the whole app in 6 lines)
-----------------------------------------
  1. You log in on /login.  The server remembers you in a signed cookie (session).
  2. The dashboard page (templates/index.html) opens at "/".
  3. You drop resume PDFs in the page and click "Analyze".
  4. The page's JavaScript sends the files to POST /api/analyze.
  5. This file passes them to agent.py, which returns a report for each resume.
  6. We save the reports in the database and send them back to the page as JSON.

Files in this project
---------------------
  main.py              <- you are here (routes, login, saving, CSV export)
  models.py            <- the database tables
  agent.py             <- the verification engine (the "brain")
  templates/index.html <- the dashboard page
  templates/auth.html  <- the login and register page

Run it with:   python -m uvicorn main:app --reload
"""

import csv
import contextvars
import hmac
import io
import json
import logging
import os
import secrets
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager

import bcrypt
import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile
from starlette.middleware.sessions import SessionMiddleware
from starlette.concurrency import run_in_threadpool

import agent
import queue_worker
from models import Candidate, SessionLocal, User, create_tables, get_db

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ----------------------------------------------------------------------------
# SETTINGS
# ----------------------------------------------------------------------------

# The secret key signs the login cookie so nobody can fake a login.
SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
ENVIRONMENT = os.getenv("ENVIRONMENT", os.getenv("ENV", "development")).strip().lower()
IS_PRODUCTION = ENVIRONMENT == "production"
MAX_RESUMES_PER_BATCH = 2000
MAX_DOCUMENTS_PER_BATCH = 2000
MAX_FILE_MB = 8
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
MAX_BATCH_BYTES = 128 * 1024 * 1024
MAX_MULTIPART_OVERHEAD = 8 * 1024 * 1024
CSRF_MAX_AGE_SECONDS = 60 * 60 * 8
CSRF_SERIALIZER = URLSafeTimedSerializer(SECRET_KEY or "startup-validation-required", salt="veribot-csrf-v1")
request_id_context = contextvars.ContextVar("request_id", default="-")


class JsonLogFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "correlation_id", request_id_context.get()),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=True)


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(message)s")
for _handler in logging.getLogger().handlers:
    _handler.setFormatter(JsonLogFormatter())
logger = logging.getLogger("veribot.web")
_auth_rate_lock = threading.Lock()
_auth_rate_fallback = {}


class RequestTooLarge(Exception):
    pass


class UploadBodyLimitMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"] not in {"/api/analyze", "/api/verify"}
        ):
            await self.app(scope, receive, send)
            return

        limit = (
            MAX_BATCH_BYTES + MAX_MULTIPART_OVERHEAD
            if scope["path"] == "/api/analyze"
            else MAX_FILE_BYTES + MAX_MULTIPART_OVERHEAD
        )
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                if int(content_length) > limit:
                    await self._reject(send)
                    return
            except ValueError:
                await self._reject(send)
                return

        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise RequestTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except RequestTooLarge:
            await self._reject(send)

    @staticmethod
    async def _reject(send):
        body = b'{"detail":"Upload request exceeds the configured size limit."}'
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            }
        )
        await send({"type": "http.response.body", "body": body})


def validate_startup_configuration():
    normalized_secret = SECRET_KEY.strip().lower()
    is_placeholder = normalized_secret in {"dev-only-change-me", "secret", "changeme"} or normalized_secret.startswith(
        ("change-me", "your-secret", "replace-me", "dev-only")
    )
    if not SECRET_KEY or len(SECRET_KEY) < 64 or is_placeholder or len(set(SECRET_KEY)) < 10:
        raise RuntimeError(
            "Invalid SECRET_KEY: configure a randomly generated, non-default secret containing at least 64 characters."
        )
    if IS_PRODUCTION and not os.getenv("DATABASE_URL", "").strip():
        raise RuntimeError("DATABASE_URL must be configured when ENVIRONMENT=production.")
    if not IS_PRODUCTION:
        create_tables()


@asynccontextmanager
async def app_lifespan(_app):
    validate_startup_configuration()
    worker_thread = None
    worker_stop_event = None
    if ENVIRONMENT == "development" and queue_worker.redis_client is not None:
        worker_stop_event = threading.Event()
        worker_thread = threading.Thread(
            target=queue_worker.run_worker,
            kwargs={"stop_event": worker_stop_event},
            name="veribot-development-queue-worker",
            daemon=True,
        )
        worker_thread.start()
        logger.info("Started the development queue worker")
    try:
        yield
    finally:
        if worker_stop_event is not None and worker_thread is not None:
            worker_stop_event.set()
            worker_thread.join(timeout=10)
            if worker_thread.is_alive():
                logger.warning("Development queue worker did not stop before the shutdown timeout.")


app = FastAPI(title="VeriBot AI", lifespan=app_lifespan)

# SessionMiddleware always sets HttpOnly; https_only adds Secure in production.
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY or "startup-validation-required",
    same_site="strict",
    https_only=IS_PRODUCTION,
    max_age=60 * 60 * 8,
)
app.add_middleware(UploadBodyLimitMiddleware)

templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


@app.middleware("http")
async def request_security_headers(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID", "")
    if not request_id or len(request_id) > 128 or any(ord(char) < 33 or ord(char) > 126 for char in request_id):
        request_id = str(uuid.uuid4())
    token = request_id_context.set(request_id)
    request.state.request_id = request_id
    request.state.csp_nonce = secrets.token_urlsafe(18)
    try:
        response = await call_next(request)
    finally:
        request_id_context.reset(token)
    nonce = request.state.csp_nonce
    response.headers["X-Request-ID"] = request_id
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}' https://cdn.tailwindcss.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.tailwindcss.com; "
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; "
        "frame-ancestors 'none'; form-action 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


def issue_csrf_token(request: Request) -> str:
    nonce = request.session.get("csrf_nonce")
    if not nonce:
        nonce = secrets.token_urlsafe(32)
        request.session["csrf_nonce"] = nonce
    return CSRF_SERIALIZER.dumps(nonce)


async def require_csrf(request: Request):
    supplied = request.headers.get("X-CSRF-Token")
    if not supplied and request.method != "DELETE":
        form = await request.form(max_files=MAX_RESUMES_PER_BATCH + MAX_DOCUMENTS_PER_BATCH + 2, max_fields=50)
        supplied = form.get("csrf_token")
    nonce = request.session.get("csrf_nonce")
    if not supplied or not nonce:
        raise HTTPException(status_code=403, detail="CSRF validation failed.")
    try:
        decoded = CSRF_SERIALIZER.loads(supplied, max_age=CSRF_MAX_AGE_SECONDS)
    except BadSignature as error:
        raise HTTPException(status_code=403, detail="CSRF validation failed.") from error
    if not isinstance(decoded, str) or not hmac.compare_digest(decoded, nonce):
        raise HTTPException(status_code=403, detail="CSRF validation failed.")


def enforce_auth_rate_limit(request: Request):
    ip_address = request.client.host if request.client else "unknown"
    key = "veribot_auth_rate:" + request.url.path + ":" + ip_address
    if queue_worker.redis_client is not None:
        try:
            count = int(queue_worker.redis_client.incr(key))
            if count == 1:
                queue_worker.redis_client.expire(key, 60)
        except Exception as error:
            logger.exception("Authentication rate limiter failed")
            if IS_PRODUCTION:
                raise HTTPException(status_code=503, detail="Authentication rate limiting is unavailable.") from error
            count = _fallback_rate_count(key)
    else:
        if IS_PRODUCTION:
            raise HTTPException(status_code=503, detail="Authentication rate limiting is unavailable.")
        count = _fallback_rate_count(key)
    if count > 5:
        raise HTTPException(status_code=429, detail="Too many authentication attempts. Try again in one minute.")


def _fallback_rate_count(key: str) -> int:
    now = time.monotonic()
    with _auth_rate_lock:
        start, count = _auth_rate_fallback.get(key, (now, 0))
        if now - start >= 60:
            start, count = now, 0
        count += 1
        _auth_rate_fallback[key] = (start, count)
        return count


def cleanup_scratch_directory(scratch_dir):
    try:
        target = scratch_dir.resolve()
        if queue_worker.UPLOAD_ROOT.resolve() not in target.parents:
            raise ValueError("Refusing to clean a path outside the configured upload scratch directory.")
        shutil.rmtree(target)
    except FileNotFoundError:
        return
    except (OSError, ValueError):
        logger.exception("Could not clean batch upload scratch directory")


# ============================================================================
# HELPERS - passwords and "who is logged in?"
# ============================================================================

def hash_password(plain_password):
    """Scramble a password with bcrypt. The result is safe to store."""
    return bcrypt.hashpw(plain_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def password_is_correct(plain_password, stored_hash):
    """Check a typed password against the stored bcrypt hash."""
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), stored_hash.encode("utf-8"))
    except ValueError:
        return False  # e.g. password longer than bcrypt allows


def get_current_user(request, db):
    """
    Look at the session cookie and return the logged-in User, or None.
    Uses JWT for session authentication.
    """
    token = request.session.get("jwt_token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        user_id = payload.get("user_id")
        return db.query(User).filter(User.id == user_id).first()
    except jwt.PyJWTError:
        return None


def require_user(request: Request, db: Session = Depends(get_db)):
    """
    Used by API routes:  user = Depends(require_user)
    If nobody is logged in, the request stops here with a 401 error.
    """
    user = get_current_user(request, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Please log in first.")
    return user


# ============================================================================
# PAGES - login, register, logout, dashboard
# ============================================================================

def show_auth_page(request, mode, error="", name="", email="", status_code=200):
    """Render the login or register page (both use templates/auth.html)."""
    return templates.TemplateResponse(
        request,
        "auth.html",
        {
            "mode": mode,
            "error": error,
            "name": name,
            "email": email,
            "csrf_token": issue_csrf_token(request),
            "csp_nonce": request.state.csp_nonce,
        },
        status_code=status_code,
    )


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    """The main dashboard. Visitors who are not logged in are sent to /login."""
    user = get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "user": user,
            "llm_enabled": bool(agent.LLM_API_KEY),
            "github_token_set": bool(agent.GITHUB_TOKEN),
            "csrf_token": issue_csrf_token(request),
            "csp_nonce": request.state.csp_nonce,
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return show_auth_page(request, "login")


@app.post("/login", dependencies=[Depends(require_csrf)])
def login_submit(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    """Runs when the login form is submitted."""
    enforce_auth_rate_limit(request)
    email = email.strip().lower()
    user = db.query(User).filter(User.email == email).first()

    # We give the SAME message for "no such email" and "wrong password",
    # so nobody can use this form to find out which emails are registered.
    if user is None or not password_is_correct(password, user.password_hash):
        return show_auth_page(request, "login", "Incorrect email or password.", email=email, status_code=400)

    # Create JWT session token
    token = jwt.encode({"user_id": user.id}, SECRET_KEY, algorithm="HS256")
    request.session.clear()
    request.session["jwt_token"] = token
    return RedirectResponse("/", status_code=303)


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return show_auth_page(request, "register")


@app.post("/register", dependencies=[Depends(require_csrf)])
def register_submit(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form("officer"),
    db: Session = Depends(get_db),
):
    """Runs when the register form is submitted."""
    enforce_auth_rate_limit(request)
    name = name.strip()
    email = email.strip().lower()

    # --- Check what the user typed ---
    error = ""
    if len(name) < 2:
        error = "Please enter your name."
    elif "@" not in email or "." not in email.split("@")[-1]:
        error = "Please enter a valid email address."
    elif len(password) < 8:
        error = "Password must be at least 8 characters."
    elif len(password.encode("utf-8")) > 72:
        error = "Password is too long (maximum 72 bytes)."
    elif role not in ("officer", "recruiter"):
        error = "Please choose a role."
    elif db.query(User).filter(User.email == email).first() is not None:
        error = "An account with this email already exists."

    if error:
        return show_auth_page(request, "register", error, name=name, email=email, status_code=400)

    # --- Save the new user (with a hashed password) ---
    user = User(name=name, email=email, password_hash=hash_password(password), role=role)
    db.add(user)
    db.commit()
    db.refresh(user)

    # Create JWT session token
    token = jwt.encode({"user_id": user.id}, SECRET_KEY, algorithm="HS256")
    request.session.clear()
    request.session["jwt_token"] = token
    return RedirectResponse("/", status_code=303)


@app.post("/logout", dependencies=[Depends(require_csrf)])
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/health")
@app.get("/api/health")
def health():
    """Check that the API, database, and Redis dependencies are available."""
    db = None
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
    except Exception as error:
        logger.exception("Health probe failed for database")
        raise HTTPException(status_code=503, detail="Database health check failed.") from error
    finally:
        if db is not None:
            db.close()
    if queue_worker.redis_client is None:
        raise HTTPException(status_code=503, detail="Redis health check failed.")
    try:
        queue_worker.redis_client.ping()
    except Exception as error:
        logger.exception("Health probe failed for Redis")
        raise HTTPException(status_code=503, detail="Redis health check failed.") from error
    return {"status": "ok", "database": "ok", "redis": "ok"}


# ============================================================================
# API - the dashboard's JavaScript calls these routes and gets JSON back
# ============================================================================

def candidate_summary(row):
    """The short version of a candidate (one table row on the dashboard)."""
    return {
        "id": row.id,
        "name": row.name,
        "email": row.email,
        "filename": row.filename,
        "role_key": row.role_key,
        "role_label": row.role_label,
        "ats": row.ats_score,
        "trust": row.trust_score,
        "fit": row.fit_score,
        "flags": row.flag_count,
        "high_flags": row.high_flag_count,
        "verdict": row.verdict,
        "doc_status": row.doc_status,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


def file_looks_valid(filename, data, allowed_extensions):
    """
    Basic upload safety: the file name must end with an allowed extension AND
    the first bytes of the file must match that type (not just the name).
    """
    lowered = filename.lower()
    extension = os.path.splitext(lowered)[1]
    if extension not in allowed_extensions:
        return False
    if extension == ".pdf":
        return data.startswith(b"%PDF")
    if extension == ".png":
        return data.startswith(b"\x89PNG")
    if extension in (".jpg", ".jpeg"):
        return data.startswith(b"\xff\xd8")
    return False


def read_uploads(uploads, allowed_extensions, errors, scratch_dir, total_bytes):
    """
    Stream validated uploads into the batch's private scratch directory.
    Bad files (wrong type, too big) are skipped and explained in 'errors'.
    """
    good_files = []
    for upload in uploads or []:
        if not upload.filename:
            continue  # an empty file slot
        # os.path.basename removes any folder parts like "../../" from the name.
        filename = os.path.basename(upload.filename.replace("\\", "/"))
        extension = os.path.splitext(filename.lower())[1]
        path = scratch_dir / (uuid.uuid4().hex + extension)
        size = 0
        prefix = b""
        batch_over_limit = False
        with path.open("xb") as output:
            while True:
                chunk = upload.file.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                total_bytes[0] += len(chunk)
                if size > MAX_FILE_BYTES:
                    break
                if total_bytes[0] > MAX_BATCH_BYTES:
                    batch_over_limit = True
                    break
                if len(prefix) < 16:
                    prefix += chunk[:16 - len(prefix)]
                output.write(chunk)
        if batch_over_limit:
            path.unlink(missing_ok=True)
            raise HTTPException(status_code=413, detail="Combined upload size exceeds 128 MB.")
        if size > MAX_FILE_BYTES:
            path.unlink(missing_ok=True)
            errors.append({"filename": filename, "error": "File is larger than " + str(MAX_FILE_MB) + " MB."})
        elif not file_looks_valid(filename, prefix, allowed_extensions):
            path.unlink(missing_ok=True)
            errors.append({"filename": filename, "error": "Not a valid " + "/".join(sorted(allowed_extensions)) + " file."})
        else:
            good_files.append({"filename": filename, "path": str(path)})
    return good_files


@app.get("/api/roles")
def api_roles(user: User = Depends(require_user)):
    """The list of job roles for the dropdown."""
    return agent.list_roles()


@app.post("/api/verify", dependencies=[Depends(require_csrf)])
async def api_verify(request: Request, user: User = Depends(require_user)):
    """Run one authenticated, non-queued verification for text or a PDF."""
    form = await request.form(max_files=1, max_fields=4)
    role_key = str(form.get("role_key") or "sde")
    if role_key not in {role["key"] for role in agent.list_roles()}:
        raise HTTPException(status_code=400, detail="Unknown job role.")

    upload = form.get("file")
    resume_text = str(form.get("resume_text") or "")
    if isinstance(upload, StarletteUploadFile):
        filename = os.path.basename((upload.filename or "resume.pdf").replace("\\", "/"))
        if os.path.splitext(filename.lower())[1] != ".pdf":
            raise HTTPException(status_code=400, detail="Please upload a PDF resume.")
        file_bytes = await upload.read(MAX_FILE_BYTES + 1)
        if len(file_bytes) > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail="Resume PDF exceeds the 8 MB file limit.")
    elif resume_text:
        filename = "resume.txt"
        file_bytes = resume_text.encode("utf-8")
    else:
        raise HTTPException(status_code=400, detail="Upload a resume PDF or provide resume text.")

    try:
        return await agent.analyze_single_candidate(filename, file_bytes, role_key)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        logger.exception("Direct candidate verification failed")
        raise HTTPException(status_code=500, detail="Candidate verification failed.") from error


@app.post("/api/analyze", dependencies=[Depends(require_csrf)])
async def api_analyze(request: Request, user: User = Depends(require_user)):
    """
    BATCH ANALYSIS - the heart of the app.
    The browser sends: a role, several resume PDFs and (optionally) marksheets / IDs.
    """
    form = await request.form(max_files=MAX_RESUMES_PER_BATCH + MAX_DOCUMENTS_PER_BATCH + 2, max_fields=50)
    role_key = form.get("role_key", "")
    resumes = form.getlist("resumes")
    documents = form.getlist("documents")
    resumes = [item for item in resumes if isinstance(item, StarletteUploadFile)]
    documents = [item for item in documents if isinstance(item, StarletteUploadFile)]
    if len(resumes) > MAX_RESUMES_PER_BATCH:
        raise HTTPException(status_code=400, detail="Please upload at most " + str(MAX_RESUMES_PER_BATCH) + " resumes at a time.")
    if documents and len(documents) > MAX_DOCUMENTS_PER_BATCH:
        raise HTTPException(status_code=400, detail="Please upload at most " + str(MAX_DOCUMENTS_PER_BATCH) + " documents at a time.")
    if role_key not in {role["key"] for role in agent.list_roles()}:
        raise HTTPException(status_code=400, detail="Unknown job role.")

    if queue_worker.redis_client is None:
        raise HTTPException(status_code=503, detail="Analysis queue is unavailable.")

    errors = []
    job_id = str(uuid.uuid4())
    scratch_dir = queue_worker.UPLOAD_ROOT / job_id
    scratch_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    try:
        total_bytes = [0]
        resume_files = await run_in_threadpool(
            read_uploads, resumes, {".pdf"}, errors, scratch_dir, total_bytes
        )
        document_files = await run_in_threadpool(
            read_uploads, documents, {".pdf", ".png", ".jpg", ".jpeg"}, errors, scratch_dir, total_bytes
        )
    except Exception:
        cleanup_scratch_directory(scratch_dir)
        raise
    if not resume_files:
        cleanup_scratch_directory(scratch_dir)
        raise HTTPException(status_code=400, detail="No valid resume PDF was uploaded.")

    job_data = {
        "owner_id": user.id,
        "role_key": role_key,
        "resumes": resume_files,
        "documents": document_files,
        "correlation_id": request_id_context.get(),
    }
    if not queue_worker.enqueue_batch_job(job_id, job_data):
        cleanup_scratch_directory(scratch_dir)
        raise HTTPException(status_code=503, detail="Could not enqueue the analysis batch.")
    return {
        "task_id": job_id,
        "status": "queued",
        "status_url": "/api/jobs/" + job_id,
        "accepted_resumes": len(resume_files),
        "errors": errors,
    }


@app.get("/api/jobs/{job_id}")
def api_job_status(job_id: str, user: User = Depends(require_user)):
    if len(job_id) != 36:
        raise HTTPException(status_code=404, detail="Analysis job not found.")
    if queue_worker.redis_client is None:
        raise HTTPException(status_code=503, detail="Analysis status service is unavailable.")
    try:
        status = queue_worker.get_job_status_from_redis(job_id)
    except Exception as error:
        raise HTTPException(status_code=503, detail="Analysis status service is unavailable.") from error
    if not status or status.get("owner_id") != user.id:
        raise HTTPException(status_code=404, detail="Analysis job not found.")
    public_status = {key: value for key, value in status.items() if key != "owner_id"}
    public_status["worker_online"] = queue_worker.is_worker_online()
    return public_status


def filtered_candidates(db, user, role_key, min_trust, verdict, doc_status, search):
    """
    Build the list of this user's candidates after applying the dashboard filters.
    Used by BOTH the table and the CSV export, so they always agree.
    """
    query = db.query(Candidate).filter(Candidate.owner_id == user.id)  # only MY candidates
    if role_key:
        query = query.filter(Candidate.role_key == role_key)
    if min_trust > 0:
        query = query.filter(Candidate.trust_score >= min_trust)
    if verdict:
        query = query.filter(Candidate.verdict == verdict)
    if doc_status:
        query = query.filter(Candidate.doc_status == doc_status)
    if search:
        like = "%" + search.strip() + "%"
        query = query.filter(Candidate.name.ilike(like) | Candidate.email.ilike(like))
    return query.order_by(Candidate.created_at.desc()).all()


@app.get("/api/candidates")
def api_candidates(
    role_key: str = "",
    min_trust: int = 0,
    verdict: str = "",
    doc_status: str = "",
    search: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """The talent pool table (with the filters from the page)."""
    rows = filtered_candidates(db, user, role_key, min_trust, verdict, doc_status, search)
    return [candidate_summary(row) for row in rows]


@app.get("/api/candidates/{candidate_id}")
def api_candidate_detail(candidate_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The full report of one candidate (opens in the side panel)."""
    row = db.query(Candidate).filter(Candidate.id == candidate_id, Candidate.owner_id == user.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Candidate not found.")
    report = json.loads(row.report_json)
    report["id"] = row.id
    return report


@app.delete("/api/candidates/{candidate_id}", dependencies=[Depends(require_csrf)])
def api_candidate_delete(candidate_id: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Only placement officers may delete candidates. Recruiters can only view."""
    if user.role != "officer":
        raise HTTPException(status_code=403, detail="Only placement officers can delete candidates.")
    row = db.query(Candidate).filter(Candidate.id == candidate_id, Candidate.owner_id == user.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Candidate not found.")
    db.delete(row)
    db.commit()
    return {"deleted": candidate_id}


@app.get("/api/stats")
def api_stats(user: User = Depends(require_user), db: Session = Depends(get_db)):
    """The numbers for the statistics cards on the Overview page."""
    rows = db.query(Candidate).filter(Candidate.owner_id == user.id).all()
    total = len(rows)

    verdicts = {"Verified": 0, "Needs Review": 0, "High Risk": 0}
    documents_verified = 0
    ats_sum = 0
    trust_sum = 0
    fit_sum = 0
    for row in rows:
        ats_sum += row.ats_score
        trust_sum += row.trust_score
        fit_sum += row.fit_score
        if row.verdict in verdicts:
            verdicts[row.verdict] += 1
        if row.doc_status == "verified":
            documents_verified += 1

    def average(total_points):
        if total == 0:
            return 0
        return round(total_points / total)

    return {
        "total": total,
        "avg_ats": average(ats_sum),
        "avg_trust": average(trust_sum),
        "avg_fit": average(fit_sum),
        "verdicts": verdicts,
        "documents_verified": documents_verified,
    }


def csv_safe(value):
    """
    Protect against "CSV injection": a cell starting with = + - or @ can run as a
    formula when opened in Excel. We put an apostrophe in front to make it plain text.
    """
    text = "" if value is None else str(value)
    if text and text[0] in ("=", "+", "-", "@", "\t", "\r"):
        text = "'" + text
    return text


@app.get("/api/export.csv")
def api_export_csv(
    role_key: str = "",
    min_trust: int = 0,
    verdict: str = "",
    doc_status: str = "",
    search: str = "",
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    """Download the CURRENT FILTERED talent pool as a CSV file (opens in Excel)."""
    rows = filtered_candidates(db, user, role_key, min_trust, verdict, doc_status, search)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Name", "Email", "Phone", "University", "CGPA", "Roll Number", "Target Role",
        "ATS Score", "Trust Score", "Role Fit %", "Verdict", "Flag Count", "Flag Details",
        "Document Status", "GitHub", "LinkedIn", "Resume File",
    ])

    for row in rows:
        report = json.loads(row.report_json)
        flag_text = " | ".join(flag["severity"].upper() + ": " + flag["text"] for flag in report["flags"])
        github_links = " ".join(profile["url"] for profile in report["links"]["github_profiles"])
        linkedin_links = " ".join(report["links"]["linkedin"])
        writer.writerow([
            csv_safe(row.name), csv_safe(row.email), csv_safe(report.get("phone")),
            csv_safe(report.get("university")), csv_safe(report.get("cgpa")),
            csv_safe(report.get("roll_number")), csv_safe(row.role_label),
            row.ats_score, row.trust_score, row.fit_score, csv_safe(row.verdict),
            row.flag_count, csv_safe(flag_text), csv_safe(row.doc_status),
            csv_safe(github_links), csv_safe(linkedin_links), csv_safe(row.filename),
        ])

    # The "﻿" at the start makes Excel read the file as UTF-8 (so names look right).
    return Response(
        content="﻿" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="veribot_talent_pool.csv"'},
    )
