import hashlib
import hmac
import re

import requests

from app.config import (
    META_GRAPH_API_VERSION,
    WHATSAPP_ACCESS_TOKEN,
    WHATSAPP_PHONE_NUMBER_ID,
)

GRAPH_API_URL = "https://graph.facebook.com"
PHONE_NUMBER_PATTERN = re.compile(r"^\+?[1-9]\d{7,14}$")
GRAPH_VERSION_PATTERN = re.compile(r"^v\d+\.\d+$")
MAX_MESSAGE_LENGTH = 4096


class WhatsAppConfigurationError(RuntimeError):
    pass


class WhatsAppProviderError(RuntimeError):
    pass


def send_text_message(recipient: str, message: str) -> str:
    if not WHATSAPP_ACCESS_TOKEN or not WHATSAPP_PHONE_NUMBER_ID:
        raise WhatsAppConfigurationError(
            "Configure WhatsApp access token and phone number ID before sending"
        )
    if not WHATSAPP_PHONE_NUMBER_ID.isdigit():
        raise WhatsAppConfigurationError("WhatsApp phone number ID must be numeric")
    if not GRAPH_VERSION_PATTERN.fullmatch(META_GRAPH_API_VERSION):
        raise WhatsAppConfigurationError("META_GRAPH_API_VERSION has an invalid format")
    if not PHONE_NUMBER_PATTERN.fullmatch(recipient):
        raise ValueError("Recipient must be an international phone number in E.164 format")
    if not message.strip() or len(message) > MAX_MESSAGE_LENGTH:
        raise ValueError("Message must contain 1 to 4096 characters")

    url = (
        f"{GRAPH_API_URL}/{META_GRAPH_API_VERSION}/"
        f"{WHATSAPP_PHONE_NUMBER_ID}/messages"
    )
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"},
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient.removeprefix("+"),
                "type": "text",
                "text": {"preview_url": False, "body": message},
            },
            timeout=20,
        )
    except requests.RequestException as exc:
        raise WhatsAppProviderError("Could not reach the WhatsApp Cloud API") from exc

    if not response.ok:
        try:
            payload = response.json()
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            error_code = error.get("code") if isinstance(error, dict) else None
        except (TypeError, ValueError):
            error_code = None
        suffix = (
            f", provider code {error_code}"
            if isinstance(error_code, (int, str)) and error_code
            else ""
        )
        raise WhatsAppProviderError(
            f"WhatsApp Cloud API rejected the message (HTTP {response.status_code}{suffix})"
        )

    try:
        message_id = response.json()["messages"][0]["id"]
    except (IndexError, KeyError, TypeError, ValueError) as exc:
        raise WhatsAppProviderError(
            "WhatsApp Cloud API returned an invalid message response"
        ) from exc
    if not isinstance(message_id, str) or not message_id:
        raise WhatsAppProviderError(
            "WhatsApp Cloud API returned an invalid message response"
        )
    return message_id


def verify_webhook_signature(raw_body: bytes, signature: str, app_secret: str) -> bool:
    if not app_secret:
        raise WhatsAppConfigurationError("Configure META_APP_SECRET to accept WhatsApp webhooks")
    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def _contact_number(value: object) -> str:
    if not isinstance(value, str) or not PHONE_NUMBER_PATTERN.fullmatch(value):
        raise ValueError("WhatsApp webhook contained an invalid contact number")
    return value if value.startswith("+") else f"+{value}"


def _message_text(message: dict) -> str:
    kind = message.get("type")
    if kind == "text":
        content = message.get("text")
        value = content.get("body") if isinstance(content, dict) else ""
    elif kind == "button":
        content = message.get("button")
        value = content.get("text") if isinstance(content, dict) else ""
    elif kind == "interactive":
        content = message.get("interactive")
        value = ""
        if isinstance(content, dict):
            reply = content.get("button_reply") or content.get("list_reply")
            if isinstance(reply, dict):
                value = reply.get("title", "")
    else:
        value = ""
    if not isinstance(value, str):
        raise ValueError("WhatsApp webhook contained invalid message text")
    return value[:MAX_MESSAGE_LENGTH]


def extract_webhook_events(payload: object) -> dict[str, list[dict[str, str]]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("entry"), list):
        raise ValueError("WhatsApp webhook payload has an unsupported format")

    messages = []
    statuses = []
    for entry in payload["entry"]:
        if not isinstance(entry, dict):
            raise ValueError("WhatsApp webhook payload has an unsupported format")
        changes = entry.get("changes", [])
        if not isinstance(changes, list):
            raise ValueError("WhatsApp webhook payload has an unsupported format")
        for change in changes:
            if not isinstance(change, dict) or change.get("field") != "messages":
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                raise ValueError("WhatsApp webhook payload has an unsupported format")
            incoming_messages = value.get("messages", [])
            if not isinstance(incoming_messages, list):
                raise ValueError("WhatsApp webhook contained invalid messages")
            for message in incoming_messages:
                if not isinstance(message, dict):
                    raise ValueError("WhatsApp webhook contained an invalid message")
                provider_id = message.get("id")
                message_type = message.get("type")
                timestamp = message.get("timestamp", "")
                if (
                    not isinstance(provider_id, str)
                    or not provider_id
                    or len(provider_id) > 500
                    or not isinstance(message_type, str)
                    or not isinstance(timestamp, str)
                ):
                    raise ValueError("WhatsApp webhook contained an invalid message")
                messages.append({
                    "provider_message_id": provider_id,
                    "direction": "inbound",
                    "contact": _contact_number(message.get("from")),
                    "body": _message_text(message),
                    "message_type": message_type[:50],
                    "status": "received",
                    "provider_timestamp": timestamp[:30],
                })

            message_statuses = value.get("statuses", [])
            if not isinstance(message_statuses, list):
                raise ValueError("WhatsApp webhook contained invalid statuses")
            for status in message_statuses:
                if not isinstance(status, dict):
                    raise ValueError("WhatsApp webhook contained an invalid status")
                provider_id = status.get("id")
                state = status.get("status")
                timestamp = status.get("timestamp", "")
                if (
                    not isinstance(provider_id, str)
                    or not provider_id
                    or len(provider_id) > 500
                    or not isinstance(state, str)
                    or state not in {"sent", "delivered", "read", "failed"}
                    or not isinstance(timestamp, str)
                ):
                    raise ValueError("WhatsApp webhook contained an invalid status")
                recipient = status.get("recipient_id", "")
                if recipient:
                    recipient = _contact_number(recipient)
                statuses.append({
                    "provider_message_id": provider_id,
                    "status": state,
                    "contact": recipient,
                    "provider_timestamp": timestamp[:30],
                })

    return {"messages": messages, "statuses": statuses}
