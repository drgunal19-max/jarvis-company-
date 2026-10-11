import asyncio
import json
import logging
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from requests import RequestException

from app.config import (
    APP_API_TOKEN,
    APP_NAME,
    ENABLE_SCHEDULER,
    GOOGLE_REDIRECT_URI,
    INSTAGRAM_REDIRECT_URI,
    LLM_API_KEY,
    LLM_MODEL,
    META_APP_SECRET,
    META_GRAPH_API_VERSION,
    POLL_INTERVAL_MINUTES,
    TAVILY_API_KEY,
    WHATSAPP_ACCESS_TOKEN,
    WHATSAPP_PHONE_NUMBER_ID,
    WHATSAPP_VERIFY_TOKEN,
)
from app.database import connect, init_db
from app.schemas import (
    AssistantCommand,
    ChatRequest,
    FindingUpdate,
    MemoryCreate,
    N8nFindingBatch,
    PreferencesUpdate,
    PublicFeedCreate,
    TaskCreate,
    TaskUpdate,
    WhatsAppMessageCreate,
)
from app.services.discovery import TaskActionError, run_discovery
from app.services import google_workspace
from app.services import instagram
from app.services.google_workspace import (
    GOOGLE_SCOPES,
    GoogleConfigurationError,
    GoogleNotConnectedError,
    GoogleProviderError,
    GoogleReauthorizationRequired,
)
from app.services.instagram import (
    InstagramConfigurationError,
    InstagramNotConnectedError,
    InstagramProviderError,
    InstagramReauthorizationRequired,
)
from app.services.llm import generate_chat_response, interpret_task
from app.services.rss import FeedError, validate_public_feed_url
from app.services.web_search import search_web
from app.services.whatsapp import (
    WhatsAppConfigurationError,
    WhatsAppProviderError,
    extract_webhook_events,
    send_text_message,
    verify_webhook_signature,
)

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)
PREFERENCE_DEFAULTS = {
    "quiet_hours_start": "",
    "quiet_hours_end": "",
    "morning_briefing": True,
    "default_location": "",
    "default_keywords": [],
}
LOCATION_PATTERN = re.compile(
    r"\b(?:in|near|around)\s+([A-Za-z][A-Za-z .'-]{1,50}?)"
    r"(?=\s+(?:with|who|that|for|having)\b|[,.!?]|$)",
    re.IGNORECASE,
)
KNOWN_KEYWORDS = (
    "assistant", "administrative", "computer", "excel", "email", "scheduling",
    "troubleshooting", "it support", "technical support", "help desk", "sales",
    "recruitment", "customer service", "project management", "python", "remote",
)
STOP_WORDS = {
    "find", "search", "look", "for", "a", "an", "the", "me", "please", "with",
    "and", "or", "in", "near", "around", "new", "relevant", "opportunities",
    "jobs", "job", "candidates", "candidate", "that", "who", "has", "have",
}


async def _scheduled_worker():
    while True:
        try:
            await asyncio.to_thread(run_due_tasks)
        except Exception:
            logger.exception("Scheduled task scan failed")
        await asyncio.sleep(max(1, POLL_INTERVAL_MINUTES) * 60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    worker = asyncio.create_task(_scheduled_worker()) if ENABLE_SCHEDULER else None
    try:
        yield
    finally:
        if worker:
            worker.cancel()
            with suppress(asyncio.CancelledError):
                await worker


app = FastAPI(
    title=APP_NAME,
    version="1.0.0",
    description="Company intelligence automation. Integrations are explicit and source-scoped.",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def protect_api(request: Request, call_next):
    public_api_paths = {
        "/api/health",
        "/api/webhooks/whatsapp",
        "/api/google/callback",
        "/api/instagram/callback",
    }
    if (
        request.url.path.startswith("/api/")
        and request.url.path not in public_api_paths
        and APP_API_TOKEN
    ):
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {APP_API_TOKEN}"
        if not secrets.compare_digest(supplied, expected):
            return JSONResponse(status_code=401, content={"detail": "A valid bearer token is required"})
    return await call_next(request)


def _task_json(row):
    item = dict(row)
    item["keywords"] = [word for word in (item.get("keywords") or "").split(",") if word]
    return item


def _create_task(payload: TaskCreate):
    keywords = ",".join(dict.fromkeys(word.strip() for word in payload.keywords if word.strip()))
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO tasks(title, instruction, keywords, location, interval_minutes)
            VALUES(?,?,?,?,?)
            """,
            (
                payload.title.strip(),
                payload.instruction.strip(),
                keywords,
                payload.location.strip(),
                payload.interval_minutes,
            ),
        )
        task_id = cursor.lastrowid
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        conn.execute(
            "INSERT INTO audit_log(action, entity_type, entity_id, details) VALUES(?,?,?,?)",
            ("task.created", "task", str(task_id), payload.title.strip()),
        )
    return _task_json(row)


def _record_run_failure(task_id: int, message: str):
    with connect() as conn:
        task = conn.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task:
            conn.execute(
                """
                INSERT INTO runs(task_id,status,message,item_count,checked_count)
                VALUES(?, 'failed', ?, 0, 0)
                """,
                (task_id, message[:500]),
            )
            conn.execute("UPDATE tasks SET last_run_at=CURRENT_TIMESTAMP WHERE id=?", (task_id,))
            conn.execute(
                "INSERT INTO audit_log(action, entity_type, entity_id, details) VALUES(?,?,?,?)",
                ("discovery.failed", "task", str(task_id), message[:500]),
            )


def run_due_tasks():
    with connect() as conn:
        stopped = conn.execute(
            "SELECT value FROM system_state WHERE key='emergency_stop'"
        ).fetchone()
        if stopped and stopped["value"] == "true":
            return {"ran": 0, "skipped": "Emergency stop is active"}
        tasks = conn.execute(
            """
            SELECT id, interval_minutes, last_run_at
            FROM tasks
            WHERE status='active' AND interval_minutes > 0
            """
        ).fetchall()
    ran = 0
    for task in tasks:
        try:
            last_run = task["last_run_at"]
            if last_run:
                elapsed = datetime.now(timezone.utc).replace(tzinfo=None) - datetime.fromisoformat(last_run)
                if elapsed.total_seconds() < task["interval_minutes"] * 60:
                    continue
            run_discovery(task["id"])
        except Exception as exc:
            _record_run_failure(task["id"], str(exc))
        ran += 1
    return {"ran": ran}


@app.get("/", response_class=HTMLResponse)
def home():
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health():
    try:
        with connect() as conn:
            conn.execute("SELECT 1").fetchone()
    except sqlite3.Error as exc:
        logger.exception("Database readiness check failed")
        raise HTTPException(503, "Database unavailable") from exc
    return {
        "status": "ok",
        "app": APP_NAME,
        "database": "ok",
        "scheduler": "enabled" if ENABLE_SCHEDULER else "disabled",
    }


@app.get("/api/tasks")
def list_tasks():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchall()
    return [_task_json(row) for row in rows]


@app.get("/api/task-sources")
def list_task_sources():
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT task_sources.*, tasks.title AS task_title,
                source_health.status AS health_status,
                source_health.details AS health_details,
                source_health.last_checked_at
            FROM task_sources
            JOIN tasks ON tasks.id=task_sources.task_id
            LEFT JOIN source_health
                ON source_health.source='RSS:' || task_sources.id || ':' || task_sources.name
            ORDER BY task_sources.id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/tasks/{task_id}/sources", status_code=201)
def add_task_source(task_id: int, payload: PublicFeedCreate):
    try:
        url = validate_public_feed_url(payload.url)
    except FeedError as exc:
        raise HTTPException(422, str(exc)) from exc
    name = payload.name.strip()
    with connect() as conn:
        task = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")
        if task["status"] == "cancelled":
            raise HTTPException(409, "Cannot add a source to a cancelled task")
        try:
            cursor = conn.execute(
                "INSERT INTO task_sources(task_id,name,url) VALUES(?,?,?)",
                (task_id, name, url),
            )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "This feed URL is already configured for this task") from exc
        source_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO audit_log(action, entity_type, entity_id, details) VALUES(?,?,?,?)",
            ("source.added", "task_source", str(source_id), f"{name} for task {task_id}"),
        )
        row = conn.execute(
            "SELECT * FROM task_sources WHERE id=?", (source_id,)
        ).fetchone()
    return dict(row)


@app.delete("/api/task-sources/{source_id}")
def delete_task_source(source_id: int):
    with connect() as conn:
        row = conn.execute(
            "SELECT id, task_id, name FROM task_sources WHERE id=?", (source_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "Configured source not found")
        conn.execute("DELETE FROM task_sources WHERE id=?", (source_id,))
        conn.execute(
            "INSERT INTO audit_log(action, entity_type, entity_id, details) VALUES(?,?,?,?)",
            ("source.removed", "task_source", str(source_id), row["name"]),
        )
        conn.execute(
            "DELETE FROM source_health WHERE source=?",
            (f"RSS:{source_id}:{row['name']}",),
        )
    return {"deleted": True}


@app.post("/api/tasks", status_code=201)
def create_task(payload: TaskCreate):
    return _create_task(payload)


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: int):
    with connect() as conn:
        task = conn.execute("SELECT title FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            raise HTTPException(404, "Task not found")
        conn.execute("DELETE FROM findings WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM runs WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM task_sources WHERE task_id=?", (task_id,))
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        conn.execute(
            "INSERT INTO audit_log(action, entity_type, entity_id, details) VALUES(?,?,?,?)",
            ("task.deleted", "task", str(task_id), task["title"]),
        )
    return {"deleted": True, "task_id": task_id}


@app.patch("/api/tasks/{task_id}")
def update_task(task_id: int, payload: TaskUpdate):
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(400, "No fields supplied")
    with connect() as conn:
        row = conn.execute("SELECT id FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Task not found")
        assignments = []
        values = []
        for key, value in data.items():
            if value is None:
                continue
            if key == "keywords":
                value = ",".join(dict.fromkeys(word.strip() for word in value if word.strip()))
            elif isinstance(value, str):
                value = value.strip()
            assignments.append(f"{key}=?")
            values.append(value)
        if not assignments:
            raise HTTPException(400, "No valid fields supplied")
        values.append(task_id)
        conn.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE id=?", values)
        conn.execute(
            "INSERT INTO audit_log(action, entity_type, entity_id, details) VALUES(?,?,?,?)",
            ("task.updated", "task", str(task_id), ",".join(data)),
        )
        updated = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _task_json(updated)


@app.post("/api/tasks/{task_id}/{action}")
def task_action(task_id: int, action: str):
    with connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Task not found")
        transitions = {
            "pause": ("active", "paused"),
            "resume": ("paused", "active"),
            "cancel": (("active", "paused"), "cancelled"),
        }
        if action not in transitions:
            raise HTTPException(404, "Unknown action")
        allowed_from, new_status = transitions[action]
        allowed = (allowed_from,) if isinstance(allowed_from, str) else allowed_from
        if row["status"] not in allowed:
            raise HTTPException(409, f"Cannot {action} a {row['status']} task")
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (new_status, task_id))
        conn.execute(
            "INSERT INTO audit_log(action, entity_type, entity_id) VALUES(?,?,?)",
            (f"task.{action}", "task", str(task_id)),
        )
        updated = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _task_json(updated)


@app.post("/api/tasks/{task_id}/run")
def run_task(task_id: int):
    try:
        return run_discovery(task_id)
    except TaskActionError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        _record_run_failure(task_id, str(exc))
        raise HTTPException(502, f"Source check failed: {str(exc)[:250]}") from exc


@app.get("/api/findings")
def list_findings(
    task_id: int | None = None,
    status: str | None = None,
):
    if status and status not in {"new", "relevant", "irrelevant", "duplicate", "contacted", "completed"}:
        raise HTTPException(422, "Unknown finding status")
    clauses = []
    values = []
    if task_id is not None:
        clauses.append("findings.task_id=?")
        values.append(task_id)
    if status:
        clauses.append("findings.status=?")
        values.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with connect() as conn:
        rows = conn.execute(
            f"""
            SELECT findings.*, tasks.title AS task_title FROM findings
            JOIN tasks ON tasks.id=findings.task_id
            {where}
            ORDER BY score DESC, discovered_at DESC LIMIT 500
            """,
            values,
        ).fetchall()
    return [dict(row) for row in rows]


@app.patch("/api/findings/{finding_id}")
def update_finding(finding_id: int, payload: FindingUpdate):
    with connect() as conn:
        cursor = conn.execute("UPDATE findings SET status=? WHERE id=?", (payload.status, finding_id))
        if not cursor.rowcount:
            raise HTTPException(404, "Finding not found")
        row = conn.execute("SELECT * FROM findings WHERE id=?", (finding_id,)).fetchone()
        conn.execute(
            "INSERT INTO audit_log(action, entity_type, entity_id, details) VALUES(?,?,?,?)",
            ("finding.status_changed", "finding", str(finding_id), payload.status),
        )
    return dict(row)


@app.get("/api/runs")
def list_runs():
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT runs.*, tasks.title AS task_title FROM runs
            JOIN tasks ON tasks.id=runs.task_id ORDER BY runs.id DESC LIMIT 200
            """
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/assistant/command", status_code=201)
def assistant_command(payload: AssistantCommand):
    instruction = payload.instruction.strip()
    location = (payload.location or "").strip()
    keywords = payload.keywords
    title = payload.title
    ai_assisted = False
    if (not title or keywords is None or not location) and LLM_API_KEY:
        try:
            interpretation = interpret_task(instruction)
        except (ValueError, RequestException) as exc:
            raise HTTPException(502, f"Configured AI provider failed: {str(exc)[:200]}") from exc
        if interpretation:
            ai_assisted = True
            title = title or interpretation["title"]
            location = location or interpretation["location"]
            keywords = keywords if keywords is not None else interpretation["keywords"]
    if not location:
        match = LOCATION_PATTERN.search(instruction)
        location = match.group(1).strip() if match else ""
    if keywords is None:
        lowered = instruction.casefold()
        keywords = [word for word in KNOWN_KEYWORDS if word in lowered]
        if not keywords:
            words = re.findall(r"[a-zA-Z][a-zA-Z+#.-]{2,}", instruction.casefold())
            keywords = list(dict.fromkeys(word for word in words if word not in STOP_WORDS))[:8]
    if not location or not keywords:
        with connect() as conn:
            saved_preferences = {
                row["key"]: json.loads(row["value"])
                for row in conn.execute("SELECT key,value FROM preferences")
            }
        location = location or saved_preferences.get("default_location", "")
        keywords = keywords or saved_preferences.get("default_keywords", [])
    title = title or instruction[:157].rstrip(" .,!?") or "New monitoring task"
    task = _create_task(
        TaskCreate(
            title=title,
            instruction=instruction,
            location=location,
            keywords=keywords,
            interval_minutes=payload.interval_minutes,
        )
    )
    reply = (
        f"Created task '{task['title']}' for {task['location'] or 'any location'}. "
        f"Keywords: {', '.join(task['keywords']) or 'default matching rules'}. "
        f"{'Task fields were extracted with the configured AI provider.' if ai_assisted else 'Task fields were extracted with the local rule-based parser.'}"
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO assistant_messages(role, content) VALUES('user', ?)", (instruction,)
        )
        conn.execute(
            "INSERT INTO assistant_messages(role, content) VALUES('assistant', ?)", (reply,)
        )
    return {
        "task": task,
        "reply": reply,
        "verified_facts": ["Task saved to the local database"],
        "generated_assessment": "AI-assisted task field extraction" if ai_assisted else None,
    }


@app.get("/api/assistant/messages")
def assistant_messages():
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, role, content, created_at FROM assistant_messages ORDER BY id DESC LIMIT 50"
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


def _google_api_call(operation: Callable[[], Any]) -> Any:
    try:
        return operation()
    except GoogleNotConnectedError as exc:
        raise HTTPException(409, str(exc)) from exc
    except GoogleConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    except GoogleReauthorizationRequired as exc:
        raise HTTPException(401, str(exc)) from exc
    except GoogleProviderError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/google/status")
def google_status():
    status = google_workspace.connection_status()
    return {**status, "scopes": list(GOOGLE_SCOPES)}


@app.get("/api/google/authorize")
def google_authorize(request: Request):
    state = secrets.token_urlsafe(32)
    try:
        authorization = google_workspace.authorization_url(state)
    except GoogleConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    response = JSONResponse({"authorization_url": authorization})
    response.set_cookie(
        "jarvis_google_oauth_state",
        state,
        max_age=600,
        httponly=True,
        secure=urlparse(GOOGLE_REDIRECT_URI).scheme == "https",
        samesite="lax",
        path="/api/google/callback",
    )
    return response


@app.get("/api/google/callback", response_class=HTMLResponse)
def google_callback(request: Request):
    response_error = request.query_params.get("error")
    if response_error:
        response = HTMLResponse(
            "<h1>Google connection cancelled</h1><p>You can close this tab and return to JARVIS.</p>",
            status_code=400,
        )
        response.delete_cookie("jarvis_google_oauth_state", path="/api/google/callback")
        return response

    state = request.query_params.get("state", "")
    cookie_state = request.cookies.get("jarvis_google_oauth_state", "")
    code = request.query_params.get("code", "")
    if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        raise HTTPException(400, "Google OAuth state validation failed")
    if not code or len(code) > 4096:
        raise HTTPException(400, "Google authorization code is missing or invalid")
    try:
        google_workspace.exchange_authorization_code(code)
    except (GoogleConfigurationError, GoogleProviderError) as exc:
        logger.warning("Google OAuth callback failed: %s", exc)
        response = HTMLResponse(
            "<h1>Google connection failed</h1>"
            "<p>Check the Google OAuth configuration and consent-screen status, then retry.</p>"
            '<p><a href="/">Return to JARVIS</a></p>',
            status_code=502,
        )
        response.delete_cookie("jarvis_google_oauth_state", path="/api/google/callback")
        return response

    response = HTMLResponse(
        "<h1>Google account connected</h1>"
        '<p>Read-only access was authorized. <a href="/">Return to JARVIS</a>.</p>'
    )
    response.delete_cookie("jarvis_google_oauth_state", path="/api/google/callback")
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_log(action,entity_type,details) VALUES(?,?,?)",
            ("google.connected", "google_account", "Read-only Gmail, Calendar, and Drive metadata scopes"),
        )
    return response


@app.post("/api/google/disconnect")
def google_disconnect():
    google_workspace.disconnect()
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_log(action,entity_type,details) VALUES(?,?,?)",
            ("google.disconnected", "google_account", "Google OAuth tokens removed"),
        )
    return {"connected": False}


@app.get("/api/google/gmail")
def google_gmail(q: str = ""):
    if len(q) > 200:
        raise HTTPException(422, "Gmail search query cannot exceed 200 characters")
    return _google_api_call(lambda: google_workspace.search_gmail(q))


@app.get("/api/google/calendar")
def google_calendar():
    return _google_api_call(google_workspace.list_calendar_events)


@app.get("/api/google/drive")
def google_drive(q: str = ""):
    if len(q) > 120:
        raise HTTPException(422, "Drive search query cannot exceed 120 characters")
    return _google_api_call(lambda: google_workspace.search_drive(q))


def _instagram_api_call(operation: Callable[[], Any]) -> Any:
    try:
        return operation()
    except InstagramNotConnectedError as exc:
        raise HTTPException(409, str(exc)) from exc
    except InstagramConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    except InstagramReauthorizationRequired as exc:
        raise HTTPException(401, str(exc)) from exc
    except InstagramProviderError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/instagram/status")
def instagram_status():
    return instagram.connection_status()


@app.get("/api/instagram/authorize")
def instagram_authorize():
    state = secrets.token_urlsafe(32)
    try:
        authorization = instagram.authorization_url(state)
    except InstagramConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    response = JSONResponse({"authorization_url": authorization})
    response.set_cookie(
        "jarvis_instagram_oauth_state",
        state,
        max_age=600,
        httponly=True,
        secure=urlparse(INSTAGRAM_REDIRECT_URI).scheme == "https",
        samesite="lax",
        path="/api/instagram/callback",
    )
    return response


@app.get("/api/instagram/callback", response_class=HTMLResponse)
def instagram_callback(request: Request):
    if request.query_params.get("error"):
        response = HTMLResponse(
            "<h1>Instagram connection cancelled</h1>"
            "<p>Return to JARVIS and retry if you want to connect.</p>",
            status_code=400,
        )
        response.delete_cookie(
            "jarvis_instagram_oauth_state", path="/api/instagram/callback"
        )
        return response

    state = request.query_params.get("state", "")
    cookie_state = request.cookies.get("jarvis_instagram_oauth_state", "")
    code = request.query_params.get("code", "")
    if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        raise HTTPException(400, "Instagram OAuth state validation failed")
    if not code or len(code) > 4096:
        raise HTTPException(400, "Instagram authorization code is missing or invalid")
    try:
        instagram.exchange_authorization_code(code)
    except (InstagramConfigurationError, InstagramProviderError) as exc:
        logger.warning("Instagram OAuth callback failed: %s", exc)
        response = HTMLResponse(
            "<h1>Instagram connection failed</h1>"
            "<p>Check Instagram Login configuration and professional-account access, then retry.</p>"
            '<p><a href="/">Return to JARVIS</a></p>',
            status_code=502,
        )
        response.delete_cookie(
            "jarvis_instagram_oauth_state", path="/api/instagram/callback"
        )
        return response

    response = HTMLResponse(
        "<h1>Instagram account connected</h1>"
        '<p>Read-only profile and media access was authorized. <a href="/">Return to JARVIS</a>.</p>'
    )
    response.delete_cookie(
        "jarvis_instagram_oauth_state", path="/api/instagram/callback"
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_log(action,entity_type,details) VALUES(?,?,?)",
            ("instagram.connected", "instagram_account", "Read-only professional profile and media scope"),
        )
    return response


@app.post("/api/instagram/disconnect")
def instagram_disconnect():
    instagram.disconnect()
    with connect() as conn:
        conn.execute(
            "INSERT INTO audit_log(action,entity_type,details) VALUES(?,?,?)",
            ("instagram.disconnected", "instagram_account", "Instagram OAuth token removed"),
        )
    return {"connected": False}


@app.get("/api/instagram/profile")
def instagram_profile():
    return _instagram_api_call(instagram.get_profile)


@app.get("/api/instagram/media")
def instagram_media():
    return _instagram_api_call(instagram.list_media)


@app.get("/api/webhooks/whatsapp", response_class=PlainTextResponse)
def verify_whatsapp_webhook(request: Request):
    if not WHATSAPP_VERIFY_TOKEN:
        raise HTTPException(503, "Configure WHATSAPP_VERIFY_TOKEN before registering webhooks")
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token", "")
    challenge = request.query_params.get("hub.challenge")
    if mode != "subscribe" or not secrets.compare_digest(token, WHATSAPP_VERIFY_TOKEN):
        raise HTTPException(403, "Webhook verification failed")
    if challenge is None:
        raise HTTPException(400, "Webhook challenge is missing")
    return challenge


@app.post("/api/webhooks/whatsapp")
async def receive_whatsapp_webhook(request: Request):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > 1_000_000:
                raise HTTPException(413, "WhatsApp webhook payload is too large")
        except ValueError as exc:
            raise HTTPException(400, "Invalid Content-Length header") from exc
    raw_body = await request.body()
    if len(raw_body) > 1_000_000:
        raise HTTPException(413, "WhatsApp webhook payload is too large")
    try:
        if not verify_webhook_signature(
            raw_body,
            request.headers.get("x-hub-signature-256", ""),
            META_APP_SECRET,
        ):
            raise HTTPException(401, "Invalid WhatsApp webhook signature")
    except WhatsAppConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    try:
        payload = json.loads(raw_body)
        events = extract_webhook_events(payload)
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc

    inserted = 0
    updated = 0
    with connect() as conn:
        for message in events["messages"]:
            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO whatsapp_messages
                    (provider_message_id,direction,contact,body,message_type,status,provider_timestamp)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    message["provider_message_id"],
                    message["direction"],
                    message["contact"],
                    message["body"],
                    message["message_type"],
                    message["status"],
                    message["provider_timestamp"],
                ),
            )
            inserted += int(conn.total_changes > before)
        for status in events["statuses"]:
            cursor = conn.execute(
                """
                UPDATE whatsapp_messages
                SET status=?, provider_timestamp=?,
                    contact=CASE WHEN contact='' THEN ? ELSE contact END
                WHERE provider_message_id=? AND direction='outbound'
                """,
                (
                    status["status"],
                    status["provider_timestamp"],
                    status["contact"],
                    status["provider_message_id"],
                ),
            )
            updated += cursor.rowcount
        conn.execute(
            "INSERT INTO audit_log(action,entity_type,details) VALUES(?,?,?)",
            (
                "whatsapp.webhook_processed",
                "whatsapp_message",
                f"Stored {inserted} inbound messages; updated {updated} delivery statuses",
            ),
        )
    return {
        "received_messages": len(events["messages"]),
        "stored_messages": inserted,
        "received_statuses": len(events["statuses"]),
        "updated_statuses": updated,
    }


@app.get("/api/whatsapp/messages")
def list_whatsapp_messages(limit: int = 100):
    if not 1 <= limit <= 200:
        raise HTTPException(422, "Message history limit must be between 1 and 200")
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM whatsapp_messages ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/whatsapp/messages", status_code=201)
def send_whatsapp_message(payload: WhatsAppMessageCreate):
    if not payload.consent_confirmed:
        raise HTTPException(
            422,
            "Confirm the recipient opted in to receive this WhatsApp message",
        )
    try:
        provider_message_id = send_text_message(payload.recipient, payload.message)
    except WhatsAppConfigurationError as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except WhatsAppProviderError as exc:
        raise HTTPException(502, str(exc)) from exc

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO whatsapp_messages
                (provider_message_id,direction,contact,body,message_type,status)
            VALUES(?, 'outbound', ?, ?, 'text', 'accepted')
            ON CONFLICT(provider_message_id) DO NOTHING
            """,
            (provider_message_id, payload.recipient, payload.message),
        )
        conn.execute(
            "INSERT INTO audit_log(action,entity_type,entity_id,details) VALUES(?,?,?,?)",
            (
                "whatsapp.message_sent",
                "whatsapp_message",
                provider_message_id,
                f"User-initiated text message to {payload.recipient}",
            ),
        )
        row = conn.execute(
            "SELECT * FROM whatsapp_messages WHERE provider_message_id=?",
            (provider_message_id,),
        ).fetchone()
    return dict(row)


@app.post("/api/assistant/chat")
def assistant_chat(payload: ChatRequest):
    if not LLM_API_KEY:
        raise HTTPException(503, "Configure LLM_API_KEY to use assistant chat")

    search_results = []
    if payload.web_search:
        if not TAVILY_API_KEY:
            raise HTTPException(503, "Configure TAVILY_API_KEY to use web search")
        try:
            search_results = search_web(payload.message.strip())
        except (ValueError, RequestException) as exc:
            raise HTTPException(502, f"Web search failed: {str(exc)[:200]}") from exc

    with connect() as conn:
        history = conn.execute(
            "SELECT role,content FROM assistant_messages ORDER BY id DESC LIMIT 20"
        ).fetchall()
    messages = [
        {"role": row["role"], "content": row["content"]}
        for row in reversed(history)
    ]
    messages.append({"role": "user", "content": payload.message.strip()})

    try:
        response = generate_chat_response(messages, search_results)
    except (ValueError, RequestException) as exc:
        raise HTTPException(502, f"Configured AI provider failed: {str(exc)[:200]}") from exc

    tool_calls = response.get("tool_calls") or []
    created_task = None
    if tool_calls:
        if len(tool_calls) != 1 or not isinstance(tool_calls[0], dict):
            raise HTTPException(502, "The AI provider returned unsupported actions")
        function = tool_calls[0].get("function")
        if not isinstance(function, dict) or function.get("name") != "create_monitoring_task":
            raise HTTPException(502, "The AI provider requested an unsupported action")
        try:
            arguments = json.loads(function.get("arguments", ""))
            if not isinstance(arguments, dict):
                raise ValueError("Task arguments must be an object")
            task_payload = TaskCreate(**arguments)
        except (TypeError, ValueError) as exc:
            raise HTTPException(502, "The AI provider returned invalid task details") from exc
        created_task = _create_task(task_payload)
        reply = (
            f"Created task '{created_task['title']}' for "
            f"{created_task['location'] or 'any location'}. "
            f"Keywords: {', '.join(created_task['keywords']) or 'default matching rules'}. "
            "It is saved for manual discovery and has not been run."
        )
    else:
        reply = response.get("content")
        if not isinstance(reply, str) or not reply.strip():
            raise HTTPException(502, "The AI provider returned an empty chat response")
        reply = reply.strip()
    if search_results:
        reply += "\n\nSources:\n" + "\n".join(
            f"- {result['title']}: {result['url']}" for result in search_results
        )

    with connect() as conn:
        conn.execute(
            "INSERT INTO assistant_messages(role,content) VALUES('user',?)",
            (payload.message.strip(),),
        )
        conn.execute(
            "INSERT INTO assistant_messages(role,content) VALUES('assistant',?)",
            (reply,),
        )
    return {
        "reply": reply,
        "sources": search_results,
        "created_task": created_task,
    }


@app.get("/api/preferences")
def get_preferences():
    with connect() as conn:
        rows = conn.execute("SELECT key, value FROM preferences").fetchall()
    saved = {row["key"]: json.loads(row["value"]) for row in rows}
    return PREFERENCE_DEFAULTS | saved


@app.put("/api/preferences")
def update_preferences(payload: PreferencesUpdate):
    data = payload.model_dump(exclude_unset=True)
    with connect() as conn:
        for key, value in data.items():
            conn.execute(
                "INSERT INTO preferences(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )
        conn.execute(
            "INSERT INTO audit_log(action, entity_type, details) VALUES(?,?,?)",
            ("preferences.updated", "preferences", ",".join(data)),
        )
    return get_preferences()


@app.get("/api/memory")
def list_memory():
    with connect() as conn:
        rows = conn.execute("SELECT key, value, updated_at FROM memories ORDER BY key").fetchall()
    return [dict(row) for row in rows]


@app.put("/api/memory")
def save_memory(payload: MemoryCreate):
    key = payload.key.strip()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO memories(key,value,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP
            """,
            (key, payload.value.strip()),
        )
        conn.execute(
            "INSERT INTO audit_log(action, entity_type, entity_id) VALUES(?,?,?)",
            ("memory.saved", "memory", key),
        )
        row = conn.execute("SELECT key,value,updated_at FROM memories WHERE key=?", (key,)).fetchone()
    return dict(row)


@app.delete("/api/memory/{key}")
def delete_memory(key: str):
    with connect() as conn:
        cursor = conn.execute("DELETE FROM memories WHERE key=?", (key,))
        if not cursor.rowcount:
            raise HTTPException(404, "Memory not found")
        conn.execute(
            "INSERT INTO audit_log(action, entity_type, entity_id) VALUES(?,?,?)",
            ("memory.deleted", "memory", key),
        )
    return {"deleted": True}


@app.get("/api/briefing")
def get_briefing():
    with connect() as conn:
        state = conn.execute(
            "SELECT value FROM system_state WHERE key='emergency_stop'"
        ).fetchone()
        new_findings = conn.execute(
            """
            SELECT id,title,company,location,score,source,discovered_at,status
            FROM findings WHERE status='new' ORDER BY score DESC, discovered_at DESC LIMIT 10
            """
        ).fetchall()
        failed_runs = conn.execute(
            """
            SELECT runs.id,runs.task_id,tasks.title,runs.message,runs.created_at
            FROM runs JOIN tasks ON tasks.id=runs.task_id
            WHERE runs.status='failed' ORDER BY runs.id DESC LIMIT 5
            """
        ).fetchall()
        pending = conn.execute("SELECT COUNT(*) AS n FROM findings WHERE status='new'").fetchone()["n"]
        source = conn.execute("SELECT * FROM source_health ORDER BY source").fetchall()
        last_check = conn.execute("SELECT MAX(last_run_at) AS at FROM tasks").fetchone()["at"]
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "last_discovery_at": last_check,
        "new_findings": [dict(row) for row in new_findings],
        "failed_runs": [dict(row) for row in failed_runs],
        "awaiting_review": pending,
        "source_health": [dict(row) for row in source],
        "emergency_stop": bool(state and state["value"] == "true"),
    }


@app.get("/api/integrations")
def list_integrations():
    with connect() as conn:
        health_rows = conn.execute("SELECT * FROM source_health ORDER BY source").fetchall()
    sources = {row["source"]: dict(row) for row in health_rows}
    google_status = google_workspace.connection_status()
    instagram_status = instagram.connection_status()
    return {
        "application_api_authentication": {
            "configured": bool(APP_API_TOKEN),
            "status": "enabled" if APP_API_TOKEN else "disabled",
        },
        "language_model": {
            "configured": bool(LLM_API_KEY),
            "model": LLM_MODEL if LLM_API_KEY else None,
            "status": "configured_not_verified" if LLM_API_KEY else "not_configured",
        },
        "web_search": {
            "configured": bool(TAVILY_API_KEY),
            "provider": "Tavily",
            "status": "configured_not_verified" if TAVILY_API_KEY else "not_configured",
        },
        "google_workspace": {
            **google_status,
            "scopes": list(GOOGLE_SCOPES),
        },
        "instagram": instagram_status,
        "scheduler": {
            "configured": ENABLE_SCHEDULER,
            "status": "enabled" if ENABLE_SCHEDULER else "disabled",
            "scan_interval_minutes": POLL_INTERVAL_MINUTES,
            "owner": "backend",
        },
        "sources": [
            {
                "name": "Arbeitnow",
                "type": "public job API",
                "status": sources.get("Arbeitnow", {}).get("status", "not_checked"),
                "last_checked_at": sources.get("Arbeitnow", {}).get("last_checked_at"),
                "details": sources.get("Arbeitnow", {}).get("details", ""),
            },
            {"name": "Zoho", "status": "not_connected", "details": "OAuth credentials required"},
            {"name": "Email", "status": "not_connected", "details": "Provider OAuth required"},
            {
                "name": "Google Workspace",
                "status": google_status["status"],
                "details": google_status["details"],
            },
            {
                "name": "WhatsApp",
                "status": (
                    "configured_not_verified"
                    if WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID
                    else "not_connected"
                ),
                "details": (
                    f"Cloud API {META_GRAPH_API_VERSION}; "
                    f"webhook {'configured' if WHATSAPP_VERIFY_TOKEN and META_APP_SECRET else 'not configured'}"
                    if WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID
                    else "WhatsApp Cloud API credentials required"
                ),
            },
            {
                "name": "Instagram",
                "status": instagram_status["status"],
                "details": instagram_status["details"],
            },
            {"name": "LinkedIn", "status": "not_connected", "details": "Use authorized APIs or alerts"},
            *[
                {
                    "name": f"RSS: {row['name']}",
                    "task_title": row["task_title"],
                    "status": row["health_status"] or "configured_not_checked",
                    "last_checked_at": row["last_checked_at"],
                    "details": row["health_details"] or row["url"],
                }
                for row in list_task_sources()
            ],
        ],
    }


@app.get("/api/system")
def get_system_state():
    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM system_state WHERE key='emergency_stop'"
        ).fetchone()
    return {"emergency_stop": bool(row and row["value"] == "true")}


@app.post("/api/system/{action}")
def system_action(action: str):
    if action not in {"stop", "resume"}:
        raise HTTPException(404, "Unknown system action")
    stopped = "true" if action == "stop" else "false"
    with connect() as conn:
        conn.execute(
            "UPDATE system_state SET value=? WHERE key='emergency_stop'", (stopped,)
        )
        conn.execute(
            "INSERT INTO audit_log(action, entity_type, details) VALUES(?,?,?)",
            (f"system.{action}", "system", "Global discovery execution control"),
        )
    return {"emergency_stop": stopped == "true"}


@app.get("/api/export")
def export_data():
    with connect() as conn:
        tables = {
            "tasks": "SELECT * FROM tasks ORDER BY id",
            "task_sources": "SELECT * FROM task_sources ORDER BY id",
            "findings": "SELECT * FROM findings ORDER BY id",
            "runs": "SELECT * FROM runs ORDER BY id",
            "preferences": "SELECT key,value FROM preferences ORDER BY key",
            "memories": "SELECT key,value,updated_at FROM memories ORDER BY key",
            "assistant_messages": "SELECT role,content,created_at FROM assistant_messages ORDER BY id",
            "whatsapp_messages": "SELECT * FROM whatsapp_messages ORDER BY id",
            "source_health": "SELECT * FROM source_health ORDER BY source",
            "system_state": "SELECT key,value FROM system_state ORDER BY key",
            "audit_log": "SELECT * FROM audit_log ORDER BY id",
        }
        snapshot = {
            table: [dict(row) for row in conn.execute(query).fetchall()]
            for table, query in tables.items()
        }
    return {
        "format": "jarvis-data-export",
        "version": 1,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "data": snapshot,
    }


@app.get("/api/audit")
def list_audit_log():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 200").fetchall()
    return [dict(row) for row in rows]


@app.post("/api/n8n/findings")
def import_n8n_findings(payload: N8nFindingBatch, authorization: str | None = Header(default=None)):
    if not APP_API_TOKEN:
        raise HTTPException(503, "Configure APP_API_TOKEN before enabling n8n ingestion")
    if not authorization or not secrets.compare_digest(authorization, f"Bearer {APP_API_TOKEN}"):
        raise HTTPException(401, "A valid bearer token is required")

    inserted = 0
    with connect() as conn:
        for finding in payload.findings:
            task = conn.execute("SELECT id FROM tasks WHERE id=?", (finding.task_id,)).fetchone()
            if not task:
                raise HTTPException(404, f"Task {finding.task_id} not found")
            parsed = urlparse(finding.url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise HTTPException(422, "Finding URL must be an absolute HTTP or HTTPS URL")
            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO findings
                    (task_id,source,source_id,title,company,location,url,summary,score)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    finding.task_id,
                    finding.source,
                    finding.source_id,
                    finding.title,
                    finding.company,
                    finding.location,
                    finding.url,
                    finding.summary,
                    finding.score,
                ),
            )
            inserted += int(conn.total_changes > before)
        conn.execute(
            "INSERT INTO audit_log(action, entity_type, details) VALUES(?,?,?)",
            ("n8n.findings_imported", "finding", f"Inserted {inserted} of {len(payload.findings)}"),
        )
    return {"received": len(payload.findings), "inserted": inserted, "duplicates": len(payload.findings) - inserted}
