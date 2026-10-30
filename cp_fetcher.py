"""Bounded public-profile fetchers for competitive programming platforms."""

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import quote

import httpx

logger = logging.getLogger("veribot.cp_fetcher")

_USERNAME = re.compile(r"^[A-Za-z0-9_-]{1,39}$")
_RESERVED_LEETCODE = {
    "accounts", "articles", "company", "contest", "discuss", "explore",
    "graphql", "list", "problems", "solution", "tag",
}
def _timeout_seconds() -> float:
    try:
        configured = float(os.getenv("CP_SCRAPE_TIMEOUT", "11"))
    except (TypeError, ValueError):
        configured = 11.0
    return max(10.0, min(12.0, configured))


def _user_agent() -> str:
    return os.getenv("USER_AGENT_HEADER", "VeriBotAI/1.0 (public profile verification)")


def _valid_username(username: str) -> str:
    username = username.strip()
    return username if _USERNAME.fullmatch(username) else ""


def extract_profile_usernames(text: str) -> dict:
    """Extract only usernames present in recognized public profile URLs."""
    profiles = {"leetcode": [], "codechef": [], "hackerrank": []}
    patterns = {
        "leetcode": re.compile(
            r"(?:https?://)?(?:www\.)?leetcode\.com/(?:u/)?([A-Za-z0-9_-]+)",
            re.I,
        ),
        "codechef": re.compile(
            r"(?:https?://)?(?:www\.)?codechef\.com/users/([A-Za-z0-9_-]+)",
            re.I,
        ),
        "hackerrank": re.compile(
            r"(?:https?://)?(?:www\.)?hackerrank\.com/profile/([A-Za-z0-9_-]+)",
            re.I,
        ),
    }
    for platform, pattern in patterns.items():
        for match in pattern.finditer(text or ""):
            username = _valid_username(match.group(1))
            if (
                username
                and username.casefold() not in _RESERVED_LEETCODE
                and username.casefold() not in {item.casefold() for item in profiles[platform]}
            ):
                profiles[platform].append(username)
    return profiles


def _empty_leetcode(username: str = "") -> dict:
    return {
        "status": "not_provided" if not username else "unavailable",
        "username": username or None,
        "global_ranking": None,
        "reputation": None,
        "star_rating": None,
        "solved": {"easy": None, "medium": None, "hard": None, "total": None},
        "contest": {
            "rating": None,
            "global_percentile": None,
            "global_rank": None,
            "attended": None,
            "badges": [],
        },
        "streak": {"current": None, "maximum": None},
        "heatmap": {
            "active_days": None,
            "submissions": None,
            "active_days_last_30": None,
        },
        "errors": [],
    }


def _empty_codechef(username: str = "") -> dict:
    return {
        "status": "not_provided" if not username else "unavailable",
        "username": username or None,
        "global_rank": None,
        "country_rank": None,
        "star_rating": None,
        "current_rating": None,
        "highest_rating": None,
        "division": None,
        "problems_solved": None,
        "contest_participation": {
            "starters": 0,
            "cook_off": 0,
            "lunchtime": 0,
            "long_challenges": 0,
            "history": [],
        },
        "errors": [],
    }


def _empty_hackerrank(username: str = "") -> dict:
    return {
        "status": "not_provided" if not username else "unavailable",
        "username": username or None,
        "level": None,
        "followers": None,
        "total_submissions": None,
        "badges_count": None,
        "domain_badges": {
            domain: {"stars": 0, "badges": []}
            for domain in ("problem_solving", "python", "sql", "cpp", "java")
        },
        "errors": [],
    }


def empty_competitive_profiles() -> dict:
    return {
        "leetcode": _empty_leetcode(),
        "codechef": _empty_codechef(),
        "hackerrank": _empty_hackerrank(),
    }


async def _request(client, method: str, url: str, semaphore=None, **kwargs):
    headers = {"User-Agent": _user_agent(), **kwargs.pop("headers", {})}
    if semaphore is None:
        response = await getattr(client, method)(
            url, timeout=_timeout_seconds(), headers=headers, **kwargs
        )
    else:
        async with semaphore:
            response = await getattr(client, method)(
                url, timeout=_timeout_seconds(), headers=headers, **kwargs
            )
    response.raise_for_status()
    return response


def _calendar_summary(calendar_value) -> tuple[dict, dict]:
    if isinstance(calendar_value, str):
        try:
            calendar_value = json.loads(calendar_value)
        except (json.JSONDecodeError, TypeError):
            return {"current": None, "maximum": None}, {
                "active_days": None,
                "submissions": None,
                "active_days_last_30": None,
            }
    if not isinstance(calendar_value, dict):
        return {"current": None, "maximum": None}, {
            "active_days": None,
            "submissions": None,
            "active_days_last_30": None,
        }

    activity = {}
    for timestamp, count in calendar_value.items():
        try:
            activity[int(timestamp)] = int(count)
        except (TypeError, ValueError):
            continue
    active_dates = {
        datetime.fromtimestamp(timestamp, timezone.utc).date()
        for timestamp, count in activity.items()
        if count > 0
    }
    today = datetime.now(timezone.utc).date()
    current = 0
    day = today if today in active_dates else today - timedelta(days=1)
    while day in active_dates:
        current += 1
        day -= timedelta(days=1)
    maximum = run = 0
    previous = None
    for active_day in sorted(active_dates):
        run = run + 1 if previous and active_day == previous + timedelta(days=1) else 1
        maximum = max(maximum, run)
        previous = active_day
    recent_start = today - timedelta(days=29)
    recent_timestamps = [
        timestamp
        for timestamp, count in activity.items()
        if count > 0
        and datetime.fromtimestamp(timestamp, timezone.utc).date() >= recent_start
    ]
    return {"current": current, "maximum": maximum}, {
        "active_days": len(active_dates),
        "submissions": sum(activity.values()),
        "active_days_last_30": len(
            {
                datetime.fromtimestamp(timestamp, timezone.utc).date()
                for timestamp in recent_timestamps
            }
        ),
    }


async def fetch_leetcode(username: str, client, semaphore=None) -> dict:
    username = _valid_username(username)
    report = _empty_leetcode(username)
    if not username:
        return report
    profile_query = """
      query publicProfile($username: String!) {
        matchedUser(username: $username) {
          username
          profile { ranking reputation }
          submitStats { acSubmissionNum { difficulty count } }
          submissionCalendar
        }
      }
    """
    contest_query = """
      query contestProfile($username: String!) {
        userContestRanking(username: $username) {
          attendedContestsCount rating globalRanking topPercentage badge { name }
        }
      }
    """
    profile_result, contest_result = await asyncio.gather(
        _request(
            client,
            "post",
            "https://leetcode.com/graphql",
            semaphore,
            json={"query": profile_query, "variables": {"username": username}},
        ),
        _request(
            client,
            "post",
            "https://leetcode.com/graphql",
            semaphore,
            json={"query": contest_query, "variables": {"username": username}},
        ),
        return_exceptions=True,
    )
    if isinstance(profile_result, BaseException):
        logger.info("LeetCode profile fetch failed for %s: %s", username, profile_result)
        report["errors"].append(
            f"LeetCode profile could not be fetched: {type(profile_result).__name__}."
        )
    else:
        try:
            payload = profile_result.json()
            matched = (payload.get("data") or {}).get("matchedUser")
            if not isinstance(matched, dict):
                report["errors"].append("Public profile data was unavailable.")
            else:
                report["status"] = "available"
                profile = matched.get("profile")
                profile = profile if isinstance(profile, dict) else {}
                report["global_ranking"] = profile.get("ranking")
                report["reputation"] = profile.get("reputation")
                report["star_rating"] = profile.get("starRating")
                submit_stats = matched.get("submitStats")
                submit_stats = submit_stats if isinstance(submit_stats, dict) else {}
                solved = {
                    str(item.get("difficulty", "")).casefold(): item.get("count")
                    for item in submit_stats.get("acSubmissionNum", [])
                    if isinstance(item, dict)
                }
                easy = solved.get("easy")
                medium = solved.get("medium")
                hard = solved.get("hard")
                total = (
                    easy + medium + hard
                    if all(isinstance(value, int) for value in (easy, medium, hard))
                    else None
                )
                report["solved"] = {
                    "easy": easy,
                    "medium": medium,
                    "hard": hard,
                    "total": total,
                }
                report["streak"], report["heatmap"] = _calendar_summary(
                    matched.get("submissionCalendar")
                )
        except Exception as error:
            logger.info("LeetCode profile parsing failed for %s: %s", username, error)
            report["errors"].append(
                f"LeetCode profile data could not be parsed: {type(error).__name__}."
            )
    if isinstance(contest_result, BaseException):
        logger.info("LeetCode contest fetch failed for %s: %s", username, contest_result)
        report["errors"].append(
            f"LeetCode contest data could not be fetched: {type(contest_result).__name__}."
        )
    else:
        try:
            contest_payload = contest_result.json()
            contest = (contest_payload.get("data") or {}).get("userContestRanking")
            if isinstance(contest, dict):
                badge = contest.get("badge")
                badge = badge if isinstance(badge, dict) else {}
                badges = [badge["name"]] if badge.get("name") else []
                rating = contest.get("rating")
                if not badges and isinstance(rating, (int, float)):
                    if rating >= 2200:
                        badges.append("Guardian")
                    elif rating >= 1850:
                        badges.append("Knight")
                report["contest"].update(
                    {
                        "rating": rating,
                        "global_percentile": contest.get("topPercentage"),
                        "global_rank": contest.get("globalRanking"),
                        "attended": contest.get("attendedContestsCount"),
                        "badges": badges,
                    }
                )
            elif report["status"] == "available":
                report["errors"].append("Contest profile data was unavailable.")
        except Exception as error:
            logger.info("LeetCode contest parsing failed for %s: %s", username, error)
            report["errors"].append(
                f"LeetCode contest data could not be parsed: {type(error).__name__}."
            )
    return report


class _ProfileTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.class_text = {}
        self._elements = []
        self._anchors = []
        self.links = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = values.get("class", "").split()
        if tag not in {
            "area", "base", "br", "col", "embed", "hr", "img", "input",
            "link", "meta", "param", "source", "track", "wbr",
        }:
            self._elements.append((tag, set(classes)))
        if tag == "a":
            self._anchors.append(
                {
                    "href": values.get("href", ""),
                    "title": values.get("title", ""),
                    "text": [],
                }
            )

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "a" and self._anchors:
            anchor = self._anchors.pop()
            href = anchor["href"]
            if href:
                title = anchor["title"] or " ".join(anchor["text"]) or href
                self.links.append((href, title))
        for index in range(len(self._elements) - 1, -1, -1):
            if self._elements[index][0] == tag:
                del self._elements[index:]
                break

    def handle_data(self, data):
        value = " ".join(data.split())
        if value:
            self.text.append(value)
            if self._anchors:
                self._anchors[-1]["text"].append(value)
            for _, classes in self._elements:
                for class_name in classes:
                    self.class_text.setdefault(class_name, []).append(value)


def _labeled_number(text: str, label: str):
    match = re.search(
        rf"(?:{label})\s*:?\s*(?:#\s*)?([\d,]+)",
        text,
        re.I,
    )
    return int(match.group(1).replace(",", "")) if match else None


def _contest_kind(value: str) -> str | None:
    lowered = value.casefold()
    if "starter" in lowered:
        return "starters"
    if "cook-off" in lowered or "cook off" in lowered:
        return "cook_off"
    if "lunchtime" in lowered:
        return "lunchtime"
    if "long challenge" in lowered:
        return "long_challenges"
    return None


async def fetch_codechef(username: str, client, semaphore=None) -> dict:
    username = _valid_username(username)
    report = _empty_codechef(username)
    if not username:
        return report
    try:
        response = await _request(
            client,
            "get",
            f"https://www.codechef.com/users/{quote(username, safe='')}",
            semaphore,
        )
        parser = _ProfileTextParser()
        parser.feed(response.text)
        text = " ".join(parser.text)
        report["status"] = "available"
        report["global_rank"] = _labeled_number(text, r"Global\s+Rank")
        report["country_rank"] = _labeled_number(text, r"Country\s+Rank")
        report["current_rating"] = _labeled_number(
            text, r"Current\s+Rating|(?<!Highest )\bRating"
        )
        if report["current_rating"] is None:
            rating_number = " ".join(parser.class_text.get("rating-number", []))
            rating_match = re.search(r"\b([\d,]+)\b", rating_number)
            if rating_match:
                report["current_rating"] = int(rating_match.group(1).replace(",", ""))
        report["highest_rating"] = _labeled_number(text, r"Highest\s+Rating")
        rating_text = " ".join(parser.class_text.get("rating", []))
        star_match = re.search(r"([1-7])\s*(?:★|star)", text + " " + rating_text, re.I)
        if star_match:
            report["star_rating"] = int(star_match.group(1))
        division_match = re.search(r"\bDiv(?:ision)?\s*([1-4])\b", text, re.I)
        if division_match:
            report["division"] = f"Div {division_match.group(1)}"
        solved_match = re.search(
            r"(?:Fully\s+Solved|Problems?\s+Solved|Total\s+Problems?\s+Solved)"
            r"\s*:?\s*([\d,]+)",
            text,
            re.I,
        )
        if solved_match:
            report["problems_solved"] = int(solved_match.group(1).replace(",", ""))
        participation = report["contest_participation"]
        history = []
        for href, title in parser.links:
            contest_title = title.strip()
            if not contest_title:
                continue
            kind = _contest_kind(contest_title)
            if kind:
                participation[kind] += 1
                history.append({"type": kind, "title": contest_title[:120]})
        report["contest_participation"]["history"] = history[:30]
        for kind, label in (
            ("starters", "Starters"),
            ("cook_off", "Cook[- ]Off"),
            ("lunchtime", "Lunchtime"),
            ("long_challenges", "Long Challenge"),
        ):
            if not participation[kind]:
                participation[kind] = len(re.findall(label, text, re.I))
    except Exception as error:
        logger.info("CodeChef profile fetch failed for %s: %s", username, error)
        report["errors"].append(f"CodeChef profile could not be fetched: {type(error).__name__}.")
    return report


def _badge_label(badge: dict) -> str:
    return str(badge.get("badge_name") or badge.get("name") or badge.get("category") or "")


def _badge_stars(badge: dict) -> int:
    for key in ("stars", "star_count", "badge_level", "level"):
        value = badge.get(key)
        if isinstance(value, list):
            value = len(value)
        try:
            return max(0, min(5, int(value)))
        except (TypeError, ValueError):
            continue
    return 0


def _parse_hackerrank_badges(payload) -> list:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("models", "badges", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                return _parse_hackerrank_badges(value)
    return []


def _badge_domain(label: str) -> str | None:
    lowered = label.casefold()
    if "problem solving" in lowered or re.search(r"\bdsa\b", lowered):
        return "problem_solving"
    if re.search(r"\bpython\b", lowered):
        return "python"
    if re.search(r"\bsql\b", lowered):
        return "sql"
    if "c++" in lowered or re.search(r"\bcpp\b", lowered):
        return "cpp"
    if re.search(r"\bjava\b", lowered):
        return "java"
    return None


async def fetch_hackerrank(username: str, client, semaphore=None) -> dict:
    username = _valid_username(username)
    report = _empty_hackerrank(username)
    if not username:
        return report
    safe_username = quote(username, safe="")
    base_url = f"https://www.hackerrank.com/rest/hackers/{safe_username}"
    profile_result, badges_result = await asyncio.gather(
        _request(client, "get", f"{base_url}/profile", semaphore),
        _request(client, "get", f"{base_url}/badges", semaphore),
        return_exceptions=True,
    )
    embedded_badges = []
    if isinstance(profile_result, BaseException):
        logger.info("HackerRank profile fetch failed for %s: %s", username, profile_result)
        report["errors"].append(
            f"HackerRank profile could not be fetched: {type(profile_result).__name__}."
        )
    else:
        try:
            payload = profile_result.json()
            profile = payload.get("data", payload) if isinstance(payload, dict) else {}
            if isinstance(profile, dict):
                report["status"] = "available"
                report["level"] = profile.get("level")
                report["followers"] = profile.get("followers")
                report["total_submissions"] = next(
                    (
                        profile[key]
                        for key in ("total_submissions", "submissions", "submission_count")
                        if profile.get(key) is not None
                    ),
                    None,
                )
                embedded_badges = _parse_hackerrank_badges(profile.get("badges"))
        except Exception as error:
            logger.info("HackerRank profile parsing failed for %s: %s", username, error)
            report["errors"].append(
                f"HackerRank profile data could not be parsed: {type(error).__name__}."
            )
    if isinstance(badges_result, BaseException):
        logger.info("HackerRank badges fetch failed for %s: %s", username, badges_result)
        badges = embedded_badges
        report["errors"].append(
            f"HackerRank badges could not be fetched: {type(badges_result).__name__}."
        )
    else:
        try:
            badges = _parse_hackerrank_badges(badges_result.json())
        except Exception as error:
            logger.info("HackerRank badges parsing failed for %s: %s", username, error)
            badges = embedded_badges
            report["errors"].append(
                f"HackerRank badges could not be parsed: {type(error).__name__}."
            )
    badges = badges or embedded_badges
    if badges or report["status"] == "available":
        report["badges_count"] = len(badges)
    for badge in badges:
        label = _badge_label(badge)
        domain = _badge_domain(label)
        if domain:
            bucket = report["domain_badges"][domain]
            bucket["stars"] = max(bucket["stars"], _badge_stars(badge))
            if label and label not in bucket["badges"]:
                bucket["badges"].append(label)
    return report


async def fetch_competitive_profiles(
    text: str, client=None, semaphore=None, profile_usernames=None
) -> dict:
    usernames = extract_profile_usernames(text)
    if isinstance(profile_usernames, dict):
        for platform, values in profile_usernames.items():
            if platform not in usernames:
                continue
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list):
                continue
            for value in values:
                username = _valid_username(str(value))
                if username and username.casefold() not in {
                    item.casefold() for item in usernames[platform]
                }:
                    usernames[platform].append(username)
    empty = empty_competitive_profiles()
    if not any(usernames.values()):
        return empty
    if client is None:
        async with httpx.AsyncClient(timeout=_timeout_seconds()) as owned_client:
            return await fetch_competitive_profiles(
                text, owned_client, semaphore, usernames
            )
    fetchers = {
        "leetcode": fetch_leetcode,
        "codechef": fetch_codechef,
        "hackerrank": fetch_hackerrank,
    }
    selected = [
        (platform, usernames[platform], fetchers[platform])
        for platform in fetchers
        if usernames[platform]
    ]
    results = await asyncio.gather(
        *(
            fetcher(platform_usernames[0], client, semaphore)
            for _, platform_usernames, fetcher in selected
        )
    )
    for (platform, platform_usernames, _), result in zip(selected, results):
        empty[platform] = result
        empty[platform]["additional_profiles"] = len(platform_usernames) - 1
    return empty


def calculate_technical_rating(profiles: dict, github: dict | None = None) -> dict:
    """Calculate an explainable, independent score from available public metrics."""
    components = {}
    leetcode = profiles.get("leetcode", {})
    solved = leetcode.get("solved", {})
    total_solved = solved.get("total")
    if isinstance(total_solved, (int, float)):
        components["leetcode_problem_solving"] = {
            "points": round(min(30, max(0, total_solved) / 400 * 30), 1),
            "max": 30,
        }
    contest_rating = leetcode.get("contest", {}).get("rating")
    if isinstance(contest_rating, (int, float)):
        components["leetcode_contest_rating"] = {
            "points": round(min(20, max(0, contest_rating) / 3000 * 20), 1),
            "max": 20,
        }
    codechef_rating = profiles.get("codechef", {}).get("current_rating")
    if isinstance(codechef_rating, (int, float)):
        components["codechef_rating"] = {
            "points": round(min(20, max(0, codechef_rating) / 3000 * 20), 1),
            "max": 20,
        }
    hackerrank = profiles.get("hackerrank", {})
    if hackerrank.get("status") == "available":
        stars = sum(
            badge.get("stars", 0)
            for badge in hackerrank.get("domain_badges", {}).values()
        )
        components["hackerrank_domain_stars"] = {
            "points": round(min(10, max(0, stars) / 25 * 10), 1),
            "max": 10,
        }
    if github and github.get("checked"):
        evidence = min(
            100,
            25
            + min(45, github.get("original_repos", 0) * 15)
            + min(15, github.get("recent_repos", 0) * 5)
            + min(15, github.get("total_stars", 0) * 3),
        )
        components["github_public_evidence"] = {
            "points": round(evidence / 100 * 20, 1),
            "max": 20,
        }
    available_max = sum(item["max"] for item in components.values())
    return {
        "score": (
            round(
                sum(item["points"] for item in components.values())
                / available_max
                * 100
            )
            if available_max
            else None
        ),
        "components": components,
        "available_components": len(components),
        "max_available_points": available_max,
        "note": (
            "Supplementary public-profile evidence normalized over available "
            "components; not a hiring decision."
        ),
    }
