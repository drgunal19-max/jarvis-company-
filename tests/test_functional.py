import ipaddress
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, Request
from starlette.responses import Response

import app.database as database
import app.main as main
from app.schemas import (
    AssistantCommand,
    FindingUpdate,
    ImportedFinding,
    MemoryCreate,
    N8nFindingBatch,
    PreferencesUpdate,
    PublicFeedCreate,
    TaskCreate,
)
from app.services import discovery
from app.services import rss


JOB = {
    "title": "Computer assistant with Excel and IT support",
    "company_name": "Example Co",
    "location": "Bengaluru",
    "description": "<p>Assistant role needing computer skills, Excel and IT support.</p>",
    "url": "https://example.com/jobs/assistant",
    "slug": "assistant-example",
}


class FunctionalAppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self.temp_dir.name, "jarvis.db"))
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def create_task(self, interval_minutes=0):
        return main.create_task(TaskCreate(
            title="Assistant discovery",
            instruction="Find a computer-skilled assistant in Bengaluru",
            keywords=["assistant", "computer", "IT support"],
            location="Bengaluru",
            interval_minutes=interval_minutes,
        ))

    def test_task_command_parsing_and_persistent_memory(self):
        result = main.assistant_command(AssistantCommand(
            instruction="Find an administrative assistant in Bengaluru with Excel and scheduling"
        ))
        task = result["task"]
        self.assertEqual(task["location"], "Bengaluru")
        self.assertIn("assistant", task["keywords"])
        self.assertIn("Task saved", result["verified_facts"][0])
        self.assertEqual(len(main.assistant_messages()), 2)

    def test_configured_ai_extraction_is_validated_and_labeled(self):
        with patch.object(main, "LLM_API_KEY", "configured-key"), patch.object(
            main, "interpret_task",
            return_value={"title": "Excel assistant", "location": "Bengaluru", "keywords": ["Excel"]},
        ):
            result = main.assistant_command(AssistantCommand(
                instruction="Find an assistant with spreadsheet skills"
            ))
        self.assertEqual(result["generated_assessment"], "AI-assisted task field extraction")
        self.assertEqual(result["task"]["location"], "Bengaluru")
        self.assertEqual(result["task"]["keywords"], ["Excel"])

    def test_discovery_is_deduplicated_and_paused_tasks_cannot_run(self):
        task = self.create_task()
        with patch.object(discovery, "fetch_jobs", return_value=[JOB]):
            first = main.run_task(task["id"])
            again = main.run_task(task["id"])
        self.assertEqual(first["new_findings"], 1)
        self.assertEqual(again["new_findings"], 0)
        findings = main.list_findings(task_id=task["id"])
        self.assertEqual(len(findings), 1)
        self.assertIn("Matched:", findings[0]["match_reasons"])
        self.assertEqual(findings[0]["summary"], "Assistant role needing computer skills, Excel and IT support.")
        self.assertEqual(main.list_runs()[0]["checked_count"], 1)
        main.task_action(task["id"], "pause")
        with self.assertRaises(HTTPException) as raised:
            main.run_task(task["id"])
        self.assertEqual(raised.exception.status_code, 400)

    def test_source_failures_are_logged_as_gateway_errors(self):
        task = self.create_task()
        with patch.object(discovery, "fetch_jobs", side_effect=RuntimeError("source offline")):
            with self.assertRaises(HTTPException) as raised:
                main.run_task(task["id"])
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(main.list_runs()[0]["status"], "failed")
        self.assertIn("source offline", main.list_runs()[0]["message"])

    def test_all_source_failures_keep_per_connector_health(self):
        task = self.create_task()
        with patch.object(main, "validate_public_feed_url", side_effect=lambda url: url):
            main.add_task_source(task["id"], PublicFeedCreate(
                name="Careers", url="https://careers.example/rss.xml"
            ))
        with patch.object(discovery, "fetch_jobs", side_effect=RuntimeError("jobs offline")), patch.object(
            rss, "fetch_rss_feed", side_effect=rss.FeedError("feed unavailable")
        ):
            with self.assertRaises(HTTPException) as raised:
                main.run_task(task["id"])
        self.assertEqual(raised.exception.status_code, 502)
        with database.connect() as conn:
            health = {
                row["source"]: row["status"]
                for row in conn.execute("SELECT source,status FROM source_health")
            }
        self.assertEqual(health["Arbeitnow"], "error")
        self.assertEqual(health["RSS:1:Careers"], "error")
        self.assertIn("feed unavailable", main.list_runs()[0]["message"])

    def test_public_feed_url_blocks_insecure_and_private_targets(self):
        for url in (
            "http://example.com/feed.xml",
            "https://127.0.0.1/feed.xml",
            "https://localhost/feed.xml",
            "https://example.com:8443/feed.xml",
            "https://example.com:invalid/feed.xml",
        ):
            with self.subTest(url=url), self.assertRaises(rss.FeedError):
                rss.validate_public_feed_url(url)

    def test_atom_entries_are_normalized(self):
        atom = b"""<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry><title>Excel assistant</title>
            <link href="https://careers.example/jobs/1" rel="alternate"/>
            <id>job-1</id><summary>&lt;p&gt;Excel and scheduling&lt;/p&gt;</summary>
          </entry>
        </feed>"""
        with patch.object(rss, "_fetch_body", return_value=atom):
            entries = rss.fetch_rss_feed({
                "name": "Careers",
                "url": "https://careers.example/feed.xml",
            })
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["title"], "Excel assistant")
        self.assertEqual(entries[0]["url"], "https://careers.example/jobs/1")
        self.assertEqual(entries[0]["description"], "Excel and scheduling")

    def test_feed_transport_pins_public_address_and_rejects_redirects_or_oversize(self):
        class FakeResponse:
            def __init__(self, status, body):
                self.status = status
                self.body = body

            def read(self, limit):
                return self.body[:limit]

        class FakeConnection:
            response = FakeResponse(200, b"<feed/>")
            last = None

            def __init__(self, host, address):
                self.host = host
                self.address = address
                self.closed = False
                self.requested_path = None
                FakeConnection.last = self

            def request(self, method, path, headers):
                self.requested_path = path

            def getresponse(self):
                return self.response

            def close(self):
                self.closed = True

        public_ips = {ipaddress.ip_address("8.8.8.8")}
        with patch.object(rss, "_public_addresses", return_value=public_ips), patch.object(
            rss, "_PinnedHTTPSConnection", FakeConnection
        ):
            self.assertEqual(
                rss._fetch_body("https://public.example/feed.xml?type=rss"),
                b"<feed/>",
            )
            self.assertEqual(FakeConnection.last.address, "8.8.8.8")
            self.assertEqual(FakeConnection.last.requested_path, "/feed.xml?type=rss")
            self.assertTrue(FakeConnection.last.closed)

            FakeConnection.response = FakeResponse(302, b"redirect")
            with self.assertRaises(rss.FeedError):
                rss._fetch_body("https://public.example/feed.xml")
            self.assertTrue(FakeConnection.last.closed)

            FakeConnection.response = FakeResponse(200, b"x" * (rss.MAX_FEED_BYTES + 1))
            with self.assertRaises(rss.FeedError):
                rss._fetch_body("https://public.example/feed.xml")

    def test_task_scoped_feed_discovers_and_deduplicates(self):
        task = self.create_task()
        with patch.object(main, "validate_public_feed_url", side_effect=lambda url: url):
            source = main.add_task_source(task["id"], PublicFeedCreate(
                name="Company Careers",
                url="https://careers.example/rss.xml",
            ))
        feed_item = {
            "title": "Computer assistant with IT support",
            "company_name": "Company Careers",
            "location": "Bengaluru",
            "description": "Assistant position with computer experience and IT support.",
            "url": "https://careers.example/jobs/assistant",
            "slug": "role-123",
        }
        with patch.object(discovery, "fetch_jobs", return_value=[]), patch.object(
            rss, "fetch_rss_feed", return_value=[feed_item]
        ):
            first = main.run_task(task["id"])
            again = main.run_task(task["id"])
        self.assertEqual(first["sources"], ["Arbeitnow", "Company Careers"])
        self.assertEqual(first["new_findings"], 1)
        self.assertEqual(again["new_findings"], 0)
        self.assertEqual(main.list_findings(task_id=task["id"])[0]["source"], f"RSS:{source['id']}:Company Careers")
        self.assertEqual(main.list_task_sources()[0]["health_status"], "online")

        with patch.object(discovery, "fetch_jobs", side_effect=RuntimeError("job API unavailable")), patch.object(
            rss, "fetch_rss_feed", return_value=[feed_item]
        ):
            partial = main.run_task(task["id"])
        self.assertEqual(partial["errors"][0].split(":", 1)[0], "Arbeitnow")
        self.assertEqual(main.list_runs()[0]["status"], "partial")

    def test_task_feed_route_rejects_unsafe_url(self):
        task = self.create_task()
        with self.assertRaises(HTTPException) as raised:
            main.add_task_source(task["id"], PublicFeedCreate(
                name="Local Feed", url="http://127.0.0.1/private.xml"
            ))
        self.assertEqual(raised.exception.status_code, 422)

    def test_task_delete_removes_related_data_and_preserves_audit(self):
        task = self.create_task()
        with patch.object(main, "validate_public_feed_url", side_effect=lambda url: url):
            source = main.add_task_source(task["id"], PublicFeedCreate(
                name="Company Careers", url="https://careers.example/rss.xml"
            ))
        with patch.object(discovery, "fetch_jobs", return_value=[JOB]):
            main.run_task(task["id"])
        deleted = main.delete_task(task["id"])
        self.assertTrue(deleted["deleted"])
        self.assertEqual(main.list_tasks(), [])
        self.assertEqual(main.list_findings(), [])
        self.assertEqual(main.list_runs(), [])
        self.assertNotIn(source["id"], [row["id"] for row in main.list_task_sources()])
        self.assertEqual(main.list_audit_log()[0]["action"], "task.deleted")
        with self.assertRaises(HTTPException) as raised:
            main.delete_task(task["id"])
        self.assertEqual(raised.exception.status_code, 404)

    def test_data_export_contains_persisted_records(self):
        task = self.create_task()
        main.save_memory(MemoryCreate(key="preferred_location", value="Bengaluru"))
        export = main.export_data()
        self.assertEqual(export["format"], "jarvis-data-export")
        self.assertEqual(export["data"]["tasks"][0]["id"], task["id"])
        self.assertEqual(export["data"]["memories"][0]["value"], "Bengaluru")
        self.assertIn("findings", export["data"])

    def test_empty_but_successful_source_is_a_completed_check(self):
        task = self.create_task()
        with patch.object(discovery, "fetch_jobs", return_value=[]):
            result = main.run_task(task["id"])
        self.assertEqual(result["checked"], 0)
        self.assertEqual(result["new_findings"], 0)
        self.assertEqual(main.list_runs()[0]["status"], "completed")

    def test_finding_feedback_preferences_and_memory_persist(self):
        task = self.create_task()
        with patch.object(discovery, "fetch_jobs", return_value=[JOB]):
            main.run_task(task["id"])
        finding = main.list_findings(task_id=task["id"])[0]
        updated = main.update_finding(finding["id"], FindingUpdate(status="relevant"))
        self.assertEqual(updated["status"], "relevant")

        prefs = main.update_preferences(PreferencesUpdate(
            default_location="Bengaluru", morning_briefing=False, default_keywords=["Excel"]
        ))
        self.assertFalse(prefs["morning_briefing"])
        self.assertEqual(prefs["default_keywords"], ["Excel"])
        saved = main.save_memory(MemoryCreate(key="preferred_location ", value="Bengaluru"))
        self.assertEqual(saved["key"], "preferred_location")
        database.init_db()
        self.assertEqual(main.get_preferences()["default_location"], "Bengaluru")
        self.assertEqual(main.list_memory()[0]["value"], "Bengaluru")

    def test_emergency_stop_blocks_manual_and_scheduled_runs(self):
        task = self.create_task(interval_minutes=1)
        main.system_action("stop")
        with self.assertRaises(Exception):
            main.run_task(task["id"])
        self.assertEqual(main.run_due_tasks()["ran"], 0)
        main.system_action("resume")
        with patch.object(discovery, "fetch_jobs", return_value=[]):
            self.assertEqual(main.run_due_tasks()["ran"], 1)
            self.assertEqual(main.run_due_tasks()["ran"], 0)

    def test_n8n_ingestion_requires_token_and_is_idempotent(self):
        task = self.create_task()
        payload = N8nFindingBatch(findings=[ImportedFinding(
            task_id=task["id"], source="n8n-source", source_id="external-123",
            title="Imported opportunity", url="https://example.com/opportunity",
            score=75,
        )])
        with patch.object(main, "APP_API_TOKEN", "test-secret"):
            headers = {"authorization": "Bearer test-secret"}
            first = main.import_n8n_findings(payload, authorization=headers["authorization"])
            second = main.import_n8n_findings(payload, authorization=headers["authorization"])
        self.assertEqual(first["inserted"], 1)
        self.assertEqual(second["inserted"], 0)
        self.assertEqual(len(main.list_findings(task_id=task["id"])), 1)

    def test_old_database_schema_is_migrated(self):
        with database.connect() as conn:
            conn.execute("DROP TABLE findings")
            conn.execute("DROP TABLE runs")
            conn.execute("DROP TABLE tasks")
            conn.executescript("""
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                    instruction TEXT NOT NULL, keywords TEXT NOT NULL DEFAULT '',
                    location TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_run_at TEXT
                );
                CREATE TABLE findings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
                    source TEXT NOT NULL, source_id TEXT NOT NULL, title TEXT NOT NULL,
                    company TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '',
                    url TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', score INTEGER NOT NULL DEFAULT 0,
                    discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(task_id, source, source_id)
                );
                CREATE TABLE runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
                    status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
                    item_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)
        database.init_db()
        with database.connect() as conn:
            self.assertIn("interval_minutes", {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")})
            self.assertIn("status", {row["name"] for row in conn.execute("PRAGMA table_info(findings)")})
            self.assertIn("checked_count", {row["name"] for row in conn.execute("PRAGMA table_info(runs)")})


class AuthenticationMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_token_is_required_when_configured(self):
        async def endpoint(_request):
            return Response("ok")

        def request(headers, path="/api/tasks"):
            scope = {
                "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                "method": "GET", "scheme": "http", "path": path,
                "raw_path": path.encode(), "query_string": b"", "root_path": "",
                "headers": headers, "client": ("127.0.0.1", 1234),
                "server": ("127.0.0.1", 8000),
            }
            return Request(scope)

        with patch.object(main, "APP_API_TOKEN", "test-secret"):
            denied = await main.protect_api(request([]), endpoint)
            allowed = await main.protect_api(
                request([(b"authorization", b"Bearer test-secret")]), endpoint
            )
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(allowed.status_code, 200)
        with patch.object(main, "APP_API_TOKEN", "test-secret"):
            webhook = await main.protect_api(
                request([], "/api/webhooks/whatsapp"), endpoint
            )
        self.assertEqual(webhook.status_code, 200)


if __name__ == "__main__":
    unittest.main()
