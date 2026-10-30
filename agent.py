"""
agent.py - Comprehensive Verification Engine for VeriBot AI.
Combines Deep GitHub Auditing, Commit Analysis, Skill Cross-Verification Matrix,
Rule-based Fraud & Inflation Detection, and Async Batch Scaling.
"""

import asyncio
import hashlib
import inspect
import io
import json
import os
import re
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
import cp_fetcher
import httpx
from dotenv import load_dotenv

# Load Environment Variables
load_dotenv()

logger = logging.getLogger("veribot.agent")

# Configuration
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT = 20.0
TESSERACT_CMD = os.getenv("TESSERACT_CMD", "")
MAX_RESUME_CHARS = 100_000
MAX_GITHUB_CONCURRENCY = 4
MAX_REPOSITORIES_PER_PROFILE = 30
MAX_DEEP_AUDIT_REPOSITORIES = 3
MAX_COMMITS_PER_REPOSITORY = 100
GITHUB_CACHE_TTL_SECONDS = 60 * 60

# Predefined Target Role Profiles
ROLE_PROFILES = {
    "sde": {
        "label": "Software Development Engineer (SDE)",
        "kind": "tech",
        "keywords": [
            "python", "java", "javascript", "typescript", "c++", "c#", "go", "rust",
            "react", "node", "express", "fastapi", "django", "flask", "sql", "postgresql",
            "mongodb", "redis", "git", "docker", "kubernetes", "aws", "rest api", "graphql",
            "data structures", "algorithms", "system design", "html", "css", "testing"
        ]
    },
    "data_scientist": {
        "label": "Data Scientist / ML Engineer",
        "kind": "tech",
        "keywords": [
            "python", "r", "sql", "machine learning", "deep learning", "tensorflow",
            "pytorch", "scikit-learn", "pandas", "numpy", "matplotlib", "seaborn",
            "nlp", "computer vision", "statistics", "data analysis", "xgboost", "git", "docker"
        ]
    },
    "devops": {
        "label": "DevOps / Cloud Engineer",
        "kind": "tech",
        "keywords": [
            "docker", "kubernetes", "terraform", "ansible", "aws", "azure", "gcp",
            "ci/cd", "github actions", "jenkins", "bash", "linux", "python", "yaml",
            "prometheus", "grafana", "git", "networking", "security"
        ]
    },
    "product_manager": {
        "label": "Product Manager / Analyst",
        "kind": "non_tech",
        "keywords": [
            "jira", "agile", "scrum", "product roadmap", "kpi", "metrics", "analytics",
            "user research", "user stories", "wireframing", "figma", "prioritization",
            "go-to-market", "mvp", "a/b testing", "stakeholder management"
        ]
    },
    "hr": {
        "label": "Human Resources (HR)",
        "kind": "non_tech",
        "keywords": [
            "recruitment", "talent acquisition", "employee relations", "onboarding",
            "performance management", "human resources", "hris", "payroll",
            "benefits administration", "workforce planning", "labor law",
            "employment law", "training and development", "learning and development",
            "conflict resolution", "employee engagement", "compensation",
            "applicant tracking system"
        ]
    }
}


def list_roles() -> list[dict]:
    """Return the role options expected by the API and dashboard."""
    return [
        {"key": key, "label": role["label"], "kind": role["kind"]}
        for key, role in ROLE_PROFILES.items()
    ]


def _empty_github_summary(checked: bool = False) -> dict:
    """Create one stable GitHub report shape for both success and no-link cases."""
    return {
        "checked": checked,
        "profiles": [],
        "repos": [],
        "languages": {},
        "all_languages": {},
        "language_usage_percent": {},
        "errors": [],
        "total_repos": 0,
        "repositories_sampled": 0,
        "public_repos_reported": 0,
        "original_repos": 0,
        "fork_repos": 0,
        "fork_ratio": 0.0,
        "recent_repos": 0,
        "recent_commits_sampled": 0,
        "candidate_recent_commits_sampled": 0,
        "activity_audits_completed": 0,
        "total_stars": 0,
        "candidate_commits_sampled": 0,
    }


def _validated_text(text: str) -> str:
    if len(text) > MAX_RESUME_CHARS:
        raise ValueError(f"Resume text exceeds the {MAX_RESUME_CHARS} character limit.")
    return text.strip()


def extract_pdf_text(file_bytes: bytes) -> str:
    """Extract text from a PDF upload without requiring a filename."""
    return extract_text_from_file("resume.pdf", file_bytes)


def extract_pdf_hyperlinks(file_bytes: bytes) -> list[str]:
    """Read URI annotations from PDFs so link text need not be visible on the page."""
    links = set()
    try:
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(file_bytes), strict=False)
        for page in reader.pages[:10]:
            for annotation_ref in page.get("/Annots", []):
                annotation = annotation_ref.get_object()
                action = annotation.get("/A", {})
                url = action.get("/URI")
                if isinstance(url, str) and url.startswith(("https://", "http://")):
                    links.add(url)
    except Exception:
        logger.debug("Could not read PDF URI annotations with pypdf", exc_info=True)
    return sorted(links)


def extract_email(text: str) -> str:
    text = _validated_text(text)
    match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", text)
    return match.group(0) if match else ""


def _extract_links(text: str, hyperlinks: list[str] | None = None) -> dict:
    combined_text = "\n".join([text, *(hyperlinks or [])])
    details = parse_resume_details(combined_text)
    profiles = [
        {"username": username, "url": f"https://github.com/{username}"}
        for username in details["github_usernames"]
    ]
    repositories = []
    seen = set()
    for url in details["github_repo_urls"]:
        parsed = parse_repo_url(url)
        if parsed:
            owner, repo = parsed
            key = (owner.lower(), repo.lower())
            if key not in seen:
                repositories.append({"owner": owner, "repo": repo})
                seen.add(key)
    return {
        "github_profiles": profiles,
        "github_repos": repositories,
        "linkedin": details["linkedin"],
        "competitive_programming_profiles": cp_fetcher.extract_profile_usernames(
            combined_text
        ),
    }


def read_resume(filename: str, file_bytes: bytes) -> dict:
    """Extract bounded resume text, candidate fields, and public profile links."""
    text = extract_text_from_file(filename, file_bytes)
    if filename.lower().endswith(".pdf") and len(text.strip()) < 40:
        text, note = extract_document_text(filename, file_bytes)
        if note:
            logger.info("Resume OCR note for %s: %s", filename, note)
    text = _validated_text(text)
    if len(text) < 20:
        raise ValueError("Could not read enough text from this resume.")
    details = parse_resume_details(text, filename)
    fields = {
        "name": details["name"],
        "email": details["email"],
        "phone": details["phone"],
        "university": details["university"],
        "degree": details["degree"],
        "cgpa": details["cgpa"],
        "roll_number": details["roll_number"],
        "grad_year": details["grad_year"],
        "experience_years": details["experience_years"],
    }
    hyperlinks = (
        extract_pdf_hyperlinks(file_bytes)
        if filename.lower().endswith(".pdf")
        else []
    )
    return {
        "filename": filename,
        "text": text,
        "fields": fields,
        "links": _extract_links(text, hyperlinks),
    }


def _github_date(value: str):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
    except (AttributeError, TypeError, ValueError):
        return None


async def analyze_github(
    links: dict,
    request_semaphore: asyncio.Semaphore | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Fetch bounded profile/repository evidence with a shared concurrency limit."""
    profiles = links.get("github_profiles", [])[:2]
    repo_links = links.get("github_repos", [])[:MAX_DEEP_AUDIT_REPOSITORIES]
    if not profiles and not repo_links:
        return _empty_github_summary()

    summary = _empty_github_summary(checked=True)
    semaphore = request_semaphore or asyncio.Semaphore(MAX_GITHUB_CONCURRENCY)
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "VeriBotAI"}
    if GITHUB_TOKEN:
        headers["Authorization"] = "Bearer " + GITHUB_TOKEN

    async def get_json(api_client, path: str, params: dict | None = None):
        cache_key = "veribot_github:" + hashlib.sha256(
            (path + "?" + json.dumps(params or {}, sort_keys=True)).encode("utf-8")
        ).hexdigest()
        async with semaphore:
            cache = None
            try:
                from queue_worker import redis_client

                cache = redis_client
                if cache is not None:
                    cached = await asyncio.to_thread(cache.get, cache_key)
                    if isinstance(cached, bytes):
                        cached = cached.decode("utf-8")
                    if cached:
                        return json.loads(cached)
            except Exception:
                logger.warning("GitHub metadata cache read failed for %s", path, exc_info=True)
            response = await api_client.get(
                f"{GITHUB_API}{path}", params=params, headers=headers
            )
            if response.status_code == 404:
                raise ValueError("not found (or private)")
            if response.status_code == 403:
                raise ValueError("GitHub rate limit reached; configure GITHUB_TOKEN.")
            response.raise_for_status()
            data = response.json()
            if cache is not None:
                try:
                    await asyncio.to_thread(
                        cache.set,
                        cache_key,
                        json.dumps(data),
                        ex=GITHUB_CACHE_TTL_SECONDS,
                    )
                except Exception:
                    logger.warning("GitHub metadata cache write failed for %s", path, exc_info=True)
            return data

    repo_metadata: dict[str, dict] = {}
    byte_counts: Counter = Counter()
    language_counts: Counter = Counter()
    all_language_counts: Counter = Counter()
    candidate_commits = 0
    recent_commits = 0
    candidate_recent_commits = 0
    activity_audits_completed = 0
    inspected_repo_keys = set()
    errors = []
    candidate_usernames = {
        str(profile.get("username", "")).strip().casefold()
        for profile in profiles
        if profile.get("username")
    }

    def remember_repo(repo: dict, source: str, username: str = "") -> str:
        full_name = repo.get("full_name") or (
            f"{repo.get('owner', {}).get('login', username)}/{repo.get('name', '')}"
        )
        key = str(full_name).lower()
        repo_metadata.setdefault(
            key,
            {
                "name": full_name,
                "url": repo.get("html_url") or f"https://github.com/{full_name}",
                "language": repo.get("language") or "",
                "stars": repo.get("stargazers_count", 0),
                "is_fork": bool(repo.get("fork", False)),
                "pushed_at": repo.get("pushed_at") or "",
                "description": repo.get("description") or "",
                "topics": repo.get("topics") or [],
                "source": source,
                "commits_sampled": 0,
                "claimed_user_commits_sampled": 0,
                "recent_commits_sampled": 0,
                "last_commit_at": None,
                "commit_audit_completed": False,
                "commit_count_sampled": 0,
                "claimed_user_commits": 0,
                "first_commit_in_sample": None,
                "latest_commit": None,
                "sample_commit_messages": [],
                "language_breakdown_percent": {},
            },
        )
        return key

    async def inspect_repo(api_client, repo_info: dict, source: str):
        nonlocal candidate_commits, recent_commits, candidate_recent_commits
        nonlocal activity_audits_completed
        key = remember_repo(repo_info, source)
        if key in inspected_repo_keys:
            return
        inspected_repo_keys.add(key)
        full_name = str(repo_metadata[key]["name"])
        owner, _, repo_name = full_name.partition("/")
        if not owner or not repo_name:
            return
        try:
            language_data, commit_data = await asyncio.gather(
                get_json(api_client, f"/repos/{owner}/{repo_name}/languages"),
                get_json(
                    api_client,
                    f"/repos/{owner}/{repo_name}/commits",
                    {"per_page": MAX_COMMITS_PER_REPOSITORY},
                ),
            )
            language_data = language_data if isinstance(language_data, dict) else {}
            commit_data = commit_data if isinstance(commit_data, list) else []
            now = datetime.now(timezone.utc)
            commit_dates = []
            claimed_commit_count = 0
            claimed_recent_commit_count = 0
            sample_messages = []
            for commit in commit_data:
                if not isinstance(commit, dict):
                    continue
                github_author = commit.get("author")
                is_candidate_commit = (
                    isinstance(github_author, dict)
                    and str(github_author.get("login", "")).casefold()
                    in candidate_usernames
                )
                if is_candidate_commit:
                    claimed_commit_count += 1
                details = commit.get("commit")
                author = details.get("author") if isinstance(details, dict) else None
                committer = details.get("committer") if isinstance(details, dict) else None
                date_value = author.get("date") if isinstance(author, dict) else None
                if not date_value and isinstance(committer, dict):
                    date_value = committer.get("date")
                parsed_date = _github_date(date_value or "")
                if parsed_date is not None:
                    commit_dates.append((parsed_date, str(date_value)))
                    if (
                        is_candidate_commit
                        and now - timedelta(days=365) <= parsed_date <= now
                    ):
                        claimed_recent_commit_count += 1
                message = details.get("message") if isinstance(details, dict) else None
                if isinstance(message, str) and message.splitlines() and len(sample_messages) < 8:
                    sample_messages.append(message.splitlines()[0][:90])
            recent_dates = [
                date
                for date, _ in commit_dates
                if now - timedelta(days=365) <= date <= now
            ]
            total_bytes = sum(value for value in language_data.values() if isinstance(value, int))
            percentages = {
                language: round(byte_count * 100 / total_bytes, 1)
                for language, byte_count in language_data.items()
                if total_bytes and isinstance(byte_count, int)
            }
            item = repo_metadata[key]
            item["language_breakdown_percent"] = percentages
            item["commits_sampled"] = len(commit_data)
            item["claimed_user_commits_sampled"] = claimed_commit_count
            item["recent_commits_sampled"] = len(recent_dates)
            item["last_commit_at"] = max(commit_dates)[0].isoformat() if commit_dates else None
            item["commit_count_sampled"] = len(commit_data)
            item["claimed_user_commits"] = claimed_commit_count
            item["first_commit_in_sample"] = (
                min(commit_dates)[1] if commit_dates else None
            )
            item["latest_commit"] = max(commit_dates)[1] if commit_dates else None
            item["sample_commit_messages"] = sample_messages
            item["commit_audit_completed"] = True
            candidate_commits += claimed_commit_count
            recent_commits += len(recent_dates)
            candidate_recent_commits += claimed_recent_commit_count
            activity_audits_completed += 1
            for language, byte_count in language_data.items():
                if isinstance(byte_count, int) and byte_count > 0:
                    byte_counts[language] += byte_count
                    all_language_counts[language] += 1
                    if not item["is_fork"]:
                        language_counts[language] += 1
        except Exception as error:
            errors.append(f"Repository {full_name} detail audit: {error}")

    async def collect_profile(api_client, profile: dict):
        username = str(profile.get("username", "")).strip()
        if not username:
            return
        try:
            user, repo_list = await asyncio.gather(
                get_json(api_client, f"/users/{username}"),
                get_json(
                    api_client,
                    f"/users/{username}/repos",
                    {"per_page": MAX_REPOSITORIES_PER_PROFILE, "sort": "pushed"},
                ),
            )
            created_at = _github_date(user.get("created_at", ""))
            age_days = (
                max(0, (datetime.now(timezone.utc) - created_at).days)
                if created_at
                else None
            )
            summary["profiles"].append(
                {
                    "username": username,
                    "url": profile.get("url") or f"https://github.com/{username}",
                    "public_repos": user.get("public_repos", 0),
                    "followers": user.get("followers", 0),
                    "account_age_days": age_days,
                }
            )
            summary["public_repos_reported"] += int(user.get("public_repos", 0) or 0)
            for index, repo in enumerate(repo_list[:MAX_REPOSITORIES_PER_PROFILE]):
                remember_repo(repo, "profile", username)
                if index < MAX_DEEP_AUDIT_REPOSITORIES:
                    await inspect_repo(api_client, repo, "profile")
        except Exception as error:
            errors.append(f"GitHub profile {username}: {error}")

    async def collect_repo_link(api_client, repo_link: dict):
        owner = str(repo_link.get("owner", "")).strip()
        name = str(repo_link.get("repo", "")).strip()
        if not owner or not name:
            return
        if f"{owner}/{name}".lower() in repo_metadata:
            return
        try:
            repo = await get_json(api_client, f"/repos/{owner}/{name}")
            await inspect_repo(api_client, repo, "resume_link")
        except Exception as error:
            errors.append(f"GitHub repo {owner}/{name}: {error}")

    async def run_with_client(api_client):
        await asyncio.gather(
            *(collect_profile(api_client, profile) for profile in profiles),
            *(collect_repo_link(api_client, link) for link in repo_links),
        )

    if client is None:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as owned_client:
            await run_with_client(owned_client)
    else:
        await run_with_client(client)

    repos = list(repo_metadata.values())
    # Avoid counting duplicate profile and explicit-link observations twice.
    language_counts.clear()
    all_language_counts.clear()
    for repo in repos:
        lang = repo.get("language")
        if lang:
            all_language_counts[lang] += 1
            if not repo["is_fork"]:
                language_counts[lang] += 1
    original = sum(not repo["is_fork"] for repo in repos)
    forks = len(repos) - original
    now = datetime.now(timezone.utc)
    recent = 0
    for repo in repos:
        pushed = _github_date(repo.get("pushed_at", ""))
        if pushed and (now - pushed).days <= 365:
            recent += 1
    total_bytes = sum(byte_counts.values())
    summary.update(
        {
            "profiles": sorted(summary["profiles"], key=lambda item: item["username"].lower()),
            "repos": repos,
            "languages": dict(language_counts),
            "all_languages": dict(all_language_counts),
            "language_usage_percent": {
                language: round(value * 100 / total_bytes, 1)
                for language, value in byte_counts.items()
                if total_bytes
            },
            "errors": errors,
            "total_repos": len(repos),
            "repositories_sampled": len(repos),
            "original_repos": original,
            "fork_repos": forks,
            "fork_ratio": round(forks / len(repos), 3) if repos else 0.0,
            "recent_repos": recent,
            "recent_commits_sampled": recent_commits,
            "candidate_recent_commits_sampled": candidate_recent_commits,
            "activity_audits_completed": activity_audits_completed,
            "total_stars": sum(int(repo.get("stars", 0) or 0) for repo in repos),
            "candidate_commits_sampled": candidate_commits,
        }
    )
    return summary


def build_skill_table(_role: dict, matched_keywords: list[str], _text: str, github: dict) -> list[dict]:
    """Classify resume keywords using non-fork public repository evidence."""
    original_repos = [repo for repo in github.get("repos", []) if not repo.get("is_fork")]
    role_keywords = {str(keyword).lower() for keyword in _role.get("keywords", [])}
    output = []
    for skill in matched_keywords:
        if skill.lower() not in role_keywords or not _keyword_matches(skill, _text):
            continue
        matching = next(
            (
                repo
                for repo in original_repos
                if _keyword_matches(
                    skill,
                    " ".join(
                        [
                            str(repo.get("name", "")),
                            str(repo.get("description", "")),
                            " ".join(repo.get("topics", [])),
                            str(repo.get("language", "")),
                            " ".join(repo.get("sample_commit_messages", [])),
                        ]
                    ),
                )
            ),
            None,
        )
        if matching:
            output.append(
                {
                    "skill": skill,
                    "status": "verified",
                    "classification": "verified",
                    "evidence": f"Seen in GitHub repo {matching.get('name', 'repository')}",
                }
            )
        elif github.get("original_repos", 0):
            output.append(
                {
                    "skill": skill,
                    "status": "partial",
                    "classification": "unverified",
                    "evidence": "Candidate has original repos, but none mention this skill directly",
                }
            )
        else:
            output.append(
                {
                    "skill": skill,
                    "status": "unverified",
                    "classification": (
                        "unverified" if github.get("checked") else "self_claimed"
                    ),
                    "evidence": "No GitHub code evidence available",
                }
            )
    return output


def _keyword_matches(keyword: str, text: str) -> bool:
    return re.search(
        r"(?<![a-z0-9])" + re.escape(keyword.lower()) + r"(?![a-z0-9])",
        text.lower(),
    ) is not None


def make_flag(severity: str, text: str, source: str = "rule", kind: str = "trust") -> dict:
    return {"severity": severity, "text": text, "source": source, "kind": kind}


def detect_flags(
    role: dict,
    text: str,
    fields: dict,
    links: dict,
    github: dict,
    _evidence_score: int,
    _fit_score: int,
    _matched_keywords: list[str],
    skills: list[dict] | None = None,
) -> list[dict]:
    """Compare resume claims with observable profile, repository, and timeline facts."""
    flags = []
    if role.get("kind") == "tech" and not links.get("github_profiles") and not links.get("github_repos"):
        flags.append(make_flag("medium", "No GitHub profile or repository link found for this technical role."))
    missing_contact = [
        label
        for label, value in (("email", fields.get("email")), ("phone", fields.get("phone")))
        if not value
    ]
    if missing_contact:
        flags.append(
            make_flag(
                "low",
                "Resume is missing contact details: " + " and ".join(missing_contact) + ".",
            )
        )
    for profile in github.get("profiles", []):
        age = profile.get("account_age_days")
        if age is not None and age < 90:
            flags.append(make_flag("high", f"GitHub account @{profile['username']} is only {age} days old."))
        if profile.get("public_repos") == 0:
            flags.append(make_flag("medium", f"GitHub account @{profile['username']} has no public repositories."))
    if github.get("total_repos", 0) and github.get("original_repos", 0) == 0:
        flags.append(make_flag("high", "Every GitHub repository found is a fork; no original public project was found."))
    elif (
        github.get("total_repos", 0) >= 4
        and github.get("fork_ratio", 0) >= 0.75
    ):
        flags.append(
            make_flag(
                "medium",
                f"{github['fork_ratio']:.0%} of sampled GitHub repositories are forks.",
            )
        )
    if (
        github.get("checked")
        and github.get("total_repos", 0) > 0
        and github.get("activity_audits_completed", 0) > 0
        and github.get("candidate_recent_commits_sampled", 0) == 0
    ):
        flags.append(
            make_flag(
                "low",
                "No recent commits were found in the sampled GitHub repositories.",
                kind="fit",
            )
        )
    unsupported_skills = [
        str(item.get("skill", ""))
        for item in skills or []
        if item.get("status") != "verified"
    ]
    if github.get("checked") and unsupported_skills:
        examples = ", ".join(unsupported_skills[:5])
        suffix = " and other claimed skills" if len(unsupported_skills) > 5 else ""
        flags.append(
            make_flag(
                "low",
                f"GitHub code evidence did not verify {examples}{suffix}.",
                kind="fit",
            )
        )
    for amount, unit in re.findall(
        r"(\d[\d,]*(?:\+)?)[\s+]*(users|customers|requests|followers|downloads|stars|percent|%)",
        text.lower(),
    ):
        numeric = int(amount.replace(",", "").replace("+", ""))
        if numeric >= 1000 and github.get("original_repos", 0) == 0:
            flags.append(make_flag("medium", f"Resume claim of {numeric} {unit} has little matching original GitHub evidence."))
            break
    experience = re.search(r"(\d+)\+?\s*years?\s*of\s*experience", text.lower())
    grad_year = fields.get("grad_year")
    if experience and grad_year and int(experience.group(1)) >= 5 and grad_year >= datetime.now().year - 1:
        flags.append(make_flag("medium", "Experience duration may conflict with the stated graduation timeline."))
    cgpa = fields.get("cgpa")
    if cgpa is not None and not 0 <= float(cgpa) <= 10:
        flags.append(make_flag("high", f"CGPA {cgpa} is outside the possible 0-10 range."))
    if _matched_keywords and _fit_score < 15:
        flags.append(
            make_flag(
                "low",
                f"Limited public evidence supports the selected role: {len(_matched_keywords)} resume keyword(s) matched.",
                kind="fit",
            )
        )
    if github.get("checked") and _evidence_score < 30 and _matched_keywords:
        flags.append(
            make_flag(
                "low",
                "GitHub evidence is limited for the skills claimed on the resume.",
                kind="fit",
            )
        )
    for error in github.get("errors", []):
        flags.append(make_flag("low", f"Could not verify GitHub evidence: {error}"))
    return flags


def build_claim_analysis(
    role_key: str,
    role: dict,
    text: str,
    fields: dict,
    links: dict,
    github: dict,
    skills: list[dict],
    flags: list[dict],
) -> dict:
    """Write an evidence-oriented summary of claims and connected platform limits."""
    claims = []
    for amount, unit in re.findall(
        r"(\d[\d,]*(?:\+)?)[\s+]*(users|customers|requests|followers|downloads|stars|percent|%)",
        text.lower(),
    ):
        claims.append(
            {
                "claim": f"{amount.replace(',', '')} {unit}",
                "status": "review_required",
                "evidence": "GitHub metadata cannot independently confirm product usage or business impact.",
            }
        )
        break
    experience = re.search(r"(\d+)\+?\s*years?\s*of\s*experience", text.lower())
    if experience and fields.get("grad_year"):
        claims.append(
            {
                "claim": f"{experience.group(1)} years of experience",
                "status": "potential_conflict",
                "evidence": f"Compare the claimed duration with graduation year {fields['grad_year']}.",
            }
        )
    skill_counts = Counter(skill.get("status", "unverified") for skill in skills)
    return {
        "target_category": {
            "key": role_key,
            "label": role["label"],
            "selection_source": "batch_role",
        },
        "role_comparison": {
            "suggestion": {"role_key": role_key, "label": role["label"]},
        },
        "skill_summary": {
            "verified": skill_counts["verified"],
            "partial": skill_counts["partial"],
            "unverified": skill_counts["unverified"],
        },
        "claim_checks": claims,
        "platforms": {
            "github": {
                "status": "checked" if github.get("checked") else "not_provided",
                "repositories": github.get("repos", []),
                "scope": "Repository and commit values are bounded samples, not lifetime totals.",
            },
            "linkedin": {
                "status": "link_only" if links.get("linkedin") else "not_provided",
                "urls": links.get("linkedin", []),
                "note": "VeriBot checks LinkedIn links but does not scrape LinkedIn.",
            },
        },
        "findings": [flag["text"] for flag in flags],
    }


async def llm_review(role, _fields, github, matched_keywords, rule_flags, _resume_excerpt):
    """Return an advisory review using aggregate evidence only; never send resume PII."""
    del _fields, _resume_excerpt
    if not LLM_API_KEY:
        return None
    flag_counts = Counter(
        f"{flag.get('severity', 'unknown')}:{flag.get('kind', 'unknown')}"
        for flag in rule_flags
    )
    prompt = {
        "target_role": role["label"],
        "matched_resume_skills": matched_keywords,
        "github_summary": {
            "profile_count": len(github.get("profiles", [])),
            "public_repos_reported": github.get("public_repos_reported", 0),
            "repositories_sampled": github.get("repositories_sampled", 0),
            "languages": github.get("languages", {}),
            "original_repos": github.get("original_repos", 0),
            "fork_repos": github.get("fork_repos", 0),
            "recent_repos": github.get("recent_repos", 0),
            "recent_commits_sampled": github.get("recent_commits_sampled", 0),
            "candidate_recent_commits_sampled": github.get(
                "candidate_recent_commits_sampled", 0
            ),
            "total_stars": github.get("total_stars", 0),
            "candidate_commits_sampled": github.get("candidate_commits_sampled", 0),
            "sampling_note": "Repository and commit values are samples, not lifetime totals.",
        },
        "rule_flag_counts": dict(flag_counts),
        "instruction": (
            "Assess the aggregate evidence conservatively. Do not infer fraud from missing data. "
            "Return JSON with summary, flags (strings), claim_checks (objects with "
            "claim/status/evidence), and skill_assessment (objects with "
            "skill/status/evidence). Do not assign scores. Do not request or infer "
            "personal identity details."
        ),
    }
    request_headers = {
        "Authorization": "Bearer " + LLM_API_KEY,
        "Content-Type": "application/json",
    }
    request_body = {
        "model": LLM_MODEL,
        "temperature": 0.1,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "You are a cautious resume evidence reviewer."},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=True)},
        ],
    }
    try:
        timeout = httpx.Timeout(25.0, connect=5.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                LLM_BASE_URL.rstrip("/") + "/chat/completions",
                headers=request_headers,
                json=request_body,
            )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        result = json.loads(content)
        allowed_statuses = {
            "verified", "partial", "unverified", "needs_review",
            "review_required", "potential_conflict", "supported",
        }
        claim_checks = []
        for item in result.get("claim_checks", [])[:10]:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status", "needs_review")).lower()
            claim_checks.append(
                {
                    "claim": str(item.get("claim", ""))[:200],
                    "status": status if status in allowed_statuses else "needs_review",
                    "evidence": str(item.get("evidence", ""))[:500],
                }
            )
        skill_assessment = []
        for item in result.get("skill_assessment", [])[:30]:
            if not isinstance(item, dict):
                continue
            status = str(item.get("status", "unverified")).lower()
            skill_assessment.append(
                {
                    "skill": str(item.get("skill", ""))[:100],
                    "status": status if status in allowed_statuses else "unverified",
                    "evidence": str(item.get("evidence", ""))[:500],
                }
            )
        return {
            "summary": str(result.get("summary", ""))[:400],
            "flags": [str(item)[:200] for item in result.get("flags", [])[:3]],
            "claim_checks": claim_checks,
            "skill_assessment": skill_assessment,
        }
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
        logger.warning("Optional LLM review failed; using deterministic rules: %s", error)
        return None


async def score_candidate(
    draft: dict,
    role_key: str,
    document_results: list,
    _github_semaphore: asyncio.Semaphore | None = None,
    _github_client: httpx.AsyncClient | None = None,
) -> dict:
    role = ROLE_PROFILES[role_key]
    fields = draft["fields"]
    links = draft["links"]
    text = draft["text"]
    if role["kind"] == "tech":
        github = await analyze_github(links, _github_semaphore, _github_client)
        competitive_programming = await cp_fetcher.fetch_competitive_profiles(
            text,
            _github_client,
            _github_semaphore,
            links.get("competitive_programming_profiles"),
        )
        technical_rating = cp_fetcher.calculate_technical_rating(
            competitive_programming, github
        )
        professional_profile = None
    else:
        github = _empty_github_summary()
        competitive_programming = None
        technical_rating = None
        professional_profile = {
            "linkedin": {
                "status": "link_found" if links.get("linkedin") else "not_provided",
                "urls": links.get("linkedin", []),
                "verification": "resume_link_only",
                "scraped": False,
            },
            "resume_metrics": {
                "experience_years": fields.get("experience_years"),
                "education": {
                    "university": fields.get("university"),
                    "degree": fields.get("degree"),
                    "graduation_year": fields.get("grad_year"),
                },
                "certificates": len(
                    re.findall(
                        r"\b(certification|certificate|certified|license)\b",
                        text,
                        re.I,
                    )
                ),
                "role_skills_claimed": sum(
                    _keyword_matches(keyword, text) for keyword in role["keywords"]
                ),
                "supporting_document_status": _document_status(document_results),
            },
            "note": (
                "Professional metrics are extracted from the resume and supplied "
                "documents. LinkedIn profile contents are not scraped."
            ),
        }
    matched = [
        keyword for keyword in role["keywords"] if _keyword_matches(keyword, text)
    ]
    skills = build_skill_table(role, matched, text, github)
    claimed_skills = {item["skill"].casefold(): item for item in skills}
    relevant_skills = [
        claimed_skills.get(
            keyword.casefold(),
            {
                "skill": keyword,
                "status": "not_claimed",
                "classification": "not_claimed",
                "evidence": "Not identified in the resume text.",
            },
        )
        for keyword in role["keywords"]
    ]
    verified = sum(item["status"] == "verified" for item in skills)
    partial = sum(item["status"] == "partial" for item in skills)
    fit = min(
        100,
        round((verified + 0.5 * partial) * 100 / max(1, len(skills))),
    )
    ats_breakdown = [
        {"label": "Base score", "points": 25, "max": 25},
        {"label": "Role keywords", "points": min(40, len(matched) * 4), "max": 40},
        {
            "label": "Resume content",
            "points": 15 if len(text) > 200 else 5,
            "max": 15,
        },
        {"label": "Email", "points": 10 if fields.get("email") else 0, "max": 10},
    ]
    ats = min(100, sum(item["points"] for item in ats_breakdown))
    if github.get("checked"):
        evidence_score = min(
            100,
            25
            + min(45, github.get("original_repos", 0) * 15)
            + min(15, github.get("recent_repos", 0) * 5)
            + min(15, github.get("total_stars", 0) * 3),
        )
    else:
        evidence_score = 15
    flags = detect_flags(
        role, text, fields, links, github, evidence_score, fit, matched, skills
    )
    flags.extend(_document_flags(document_results))
    claim_analysis = build_claim_analysis(role_key, role, text, fields, links, github, skills, flags)
    if competitive_programming is not None:
        claim_analysis["platforms"]["competitive_programming"] = (
            competitive_programming
        )
    doc_status = _document_status(document_results)
    doc_adjustment = {"verified": 5, "partial": -5, "mismatch": -15, "unreadable": -10, "none": 0}
    trust = max(
        5,
        min(
            100,
            85
            - 20 * sum(flag["severity"] == "high" for flag in flags)
            - 10 * sum(flag["severity"] == "medium" for flag in flags)
            + doc_adjustment[doc_status],
        ),
    )
    if _github_semaphore is None:
        review = await llm_review(role, fields, github, matched, flags, text[:4000])
    else:
        async with _github_semaphore:
            review = await llm_review(role, fields, github, matched, flags, text[:4000])
    if review:
        for flag in review.get("flags", []):
            flags.append(make_flag("medium", str(flag), source="ai"))
    investigation = {
        **claim_analysis,
        "analyst_mode": "llm+rules" if review else "rules",
        "findings": claim_analysis["findings"],
    }
    if review:
        investigation["ai_review"] = {
            "summary": str(review.get("summary", "")),
            "flags": [str(flag) for flag in review.get("flags", [])],
            "claim_checks": review.get("claim_checks", []),
            "skill_assessment": review.get("skill_assessment", []),
            "score_authority": "AI findings are advisory and do not modify deterministic scores.",
        }
    return {
        "filename": draft["filename"],
        "name": fields.get("name", "Candidate"),
        "email": fields.get("email", ""),
        "phone": fields.get("phone", ""),
        "university": fields.get("university", ""),
        "degree": fields.get("degree", ""),
        "cgpa": fields.get("cgpa"),
        "roll_number": fields.get("roll_number", ""),
        "grad_year": fields.get("grad_year"),
        "experience_years": fields.get("experience_years"),
        "role_key": role_key,
        "role_label": role["label"],
        "role_kind": role["kind"],
        "links": links,
        "professional_profile": professional_profile,
        "competitive_programming": competitive_programming,
        "technical_rating": technical_rating,
        "scores": {"ats": ats, "trust": trust, "fit": fit},
        "evidence_score": evidence_score,
        "ats_breakdown": ats_breakdown,
        "skills": skills,
        "relevant_skills": relevant_skills,
        "certificates": [
            line.strip()
            for line in text.splitlines()
            if re.search(r"\b(certification|certificate|certified|license)\b", line, re.I)
        ][:20],
        "github": github,
        "flags": flags,
        "documents": document_results,
        "doc_status": doc_status,
        "verdict": "Verified" if trust >= 70 and fit >= 50 else "Needs Review" if trust >= 40 else "High Risk",
        "summary": f"{fields.get('name', 'Candidate')} evaluated for {role['label']}. {len(flags)} flag(s) raised.",
        "investigation": investigation,
        "text_preview": text[:800],
        "analysed_at": datetime.now(timezone.utc).isoformat(),
    }


def extract_document_text(filename: str, file_bytes: bytes) -> tuple[str, str]:
    """Extract text from a supporting PDF/image, using bounded OCR as a fallback."""
    lowered = filename.lower()
    if lowered.endswith(".pdf"):
        text = extract_pdf_text(file_bytes)
        if len(text) >= 30:
            return text[:MAX_RESUME_CHARS], ""
        try:
            import pdfplumber
            import forensics

            collected = []
            note = ""
            with pdfplumber.open(io.BytesIO(file_bytes)) as document:
                for page in document.pages[:2]:
                    image = page.to_image(resolution=150).original
                    image.thumbnail((2400, 2400))
                    buffer = io.BytesIO()
                    image.save(buffer, format="PNG")
                    page_text, page_note = forensics.ocr_image_bytes(
                        buffer.getvalue(), TESSERACT_CMD
                    )
                    collected.append(page_text)
                    note = note or page_note
            return "\n".join(collected)[:MAX_RESUME_CHARS], note
        except Exception as error:
            logger.warning("Scanned PDF OCR failed for %s: %s", filename, error)
            return "", f"Could not OCR the scanned PDF ({str(error)[:120]})."
    try:
        import forensics

        text, note = forensics.ocr_image_bytes(file_bytes, TESSERACT_CMD)
        return text[:MAX_RESUME_CHARS], note
    except Exception as error:
        logger.exception("Document OCR failed for %s", filename)
        return "", f"Could not read the image document ({str(error)[:120]})."


def _normalise_identity(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def verify_document(document: dict, fields: dict) -> dict:
    """Cross-check document text and image metadata without claiming issuer validation."""
    filename = str(document.get("filename", ""))
    text = str(document.get("text", ""))
    lowered = text.lower()
    checks = []
    details = parse_resume_details(text, filename)
    resume_name = str(fields.get("name") or "")
    document_name = details.get("name", "")
    if resume_name and resume_name.lower() != "candidate":
        name_tokens = re.findall(r"[a-z0-9]+", resume_name.lower())
        name_matches = bool(name_tokens) and all(token in lowered for token in name_tokens)
        checks.append(
            {
                "field": "Student name",
                "resume": resume_name,
                "document": document_name or "-",
                "result": "match" if name_matches else "mismatch" if document_name else "not_found",
            }
        )

    resume_roll = str(fields.get("roll_number") or "")
    document_roll = str(details.get("roll_number") or "")
    if resume_roll:
        roll_matches = (
            bool(document_roll)
            and _normalise_identity(resume_roll) == _normalise_identity(document_roll)
        )
        checks.append(
            {
                "field": "Roll number",
                "resume": resume_roll,
                "document": document_roll or "-",
                "result": "match" if roll_matches else "mismatch" if document_roll else "not_found",
            }
        )

    resume_cgpa = fields.get("cgpa")
    document_cgpa = details.get("cgpa")
    if resume_cgpa is not None:
        if document_cgpa is None:
            cgpa_result = "not_found"
        else:
            cgpa_result = "match" if abs(float(resume_cgpa) - float(document_cgpa)) <= 0.05 else "mismatch"
        checks.append(
            {
                "field": "CGPA",
                "resume": str(resume_cgpa),
                "document": str(document_cgpa) if document_cgpa is not None else "-",
                "result": cgpa_result,
            }
        )

    resume_university = str(fields.get("university") or "")
    if resume_university and resume_university.lower() != "university / institution":
        uni_matches = _normalise_identity(resume_university) in _normalise_identity(text)
        checks.append(
            {
                "field": "University",
                "resume": resume_university,
                "document": details.get("university") or "-",
                "result": "match" if uni_matches else "mismatch" if details.get("university") else "not_found",
            }
        )

    has_document_text = len(text.strip()) >= 20
    results = [check["result"] for check in checks]
    if not has_document_text:
        status = "unreadable"
    elif "mismatch" in results:
        status = "mismatch"
    elif results.count("match") >= 2:
        status = "verified"
    else:
        status = "partial"

    markers = [
        bool(re.search(pattern, lowered))
        for pattern in (
            r"university|institute|board|college",
            r"marksheet|mark sheet|certificate|transcript|grade card|identity card|id card",
            r"roll|enrol|registration|seat no",
            r"cgpa|sgpa|gpa|percentage|marks|grade|result",
            r"\bname\b",
        )
    ]
    marker_count = sum(markers)
    simulated_status = "sim_pass" if marker_count >= 4 else "sim_review" if marker_count >= 2 else "sim_fail"
    digilocker = {
        "status": simulated_status,
        "reference_id": "SIM-" + hashlib.sha256(
            document.get("bytes", b"")
        ).hexdigest()[:12].upper(),
        "disclaimer": (
            "Simulation only; this does not contact DigiLocker or prove document authenticity."
        ),
    }

    forensic_report = None
    if filename.lower().endswith((".png", ".jpg", ".jpeg")):
        try:
            import forensics

            forensic_report = forensics.analyze_image_forensics(
                filename, document.get("bytes", b"")
            )
        except Exception:
            logger.exception("Image forensics failed for %s", filename)
    return {
        "filename": filename,
        "status": status,
        "checks": checks,
        "ocr_note": document.get("note", ""),
        "digilocker": digilocker,
        "text_length": len(text),
        "forensics": forensic_report,
    }


def _document_status(document_results: list[dict]) -> str:
    statuses = [document.get("status") for document in document_results]
    for status in ("mismatch", "unreadable", "partial"):
        if status in statuses:
            return status
    return "verified" if statuses else "none"


def _document_flags(document_results: list[dict]) -> list[dict]:
    flags = []
    for document in document_results:
        for check in document.get("checks", []):
            if check.get("result") == "mismatch":
                flags.append(
                    make_flag(
                        "high",
                        f"{check['field']} mismatch in {document['filename']}: "
                        f"resume says '{check['resume']}', document says '{check['document']}'.",
                    )
                )
        if document.get("status") == "unreadable":
            flags.append(
                make_flag(
                    "low",
                    f"Document {document['filename']} could not be read. {document.get('ocr_note', '')}",
                )
            )
        forensic = document.get("forensics") or {}
        flags.extend(forensic.get("flags", []))
    return flags


def match_documents_to_resumes(drafts: list, documents: list) -> tuple[dict, list]:
    """Assign each supporting document to the best identity match, if any."""
    matches = {}
    unmatched = []
    for document in documents:
        text = str(document.get("text", ""))
        normalized_document = _normalise_identity(text)
        filename_key = _normalise_identity(Path(document.get("filename", "")).stem)
        best_index = None
        best_score = 0
        for index, draft in enumerate(drafts):
            fields = draft.get("fields", {})
            score = 0
            roll = _normalise_identity(str(fields.get("roll_number") or ""))
            if roll and roll in normalized_document:
                score += 3
            name_tokens = re.findall(r"[a-z0-9]+", str(fields.get("name", "")).lower())
            if len(name_tokens) >= 2 and all(token in text.lower() for token in name_tokens):
                score += 2
            resume_key = _normalise_identity(Path(draft.get("filename", "")).stem)
            if len(resume_key) >= 3 and resume_key in filename_key:
                score += 1
            if score > best_score:
                best_index, best_score = index, score
        if best_index is None:
            unmatched.append(document.get("filename", ""))
        else:
            matches.setdefault(best_index, []).append(
                verify_document(document, drafts[best_index]["fields"])
            )
    return matches, unmatched

# ----------------------------------------------------------------------------
# 1. TEXT EXTRACTION & DEMOGRAPHIC PARSING
# ----------------------------------------------------------------------------

def extract_text_from_file(filename: str, file_bytes: bytes) -> str:
    """Extract plain text from PDF, TXT, or Markdown binary streams."""
    fname = filename.lower()
    if fname.endswith(".pdf"):
        try:
            import pypdf

            reader = pypdf.PdfReader(io.BytesIO(file_bytes))
            text = "\n".join(
                page.extract_text() or "" for page in reader.pages[:10]
            )
            if text.strip():
                return text
        except Exception:
            logger.debug("PDF text extraction with pypdf failed", exc_info=True)

        try:
            import pdfplumber

            with pdfplumber.open(io.BytesIO(file_bytes)) as document:
                text = "\n".join(
                    page.extract_text() or "" for page in document.pages[:10]
                )
                if text.strip():
                    return text
        except Exception:
                logger.debug("PDF text extraction with pdfplumber failed", exc_info=True)
        return ""

    return file_bytes.decode("utf-8", errors="ignore")


def parse_resume_details(text: str, _filename: str = "") -> dict:
    """Extract candidate info, contact details, academics, and social handles."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]

    # Extract Candidate Name
    name = "Candidate"
    if lines:
        for line in lines[:5]:
            if not re.search(r"resume|curriculum|vitae|page|statement|marksheet", line, re.I):
                if len(line.split()) <= 4 and re.match(r"^[A-Za-z\s\.\'-]+$", line):
                    name = line
                    break
    if name == "Candidate" and _filename:
        filename_name = re.sub(
            r"\b(resume|cv|curriculum|vitae|candidate)\b",
            "",
            Path(_filename).stem.replace("_", " ").replace("-", " "),
            flags=re.I,
        ).strip()
        if filename_name and len(filename_name.split()) <= 4:
            name = filename_name.title()

    # Email
    email_match = re.search(r"[\w\.-]+@[\w\.-]+\.\w+", text)
    email = email_match.group(0) if email_match else ""

    # Phone
    phone_match = re.search(r"(\+?\d{1,3}[\s-]?)?\(?\d{3}\)?[\s-]?\d{3}[\s-]?\d{4}", text)
    phone = phone_match.group(0) if phone_match else ""

    # University
    uni_match = re.search(r"([A-Za-z\s]+(?:University|Institute|College|Academy|IIT|NIT|BITS)[A-Za-z\s]*)", text, re.I)
    university = uni_match.group(1).strip() if uni_match else "University / Institution"

    # CGPA / Percentage
    cgpa_match = re.search(r"(?:cgpa|gpa)[\s:]*(\d\.\d{1,2}|\d{1,2}\.?\d?)\s*(?:\/\s*10)?", text, re.I)
    cgpa = None
    if cgpa_match:
        try:
            val = float(cgpa_match.group(1))
            cgpa = val
        except ValueError:
            pass

    degree_match = re.search(
        r"\b(?:B\.?\s?Tech|B\.?\s?E\.?|M\.?\s?Tech|M\.?\s?E\.?|"
        r"Bachelor(?:'s)?(?:\s+of\s+[A-Za-z]+){0,3}|"
        r"Master(?:'s)?(?:\s+of\s+[A-Za-z]+){0,3}|"
        r"M\.?\s?S\.?|Ph\.?\s?D\.?)\b",
        text,
        re.I,
    )
    degree = degree_match.group(0).strip() if degree_match else ""

    experience_match = re.search(
        r"\b(\d{1,2})\+?\s*(?:years?|yrs?)\s*(?:of\s+)?"
        r"(?:professional\s+)?experience\b",
        text,
        re.I,
    )
    experience_years = int(experience_match.group(1)) if experience_match else None

    # Keep labels bounded so words like "Engineering" cannot be mistaken for "id".
    roll_match = re.search(
        r"\b(?:roll(?:\s*(?:no\.?|number))?|"
        r"registration(?:\s*(?:no\.?|number))?|"
        r"reg(?:istration)?\s*(?:no\.?|number)|"
        r"student\s*id)\b\s*[:#-]?\s*([A-Za-z0-9][A-Za-z0-9/-]*)",
        text,
        re.I,
    )
    roll_number = roll_match.group(1) if roll_match else ""

    # Graduation Year
    grad_match = re.search(r"\b(202[3-9]|2030)\b", text)
    grad_year = int(grad_match.group(1)) if grad_match else None

    # GitHub Usernames & Repo URLs
    github_profiles = list(set(re.findall(r"github\.com/([a-zA-Z0-9-]+)/?", text, re.I)))
    reserved = {"sponsors", "about", "features", "pricing", "login", "signup", "explore", "topics", "trending", "orgs"}
    github_profiles = [g for g in github_profiles if g.lower() not in reserved]

    github_repo_urls = re.findall(r"https?://github\.com/[a-zA-Z0-9-]+/[a-zA-Z0-9_\.-]+", text, re.I)

    # LinkedIn Profile URLs
    linkedin = list(set(re.findall(r"https?://(?:www\.)?linkedin\.com/in/[a-zA-Z0-9_-]+", text, re.I)))

    return {
        "name": name,
        "email": email,
        "phone": phone,
        "university": university,
        "degree": degree,
        "cgpa": cgpa,
        "roll_number": roll_number,
        "grad_year": grad_year,
        "experience_years": experience_years,
        "github_usernames": sorted(set(github_profiles), key=str.lower),
        "github_repo_urls": sorted(set(github_repo_urls), key=str.lower),
        "linkedin": sorted(set(linkedin), key=str.lower)
    }


def parse_repo_url(url: str):
    """Parse 'https://github.com/owner/repo' into ('owner', 'repo')."""
    match = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", url.strip())
    if not match:
        return None
    owner, repo = match.group(1), match.group(2)
    if repo.endswith(".git"):
        repo = repo[:-4]
    return owner, repo

# ----------------------------------------------------------------------------
# 2. DEEP ASYNC GITHUB AUDITING ENGINE
# ----------------------------------------------------------------------------

async def _legacy_fetch_github_audit_async(usernames: list[str], explicit_repo_urls: list[str] = None) -> dict:
    """
    Deep async investigation of GitHub profiles, repositories, languages, commit depth,
    and recent activity. Fully asynchronous with rate limit safety.
    """
    if explicit_repo_urls is None:
        explicit_repo_urls = []

    if not usernames and not explicit_repo_urls:
        return {
            "checked": False, "profiles": [], "repos": [], "languages": {},
            "errors": [], "total_repos": 0, "original_repos": 0, "fork_repos": 0,
            "recent_repos": 0, "total_stars": 0
        }

    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = "Bearer " + GITHUB_TOKEN

    profiles = []
    all_repos = []
    languages_byte_count = {}
    errors = []
    total_stars = 0
    original_repos = 0
    fork_repos = 0
    recent_repos = 0

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, headers=headers) as client:
        # 1. Audit User Profiles
        for username in usernames:
            try:
                res = await client.get(f"{GITHUB_API}/users/{username}")
                if res.status_code == 404:
                    errors.append(f"GitHub profile {username}: not found (or private)")
                    continue
                elif res.status_code == 403:
                    errors.append("GitHub rate limit hit. Configure GITHUB_TOKEN in environment.")
                    break
                res.raise_for_status()
                data = res.json()

                created_at = data.get("created_at", "")
                age_days = 0
                if created_at:
                    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    age_days = (datetime.now(timezone.utc) - dt).days

                profiles.append({
                    "username": username,
                    "url": f"https://github.com/{username}",
                    "public_repos": data.get("public_repos", 0),
                    "followers": data.get("followers", 0),
                    "account_age_days": age_days
                })

                # Fetch User's Public Repositories (up to 30 sorted by push date)
                r_res = await client.get(f"{GITHUB_API}/users/{username}/repos?per_page=30&sort=pushed")
                if r_res.status_code == 200:
                    repos_data = r_res.json()
                    for r in repos_data:
                        is_fork = r.get("fork", False)
                        stars = r.get("stargazers_count", 0)
                        lang = r.get("language") or ""
                        pushed_at = r.get("pushed_at", "")

                        total_stars += stars
                        if is_fork:
                            fork_repos += 1
                        else:
                            original_repos += 1

                        if lang:
                            languages_byte_count[lang] = languages_byte_count.get(lang, 0) + 1

                        if pushed_at:
                            p_dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
                            if (datetime.now(timezone.utc) - p_dt).days <= 365:
                                recent_repos += 1

                        all_repos.append({
                            "name": r.get("full_name"),
                            "url": r.get("html_url"),
                            "language": lang,
                            "stars": stars,
                            "is_fork": is_fork,
                            "pushed_at": pushed_at,
                            "description": r.get("description") or "",
                            "topics": r.get("topics", [])
                        })
            except Exception as exc:
                errors.append(f"GitHub profile {username} audit error: {str(exc)}")

        # 2. Audit Explicit Repo Links
        for repo_url in explicit_repo_urls:
            parsed = parse_repo_url(repo_url)
            if not parsed:
                continue
            owner, repo_name = parsed
            if any(r["url"].lower() == repo_url.lower() for r in all_repos):
                continue

            try:
                res = await client.get(f"{GITHUB_API}/repos/{owner}/{repo_name}")
                if res.status_code == 200:
                    r = res.json()
                    is_fork = r.get("fork", False)
                    stars = r.get("stargazers_count", 0)
                    lang = r.get("language") or ""
                    pushed_at = r.get("pushed_at", "")

                    total_stars += stars
                    if is_fork:
                        fork_repos += 1
                    else:
                        original_repos += 1

                    if lang:
                        languages_byte_count[lang] = languages_byte_count.get(lang, 0) + 1

                    if pushed_at:
                        p_dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
                        if (datetime.now(timezone.utc) - p_dt).days <= 365:
                            recent_repos += 1

                    all_repos.append({
                        "name": r.get("full_name"),
                        "url": r.get("html_url"),
                        "language": lang,
                        "stars": stars,
                        "is_fork": is_fork,
                        "pushed_at": pushed_at,
                        "description": r.get("description") or "",
                        "topics": r.get("topics", [])
                    })
                else:
                    errors.append(f"Repository link unreachable: {repo_url}")
            except Exception as exc:
                errors.append(f"Repository link error ({repo_url}): {str(exc)}")

    return {
        "checked": True,
        "profiles": profiles,
        "repos": all_repos,
        "languages": languages_byte_count,
        "errors": errors,
        "total_repos": len(all_repos),
        "original_repos": original_repos,
        "fork_repos": fork_repos,
        "recent_repos": recent_repos,
        "total_stars": total_stars
    }


async def fetch_github_audit_async(
    usernames: list[str], explicit_repo_urls: list[str] | None = None
) -> dict:
    """Keep the original helper API while routing requests through the bounded collector."""
    profiles = [
        {"username": username, "url": f"https://github.com/{username}"}
        for username in (usernames or [])
    ]
    repositories = []
    for url in explicit_repo_urls or []:
        parsed = parse_repo_url(url)
        if parsed:
            owner, repo = parsed
            repositories.append({"owner": owner, "repo": repo})
    return await analyze_github(
        {"github_profiles": profiles, "github_repos": repositories}
    )


# ----------------------------------------------------------------------------
# 3. SKILL MATRIX & FRAUD/INFLATION AUDIT ENGINE
# ----------------------------------------------------------------------------

def verify_skills_matrix(resume_text: str, github_audit: dict, target_keywords: list) -> list:
    """
    Cross-reference resume claims against GitHub repos, descriptions, primary languages,
    and topics to classify each skill into 'verified', 'partial', or 'unverified'.
    """
    resume_lower = resume_text.lower()
    repos = github_audit.get("repos", [])

    evidence_blob = " ".join(
        [r["name"] + " " + (r["description"] or "") + " " + " ".join(r["topics"]) + " " + (r["language"] or "") for r in repos]
    ).lower()

    languages_dict = {k.lower(): v for k, v in github_audit.get("languages", {}).items()}

    skills_matrix = []
    for kw in target_keywords:
        kw_lower = kw.lower()
        if re.search(r"\b" + re.escape(kw_lower) + r"\b", resume_lower):
            if kw_lower in evidence_blob or kw_lower in languages_dict:
                matched_repo = next(
                    (r["name"] for r in repos if kw_lower in (r["name"] + " " + (r["description"] or "") + " " + " ".join(r["topics"]) + " " + (r["language"] or "")).lower()),
                    "GitHub repositories"
                )
                skills_matrix.append({
                    "skill": kw,
                    "status": "verified",
                    "evidence": f"Seen in GitHub repo {matched_repo}"
                })
            elif github_audit.get("original_repos", 0) > 0:
                skills_matrix.append({
                    "skill": kw,
                    "status": "partial",
                    "evidence": "Candidate has original repos, but none mention this skill directly"
                })
            else:
                skills_matrix.append({
                    "skill": kw,
                    "status": "unverified",
                    "evidence": "No GitHub code evidence available"
                })

    return skills_matrix


def evaluate_fraud_and_flags(resume_text: str, github_audit: dict, role_kind: str, demo_details: dict) -> tuple[list, int]:
    """
    Rule-based fraud, discrepancy, and inflation detector.
    Generates severity flags (high, medium, low) and computes evidence score.
    """
    flags = []
    text_lower = resume_text.lower()

    # 1. GitHub requirement check for tech roles
    if role_kind == "tech" and not github_audit.get("profiles") and not github_audit.get("repos"):
        flags.append({
            "severity": "medium",
            "text": "No GitHub profile or repository link found, but the target role is technical.",
            "source": "rule",
            "kind": "trust"
        })

    # 2. Account Age & 100% Fork Checks
    for profile in github_audit.get("profiles", []):
        if profile["account_age_days"] < 90:
            flags.append({
                "severity": "high",
                "text": f"GitHub account @{profile['username']} is brand new (< 90 days old).",
                "source": "rule",
                "kind": "trust"
            })

    if github_audit.get("total_repos", 0) > 0 and github_audit.get("original_repos", 0) == 0:
        flags.append({
            "severity": "high",
            "text": "100% of candidate's GitHub repositories are forks with zero original projects.",
            "source": "rule",
            "kind": "trust"
        })

    # 3. Inflated Metrics & Claims Checks
    metric_matches = re.findall(r"(\d+(?:,\d+)*(?:\+)?)[\s+]*(users|customers|requests|followers|downloads|stars|percent|%)", text_lower)
    for amount, unit in metric_matches:
        num = int(amount.replace(",", "").replace("+", ""))
        if num >= 1000 and github_audit.get("original_repos", 0) == 0:
            flags.append({
                "severity": "medium",
                "text": f"Claims an impact of {amount} {unit}, but GitHub shows only 0 original repositories.",
                "source": "rule",
                "kind": "trust"
            })
            break

    # Exaggerated experience vs student timeline
    exp_years = re.search(r"(\d+)\+?\s*years?\s*of\s*experience", text_lower)
    if exp_years:
        years = int(exp_years.group(1))
        if years >= 5 and demo_details.get("grad_year") and demo_details["grad_year"] >= 2025:
            flags.append({
                "severity": "medium",
                "text": f"Claims {years}+ years of experience while currently a student graduating in {demo_details['grad_year']}.",
                "source": "rule",
                "kind": "trust"
            })

    # 4. Unreachable GitHub Links / Errors
    for err in github_audit.get("errors", []):
        flags.append({
            "severity": "low",
            "text": f"Could not check a GitHub link: {err}",
            "source": "rule",
            "kind": "trust"
        })

    # Compute Evidence Score
    orig = github_audit.get("original_repos", 0)
    recent = github_audit.get("recent_repos", 0)
    stars = github_audit.get("total_stars", 0)

    if not github_audit.get("checked"):
        evidence_score = 15
    else:
        base = 25
        base += min(45, orig * 15)
        base += min(15, recent * 5)
        base += min(15, stars * 3)
        evidence_score = min(100, max(15, base))

    return flags, evidence_score

# ----------------------------------------------------------------------------
# 4. PRIMARY PIPELINE ENTRY POINTS
# ----------------------------------------------------------------------------

async def analyze_single_candidate(
    filename: str, file_bytes: bytes, role_key: str = "sde"
) -> dict:
    if role_key not in ROLE_PROFILES:
        raise ValueError(f"Unknown role: {role_key}")
    draft = await asyncio.to_thread(read_resume, filename, file_bytes)
    return await score_candidate(draft, role_key, [])


async def analyze_batch_async(
    resumes: list[tuple[str, bytes]],
    documents: list[tuple[str, bytes]],
    role_key: str,
    progress=None
) -> tuple[list, list, list]:
    """Run bounded PDF extraction and candidate scoring concurrently."""
    if role_key not in ROLE_PROFILES:
        raise ValueError(f"Unknown role: {role_key}")
    reports = []
    errors = []
    unmatched = [filename for filename, _ in documents]
    total = len(resumes)
    extraction_slots = asyncio.Semaphore(16)
    github_slots = asyncio.Semaphore(MAX_GITHUB_CONCURRENCY)
    completed_reads = 0
    progress_lock = asyncio.Lock()

    async def read_one(filename: str, content: bytes):
        nonlocal completed_reads
        try:
            async with extraction_slots:
                draft = await asyncio.to_thread(read_resume, filename, content)
            return draft
        except Exception as error:
            return error
        finally:
            async with progress_lock:
                completed_reads += 1
                if progress:
                    progress("reading_resumes", completed_reads, total)

    drafts = await asyncio.gather(
        *(read_one(filename, content) for filename, content in resumes)
    )
    valid_drafts = []
    for (filename, _), draft in zip(resumes, drafts):
        if isinstance(draft, Exception):
            errors.append({"filename": filename, "error": str(draft)})
        else:
            valid_drafts.append(draft)

    completed_documents = 0

    async def read_document_one(filename: str, content: bytes):
        nonlocal completed_documents
        try:
            async with extraction_slots:
                text, note = await asyncio.to_thread(
                    extract_document_text, filename, content
                )
            return {
                "filename": filename,
                "bytes": content,
                "text": text,
                "note": note,
            }
        except Exception as error:
            errors.append({"filename": filename, "error": str(error)})
            return {
                "filename": filename,
                "bytes": content,
                "text": "",
                "note": str(error),
            }
        finally:
            async with progress_lock:
                completed_documents += 1
                if progress:
                    progress("reading_documents", completed_documents, len(documents))

    document_items = await asyncio.gather(
        *(read_document_one(filename, content) for filename, content in documents)
    )
    document_matches, unmatched = match_documents_to_resumes(valid_drafts, document_items)

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as github_client:
        async def score_one(index: int, draft: dict):
            try:
                results = score_candidate(
                    draft,
                    role_key,
                    document_matches.get(index, []),
                    _github_semaphore=github_slots,
                    _github_client=github_client,
                )
                if inspect.isawaitable(results):
                    return await results
                return results
            except Exception as error:
                return error

        scored = await asyncio.gather(
            *(score_one(index, draft) for index, draft in enumerate(valid_drafts))
        )

    if progress:
        progress("scoring", len(valid_drafts), len(valid_drafts))
    for draft, result in zip(valid_drafts, scored):
        if isinstance(result, Exception):
            errors.append({"filename": draft["filename"], "error": str(result)})
        else:
            reports.append(result)
    return reports, errors, unmatched


def analyze_batch(
    resumes: list[tuple[str, bytes]],
    documents: list[tuple[str, bytes]],
    role_key: str,
    progress=None,
) -> tuple[list, list, list]:
    """Synchronous adapter used by the existing queue worker."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(analyze_batch_async(resumes, documents, role_key, progress))
    raise RuntimeError("analyze_batch cannot run inside an event loop; await analyze_batch_async instead.")