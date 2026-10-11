import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from fastapi import HTTPException

import app.database as database
import app.main as main
from app.services import google_workspace


class FakeOAuthRequest:
    def __init__(self, query=None, cookies=None, scheme="http"):
        self.query_params = query or {}
        self.cookies = cookies or {}
        self.url = SimpleNamespace(scheme=scheme)


class GoogleWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self.temp_dir.name, "jarvis.db"))
        database.init_db()
        self.key = Fernet.generate_key().decode("ascii")

    def tearDown(self):
        database.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def google_settings(self):
        return patch.multiple(
            google_workspace,
            GOOGLE_CLIENT_ID="client-id",
            GOOGLE_CLIENT_SECRET="client-secret",
            GOOGLE_REDIRECT_URI="http://127.0.0.1:8000/api/google/callback",
            GOOGLE_TOKEN_ENCRYPTION_KEY=self.key,
        )

    def test_authorization_url_requests_only_read_only_scopes(self):
        with self.google_settings():
            url = google_workspace.authorization_url("random-state")

        self.assertIn("access_type=offline", url)
        self.assertIn("state=random-state", url)
        self.assertIn("gmail.readonly", url)
        self.assertIn("calendar.readonly", url)
        self.assertIn("drive.metadata.readonly", url)
        self.assertNotIn("gmail.modify", url)

    def test_oauth_tokens_are_encrypted_before_storage(self):
        response = Mock(ok=True)
        response.json.return_value = {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "expires_in": 3600,
            "scope": " ".join(google_workspace.GOOGLE_SCOPES),
        }
        with self.google_settings(), patch.object(
            google_workspace.requests, "post", return_value=response
        ):
            google_workspace.exchange_authorization_code("authorization-code")
            with database.connect() as conn:
                row = conn.execute(
                    "SELECT access_token,refresh_token FROM google_oauth_tokens WHERE id=1"
                ).fetchone()
            self.assertNotIn("access-secret", row["access_token"])
            self.assertNotIn("refresh-secret", row["refresh_token"])
            self.assertEqual(google_workspace._decrypt(row["access_token"]), "access-secret")
            self.assertEqual(google_workspace._decrypt(row["refresh_token"]), "refresh-secret")
            self.assertTrue(google_workspace.connection_status()["connected"])

    def test_oauth_rejects_missing_requested_scope_without_storing_tokens(self):
        response = Mock(ok=True)
        response.json.return_value = {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "expires_in": 3600,
            "scope": google_workspace.GOOGLE_SCOPES[0],
        }
        with self.google_settings(), patch.object(
            google_workspace.requests, "post", return_value=response
        ):
            with self.assertRaises(google_workspace.GoogleProviderError):
                google_workspace.exchange_authorization_code("authorization-code")
        with database.connect() as conn:
            row = conn.execute("SELECT id FROM google_oauth_tokens").fetchone()
        self.assertIsNone(row)

    def test_expired_access_token_is_refreshed_and_saved_encrypted(self):
        now = int(time.time())
        with self.google_settings():
            google_workspace._save_tokens(
                "old-access", "long-lived-refresh", now - 10,
                " ".join(google_workspace.GOOGLE_SCOPES),
            )
            response = Mock(ok=True)
            response.json.return_value = {
                "access_token": "new-access",
                "expires_in": 3600,
            }
            with patch.object(
                google_workspace.requests, "post", return_value=response
            ) as post:
                self.assertEqual(google_workspace._access_token(), "new-access")
            self.assertEqual(post.call_args.kwargs["data"]["refresh_token"], "long-lived-refresh")
            stored = google_workspace._stored_tokens()
            self.assertNotEqual(stored["access_token"], "new-access")
            self.assertEqual(google_workspace._decrypt(stored["access_token"]), "new-access")
            self.assertEqual(google_workspace._decrypt(stored["refresh_token"]), "long-lived-refresh")

    def test_gmail_search_fetches_metadata_not_message_body(self):
        with self.google_settings():
            google_workspace._save_tokens(
                "usable-access", "refresh-secret", int(time.time()) + 3600,
                " ".join(google_workspace.GOOGLE_SCOPES),
            )
            listing = Mock(ok=True)
            listing.json.return_value = {"messages": [{"id": "message-1"}]}
            details = Mock(ok=True)
            details.json.return_value = {
                "id": "message-1",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Project update"},
                        {"name": "From", "value": "sender@example.com"},
                        {"name": "Date", "value": "Mon, 1 Jan 2026 09:00:00 +0000"},
                    ],
                    "body": {"data": "must-not-be-read"},
                },
            }
            with patch.object(
                google_workspace.requests, "get", side_effect=[listing, details]
            ) as get:
                results = google_workspace.search_gmail("from:sender@example.com")

        self.assertEqual(results[0]["subject"], "Project update")
        self.assertNotIn("body", results[0])
        self.assertIn(("format", "metadata"), get.call_args.kwargs["params"])
        self.assertEqual(get.call_args.kwargs["params"].count(("metadataHeaders", "Subject")), 1)

    def test_oauth_callback_rejects_bad_state_and_clears_cookie_on_success(self):
        with self.assertRaises(HTTPException) as raised:
            main.google_callback(FakeOAuthRequest(
                query={"state": "wrong", "code": "code"},
                cookies={"jarvis_google_oauth_state": "expected"},
            ))
        self.assertEqual(raised.exception.status_code, 400)

        with patch.object(main.google_workspace, "exchange_authorization_code") as exchange:
            response = main.google_callback(FakeOAuthRequest(
                query={"state": "expected", "code": "code"},
                cookies={"jarvis_google_oauth_state": "expected"},
            ))
        exchange.assert_called_once_with("code")
        self.assertEqual(response.status_code, 200)
        self.assertIn("jarvis_google_oauth_state", response.headers["set-cookie"])
        with database.connect() as conn:
            audit = conn.execute(
                "SELECT action FROM audit_log WHERE action='google.connected'"
            ).fetchone()
        self.assertIsNotNone(audit)

    def test_authorize_route_sets_http_only_state_cookie(self):
        with patch.object(google_workspace, "GOOGLE_CLIENT_ID", "client-id"), patch.object(
            google_workspace, "GOOGLE_CLIENT_SECRET", "client-secret"
        ), patch.object(google_workspace, "GOOGLE_REDIRECT_URI", "https://example.test/api/google/callback"), patch.object(
            google_workspace, "GOOGLE_TOKEN_ENCRYPTION_KEY", self.key
        ), patch.object(
            main, "GOOGLE_REDIRECT_URI", "https://example.test/api/google/callback"
        ):
            response = main.google_authorize(FakeOAuthRequest(scheme="https"))

        cookie = response.headers["set-cookie"].lower()
        payload = json.loads(response.body)
        self.assertIn("httponly", cookie)
        self.assertIn("secure", cookie)
        self.assertIn("samesite=lax", cookie)
        self.assertIn("state=", payload["authorization_url"])

    def test_google_api_endpoint_reports_disconnected_account(self):
        with self.assertRaises(HTTPException) as raised:
            main.google_gmail()
        self.assertEqual(raised.exception.status_code, 409)
