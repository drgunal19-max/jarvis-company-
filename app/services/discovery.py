import hashlib
import html
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests

from app.database import connect

ARBEITNOW_URL = "https://www.arbeitnow.com/api/job-board-api"
DEFAULT_KEYWORDS = ["assistant", "computer", "it support", "technical support", "help desk"]


class TaskActionError(ValueError):
    pass


class SourceUnavailable(RuntimeError):
    pass


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def clean_text(value: str, limit: int = 5000) -> str:
    parser = _TextExtractor()
    parser.feed(value)
    return " ".join(html.unescape(" ".join(parser.parts)).split())[:limit]


def score_item(text: str, keywords: list[str], location: str = "") -> int:
    haystack = text.casefold()
    normalized = [keyword.strip().casefold() for keyword in keywords if keyword.strip()]
    if not normalized:
        normalized = DEFAULT_KEYWORDS
    hits = sum(1 for keyword in normalized if keyword in haystack)
    score = min(100, int((hits / max(1, len(normalized))) * 80))
    if location and location.strip().casefold() in haystack:
        score = min(100, score + 20)
    return score


def explain_match(text: str, keywords: list[str], location: str = "") -> str:
    haystack = text.casefold()
    expected = [word.strip() for word in keywords if word.strip()] or DEFAULT_KEYWORDS
    matched = [word for word in expected if word.casefold() in haystack]
    reasons = [f"Matched: {', '.join(matched)}"] if matched else []
    if location and location.casefold() in haystack:
        reasons.append(f"Location matches {location}")
    return "; ".join(reasons) or "Passed the minimum relevance threshold"


def fetch_jobs():
    response = requests.get(
        ARBEITNOW_URL,
        timeout=20,
        headers={"User-Agent": "JARVIS-Company-AI/1.0"},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise SourceUnavailable("Arbeitnow returned an unexpected response format")
    return payload["data"]


def _safe_source_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _source_health(source: str, status: str, details: str, checked: int):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO source_health(source, status, details, checked_count, last_checked_at)
            VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(source) DO UPDATE SET status=excluded.status,
                details=excluded.details, checked_count=excluded.checked_count,
                last_checked_at=CURRENT_TIMESTAMP
            """,
            (source, status, details[:500], checked),
        )


def run_discovery(task_id: int) -> dict:
    with connect() as conn:
        stopped = conn.execute(
            "SELECT value FROM system_state WHERE key='emergency_stop'"
        ).fetchone()
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise TaskActionError("Task not found")
        if stopped and stopped["value"] == "true":
            raise TaskActionError("Emergency stop is active")
        if task["status"] != "active":
            raise TaskActionError("Task is not active")
        keywords = [word.strip() for word in (task["keywords"] or "").split(",") if word.strip()]
        location = task["location"]
        rss_sources = [
            dict(row) for row in conn.execute(
                "SELECT id, name, url FROM task_sources WHERE task_id=? ORDER BY id",
                (task_id,),
            )
        ]

    collected = []
    errors = []
    successful_sources = set()
    try:
        jobs = fetch_jobs()
        collected.extend(("Arbeitnow", item) for item in jobs)
        successful_sources.add("Arbeitnow")
        _source_health("Arbeitnow", "online", f"Fetched {len(jobs)} items", len(jobs))
    except Exception as exc:
        errors.append(f"Arbeitnow: {str(exc)[:250]}")
        _source_health("Arbeitnow", "error", str(exc), 0)

    for source in rss_sources:
        source_label = f"RSS:{source['id']}:{source['name']}"
        try:
            from app.services.rss import fetch_rss_feed

            items = fetch_rss_feed(source)
            collected.extend((source_label, item) for item in items)
            successful_sources.add(source_label)
            _source_health(source_label, "online", f"Fetched {len(items)} items", len(items))
        except Exception as exc:
            errors.append(f"{source_label}: {str(exc)[:250]}")
            _source_health(source_label, "error", str(exc), 0)

    if not successful_sources:
        raise SourceUnavailable("; ".join(errors) or "No configured source returned results")

    new_count = 0
    checked = 0
    source_count = len(successful_sources)
    with connect() as conn:
        for source_label, job in collected:
            if not isinstance(job, dict):
                continue
            checked += 1
            title = clean_text(str(job.get("title") or "Untitled role"), 300)
            company = clean_text(str(job.get("company_name") or ""), 300)
            item_location = clean_text(str(job.get("location") or ""), 300)
            description = clean_text(str(job.get("description") or ""))
            url = str(job.get("url") or "").strip()
            if not _safe_source_url(url):
                continue
            source_id = str(job.get("slug") or url).strip()
            if not source_id:
                source_id = hashlib.sha256(f"{title}|{company}|{url}".encode()).hexdigest()
            searchable = " ".join([title, company, item_location, description])
            score = score_item(searchable, keywords, location)
            if score < 20:
                continue
            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO findings
                    (task_id, source, source_id, title, company, location, url, summary, score, match_reasons)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    source_label,
                    source_id[:500],
                    title,
                    company,
                    item_location,
                    url[:2000],
                    description[:420],
                    score,
                    explain_match(searchable, keywords, location),
                ),
            )
            if conn.total_changes > before:
                new_count += 1

        conn.execute("UPDATE tasks SET last_run_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
        conn.execute(
            """
            INSERT INTO runs(task_id, status, message, item_count, checked_count)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                task_id,
                "partial" if errors else "completed",
                f"Checked {checked} items across {source_count} sources; "
                f"saved {new_count} new matches."
                + (f" Source errors: {'; '.join(errors)}" if errors else ""),
                new_count,
                checked,
            ),
        )
        conn.execute(
            "INSERT INTO audit_log(action, entity_type, entity_id, details) VALUES(?,?,?,?)",
            (
                "discovery.partial" if errors else "discovery.completed",
                "task",
                str(task_id),
                f"Checked {checked}; new findings {new_count}; errors {len(errors)}",
            ),
        )
    with connect() as conn:
        checked_at = conn.execute(
            "SELECT last_run_at FROM tasks WHERE id=?", (task_id,)
        ).fetchone()["last_run_at"]
    return {
        "task_id": task_id,
        "checked": checked,
        "new_findings": new_count,
        "sources": sorted(
            "Arbeitnow" if name == "Arbeitnow" else name.split(":", 2)[-1]
            for name in successful_sources
        ),
        "errors": errors,
        "checked_at": checked_at,
    }
