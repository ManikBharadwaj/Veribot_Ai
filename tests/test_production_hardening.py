import asyncio
import os
import re
import json
import threading
import time
import uuid
import pytest
from pathlib import Path
from tempfile import gettempdir

os.environ["ENVIRONMENT"] = "test"
os.environ["SECRET_KEY"] = "test-only-secret-" + ("x" * 64)
os.environ["DATABASE_URL"] = "sqlite:///" + str(Path(gettempdir(), "veribot-hardening-tests.db")).replace("\\", "/")

import agent
import main
import models
import queue_worker
from models import Candidate, SessionLocal, User, create_tables
from starlette.testclient import TestClient

# Tests must not call a configured external LLM provider.
agent.LLM_API_KEY = ""


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


def _mock_async_http_client(monkeypatch, get_handler):
    class MockAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **kwargs):
            return await get_handler(url, **kwargs)

    monkeypatch.setattr(agent.httpx, "AsyncClient", MockAsyncClient)


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


def test_resume_detail_extraction_and_role_skill_catalogs():
    details = agent.parse_resume_details(
        "Nisha Raghav\n"
        "B.Tech Computer Science, Jaypee University\n"
        "CGPA: 9.6\n"
        "Roll Number: 24CS123\n"
        "Expected graduation year: 2025\n"
        "3 years of professional experience\n"
        "Engineering Projects and Recruitment\n"
    )

    assert details["degree"] == "B.Tech"
    assert details["cgpa"] == 9.6
    assert details["roll_number"] == "24CS123"
    assert details["grad_year"] == 2025
    assert details["experience_years"] == 3
    assert agent.ROLE_PROFILES["hr"]["label"] == "Human Resources (HR)"
    assert "talent acquisition" in agent.ROLE_PROFILES["hr"]["keywords"]


def test_analyze_batch_runs_independent_work_in_parallel_and_keeps_order(monkeypatch):
    active = 0
    max_active = 0
    lock = threading.Lock()
    progress = []

    def fake_read_resume(filename, _file_bytes):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return {"filename": filename}

    monkeypatch.setattr(agent, "read_resume", fake_read_resume)
    monkeypatch.setattr(agent, "match_documents_to_resumes", lambda _drafts, _documents: ({}, []))
    monkeypatch.setattr(
        agent,
        "score_candidate",
        lambda draft, _role, _docs, _github_semaphore=None, _github_client=None: draft["filename"],
    )

    reports, errors, unmatched = agent.analyze_batch(
        [("one.pdf", b"one"), ("two.pdf", b"two"), ("three.pdf", b"three")],
        [],
        "sde",
        progress=lambda *update: progress.append(update),
    )

    assert reports == ["one.pdf", "two.pdf", "three.pdf"]
    assert errors == []
    assert unmatched == []
    assert max_active > 1
    assert ("reading_resumes", 3, 3) in progress
    assert ("scoring", 3, 3) in progress


def test_stats_are_aggregated_for_only_the_current_owner():
    create_tables()
    db = SessionLocal()
    owner = User(
        name="Metrics User",
        email="metrics-" + uuid.uuid4().hex + "@example.test",
        password_hash="not-used",
        role="officer",
    )
    try:
        db.add(owner)
        db.flush()
        db.add_all(
            [
                Candidate(
                    owner_id=owner.id,
                    filename="verified.pdf",
                    name="Verified Candidate",
                    role_key="sde",
                    role_label="SDE",
                    ats_score=80,
                    trust_score=90,
                    fit_score=70,
                    verdict="Verified",
                    doc_status="verified",
                    report_json="{}",
                ),
                Candidate(
                    owner_id=owner.id,
                    filename="risk.pdf",
                    name="Risk Candidate",
                    role_key="sde",
                    role_label="SDE",
                    ats_score=60,
                    trust_score=50,
                    fit_score=90,
                    verdict="High Risk",
                    doc_status="none",
                    report_json="{}",
                ),
            ]
        )
        db.commit()

        stats = main.api_stats(owner, db)

        assert stats == {
            "total": 2,
            "avg_ats": 70,
            "avg_trust": 70,
            "avg_fit": 80,
            "verdicts": {"Verified": 1, "Needs Review": 0, "High Risk": 1},
            "documents_verified": 1,
        }
    finally:
        db.rollback()
        db.close()


def test_startup_validation_rejects_weak_secret_and_missing_production_database(monkeypatch):
    monkeypatch.setattr(main, "SECRET_KEY", "too-short")
    with pytest.raises(RuntimeError, match="Invalid SECRET_KEY"):
        main.validate_startup_configuration()

    monkeypatch.setattr(main, "SECRET_KEY", "production-test-secret-" + ("z" * 64))
    monkeypatch.setattr(main, "IS_PRODUCTION", True)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        main.validate_startup_configuration()


def test_role_selection_resolves_all_supported_keys_and_display_labels():
    for role in agent.list_roles():
        assert main.resolve_role_key(role["key"]) == role["key"]
        assert main.resolve_role_key(role["label"]) == role["key"]
        assert main.resolve_role_key("  " + role["label"].upper() + "  ") == role["key"]
    assert main.resolve_role_key("unknown role") == ""


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
            if self.url.endswith("/languages"):
                return {"Python": 80, "HTML": 20}
            if self.url.endswith("/commits"):
                return [
                    {
                        "author": {"login": "alexsample"},
                        "commit": {
                            "author": {"date": "2026-10-01T00:00:00Z"},
                            "message": "Add demo project",
                        },
                    }
                ]
            return {
                "public_repos": 1,
                "followers": 4,
                "created_at": "2020-01-01T00:00:00Z",
            }

    async def mocked_get(url, **kwargs):
        calls.append(url)
        return MockResponse(url)

    _mock_async_http_client(monkeypatch, mocked_get)
    result = asyncio.run(
        agent.analyze_github(
            {
                "github_profiles": [
                    {"username": "alexsample", "url": "https://github.com/alexsample"}
                ],
                "github_repos": [],
            }
        )
    )
    assert result["profiles"][0]["username"] == "alexsample"
    assert result["original_repos"] == 1
    assert result["repos"][0].get("claimed_user_commits_sampled") == 1, (repr(result["repos"][0]), calls)
    assert result["repos"][0]["commit_count_sampled"] == 1
    assert result["repos"][0]["claimed_user_commits"] == 1
    assert result["repos"][0]["first_commit_in_sample"] == "2026-10-01T00:00:00Z"
    assert result["repos"][0]["latest_commit"] == "2026-10-01T00:00:00Z"
    assert result["repos"][0]["sample_commit_messages"] == ["Add demo project"]
    assert result["repos"][0]["language_breakdown_percent"] == {"Python": 80.0, "HTML": 20.0}
    assert len(calls) == 4


def test_github_api_calls_run_concurrently_with_a_bounded_worker_pool(monkeypatch):
    monkeypatch.setattr(queue_worker, "redis_client", None)
    active = 0
    max_active = 0
    lock = threading.Lock()
    calls = []

    class MockResponse:
        status_code = 200

        def __init__(self, url):
            self.url = url

        def raise_for_status(self):
            return None

        def json(self):
            if self.url.endswith("/repos"):
                return []
            if "/commits" in self.url:
                return [{"sha": "commit"}]
            if "/users/" in self.url:
                return {"public_repos": 1, "followers": 4, "created_at": "2020-01-01T00:00:00Z"}
            return {
                "full_name": self.url.split("/repos/", 1)[1],
                "html_url": self.url,
                "language": "Python",
                "stargazers_count": 1,
                "fork": False,
                "pushed_at": "2026-01-01T00:00:00Z",
                "description": "Sample project",
                "topics": [],
            }

    async def mocked_get(url, **_kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            calls.append(url)
        await asyncio.sleep(0.03)
        with lock:
            active -= 1
        return MockResponse(url)

    _mock_async_http_client(monkeypatch, mocked_get)
    result = asyncio.run(
        agent.analyze_github(
            {
                "github_profiles": [
                    {"username": "first", "url": "https://github.com/first"},
                    {"username": "second", "url": "https://github.com/second"},
                ],
                "github_repos": [
                    {"owner": "first", "repo": "project"},
                    {"owner": "second", "repo": "project"},
                ],
            }
        )
    )

    assert len(result["profiles"]) == 2
    assert result["original_repos"] == 2
    assert len(calls) == 10
    assert max_active > 1
    assert max_active <= 4


def test_async_github_audit_collects_repo_depth_languages_and_auth(monkeypatch):
    monkeypatch.setattr(queue_worker, "redis_client", None)
    monkeypatch.setattr(agent, "GITHUB_TOKEN", "test-token")
    request_headers = []

    class MockResponse:
        status_code = 200

        def __init__(self, url):
            self.url = url

        def raise_for_status(self):
            return None

        def json(self):
            if self.url.endswith("/users/sample/repos"):
                return [
                    {
                        "full_name": "sample/project",
                        "html_url": self.url,
                        "language": "Python",
                        "stargazers_count": 3,
                        "fork": False,
                        "pushed_at": "2026-10-01T00:00:00Z",
                        "description": "Python API",
                        "topics": ["rest-api"],
                    },
                    {
                        "full_name": "sample/forked",
                        "html_url": self.url,
                        "language": "JavaScript",
                        "stargazers_count": 1,
                        "fork": True,
                        "pushed_at": "2026-01-01T00:00:00Z",
                        "description": "A fork",
                        "topics": [],
                    },
                ]
            if self.url.endswith("/commits"):
                return [
                    {
                        "sha": "latest",
                        "author": {"login": "sample"},
                        "commit": {
                            "author": {"date": "2026-10-01T00:00:00Z"},
                            "message": "Implement latest feature\nAdditional details",
                        },
                    },
                    {
                        "sha": "other-author",
                        "author": {"login": "another-user"},
                        "commit": {
                            "author": {"date": "2026-09-01T00:00:00Z"},
                            "message": "Update documentation",
                        },
                    },
                    {
                        "sha": "old",
                        "author": {"login": "sample"},
                        "commit": {
                            "author": {"date": "2024-01-01T00:00:00Z"},
                            "message": "Initial implementation",
                        },
                    }
                ]
            if self.url.endswith("/users/sample"):
                return {
                    "public_repos": 2,
                    "followers": 7,
                    "created_at": "2020-01-01T00:00:00Z",
                }
            return {
                "full_name": "sample/project",
                "html_url": "https://github.com/sample/project",
                "language": "Python",
                "stargazers_count": 3,
                "fork": False,
                "pushed_at": "2026-10-01T00:00:00Z",
                "description": "Python API",
                "topics": ["rest-api"],
            }

    async def mocked_get(url, **kwargs):
        request_headers.append(kwargs["headers"])
        return MockResponse(url)

    _mock_async_http_client(monkeypatch, mocked_get)
    result = asyncio.run(
        agent.analyze_github(
            {
                "github_profiles": [
                    {"username": "sample", "url": "https://github.com/sample"}
                ],
                "github_repos": [{"owner": "sample", "repo": "project"}],
            }
        )
    )

    assert result["profiles"][0]["public_repos"] == 2
    assert result["profiles"][0]["followers"] == 7
    assert result["repos"][0].get("commits_sampled") == 3, repr(result["repos"][0])
    assert result["repos"][0]["recent_commits_sampled"] == 2
    assert result["repos"][0]["commit_count_sampled"] == 3
    assert result["repos"][0]["claimed_user_commits"] == 2
    assert result["repos"][0]["first_commit_in_sample"] == "2024-01-01T00:00:00Z"
    assert result["repos"][0]["latest_commit"] == "2026-10-01T00:00:00Z"
    assert result["repos"][0]["sample_commit_messages"] == [
        "Implement latest feature",
        "Update documentation",
        "Initial implementation",
    ]
    assert result["candidate_commits_sampled"] == 4
    assert result["candidate_recent_commits_sampled"] == 2
    assert result["original_repos"] == 1
    assert result["fork_repos"] == 1
    assert result["fork_ratio"] == 0.5
    assert result["languages"] == {"Python": 1}
    assert result["all_languages"] == {"Python": 1, "JavaScript": 1}
    assert all(header["Authorization"] == "Bearer " + agent.GITHUB_TOKEN for header in request_headers)

    skills = agent.build_skill_table(
        agent.ROLE_PROFILES["sde"],
        ["python", "javascript"],
        "Python and JavaScript",
        result,
    )
    assert [skill["status"] for skill in skills] == ["verified", "partial"]


def test_fraud_rules_flag_inflated_claims_and_github_risk():
    github = {
        "errors": [],
        "profiles": [
            {"username": "new-account", "account_age_days": 1, "public_repos": 0}
        ],
        "total_repos": 2,
        "original_repos": 0,
        "fork_repos": 2,
        "recent_repos": 0,
    }
    text = (
        "Claims 10,000 users. 5 years of experience. "
        "Expected graduation 2028. CGPA 11."
    )
    flags = agent.detect_flags(
        agent.ROLE_PROFILES["sde"],
        text,
        {"grad_year": 2028, "cgpa": 11},
        {"github_profiles": [], "github_repos": []},
        github,
        10,
        20,
        [],
    )
    flag_text = " ".join(flag["text"] for flag in flags)
    assert "No GitHub profile" in flag_text
    assert "only 1 days old" in flag_text
    assert "no public repositories" in flag_text
    assert "Every GitHub repository found is a fork" in flag_text
    assert "10000 users" in flag_text
    assert "outside the possible 0-10 range" in flag_text


def test_skill_matrix_preserves_api_statuses_and_exposes_audit_classification():
    role = agent.ROLE_PROFILES["sde"]
    resume_text = "Python and JavaScript"
    resume_only = agent.build_skill_table(
        role,
        ["python", "javascript"],
        resume_text,
        agent._empty_github_summary(),
    )
    assert [skill["status"] for skill in resume_only] == ["unverified", "unverified"]
    assert [skill["classification"] for skill in resume_only] == [
        "self_claimed",
        "self_claimed",
    ]

    github_evidence = {
        **agent._empty_github_summary(checked=True),
        "original_repos": 1,
        "repos": [
            {
                "name": "sample/python-demo",
                "description": "",
                "topics": [],
                "language": "Python",
                "is_fork": False,
                "sample_commit_messages": ["Add JavaScript client support"],
            }
        ],
    }
    audited = agent.build_skill_table(
        role, ["python", "javascript"], resume_text, github_evidence
    )
    assert [skill["status"] for skill in audited] == ["verified", "verified"]
    assert [skill["classification"] for skill in audited] == [
        "verified",
        "verified",
    ]


def test_flag_rules_cover_contact_forks_activity_and_unverified_skills():
    github = {
        **agent._empty_github_summary(checked=True),
        "total_repos": 4,
        "original_repos": 1,
        "fork_repos": 3,
        "fork_ratio": 0.75,
        "activity_audits_completed": 1,
        "candidate_recent_commits_sampled": 0,
    }
    flags = agent.detect_flags(
        agent.ROLE_PROFILES["sde"],
        "Python developer",
        {"email": "", "phone": ""},
        {"github_profiles": [{"username": "sample"}], "github_repos": []},
        github,
        40,
        50,
        ["python"],
        [{"skill": "python", "status": "unverified"}],
    )
    flag_text = " ".join(flag["text"] for flag in flags)
    assert "missing contact details: email and phone" in flag_text
    assert "75% of sampled GitHub repositories are forks" in flag_text
    assert "No recent commits" in flag_text
    assert "did not verify python" in flag_text


def test_candidate_score_breakdown_sums_and_role_fit_uses_claimed_skills(monkeypatch):
    async def fake_github(_links, request_semaphore=None, client=None):
        return {
            **agent._empty_github_summary(checked=True),
            "total_repos": 1,
            "original_repos": 1,
            "repos": [
                {
                    "name": "sample/python-demo",
                    "description": "",
                    "topics": [],
                    "language": "Python",
                    "is_fork": False,
                }
            ],
        }

    monkeypatch.setattr(agent, "analyze_github", fake_github)
    draft = {
        "filename": "candidate.pdf",
        "text": "Python JavaScript " + ("resume content " * 20),
        "fields": {"name": "Candidate", "email": "candidate@example.test", "phone": ""},
        "links": {"github_profiles": [], "github_repos": [], "linkedin": []},
    }
    report = asyncio.run(agent.score_candidate(draft, "sde", []))

    assert sum(item["points"] for item in report["ats_breakdown"]) == report["scores"]["ats"]
    assert report["scores"]["fit"] == 75
    assert report["skills"][0]["classification"] == "verified"
    assert report["skills"][1]["classification"] == "unverified"
    assert len(report["relevant_skills"]) == len(agent.ROLE_PROFILES["sde"]["keywords"])
    assert report["relevant_skills"][0]["skill"] == "python"
    assert report["relevant_skills"][0]["status"] == "verified"
    assert report["relevant_skills"][1]["status"] == "not_claimed"
    assert report["relevant_skills"][2]["status"] == "partial"


def test_candidate_claim_analysis_records_platform_limits_and_evidence():
    github = agent._empty_github_summary(checked=True)
    github.update(
        {
            "original_repos": 1,
            "recent_repos": 0,
            "repositories_sampled": 1,
            "public_repos_reported": 4,
            "candidate_commits_sampled": 12,
            "language_usage_percent": {"Python": 100.0},
            "repos": [
                {
                    "name": "sample/api",
                    "source": "resume_link",
                    "is_fork": False,
                    "language": "Python",
                }
            ],
        }
    )
    links = {
        "github_profiles": [{"username": "sample", "url": "https://github.com/sample"}],
        "github_repos": [{"owner": "sample", "repo": "api"}],
        "linkedin": ["https://www.linkedin.com/in/sample-user"],
    }
    skills = [
        {"skill": "python", "status": "verified", "evidence": "Seen in GitHub repo sample/api"},
        {"skill": "docker", "status": "unverified", "evidence": "No GitHub code evidence available"},
    ]

    analysis = agent.build_claim_analysis(
        "sde",
        agent.ROLE_PROFILES["sde"],
        "Used Python and Docker. Reached 10,000 users. 5 years of experience.",
        {"grad_year": 2028, "cgpa": 8.5},
        links,
        github,
        skills,
        [],
    )

    assert analysis["target_category"] == {
        "key": "sde",
        "label": "Software Development Engineer (SDE)",
        "selection_source": "batch_role",
    }
    assert analysis["role_comparison"]["suggestion"]["role_key"] == "sde"
    assert analysis["skill_summary"]["verified"] == 1
    assert analysis["skill_summary"]["unverified"] == 1
    assert analysis["claim_checks"][0]["status"] == "review_required"
    assert analysis["claim_checks"][1]["status"] == "potential_conflict"
    assert analysis["platforms"]["linkedin"]["status"] == "link_only"
    assert "does not scrape LinkedIn" in analysis["platforms"]["linkedin"]["note"]
    assert analysis["platforms"]["github"]["repositories"] == github["repos"]


def test_document_verification_crosschecks_fields_and_disclaims_external_validation():
    fields = {
        "name": "Alex Sample",
        "roll_number": "A-12",
        "cgpa": 8.5,
        "university": "Example University",
    }
    document = {
        "filename": "alex-sample-marksheet.pdf",
        "bytes": b"supporting-document",
        "text": (
            "Name: Alex Sample\nRoll No: A-12\nCGPA: 8.5\n"
            "Example University marksheet"
        ),
        "note": "",
    }

    verified = agent.verify_document(document, fields)
    mismatch = agent.verify_document(
        {**document, "text": document["text"].replace("A-12", "B-92")},
        fields,
    )

    assert verified["status"] == "verified"
    assert mismatch["status"] == "mismatch"
    assert "does not contact DigiLocker" in verified["digilocker"]["disclaimer"]


def test_candidate_report_includes_advisory_ai_claim_review(monkeypatch):
    async def fake_github(_links, request_semaphore=None, client=None):
        return agent._empty_github_summary()

    async def fake_review(*_args):
        return {
            "summary": "Skills are partly supported by public project evidence.",
            "flags": ["Validate the claimed deployment scale."],
            "claim_checks": [
                {
                    "claim": "10,000 users",
                    "status": "needs_review",
                    "evidence": "GitHub does not report product usage.",
                }
            ],
            "skill_assessment": [
                {
                    "skill": "python",
                    "status": "unverified",
                    "evidence": "No matching public repo evidence.",
                }
            ],
        }

    monkeypatch.setattr(agent, "analyze_github", fake_github)
    monkeypatch.setattr(agent, "llm_review", fake_review)
    draft = {
        "filename": "candidate.pdf",
        "text": "Python developer. 10,000 users. Education Projects Skills Experience.",
        "fields": {
            "name": "Alex Sample",
            "email": "alex@example.test",
            "phone": "",
            "university": "",
            "cgpa": 8.0,
            "roll_number": "",
            "grad_year": 2026,
        },
        "links": {
            "github_profiles": [],
            "github_repos": [],
            "linkedin": [],
        },
    }

    report = asyncio.run(agent.score_candidate(draft, "sde", []))

    assert report["investigation"]["analyst_mode"] == "llm+rules"
    assert report["investigation"]["ai_review"]["claim_checks"][0]["status"] == "needs_review"
    assert report["investigation"]["ai_review"]["skill_assessment"][0]["skill"] == "python"
    assert report["investigation"]["ai_review"]["score_authority"].startswith("AI findings are advisory")
    assert report["scores"]["trust"] != 100


def test_llm_review_sends_aggregate_evidence_without_resume_pii(monkeypatch):
    captured = {}
    monkeypatch.setattr(agent, "LLM_API_KEY", "local-test-key")

    class MockResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "summary": "Evidence is limited.",
                                    "flags": [],
                                    "claim_checks": [],
                                    "skill_assessment": [],
                                }
                            )
                        }
                    }
                ]
            }

    class MockAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url, **kwargs):
            captured.update(kwargs)
            return MockResponse()

    monkeypatch.setattr(agent.httpx, "AsyncClient", MockAsyncClient)
    review = asyncio.run(
        agent.llm_review(
            agent.ROLE_PROFILES["sde"],
            {"email": "private@example.test", "name": "Private Candidate"},
            {
                **agent._empty_github_summary(checked=True),
                "profiles": [{"username": "private-handle"}],
                "repos": [{"name": "private-handle/private-repo"}],
            },
            ["python"],
            [{"severity": "medium", "kind": "trust", "text": "Private user has no repo"}],
            "Private Candidate private@example.test resume content",
        )
    )

    request_content = captured["json"]["messages"][1]["content"]
    assert review["summary"] == "Evidence is limited."
    assert "private@example.test" not in request_content
    assert "Private Candidate" not in request_content
    assert "private-handle" not in request_content
    assert "resume content" not in request_content


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
                "email": "queue-" + uuid.uuid4().hex + "@example.test",
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
        accepted_jobs = []
        for role in agent.list_roles():
            response = client.post(
                "/api/analyze",
                data={"role_key": role["label"]},
                files=[("resumes", ("resume.pdf", b"%PDF-1.4\nupload", "application/pdf"))],
                headers={"X-CSRF-Token": csrf},
            )
            assert response.status_code == 200, (role, response.text)
            accepted = response.json()
            assert accepted["status"] == "queued"
            assert accepted["accepted_resumes"] == 1
            accepted_jobs.append(accepted)

        assert fake_redis.lists[queue_worker.QUEUE_KEY] == [
            job["task_id"] for job in accepted_jobs
        ]

        status = client.get(accepted_jobs[0]["status_url"])
        assert status.status_code == 200
        assert status.json()["status"] == "queued"


def test_direct_verify_endpoint_uses_authenticated_async_agent(monkeypatch):
    async def fake_analyze(filename, content, role_key):
        return {
            "filename": filename,
            "text": content.decode("utf-8"),
            "role_key": role_key,
        }

    monkeypatch.setattr(agent, "analyze_single_candidate", fake_analyze)
    with TestClient(main.app) as client:
        page = client.get("/register")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        registered = client.post(
            "/register",
            data={
                "csrf_token": csrf,
                "name": "Direct Verify User",
                "email": "direct-" + uuid.uuid4().hex + "@example.test",
                "password": "passphrase-123",
                "role": "officer",
            },
            follow_redirects=False,
        )
        assert registered.status_code == 303
        dashboard = client.get("/")
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', dashboard.text).group(1)
        response = client.post(
            "/api/verify",
            data={"role_key": "sde", "resume_text": "Alex Sample Python engineer"},
            headers={"X-CSRF-Token": csrf},
        )

    assert response.status_code == 200
    assert response.json() == {
        "filename": "resume.txt",
        "text": "Alex Sample Python engineer",
        "role_key": "sde",
    }


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
    monkeypatch.setattr(
        agent,
        "analyze_batch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("worker failure")),
    )

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


def test_worker_heartbeat_reports_online_state(monkeypatch):
    class FakeRedis:
        def __init__(self):
            self.values = {}

        def get(self, key):
            return self.values.get(key)

    fake_redis = FakeRedis()
    monkeypatch.setattr(queue_worker, "redis_client", fake_redis)
    assert queue_worker.is_worker_online() is False

    fake_redis.values[queue_worker.WORKER_HEARTBEAT_KEY] = b'{"worker_id":"test"}'
    assert queue_worker.is_worker_online() is True


def test_development_app_lifespan_starts_and_stops_queue_worker(monkeypatch):
    monkeypatch.setattr(main, "ENVIRONMENT", "development")
    monkeypatch.setattr(main, "validate_startup_configuration", lambda: None)
    monkeypatch.setattr(queue_worker, "redis_client", object())
    worker_started = threading.Event()
    worker_stopped = threading.Event()

    def fake_run_worker(stop_event):
        worker_started.set()
        stop_event.wait(2)
        worker_stopped.set()

    monkeypatch.setattr(queue_worker, "run_worker", fake_run_worker)

    async def exercise_lifespan():
        async with main.app_lifespan(main.app):
            assert worker_started.wait(1)
            assert not worker_stopped.is_set()
        assert worker_stopped.wait(1)

    asyncio.run(exercise_lifespan())


def test_production_app_lifespan_does_not_start_embedded_queue_worker(monkeypatch):
    monkeypatch.setattr(main, "ENVIRONMENT", "production")
    monkeypatch.setattr(main, "validate_startup_configuration", lambda: None)
    monkeypatch.setattr(queue_worker, "redis_client", object())

    def fail_if_worker_starts(**_kwargs):
        raise AssertionError("Production must use its separately managed queue worker.")

    monkeypatch.setattr(queue_worker, "run_worker", fail_if_worker_starts)

    async def exercise_lifespan():
        async with main.app_lifespan(main.app):
            return

    asyncio.run(exercise_lifespan())


def test_queued_job_status_reports_when_worker_is_offline(monkeypatch):
    owner = User(id=23, name="Status User", email="status@example.test", password_hash="unused")
    monkeypatch.setattr(queue_worker, "redis_client", object())
    monkeypatch.setattr(
        queue_worker,
        "get_job_status_from_redis",
        lambda _job_id: {"status": "queued", "owner_id": owner.id},
    )
    monkeypatch.setattr(queue_worker, "is_worker_online", lambda: False)

    assert main.api_job_status("12345678-1234-1234-1234-123456789012", owner) == {
        "status": "queued",
        "worker_online": False,
    }


def test_worker_publishes_monotonic_overall_progress(monkeypatch, tmp_path):
    create_tables()
    db = SessionLocal()
    owner = User(
        name="Progress User",
        email="progress-" + uuid.uuid4().hex + "@example.test",
        password_hash="not-used",
        role="officer",
    )
    try:
        db.add(owner)
        db.commit()
        owner_id = owner.id
    finally:
        db.close()

    upload_dir = tmp_path / "batch"
    upload_dir.mkdir()
    upload_paths = []
    for filename in ("one.pdf", "two.pdf"):
        path = upload_dir / filename
        path.write_bytes(b"%PDF-1.4")
        upload_paths.append({"filename": filename, "path": str(path)})

    class FakeRedis:
        def __init__(self):
            self.values = {}
            self.history = []

        def set(self, key, value):
            self.values[key] = value
            self.history.append((key, value))

    fake_redis = FakeRedis()
    monkeypatch.setattr(queue_worker, "UPLOAD_ROOT", tmp_path)
    monkeypatch.setattr(queue_worker, "redis_client", fake_redis)
    monkeypatch.setattr(models, "SessionLocal", SessionLocal)

    def fake_analyze_batch(_resumes, _documents, _role_key, progress):
        progress("reading_resumes", 1, 2)
        progress("reading_resumes", 2, 2)
        progress("scoring", 1, 2)
        progress("scoring", 2, 2)
        return (
            [
                {
                    "filename": "one.pdf",
                    "name": "Progress Candidate",
                    "email": "candidate@example.test",
                    "role_key": "sde",
                    "role_label": "SDE",
                    "scores": {"ats": 70, "trust": 80, "fit": 75},
                    "flags": [],
                    "verdict": "Verified",
                    "doc_status": "none",
                }
            ],
            [],
            [],
        )

    monkeypatch.setattr(agent, "analyze_batch", fake_analyze_batch)
    queue_worker.process_job(
        "progress-job",
        {
            "owner_id": owner_id,
            "role_key": "sde",
            "resumes": upload_paths,
            "documents": [],
        },
    )

    statuses = [
        json.loads(value)
        for key, value in fake_redis.history
        if key == "veribot_job:progress-job"
    ]
    final_status = json.loads(fake_redis.values["veribot_job:progress-job"])
    progress_values = [
        status["progress_percent"]
        for status in statuses
        if "progress_percent" in status
    ]
    assert final_status["status"] == "completed"
    assert final_status["progress_percent"] == 100
    assert final_status["stage"] == "completed"
    assert final_status["saved"][0]["name"] == "Progress Candidate"
    assert progress_values == sorted(progress_values)


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
