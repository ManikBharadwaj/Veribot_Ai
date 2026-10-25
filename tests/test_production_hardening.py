import os
import re
import json
import pytest
from pathlib import Path
from tempfile import gettempdir

os.environ["ENVIRONMENT"] = "test"
os.environ["SECRET_KEY"] = "test-only-secret-" + ("x" * 64)
os.environ["DATABASE_URL"] = "sqlite:///" + str(Path(gettempdir(), "veribot-hardening-tests.db")).replace("\\", "/")

import agent
import main
import queue_worker
from models import Candidate, SessionLocal, User
from starlette.testclient import TestClient


def _minimal_text_pdf(text):
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content_stream = ("BT /F1 12 Tf 72 720 Td (" + escaped + ") Tj ET").encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(content_stream)).encode() + b" >>\nstream\n" + content_stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode() + body + b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode())
    return bytes(pdf)


def test_pdf_text_extraction_and_resume_text_limit():
    content = _minimal_text_pdf("Alex Sample alex@example.com CGPA 8.5")
    extracted = agent.extract_pdf_text(content)
    assert "alex@example.com" in extracted
    assert agent.extract_email(extracted) == "alex@example.com"

    try:
        agent.extract_email("x" * (agent.MAX_RESUME_CHARS + 1))
    except ValueError as error:
        assert "character limit" in str(error)
    else:
        raise AssertionError("Oversized resume text was not rejected.")


def test_startup_validation_rejects_weak_secret_and_missing_production_database(monkeypatch):
    monkeypatch.setattr(main, "SECRET_KEY", "too-short")
    with pytest.raises(RuntimeError, match="Invalid SECRET_KEY"):
        main.validate_startup_configuration()

    monkeypatch.setattr(main, "SECRET_KEY", "production-test-secret-" + ("z" * 64))
    monkeypatch.setattr(main, "IS_PRODUCTION", True)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        main.validate_startup_configuration()


def test_github_profile_api_is_mockable_and_analyzed(monkeypatch):
    monkeypatch.setattr(queue_worker, "redis_client", None)
    calls = []

    class MockResponse:
        status_code = 200

        def __init__(self, url):
            self.url = url

        def raise_for_status(self):
            return None

        def json(self):
            if "/users/alexsample/repos" in self.url:
                return [
                    {
                        "full_name": "alexsample/demo",
                        "html_url": "https://github.com/alexsample/demo",
                        "language": "Python",
                        "stargazers_count": 2,
                        "fork": False,
                        "pushed_at": "2026-01-01T00:00:00Z",
                        "description": "Demo project",
                        "topics": [],
                    }
                ]
            return {
                "public_repos": 1,
                "followers": 4,
                "created_at": "2020-01-01T00:00:00Z",
            }

    def mocked_get(url, **kwargs):
        calls.append(url)
        return MockResponse(url)

    monkeypatch.setattr(agent.requests, "get", mocked_get)
    result = agent.analyze_github(
        {"github_profiles": [{"username": "alexsample", "url": "https://github.com/alexsample"}], "github_repos": []}
    )
    assert result["profiles"][0]["username"] == "alexsample"
    assert result["original_repos"] == 1
    assert len(calls) == 2


def test_analyze_endpoint_queues_upload_without_running_analysis(monkeypatch, tmp_path):
    monkeypatch.setattr(queue_worker, "UPLOAD_ROOT", tmp_path)

    class FakeRedis:
        def __init__(self):
            self.values = {}
            self.lists = {}

        def set(self, key, value):
            self.values[key] = value

        def expire(self, _key, _seconds):
            return True

        def rpush(self, key, value):
            self.lists.setdefault(key, []).append(value)

        def get(self, key):
            return self.values.get(key)

    fake_redis = FakeRedis()
    monkeypatch.setattr(queue_worker, "redis_client", fake_redis)
    monkeypatch.setattr(agent, "analyze_batch", lambda *_args: (_ for _ in ()).throw(AssertionError("ran synchronously")))

    with TestClient(main.app) as client:
        page = client.get("/register")
        session_cookie = page.headers["set-cookie"].lower()
        assert "httponly" in session_cookie
        assert "samesite=strict" in session_cookie
        policy = page.headers["content-security-policy"]
        nonce = re.search(r"nonce-([^' ]+)", policy).group(1)
        assert f'nonce="{nonce}"' in page.text
        assert "'unsafe-inline'" not in policy.split("script-src ", 1)[1].split(";", 1)[0]
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        registered = client.post(
            "/register",
            data={
                "csrf_token": csrf,
                "name": "Queue User",
                "email": "queue-user@example.test",
                "password": "passphrase-123",
                "role": "officer",
            },
            follow_redirects=False,
        )
        assert registered.status_code == 303
        dashboard = client.get("/")
        assert "onclick=" not in dashboard.text
        assert "onchange=" not in dashboard.text
        assert "oninput=" not in dashboard.text
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', dashboard.text).group(1)
        response = client.post(
            "/api/analyze",
            data={"role_key": "sde"},
            files=[("resumes", ("resume.pdf", b"%PDF-1.4\nupload", "application/pdf"))],
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 200
        accepted = response.json()
        assert accepted["status"] == "queued"
        assert accepted["accepted_resumes"] == 1
        assert fake_redis.lists[queue_worker.QUEUE_KEY] == [accepted["task_id"]]

        status = client.get(accepted["status_url"])
        assert status.status_code == 200
        assert status.json()["status"] == "queued"


def test_upload_request_body_limit_is_enforced(monkeypatch):
    monkeypatch.setattr(main, "MAX_BATCH_BYTES", 1)
    monkeypatch.setattr(main, "MAX_MULTIPART_OVERHEAD", 0)
    with TestClient(main.app) as client:
        response = client.post("/api/analyze", content=b"oversized")
    assert response.status_code == 413


def test_health_probe_returns_503_when_database_is_down(monkeypatch):
    def unavailable_database():
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(main, "SessionLocal", unavailable_database)
    with TestClient(main.app) as client:
        response = client.get("/health")
    assert response.status_code == 503


def test_worker_failure_is_written_to_dead_letter_queue(monkeypatch, tmp_path):
    class FakeRedis:
        def __init__(self):
            self.values = {}
            self.lists = {}

        def set(self, key, value):
            self.values[key] = value

        def expire(self, _key, _seconds):
            return True

        def rpush(self, key, value):
            self.lists.setdefault(key, []).append(value)

    fake_redis = FakeRedis()
    upload_dir = tmp_path / "batch"
    upload_dir.mkdir()
    upload_path = upload_dir / "resume.pdf"
    upload_path.write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(queue_worker, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setattr(queue_worker, "redis_client", fake_redis)
    monkeypatch.setattr(agent, "analyze_batch", lambda *_args: (_ for _ in ()).throw(RuntimeError("worker failure")))

    queue_worker.process_job(
        "job-1",
        {
            "owner_id": 1,
            "role_key": "sde",
            "correlation_id": "request-1",
            "resumes": [{"filename": "resume.pdf", "path": str(upload_path)}],
            "documents": [],
        },
    )

    status = json.loads(fake_redis.values["veribot_job:job-1"])
    dead_letter = json.loads(fake_redis.lists[queue_worker.DLQ_KEY][0])
    assert status["status"] == "failed"
    assert "worker failure" in dead_letter["traceback"]
    assert dead_letter["correlation_id"] == "request-1"
    assert not upload_path.exists()


def test_recruiter_cannot_delete_candidate(monkeypatch):
    monkeypatch.setattr(queue_worker, "redis_client", None)
    with TestClient(main.app) as client:
        db = SessionLocal()
        try:
            db.query(Candidate).delete()
            db.query(User).delete()
            db.commit()
        finally:
            db.close()
        page = client.get("/register")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        response = client.post(
            "/register",
            data={
                "csrf_token": csrf,
                "name": "Recruiter User",
                "email": "recruiter@example.test",
                "password": "passphrase-123",
                "role": "recruiter",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        db = SessionLocal()
        try:
            owner = db.query(User).filter(User.email == "recruiter@example.test").one()
            candidate = Candidate(
                owner_id=owner.id,
                filename="resume.pdf",
                name="Candidate",
                email="candidate@example.test",
                role_key="sde",
                role_label="Software Development Engineer (SDE)",
                report_json="{}",
            )
            db.add(candidate)
            db.commit()
            candidate_id = candidate.id
        finally:
            db.close()

        dashboard = client.get("/")
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', dashboard.text).group(1)
        denied = client.delete(
            f"/api/candidates/{candidate_id}",
            headers={"X-CSRF-Token": csrf},
        )
        assert denied.status_code == 403
