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
from app.services import instagram


class FakeInstagramRequest:
    def __init__(self, query=None, cookies=None, scheme="http"):
        self.query_params = query or {}
        self.cookies = cookies or {}
        self.url = SimpleNamespace(scheme=scheme)


class InstagramTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self.temp_dir.name, "jarvis.db"))
        database.init_db()
        self.key = Fernet.generate_key().decode("ascii")

    def tearDown(self):
        database.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def instagram_settings(self):
        return patch.multiple(
            instagram,
            INSTAGRAM_APP_ID="instagram-app-id",
            INSTAGRAM_APP_SECRET="instagram-app-secret",
            INSTAGRAM_REDIRECT_URI="http://127.0.0.1:8000/api/instagram/callback",
            INSTAGRAM_TOKEN_ENCRYPTION_KEY=self.key,
        )

    def test_authorization_requests_basic_permission_only(self):
        with self.instagram_settings():
            url = instagram.authorization_url("expected-state")
        self.assertIn("instagram_business_basic", url)
        self.assertIn("expected-state", url)
        self.assertIn("force_reauth=true", url)
        self.assertNotIn("instagram_business_manage_messages", url)
        self.assertNotIn("instagram_business_content_publish", url)

    def test_login_token_is_exchanged_for_encrypted_long_lived_token(self):
        short = Mock(ok=True)
        short.json.return_value = {
            "data": [{
                "access_token": "short-lived-token",
                "user_id": "1234",
                "permissions": "instagram_business_basic",
            }],
        }
        long = Mock(ok=True)
        long.json.return_value = {
            "access_token": "long-lived-token",
            "expires_in": 5_184_000,
        }
        with self.instagram_settings(), patch.object(
            instagram.requests, "post", return_value=short
        ) as post, patch.object(
            instagram.requests, "get", return_value=long
        ) as get:
            instagram.exchange_authorization_code("oauth-code")
            row = instagram._stored_token()
            self.assertEqual(post.call_args.kwargs["data"]["code"], "oauth-code")
            self.assertEqual(
                get.call_args.kwargs["params"]["access_token"],
                "short-lived-token",
            )
            self.assertNotIn("long-lived-token", row["access_token"])
            self.assertEqual(instagram._decrypt(row["access_token"]), "long-lived-token")
            self.assertTrue(instagram.connection_status()["connected"])

    def test_near_expiry_token_is_refreshed(self):
        with self.instagram_settings():
            instagram._save_token(
                "old-token", int(time.time()) + 3 * 24 * 60 * 60
            )
            response = Mock(ok=True)
            response.json.return_value = {
                "access_token": "refreshed-token",
                "expires_in": 5_184_000,
            }
            with patch.object(
                instagram.requests, "get", return_value=response
            ) as get:
                self.assertEqual(instagram._access_token(), "refreshed-token")
            self.assertEqual(
                get.call_args.args[0],
                "https://graph.instagram.com/refresh_access_token",
            )
            stored = instagram._stored_token()
            self.assertEqual(instagram._decrypt(stored["access_token"]), "refreshed-token")

    def test_profile_and_media_are_fetched_on_demand_as_limited_metadata(self):
        with self.instagram_settings():
            instagram._save_token(
                "current-token", int(time.time()) + 30 * 24 * 60 * 60
            )
            profile = Mock(ok=True)
            profile.json.return_value = {
                "data": [{
                    "id": "account-123",
                    "user_id": "1234",
                    "username": "sample_creator",
                    "account_type": "BUSINESS",
                    "media_count": 4,
                }],
            }
            media_profile = Mock(ok=True)
            media_profile.json.return_value = {"data": [{"user_id": "1234"}]}
            media = Mock(ok=True)
            media.json.return_value = {
                "data": [{
                    "id": "post-1",
                    "media_type": "IMAGE",
                    "permalink": "https://www.instagram.com/p/example/",
                    "timestamp": "2026-01-01T12:00:00+0000",
                }],
            }
            with patch.object(
                instagram.requests, "get", side_effect=[profile, profile, media]
            ) as get:
                profile_result = instagram.get_profile()
                media_result = instagram.list_media()

        self.assertEqual(profile_result["username"], "sample_creator")
        self.assertEqual(profile_result["account_type"], "BUSINESS")
        self.assertEqual(media_result[0]["media_type"], "IMAGE")
        self.assertNotIn("caption", media_result[0])
        self.assertEqual(get.call_args.kwargs["params"]["limit"], 12)
        self.assertEqual(get.call_args.args[0], f"{instagram.INSTAGRAM_GRAPH_URL}/1234/media")
        self.assertNotIn("caption", get.call_args.kwargs["params"]["fields"])

    def test_oauth_callback_validates_state_and_clears_cookie_on_success(self):
        with self.assertRaises(HTTPException) as raised:
            main.instagram_callback(FakeInstagramRequest(
                query={"state": "wrong", "code": "code"},
                cookies={"jarvis_instagram_oauth_state": "expected"},
            ))
        self.assertEqual(raised.exception.status_code, 400)

        with patch.object(
            main.instagram, "exchange_authorization_code"
        ) as exchange:
            response = main.instagram_callback(FakeInstagramRequest(
                query={"state": "expected", "code": "code"},
                cookies={"jarvis_instagram_oauth_state": "expected"},
            ))
        exchange.assert_called_once_with("code")
        self.assertEqual(response.status_code, 200)
        self.assertIn("jarvis_instagram_oauth_state", response.headers["set-cookie"])
        with database.connect() as conn:
            audit = conn.execute(
                "SELECT action FROM audit_log WHERE action='instagram.connected'"
            ).fetchone()
        self.assertIsNotNone(audit)

    def test_profile_endpoint_requires_a_connected_account(self):
        with self.assertRaises(HTTPException) as raised:
            main.instagram_profile()
        self.assertEqual(raised.exception.status_code, 409)

    def test_disconnect_removes_stored_token(self):
        with self.instagram_settings():
            instagram._save_token(
                "stored-token", int(time.time()) + 30 * 24 * 60 * 60
            )
            instagram.disconnect()
            self.assertIsNone(instagram._stored_token())
