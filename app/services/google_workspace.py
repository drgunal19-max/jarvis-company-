import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken

from app.config import (
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_REDIRECT_URI,
    GOOGLE_TOKEN_ENCRYPTION_KEY,
)
from app.database import connect

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API_URL = "https://gmail.googleapis.com/gmail/v1"
CALENDAR_API_URL = "https://www.googleapis.com/calendar/v3"
DRIVE_API_URL = "https://www.googleapis.com/drive/v3"
GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
)


class GoogleConfigurationError(RuntimeError):
    pass


class GoogleNotConnectedError(RuntimeError):
    pass


class GoogleReauthorizationRequired(RuntimeError):
    pass


class GoogleProviderError(RuntimeError):
    pass


def _fernet() -> Fernet:
    if not GOOGLE_TOKEN_ENCRYPTION_KEY:
        raise GoogleConfigurationError(
            "Configure GOOGLE_TOKEN_ENCRYPTION_KEY before connecting Google"
        )
    try:
        return Fernet(GOOGLE_TOKEN_ENCRYPTION_KEY.encode("ascii"))
    except (TypeError, ValueError) as exc:
        raise GoogleConfigurationError(
            "GOOGLE_TOKEN_ENCRYPTION_KEY must be a valid Fernet key"
        ) from exc


def connection_status() -> dict[str, str | bool]:
    configured = bool(
        GOOGLE_CLIENT_ID
        and GOOGLE_CLIENT_SECRET
        and GOOGLE_REDIRECT_URI
        and GOOGLE_TOKEN_ENCRYPTION_KEY
    )
    with connect() as conn:
        row = conn.execute("SELECT id FROM google_oauth_tokens WHERE id=1").fetchone()
    connected = bool(row and configured)
    if connected:
        status = "connected_not_verified"
        details = "Read-only Gmail, Calendar, and Drive metadata scopes are authorized"
    elif configured:
        status = "ready_to_connect"
        details = "OAuth is configured; connect a Google account to continue"
    else:
        status = "not_connected"
        details = "Google OAuth client and token-encryption key are required"
    return {"configured": configured, "connected": connected, "status": status, "details": details}


def authorization_url(state: str) -> str:
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not GOOGLE_REDIRECT_URI:
        raise GoogleConfigurationError(
            "Configure GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_REDIRECT_URI"
        )
    _fernet()
    return GOOGLE_AUTHORIZE_URL + "?" + urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    })


def _stored_tokens() -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT access_token,refresh_token,expires_at,scopes "
            "FROM google_oauth_tokens WHERE id=1"
        ).fetchone()
    return dict(row) if row else None


def _encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(value: str) -> str:
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise GoogleConfigurationError(
            "Could not decrypt stored Google tokens; check GOOGLE_TOKEN_ENCRYPTION_KEY"
        ) from exc


def _save_tokens(access_token: str, refresh_token: str, expires_at: int, scopes: str):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO google_oauth_tokens(id,access_token,refresh_token,expires_at,scopes)
            VALUES(1,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                access_token=excluded.access_token,
                refresh_token=excluded.refresh_token,
                expires_at=excluded.expires_at,
                scopes=excluded.scopes,
                updated_at=CURRENT_TIMESTAMP
            """,
            (_encrypt(access_token), _encrypt(refresh_token), expires_at, scopes),
        )


def _exchange_token(data: dict) -> dict:
    try:
        response = requests.post(GOOGLE_TOKEN_URL, data=data, timeout=20)
    except requests.RequestException as exc:
        raise GoogleProviderError("Could not reach Google's OAuth service") from exc
    if not response.ok:
        raise GoogleProviderError(
            f"Google OAuth rejected the request (HTTP {response.status_code})"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise GoogleProviderError("Google OAuth returned an invalid response") from exc
    if not isinstance(payload, dict):
        raise GoogleProviderError("Google OAuth returned an invalid response")
    return payload


def exchange_authorization_code(code: str):
    _fernet()
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET or not GOOGLE_REDIRECT_URI:
        raise GoogleConfigurationError("Google OAuth client settings are incomplete")
    existing = _stored_tokens()
    payload = _exchange_token({
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "grant_type": "authorization_code",
    })
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token") or (
        _decrypt(existing["refresh_token"]) if existing else None
    )
    expires_in = payload.get("expires_in")
    if (
        not isinstance(access_token, str)
        or not access_token
        or not isinstance(refresh_token, str)
        or not refresh_token
        or type(expires_in) is not int
        or expires_in <= 0
    ):
        raise GoogleProviderError(
            "Google did not return a usable refresh token; revoke access and reconnect"
        )
    scopes = payload.get("scope")
    if not isinstance(scopes, str) or not scopes:
        scopes = " ".join(GOOGLE_SCOPES)
    if not set(GOOGLE_SCOPES).issubset(scopes.split()):
        raise GoogleProviderError(
            "Google authorization did not grant all requested read-only scopes"
        )
    _save_tokens(access_token, refresh_token, int(time.time()) + expires_in, scopes)


def disconnect():
    with connect() as conn:
        conn.execute("DELETE FROM google_oauth_tokens WHERE id=1")


def _refresh_access_token(row: dict) -> str:
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise GoogleConfigurationError("Google OAuth client settings are incomplete")
    refresh_token = _decrypt(row["refresh_token"])
    payload = _exchange_token({
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    access_token = payload.get("access_token")
    expires_in = payload.get("expires_in")
    if (
        not isinstance(access_token, str)
        or not access_token
        or type(expires_in) is not int
        or expires_in <= 0
    ):
        raise GoogleProviderError("Google returned an invalid token refresh response")
    scopes = payload.get("scope")
    if not isinstance(scopes, str) or not scopes:
        scopes = row["scopes"]
    _save_tokens(
        access_token,
        refresh_token,
        int(time.time()) + expires_in,
        scopes,
    )
    return access_token


def _access_token() -> str:
    row = _stored_tokens()
    if not row:
        raise GoogleNotConnectedError("Connect a Google account before using Workspace features")
    if int(row["expires_at"]) <= int(time.time()) + 60:
        return _refresh_access_token(row)
    return _decrypt(row["access_token"])


def _get_json(url: str, access_token: str, params: dict | list[tuple[str, str]]) -> dict:
    try:
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
            timeout=20,
        )
    except requests.RequestException as exc:
        raise GoogleProviderError("Could not reach a Google Workspace API") from exc
    if not response.ok:
        if response.status_code == 401:
            raise GoogleReauthorizationRequired(
                "Google rejected the saved authorization; reconnect the account"
            )
        raise GoogleProviderError(
            f"Google Workspace API rejected the request (HTTP {response.status_code})"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise GoogleProviderError("Google Workspace API returned an invalid response") from exc
    if not isinstance(payload, dict):
        raise GoogleProviderError("Google Workspace API returned an invalid response")
    return payload


def search_gmail(query: str = "") -> list[dict[str, str]]:
    access_token = _access_token()
    params: dict[str, str | int] = {"maxResults": 10}
    if query.strip():
        params["q"] = query.strip()[:200]
    listing = _get_json(
        f"{GMAIL_API_URL}/users/me/messages",
        access_token,
        params,
    )
    messages = listing.get("messages", [])
    if not isinstance(messages, list):
        raise GoogleProviderError("Gmail returned an invalid message list")

    results = []
    for item in messages[:10]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        details = _get_json(
            f"{GMAIL_API_URL}/users/me/messages/{item['id']}",
            access_token,
            [
                ("format", "metadata"),
                ("metadataHeaders", "Subject"),
                ("metadataHeaders", "From"),
                ("metadataHeaders", "Date"),
            ],
        )
        payload = details.get("payload", {})
        headers = payload.get("headers", []) if isinstance(payload, dict) else []
        values = {
            header.get("name", "").casefold(): header.get("value", "")
            for header in headers
            if isinstance(header, dict)
            and isinstance(header.get("name"), str)
            and isinstance(header.get("value"), str)
        } if isinstance(headers, list) else {}
        message_id = details.get("id", item["id"])
        results.append({
            "id": message_id if isinstance(message_id, str) else item["id"],
            "subject": values.get("subject", "(No subject)")[:500],
            "sender": values.get("from", "")[:500],
            "date": values.get("date", "")[:200],
            "url": f"https://mail.google.com/mail/u/0/#all/{item['id']}",
        })
    return results


def list_calendar_events() -> list[dict[str, str]]:
    now = datetime.now(timezone.utc)
    listing = _get_json(
        f"{CALENDAR_API_URL}/calendars/primary/events",
        _access_token(),
        {
            "timeMin": now.isoformat().replace("+00:00", "Z"),
            "timeMax": (now + timedelta(days=30)).isoformat().replace("+00:00", "Z"),
            "maxResults": 20,
            "singleEvents": "true",
            "orderBy": "startTime",
        },
    )
    events = listing.get("items", [])
    if not isinstance(events, list):
        raise GoogleProviderError("Google Calendar returned an invalid event list")
    results = []
    for event in events[:20]:
        if not isinstance(event, dict):
            continue
        start = event.get("start", {})
        end = event.get("end", {})
        results.append({
            "summary": str(event.get("summary") or "(No title)")[:500],
            "start": str(start.get("dateTime") or start.get("date") or "")[:100]
            if isinstance(start, dict) else "",
            "end": str(end.get("dateTime") or end.get("date") or "")[:100]
            if isinstance(end, dict) else "",
            "location": str(event.get("location") or "")[:500],
            "url": str(event.get("htmlLink") or "")[:2000],
        })
    return results


def search_drive(query: str = "") -> list[dict[str, str]]:
    escaped_query = query.strip()[:120].replace("\\", "\\\\").replace("'", "\\'")
    filters = ["trashed = false"]
    if escaped_query:
        filters.append(f"name contains '{escaped_query}'")
    listing = _get_json(
        f"{DRIVE_API_URL}/files",
        _access_token(),
        {
            "q": " and ".join(filters),
            "pageSize": 20,
            "orderBy": "modifiedTime desc",
            "fields": "files(id,name,mimeType,webViewLink,modifiedTime)",
        },
    )
    files = listing.get("files", [])
    if not isinstance(files, list):
        raise GoogleProviderError("Google Drive returned an invalid file list")
    return [
        {
            "id": str(file.get("id", ""))[:300],
            "name": str(file.get("name") or "(Unnamed file)")[:500],
            "type": str(file.get("mimeType") or "")[:200],
            "modified": str(file.get("modifiedTime") or "")[:100],
            "url": str(file.get("webViewLink") or "")[:2000],
        }
        for file in files[:20]
        if isinstance(file, dict)
    ]
