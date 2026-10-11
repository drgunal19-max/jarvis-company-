import time
from urllib.parse import urlencode

import requests
from cryptography.fernet import Fernet, InvalidToken

from app.config import (
    INSTAGRAM_APP_ID,
    INSTAGRAM_APP_SECRET,
    INSTAGRAM_GRAPH_API_VERSION,
    INSTAGRAM_REDIRECT_URI,
    INSTAGRAM_TOKEN_ENCRYPTION_KEY,
)
from app.database import connect

INSTAGRAM_AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
INSTAGRAM_TOKEN_URL = "https://api.instagram.com/oauth/access_token"
INSTAGRAM_GRAPH_URL = f"https://graph.instagram.com/{INSTAGRAM_GRAPH_API_VERSION}"
INSTAGRAM_SCOPE = "instagram_business_basic"


class InstagramConfigurationError(RuntimeError):
    pass


class InstagramNotConnectedError(RuntimeError):
    pass


class InstagramReauthorizationRequired(RuntimeError):
    pass


class InstagramProviderError(RuntimeError):
    pass


def _fernet() -> Fernet:
    if not INSTAGRAM_TOKEN_ENCRYPTION_KEY:
        raise InstagramConfigurationError(
            "Configure INSTAGRAM_TOKEN_ENCRYPTION_KEY before connecting Instagram"
        )
    try:
        return Fernet(INSTAGRAM_TOKEN_ENCRYPTION_KEY.encode("ascii"))
    except (TypeError, ValueError) as exc:
        raise InstagramConfigurationError(
            "INSTAGRAM_TOKEN_ENCRYPTION_KEY must be a valid Fernet key"
        ) from exc


def connection_status() -> dict[str, str | bool]:
    configured = bool(
        INSTAGRAM_APP_ID
        and INSTAGRAM_APP_SECRET
        and INSTAGRAM_REDIRECT_URI
        and INSTAGRAM_TOKEN_ENCRYPTION_KEY
    )
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM instagram_oauth_tokens WHERE id=1"
        ).fetchone()
    connected = bool(row and configured)
    if connected:
        status = "connected_not_verified"
        details = "Instagram professional-account profile and media are authorized read-only"
    elif configured:
        status = "ready_to_connect"
        details = "Instagram Login is configured; connect a professional account"
    else:
        status = "not_connected"
        details = "Instagram Login app credentials and token-encryption key are required"
    return {
        "configured": configured,
        "connected": connected,
        "status": status,
        "details": details,
    }


def authorization_url(state: str) -> str:
    if not INSTAGRAM_APP_ID or not INSTAGRAM_APP_SECRET or not INSTAGRAM_REDIRECT_URI:
        raise InstagramConfigurationError(
            "Configure INSTAGRAM_APP_ID, INSTAGRAM_APP_SECRET, and INSTAGRAM_REDIRECT_URI"
        )
    _fernet()
    return INSTAGRAM_AUTHORIZE_URL + "?" + urlencode({
        "client_id": INSTAGRAM_APP_ID,
        "redirect_uri": INSTAGRAM_REDIRECT_URI,
        "response_type": "code",
        "scope": INSTAGRAM_SCOPE,
        "state": state,
        "enable_fb_login": "false",
        "force_reauth": "true",
    })


def _request_json(url: str, *, params: dict | None = None) -> dict:
    try:
        response = requests.get(url, params=params, timeout=20)
    except requests.RequestException as exc:
        raise InstagramProviderError("Could not reach the Instagram API") from exc
    if not response.ok:
        if response.status_code == 401:
            raise InstagramReauthorizationRequired(
                "Instagram rejected the saved authorization; reconnect the account"
            )
        raise InstagramProviderError(
            f"Instagram API rejected the request (HTTP {response.status_code})"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise InstagramProviderError("Instagram API returned an invalid response") from exc
    if not isinstance(payload, dict):
        raise InstagramProviderError("Instagram API returned an invalid response")
    return payload


def _exchange_code(code: str) -> dict:
    try:
        response = requests.post(
            INSTAGRAM_TOKEN_URL,
            data={
                "client_id": INSTAGRAM_APP_ID,
                "client_secret": INSTAGRAM_APP_SECRET,
                "grant_type": "authorization_code",
                "redirect_uri": INSTAGRAM_REDIRECT_URI,
                "code": code,
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        raise InstagramProviderError("Could not reach Instagram Login") from exc
    if not response.ok:
        raise InstagramProviderError(
            f"Instagram Login rejected the request (HTTP {response.status_code})"
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise InstagramProviderError("Instagram Login returned an invalid response") from exc
    if not isinstance(payload, dict):
        raise InstagramProviderError("Instagram Login returned an invalid response")
    data = payload.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        payload = data[0]
    return payload


def _stored_token() -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT access_token,expires_at,scopes FROM instagram_oauth_tokens WHERE id=1"
        ).fetchone()
    return dict(row) if row else None


def _encrypt(token: str) -> str:
    return _fernet().encrypt(token.encode("utf-8")).decode("ascii")


def _decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError) as exc:
        raise InstagramConfigurationError(
            "Could not decrypt the stored Instagram token; check INSTAGRAM_TOKEN_ENCRYPTION_KEY"
        ) from exc


def _save_token(token: str, expires_at: int):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO instagram_oauth_tokens(id,access_token,expires_at,scopes)
            VALUES(1,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                access_token=excluded.access_token,
                expires_at=excluded.expires_at,
                scopes=excluded.scopes,
                updated_at=CURRENT_TIMESTAMP
            """,
            (_encrypt(token), expires_at, INSTAGRAM_SCOPE),
        )


def exchange_authorization_code(code: str):
    _fernet()
    if not INSTAGRAM_APP_ID or not INSTAGRAM_APP_SECRET or not INSTAGRAM_REDIRECT_URI:
        raise InstagramConfigurationError("Instagram Login settings are incomplete")
    short_lived = _exchange_code(code)
    token = short_lived.get("access_token")
    if not isinstance(token, str) or not token:
        raise InstagramProviderError("Instagram did not return an access token")
    permissions = short_lived.get("permissions", "")
    granted = (
        permissions.split(",")
        if isinstance(permissions, str)
        else permissions if isinstance(permissions, list) else []
    )
    if INSTAGRAM_SCOPE not in granted:
        raise InstagramProviderError(
            "Instagram authorization did not grant the required read-only permission"
        )
    long_lived = _request_json(
        "https://graph.instagram.com/access_token",
        params={
            "grant_type": "ig_exchange_token",
            "client_secret": INSTAGRAM_APP_SECRET,
            "access_token": token,
        },
    )
    token = long_lived.get("access_token")
    expires_in = long_lived.get("expires_in")
    if (
        not isinstance(token, str)
        or not token
        or type(expires_in) is not int
        or expires_in <= 0
    ):
        raise InstagramProviderError(
            "Instagram did not return a usable long-lived access token"
        )
    _save_token(token, int(time.time()) + expires_in)


def disconnect():
    with connect() as conn:
        conn.execute("DELETE FROM instagram_oauth_tokens WHERE id=1")


def _refresh_access_token(token: str) -> str:
    payload = _request_json(
        "https://graph.instagram.com/refresh_access_token",
        params={"grant_type": "ig_refresh_token", "access_token": token},
    )
    refreshed = payload.get("access_token")
    expires_in = payload.get("expires_in")
    if (
        not isinstance(refreshed, str)
        or not refreshed
        or type(expires_in) is not int
        or expires_in <= 0
    ):
        raise InstagramProviderError("Instagram returned an invalid token refresh response")
    _save_token(refreshed, int(time.time()) + expires_in)
    return refreshed


def _access_token() -> str:
    row = _stored_token()
    if not row:
        raise InstagramNotConnectedError(
            "Connect a professional Instagram account before using Instagram features"
        )
    token = _decrypt(row["access_token"])
    if int(row["expires_at"]) <= int(time.time()) + 7 * 24 * 60 * 60:
        return _refresh_access_token(token)
    return token


def get_profile() -> dict[str, str | int]:
    payload = _request_json(
        f"{INSTAGRAM_GRAPH_URL}/me",
        params={
            "fields": "id,user_id,username,account_type,media_count",
            "access_token": _access_token(),
        },
    )
    data = payload.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        payload = data[0]
    profile: dict[str, str | int] = {}
    for field in ("id", "user_id", "username", "account_type", "media_count"):
        value = payload.get(field)
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            profile[field] = value
    return profile


def list_media() -> list[dict[str, str]]:
    access_token = _access_token()
    profile = _request_json(
        f"{INSTAGRAM_GRAPH_URL}/me",
        params={"fields": "user_id", "access_token": access_token},
    )
    profile_data = profile.get("data")
    if isinstance(profile_data, list) and profile_data and isinstance(profile_data[0], dict):
        profile = profile_data[0]
    user_id = profile.get("user_id")
    if not isinstance(user_id, str) or not user_id.isdigit() or len(user_id) > 100:
        raise InstagramProviderError("Instagram returned an invalid professional account ID")
    payload = _request_json(
        f"{INSTAGRAM_GRAPH_URL}/{user_id}/media",
        params={
            "fields": "id,media_type,permalink,timestamp",
            "limit": 12,
            "access_token": access_token,
        },
    )
    items = payload.get("data", [])
    if not isinstance(items, list):
        raise InstagramProviderError("Instagram returned an invalid media list")
    return [
        {
            "id": str(item.get("id", ""))[:300],
            "media_type": str(item.get("media_type") or "")[:40],
            "permalink": str(item.get("permalink") or "")[:2000],
            "timestamp": str(item.get("timestamp") or "")[:100],
        }
        for item in items[:12]
        if isinstance(item, dict)
    ]
