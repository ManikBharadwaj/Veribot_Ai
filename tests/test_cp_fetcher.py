import asyncio

import agent
import cp_fetcher


class _Response:
    def __init__(self, payload=None, text=""):
        self.payload = payload or {}
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_extract_profile_usernames_only_accepts_supported_profile_urls():
    profiles = cp_fetcher.extract_profile_usernames(
        "https://leetcode.com/u/ada_dev/ "
        "https://www.codechef.com/users/ada_dev "
        "https://www.hackerrank.com/profile/ada_dev "
        "https://leetcode.com/problems/two-sum/"
    )

    assert profiles == {
        "leetcode": ["ada_dev"],
        "codechef": ["ada_dev"],
        "hackerrank": ["ada_dev"],
    }


def test_timeout_setting_stays_within_the_required_range(monkeypatch):
    monkeypatch.setenv("CP_SCRAPE_TIMEOUT", "3")
    assert cp_fetcher._timeout_seconds() == 10
    monkeypatch.setenv("CP_SCRAPE_TIMEOUT", "20")
    assert cp_fetcher._timeout_seconds() == 12


def test_resume_hyperlinks_are_available_to_profile_extractors():
    links = agent._extract_links(
        "Ada Example\n",
        ["https://leetcode.com/u/ada_dev/"],
    )

    assert links["competitive_programming_profiles"]["leetcode"] == ["ada_dev"]


def test_calendar_summary_computes_activity_and_streaks():
    from datetime import datetime, timedelta, timezone

    today = datetime.now(timezone.utc).date()
    timestamps = {}
    for offset in (0, 1, 2, 5):
        day = today - timedelta(days=offset)
        timestamp = int(
            datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()
        )
        timestamps[str(timestamp)] = 2

    streak, heatmap = cp_fetcher._calendar_summary(timestamps)

    assert streak == {"current": 3, "maximum": 3}
    assert heatmap["active_days"] == 4
    assert heatmap["submissions"] == 8
    assert heatmap["active_days_last_30"] == 4


def test_leetcode_fetch_extracts_public_profile_metrics():
    class _Client:
        async def post(self, _url, **kwargs):
            query = kwargs["json"]["query"]
            if "contestProfile" in query:
                return _Response(
                    {
                        "data": {
                            "userContestRanking": {
                                "attendedContestsCount": 12,
                                "rating": 1840.5,
                                "globalRanking": 5000,
                                "topPercentage": 8.2,
                                "badge": {"name": "Knight"},
                            }
                        }
                    }
                )
            return _Response(
                {
                    "data": {
                        "matchedUser": {
                            "profile": {"ranking": 4000, "reputation": 25},
                            "submitStats": {
                                "acSubmissionNum": [
                                    {"difficulty": "Easy", "count": 20},
                                    {"difficulty": "Medium", "count": 30},
                                    {"difficulty": "Hard", "count": 10},
                                ]
                            },
                            "submissionCalendar": "{}",
                        }
                    }
                }
            )

    report = asyncio.run(cp_fetcher.fetch_leetcode("ada_dev", _Client()))

    assert report["status"] == "available"
    assert report["global_ranking"] == 4000
    assert report["reputation"] == 25
    assert report["solved"] == {
        "easy": 20,
        "medium": 30,
        "hard": 10,
        "total": 60,
    }
    assert report["contest"]["rating"] == 1840.5
    assert report["contest"]["badges"] == ["Knight"]


def test_profile_fetch_failure_returns_fallback_without_raising():
    class _Client:
        async def get(self, *_args, **_kwargs):
            raise TimeoutError("network timeout")

    report = asyncio.run(cp_fetcher.fetch_codechef("ada_dev", _Client()))

    assert report["status"] == "unavailable"
    assert report["username"] == "ada_dev"
    assert report["global_rank"] is None
    assert report["errors"]


def test_codechef_html_metrics_are_parsed():
    class _Client:
        async def get(self, *_args, **_kwargs):
            return _Response(
                text=(
                    "<p>Global Rank: 1,234</p><p>Country Rank: 321</p>"
                    "<p>Current Rating: 1850</p><p>Highest Rating: 1920</p>"
                    "<p>5 star Division 2</p><p>Problems Solved: 45</p>"
                    '<a href="/contests/starters-100">CodeChef Starters 100</a>'
                    '<a href="/contests/long-2024">Long Challenge 2024</a>'
                )
            )

    report = asyncio.run(cp_fetcher.fetch_codechef("ada_dev", _Client()))

    assert report["global_rank"] == 1234
    assert report["country_rank"] == 321
    assert report["current_rating"] == 1850
    assert report["highest_rating"] == 1920
    assert report["star_rating"] == 5
    assert report["division"] == "Div 2"
    assert report["problems_solved"] == 45
    assert report["contest_participation"]["starters"] == 1
    assert report["contest_participation"]["long_challenges"] == 1


def test_hackerrank_profile_and_domain_badges_are_parsed():
    class _Client:
        async def get(self, url, **_kwargs):
            if url.endswith("/badges"):
                return _Response(
                    {
                        "data": [
                            {"badge_name": "Problem Solving", "stars": 4},
                            {"badge_name": "Python", "stars": 3},
                            {"badge_name": "SQL", "stars": 2},
                            {"badge_name": "C++", "stars": 5},
                            {"badge_name": "Java", "stars": 1},
                            {"badge_name": "JavaScript", "stars": 5},
                        ]
                    }
                )
            return _Response(
                {
                    "data": {
                        "level": 6,
                        "followers": 21,
                        "total_submissions": 0,
                    }
                }
            )

    report = asyncio.run(cp_fetcher.fetch_hackerrank("ada_dev", _Client()))

    assert report["level"] == 6
    assert report["followers"] == 21
    assert report["total_submissions"] == 0
    assert report["badges_count"] == 6
    assert report["domain_badges"]["problem_solving"]["stars"] == 4
    assert report["domain_badges"]["cpp"]["stars"] == 5
    assert report["domain_badges"]["java"]["stars"] == 1


def test_score_candidate_runs_scrapers_only_for_technical_roles(monkeypatch):
    calls = {"github": 0, "cp": 0}

    async def fake_github(*_args, **_kwargs):
        calls["github"] += 1
        return agent._empty_github_summary(checked=True)

    async def fake_cp(*_args, **_kwargs):
        calls["cp"] += 1
        return cp_fetcher.empty_competitive_profiles()

    async def no_llm(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent, "analyze_github", fake_github)
    monkeypatch.setattr(cp_fetcher, "fetch_competitive_profiles", fake_cp)
    monkeypatch.setattr(agent, "llm_review", no_llm)
    draft = {
        "filename": "candidate.pdf",
        "text": "Alex Example\nPython engineer with 5 years of experience.",
        "fields": {
            "name": "Alex Example",
            "email": "alex@example.com",
            "experience_years": 5,
            "university": "Example University",
            "degree": "B.Tech",
            "grad_year": 2020,
        },
        "links": {
            "github_profiles": [{"username": "alex", "url": "https://github.com/alex"}],
            "github_repos": [],
            "linkedin": [],
        },
    }

    technical = asyncio.run(agent.score_candidate(draft, "sde", []))
    assert technical["competitive_programming"] is not None
    assert technical["technical_rating"]["score"] == 25
    assert "github_public_evidence" in technical["technical_rating"]["components"]
    assert calls == {"github": 1, "cp": 1}

    management = asyncio.run(agent.score_candidate(draft, "hr", []))
    assert management["competitive_programming"] is None
    assert management["technical_rating"] is None
    assert management["professional_profile"]["linkedin"]["scraped"] is False
    assert management["github"]["checked"] is False
    assert calls == {"github": 1, "cp": 1}
