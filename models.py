"""
models.py - The DATABASE part of VeriBot AI.

What is in this file?
    1. The connection to the database (supports PostgreSQL/Supabase/Neon via DATABASE_URL in .env,
       or falls back smoothly to SQLite veribot.db for local development).
    2. Two tables, written as SQLAlchemy model classes:
         - User       -> placement officers and recruiters who can log in
         - Candidate  -> one row for every resume we have analyzed
    3. Helper functions: create_tables() and get_db() dependency for FastAPI.

SQLAlchemy ORM:
    SQLAlchemy translates Python objects into database queries cleanly.
    e.g., db.query(User).filter(User.email == "test@example.com").first()
"""

import os
from datetime import datetime, timezone
from dotenv import load_dotenv
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import declarative_base, sessionmaker
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

load_dotenv()

# ----------------------------------------------------------------------------
# 1. DATABASE CONNECTION CONFIGURATION
# ----------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SQLITE_URL = "sqlite:///" + os.path.join(BASE_DIR, "veribot.db").replace("\\", "/")

# Get DATABASE_URL from .env file or default to SQLite
DATABASE_URL = os.getenv("DATABASE_URL", "").strip() or DEFAULT_SQLITE_URL

# SQLAlchemy expects 'postgresql://', but some cloud providers (like Supabase/Heroku) output 'postgres://'
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# Configure connection parameters based on engine type
engine_args = {}
if DATABASE_URL.startswith("sqlite"):
    engine_args["connect_args"] = {"check_same_thread": False}
else:
    # PostgreSQL connection pool settings for cloud databases
    engine_args["pool_pre_ping"] = True
    engine_args["pool_recycle"] = 300

@retry(
    retry=retry_if_exception_type(SQLAlchemyError),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=0.25, min=0.25, max=4),
    reraise=True,
)
def _create_engine():
    return create_engine(DATABASE_URL, **engine_args)


engine = _create_engine()

# SessionLocal factory creates new database sessions
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

Base = declarative_base()


# ----------------------------------------------------------------------------
# 2. DATABASE MODELS (TABLES)
# ----------------------------------------------------------------------------

class User(Base):
    """A person who can log in: a placement officer or a recruiter."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    email = Column(String(255), unique=True, nullable=False, index=True)

    # Scrambled bcrypt password hash (never plain text)
    password_hash = Column(String(255), nullable=False)

    # Role boundaries: "officer" (placement officer - full control) or "recruiter" (read-only)
    role = Column(String(20), nullable=False, default="officer")

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Candidate(Base):
    """One analyzed candidate resume, plus scores and verification results."""

    __tablename__ = "candidates"

    id = Column(Integer, primary_key=True)

    # Which user uploaded this resume. Candidates are scoped per account owner.
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    filename = Column(String(255))
    name = Column(String(150))
    email = Column(String(255))

    role_key = Column(String(30))      # e.g. "sde"
    role_label = Column(String(100))   # e.g. "Software Development Engineer (SDE)"

    # Four headline metrics for quick database filtering and sorting
    ats_score = Column(Integer, default=0)
    trust_score = Column(Integer, default=0)
    fit_score = Column(Integer, default=0)
    flag_count = Column(Integer, default=0)
    high_flag_count = Column(Integer, default=0)

    verdict = Column(String(30))       # "Verified", "Needs Review", or "High Risk"
    doc_status = Column(String(30))    # "none", "verified", "partial", "mismatch", "unreadable"

    # Complete structured JSON report for side drawer scorecard
    report_json = Column(Text)

    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


# ----------------------------------------------------------------------------
# 3. HELPERS
# ----------------------------------------------------------------------------

@retry(
    retry=retry_if_exception_type(SQLAlchemyError),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=0.25, min=0.25, max=4),
    reraise=True,
)
def create_tables():
    """Create database tables if they do not exist yet."""
    Base.metadata.create_all(bind=engine)


@retry(
    retry=retry_if_exception_type(SQLAlchemyError),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=0.25, min=0.25, max=4),
    reraise=True,
)
def _open_session():
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return db
    except Exception:
        db.close()
        raise


def get_db():
    """FastAPI dependency yielding a clean database session and auto-closing it after use."""
    db = _open_session()
    try:
        yield db
    finally:
        db.close()
