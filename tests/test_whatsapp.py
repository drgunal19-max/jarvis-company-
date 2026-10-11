import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException

import app.database as database
import app.main as main
from app.schemas import WhatsAppMessageCreate
from app.services import whatsapp


class FakeRequest:
    def __init__(self, body=b"", headers=None, query=None):
        self._body = body
        self.headers = headers or {}
        self.query_params = query or {}

    async def body(self):
        return self._body


class WhatsAppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self.temp_dir.name, "jarvis.db"))
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_sending_uses_cloud_api_and_normalizes_e164_recipient(self):
        response = Mock()
        response.ok = True
        response.json.return_value = {"messages": [{"id": "wamid.outgoing"}]}
        with patch.object(whatsapp, "WHATSAPP_ACCESS_TOKEN", "test-token"), patch.object(
            whatsapp, "WHATSAPP_PHONE_NUMBER_ID", "123456789"
        ), patch.object(whatsapp, "META_GRAPH_API_VERSION", "v23.0"), patch.object(
            whatsapp.requests, "post", return_value=response
        ) as post:
            message_id = whatsapp.send_text_message("+14155552671", "Hello")

        self.assertEqual(message_id, "wamid.outgoing")
        self.assertEqual(
            post.call_args.args[0],
            "https://graph.facebook.com/v23.0/123456789/messages",
        )
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer test-token")
        self.assertEqual(post.call_args.kwargs["json"]["to"], "14155552671")
        self.assertEqual(post.call_args.kwargs["json"]["text"]["body"], "Hello")

    def test_sending_requires_credentials_and_valid_recipient(self):
        with patch.object(whatsapp, "WHATSAPP_ACCESS_TOKEN", ""), patch.object(
            whatsapp, "WHATSAPP_PHONE_NUMBER_ID", ""
        ):
            with self.assertRaises(whatsapp.WhatsAppConfigurationError):
                whatsapp.send_text_message("+14155552671", "Hello")
        with patch.object(whatsapp, "WHATSAPP_ACCESS_TOKEN", "test-token"), patch.object(
            whatsapp, "WHATSAPP_PHONE_NUMBER_ID", "123456789"
        ):
            with self.assertRaises(ValueError):
                whatsapp.send_text_message("not-a-phone", "Hello")

    def test_graph_errors_do_not_expose_provider_response_body(self):
        response = Mock()
        response.ok = False
        response.status_code = 400
        response.json.return_value = {"error": {"code": 131030, "message": "private details"}}
        with patch.object(whatsapp, "WHATSAPP_ACCESS_TOKEN", "test-token"), patch.object(
            whatsapp, "WHATSAPP_PHONE_NUMBER_ID", "123456789"
        ), patch.object(whatsapp.requests, "post", return_value=response):
            with self.assertRaises(whatsapp.WhatsAppProviderError) as raised:
                whatsapp.send_text_message("+14155552671", "Hello")
        self.assertIn("131030", str(raised.exception))
        self.assertNotIn("private details", str(raised.exception))

    def test_webhook_signature_verification(self):
        body = b'{"entry":[]}'
        signature = "sha256=" + hmac.new(
            b"app-secret", body, hashlib.sha256
        ).hexdigest()
        self.assertTrue(whatsapp.verify_webhook_signature(body, signature, "app-secret"))
        self.assertFalse(whatsapp.verify_webhook_signature(body, "sha256=bad", "app-secret"))
        with self.assertRaises(whatsapp.WhatsAppConfigurationError):
            whatsapp.verify_webhook_signature(body, signature, "")

    def test_webhook_events_extract_inbound_text_and_delivery_state(self):
        events = whatsapp.extract_webhook_events({
            "entry": [{
                "changes": [{
                    "field": "messages",
                    "value": {
                        "messages": [{
                            "id": "wamid.inbound",
                            "from": "14155552671",
                            "timestamp": "1760000000",
                            "type": "text",
                            "text": {"body": "Hello"},
                        }],
                        "statuses": [{
                            "id": "wamid.outbound",
                            "recipient_id": "14155552671",
                            "status": "read",
                            "timestamp": "1760000001",
                        }],
                    },
                }],
            }],
        })
        self.assertEqual(events["messages"][0]["contact"], "+14155552671")
        self.assertEqual(events["messages"][0]["body"], "Hello")
        self.assertEqual(events["statuses"][0]["status"], "read")
        with self.assertRaises(ValueError):
            whatsapp.extract_webhook_events({"entry": "invalid"})

    async def test_webhook_get_challenge_requires_verify_token(self):
        request = FakeRequest(query={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-me",
            "hub.challenge": "challenge-value",
        })
        with patch.object(main, "WHATSAPP_VERIFY_TOKEN", "verify-me"):
            self.assertEqual(main.verify_whatsapp_webhook(request), "challenge-value")
        with patch.object(main, "WHATSAPP_VERIFY_TOKEN", "verify-me"):
            with self.assertRaises(HTTPException) as raised:
                main.verify_whatsapp_webhook(FakeRequest(query={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "wrong",
                    "hub.challenge": "challenge-value",
                }))
        self.assertEqual(raised.exception.status_code, 403)

    async def test_signed_webhook_stores_inbound_and_updates_outbound_status_idempotently(self):
        with database.connect() as conn:
            conn.execute(
                """
                INSERT INTO whatsapp_messages
                    (provider_message_id,direction,contact,body,message_type,status)
                VALUES('wamid.outbound','outbound','+14155552671','Hello','text','accepted')
                """
            )
        body = json.dumps({
            "entry": [{
                "changes": [{
                    "field": "messages",
                    "value": {
                        "messages": [{
                            "id": "wamid.inbound",
                            "from": "14155552671",
                            "timestamp": "1760000000",
                            "type": "text",
                            "text": {"body": "Hi there"},
                        }],
                        "statuses": [{
                            "id": "wamid.outbound",
                            "recipient_id": "14155552671",
                            "status": "delivered",
                            "timestamp": "1760000001",
                        }],
                    },
                }],
            }],
        }).encode()
        signature = "sha256=" + hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
        request = FakeRequest(
            body=body,
            headers={
                "content-length": str(len(body)),
                "x-hub-signature-256": signature,
            },
        )
        with patch.object(main, "META_APP_SECRET", "app-secret"):
            first = await main.receive_whatsapp_webhook(request)
            again = await main.receive_whatsapp_webhook(request)

        self.assertEqual(first["stored_messages"], 1)
        self.assertEqual(first["updated_statuses"], 1)
        self.assertEqual(again["stored_messages"], 0)
        with database.connect() as conn:
            inbound = conn.execute(
                "SELECT * FROM whatsapp_messages WHERE provider_message_id='wamid.inbound'"
            ).fetchone()
            outbound = conn.execute(
                "SELECT status FROM whatsapp_messages WHERE provider_message_id='wamid.outbound'"
            ).fetchone()
        self.assertEqual(inbound["body"], "Hi there")
        self.assertEqual(inbound["contact"], "+14155552671")
        self.assertEqual(outbound["status"], "delivered")

    async def test_webhook_rejects_invalid_signature(self):
        body = b'{"entry":[]}'
        with patch.object(main, "META_APP_SECRET", "app-secret"):
            with self.assertRaises(HTTPException) as raised:
                await main.receive_whatsapp_webhook(FakeRequest(
                    body=body,
                    headers={"x-hub-signature-256": "sha256=invalid"},
                ))
        self.assertEqual(raised.exception.status_code, 401)

    def test_outbound_requires_consent_confirmation_and_records_success(self):
        with patch.object(main, "send_text_message") as send:
            with self.assertRaises(HTTPException) as raised:
                main.send_whatsapp_message(WhatsAppMessageCreate(
                    recipient="+14155552671",
                    message="Hello",
                ))
        self.assertEqual(raised.exception.status_code, 422)
        send.assert_not_called()

        with patch.object(main, "send_text_message", return_value="wamid.outbound"):
            saved = main.send_whatsapp_message(WhatsAppMessageCreate(
                recipient="+14155552671",
                message="  Hello  ",
                consent_confirmed=True,
            ))
        self.assertEqual(saved["body"], "Hello")
        self.assertEqual(saved["direction"], "outbound")
        self.assertEqual(saved["status"], "accepted")
        self.assertEqual(main.list_whatsapp_messages()[0]["provider_message_id"], "wamid.outbound")

    def test_message_history_limit_is_bounded(self):
        with self.assertRaises(HTTPException) as raised:
            main.list_whatsapp_messages(limit=201)
        self.assertEqual(raised.exception.status_code, 422)

    def test_integration_status_does_not_claim_credentials_are_verified(self):
        with patch.object(main, "WHATSAPP_ACCESS_TOKEN", "configured"), patch.object(
            main, "WHATSAPP_PHONE_NUMBER_ID", "123456789"
        ), patch.object(main, "WHATSAPP_VERIFY_TOKEN", "verify"), patch.object(
            main, "META_APP_SECRET", "secret"
        ):
            integrations = main.list_integrations()
        whatsapp_status = next(
            source for source in integrations["sources"] if source["name"] == "WhatsApp"
        )
        self.assertEqual(whatsapp_status["status"], "configured_not_verified")
        self.assertIn("webhook configured", whatsapp_status["details"])
