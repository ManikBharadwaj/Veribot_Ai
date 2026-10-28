"""Redis-backed asynchronous analysis queue and worker for VeriBot AI."""

import json
import logging
import os
import threading
import time
import traceback
import contextvars
import uuid
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

UPSTASH_REDIS_REST_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_REDIS_REST_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
REDIS_URL = os.getenv("REDIS_URL", "")
UPLOAD_DIR = os.getenv("VERIBOT_UPLOAD_DIR", "").strip()
UPLOAD_ROOT = Path(UPLOAD_DIR or Path(__file__).resolve().parent / "var" / "uploads").resolve()
QUEUE_KEY = "veribot_job_queue"
DLQ_KEY = "veribot_job_dlq"
JOB_TTL_SECONDS = 30 * 24 * 60 * 60
WORKER_HEARTBEAT_KEY = "veribot_worker_heartbeat"
WORKER_HEARTBEAT_INTERVAL_SECONDS = 5
WORKER_HEARTBEAT_TTL_SECONDS = 20

logger = logging.getLogger("veribot.queue")
worker_request_id = contextvars.ContextVar("worker_request_id", default="-")


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "correlation_id", worker_request_id.get()),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=True)


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(message)s")
for _handler in logging.getLogger().handlers:
    _handler.setFormatter(_JsonFormatter())

redis_client = None

try:
    if UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN:
        from upstash_redis import Redis as UpstashRedis

        redis_client = UpstashRedis(url=UPSTASH_REDIS_REST_URL, token=UPSTASH_REDIS_REST_TOKEN)
    elif REDIS_URL:
        import redis

        redis_client = redis.Redis.from_url(REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
    else:
        import redis

        redis_client = redis.Redis(host="localhost", port=6379, db=0, socket_timeout=2)
        redis_client.ping()
except Exception:
    logger.warning("Redis is unavailable; asynchronous analysis cannot be accepted.")
    redis_client = None


def _decode(value):
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _set_job(job_id: str, data: dict) -> None:
    redis_client.set(f"veribot_job:{job_id}", json.dumps(data))
    if hasattr(redis_client, "expire"):
        redis_client.expire(f"veribot_job:{job_id}", JOB_TTL_SECONDS)


def enqueue_batch_job(job_id: str, job_data: dict) -> bool:
    """Persist a job and enqueue its ID. Returns False if Redis is unavailable."""
    if redis_client is None:
        return False
    try:
        _set_job(
            job_id,
            {"status": "queued", "created_at": time.time(), "owner_id": job_data["owner_id"]},
        )
        redis_client.set(f"veribot_payload:{job_id}", json.dumps(job_data))
        if hasattr(redis_client, "expire"):
            redis_client.expire(f"veribot_payload:{job_id}", JOB_TTL_SECONDS)
        redis_client.rpush(QUEUE_KEY, job_id)
        return True
    except Exception:
        logger.exception("Failed to enqueue analysis job %s", job_id)
        return False


def get_job_status_from_redis(job_id: str):
    if redis_client is None:
        return None
    try:
        data = redis_client.get(f"veribot_job:{job_id}")
        return json.loads(_decode(data)) if data else None
    except Exception:
        logger.exception("Failed to read analysis job status %s", job_id)
        raise


def is_worker_online() -> bool:
    """Return whether a queue worker has published a recent heartbeat."""
    if redis_client is None:
        return False
    return bool(redis_client.get(WORKER_HEARTBEAT_KEY))


def _set_status(job_id: str, status: dict) -> None:
    _set_job(job_id, status)


def _read_job_file(item: dict) -> tuple[str, bytes]:
    root = UPLOAD_ROOT.resolve()
    path = Path(item["path"]).resolve(strict=True)
    if root not in path.parents or not path.is_file():
        raise ValueError("Queued upload path is outside the configured upload directory.")
    return item["filename"], path.read_bytes()


def process_job(job_id: str, job_data: dict) -> None:
    """Run one accepted batch, persist its reports, and publish its result."""
    import agent
    from models import Candidate, SessionLocal

    correlation_id = job_data.get("correlation_id", job_id)
    request_token = worker_request_id.set(correlation_id)
    try:
        _set_status(
            job_id,
            {
                "status": "processing",
                "correlation_id": correlation_id,
                "owner_id": job_data["owner_id"],
                "stage": "loading_files",
                "progress_percent": 0,
            },
        )
        resumes = [_read_job_file(item) for item in job_data["resumes"]]
        documents = [_read_job_file(item) for item in job_data.get("documents", [])]
        _set_status(
            job_id,
            {
                "status": "processing",
                "correlation_id": correlation_id,
                "owner_id": job_data["owner_id"],
                "stage": "reading_resumes",
                "progress_percent": 0,
            },
        )
        last_progress_update = time.monotonic()
        last_progress_stage = "reading_resumes"

        def publish_progress(stage, completed, total):
            nonlocal last_progress_update, last_progress_stage
            now = time.monotonic()
            if (
                completed != total
                and stage == last_progress_stage
                and now - last_progress_update < 0.75
            ):
                return
            stage_ranges = {
                "reading_resumes": (0, 25),
                "reading_documents": (25, 35),
                "scoring": (35, 95),
            }
            start_percent, end_percent = stage_ranges[stage]
            stage_fraction = completed / total if total else 1
            progress_percent = round(
                start_percent + (end_percent - start_percent) * stage_fraction
            )
            _set_status(
                job_id,
                {
                    "status": "processing",
                    "correlation_id": correlation_id,
                    "owner_id": job_data["owner_id"],
                    "stage": stage,
                    "stage_completed": completed,
                    "stage_total": total,
                    "progress_percent": progress_percent,
                },
            )
            last_progress_update = now
            last_progress_stage = stage

        reports, errors, unmatched = agent.analyze_batch(
            resumes, documents, job_data["role_key"], progress=publish_progress
        )
        _set_status(
            job_id,
            {
                "status": "processing",
                "correlation_id": correlation_id,
                "owner_id": job_data["owner_id"],
                "stage": "saving_results",
                "progress_percent": 95,
            },
        )
        db = SessionLocal()
        try:
            candidate_rows = []
            for report in reports:
                flags = report["flags"]
                candidate_rows.append(
                    Candidate(
                        owner_id=job_data["owner_id"],
                        filename=report["filename"],
                        name=report["name"],
                        email=report["email"],
                        role_key=report["role_key"],
                        role_label=report["role_label"],
                        ats_score=report["scores"]["ats"],
                        trust_score=report["scores"]["trust"],
                        fit_score=report["scores"]["fit"],
                        flag_count=len(flags),
                        high_flag_count=sum(flag["severity"] == "high" for flag in flags),
                        verdict=report["verdict"],
                        doc_status=report["doc_status"],
                        report_json=json.dumps(report),
                    )
                )
            db.add_all(candidate_rows)
            db.flush()
            summaries = [
                {
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
                for row in candidate_rows
            ]
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        _set_status(
            job_id,
            {
                "status": "completed",
                "correlation_id": correlation_id,
                "owner_id": job_data["owner_id"],
                "stage": "completed",
                "progress_percent": 100,
                "saved": summaries,
                "errors": errors,
                "unmatched_documents": unmatched,
            },
        )
    except Exception as error:
        trace = traceback.format_exc()
        logger.error("Analysis job %s failed: %s", job_id, error, extra={"correlation_id": correlation_id})
        failure = {
            "job_id": job_id,
            "correlation_id": correlation_id,
            "error": str(error),
            "traceback": trace,
            "payload": job_data,
            "failed_at": time.time(),
        }
        try:
            redis_client.rpush(DLQ_KEY, json.dumps(failure))
            _set_status(
                job_id,
                {
                    "status": "failed",
                    "correlation_id": correlation_id,
                    "owner_id": job_data["owner_id"],
                    "error": str(error),
                },
            )
        except Exception:
            logger.exception("Could not persist failed job %s to Redis DLQ", job_id)
            raise
    finally:
        for item in job_data.get("resumes", []) + job_data.get("documents", []):
            try:
                path = Path(item["path"]).resolve()
                if UPLOAD_ROOT.resolve() in path.parents:
                    path.unlink(missing_ok=True)
                    try:
                        path.parent.rmdir()
                    except OSError:
                        pass
            except (KeyError, OSError, TypeError, ValueError):
                logger.exception("Could not remove scratch upload for job %s", job_id)
        worker_request_id.reset(request_token)


def run_worker(
    poll_interval: float = 1.0, stop_event: threading.Event | None = None
) -> None:
    """Continuously consume Redis jobs; run with ``python queue_worker.py``."""
    if redis_client is None:
        raise RuntimeError("Redis is required to run the VeriBot analysis worker.")
    if stop_event is None:
        stop_event = threading.Event()
    redis_client.ping()
    worker_id = str(uuid.uuid4())

    def publish_heartbeat():
        while not stop_event.is_set():
            try:
                redis_client.set(
                    WORKER_HEARTBEAT_KEY,
                    json.dumps({"worker_id": worker_id, "seen_at": time.time()}),
                    ex=WORKER_HEARTBEAT_TTL_SECONDS,
                )
            except Exception:
                logger.exception("Could not publish analysis worker heartbeat")
            stop_event.wait(WORKER_HEARTBEAT_INTERVAL_SECONDS)

    heartbeat_thread = threading.Thread(
        target=publish_heartbeat, name="veribot-worker-heartbeat", daemon=True
    )
    heartbeat_thread.start()
    logger.info("Analysis worker started", extra={"correlation_id": worker_id})
    try:
        while not stop_event.is_set():
            job_id = redis_client.lpop(QUEUE_KEY)
            if not job_id:
                stop_event.wait(poll_interval)
                continue
            job_id = _decode(job_id)
            try:
                payload = redis_client.get(f"veribot_payload:{job_id}")
                if not payload:
                    raise ValueError(f"Queue payload missing for job {job_id}")
                process_job(job_id, json.loads(_decode(payload)))
                redis_client.delete(f"veribot_payload:{job_id}")
            except Exception as error:
                logger.exception("Worker could not process job %s", job_id)
                try:
                    current = get_job_status_from_redis(job_id) or {}
                    if current.get("status") != "failed":
                        failure = {
                            "job_id": job_id,
                            "error": str(error),
                            "traceback": traceback.format_exc(),
                            "failed_at": time.time(),
                        }
                        redis_client.rpush(DLQ_KEY, json.dumps(failure))
                        current.update({"status": "failed", "error": str(error)})
                        _set_status(job_id, current)
                except Exception:
                    logger.exception("Could not persist worker failure for job %s", job_id)
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=WORKER_HEARTBEAT_INTERVAL_SECONDS + 1)


if __name__ == "__main__":
    run_worker()
