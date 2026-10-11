import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

import app.database as database
import app.main as main
from app.schemas import ChatRequest
from app.services import web_search


class AssistantChatTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_db_path = database.DB_PATH
        database.DB_PATH = str(Path(self.temp_dir.name, "jarvis.db"))
        database.init_db()

    def tearDown(self):
        database.DB_PATH = self.old_db_path
        self.temp_dir.cleanup()

    def test_chat_requires_configured_language_model(self):
        with patch.object(main, "LLM_API_KEY", ""):
            with self.assertRaises(HTTPException) as raised:
                main.assistant_chat(ChatRequest(message="Hello"))
        self.assertEqual(raised.exception.status_code, 503)

    def test_chat_persists_user_and_assistant_messages(self):
        with patch.object(main, "LLM_API_KEY", "configured"), patch.object(
            main, "generate_chat_response", return_value={"content": "Hello from JARVIS"}
        ):
            result = main.assistant_chat(ChatRequest(message="Hello"))
        self.assertEqual(result["reply"], "Hello from JARVIS")
        self.assertIsNone(result["created_task"])
        self.assertEqual(
            [message["role"] for message in main.assistant_messages()],
            ["user", "assistant"],
        )

    def test_chat_can_create_a_task_but_does_not_run_it(self):
        tool_call = {
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "create_monitoring_task",
                "arguments": json.dumps({
                    "title": "Excel assistant roles",
                    "instruction": "Monitor assistant roles requiring Excel",
                    "keywords": ["assistant", "Excel"],
                    "location": "Bengaluru",
                }),
            },
        }
        with patch.object(main, "LLM_API_KEY", "configured"), patch.object(
            main, "generate_chat_response",
            return_value={"content": None, "tool_calls": [tool_call]},
        ):
            result = main.assistant_chat(
                ChatRequest(message="Please monitor assistant roles requiring Excel")
            )
        self.assertEqual(result["created_task"]["title"], "Excel assistant roles")
        self.assertIn("has not been run", result["reply"])
        self.assertEqual(main.list_runs(), [])

    def test_web_search_requires_key_and_returns_citations_when_configured(self):
        search_result = {
            "title": "Example result",
            "url": "https://example.com/result",
            "content": "Relevant public information.",
        }
        with patch.object(main, "LLM_API_KEY", "configured"), patch.object(
            main, "TAVILY_API_KEY", ""
        ):
            with self.assertRaises(HTTPException) as raised:
                main.assistant_chat(ChatRequest(message="Latest news", web_search=True))
        self.assertEqual(raised.exception.status_code, 503)

        with patch.object(main, "LLM_API_KEY", "configured"), patch.object(
            main, "TAVILY_API_KEY", "configured"
        ), patch.object(main, "search_web", return_value=[search_result]), patch.object(
            main, "generate_chat_response", return_value={"content": "A short summary."}
        ):
            result = main.assistant_chat(
                ChatRequest(message="Latest news", web_search=True)
            )
        self.assertEqual(result["sources"], [search_result])
        self.assertIn("https://example.com/result", result["reply"])


class WebSearchTests(unittest.TestCase):
    def test_search_normalizes_results_and_discards_non_http_urls(self):
        response = unittest.mock.Mock()
        response.json.return_value = {
            "results": [
                {
                    "title": "Good result",
                    "url": "https://example.com/article",
                    "content": "Useful excerpt",
                },
                {
                    "title": "Unsafe result",
                    "url": "javascript:alert(1)",
                    "content": "Do not render",
                },
            ]
        }
        with patch.object(web_search, "TAVILY_API_KEY", "test-key"), patch.object(
            web_search.requests, "post", return_value=response
        ) as post:
            results = web_search.search_web("test query")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["url"], "https://example.com/article")
        self.assertEqual(post.call_args.kwargs["json"]["query"], "test query")
