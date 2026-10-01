import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import main
from fastapi.testclient import TestClient


class _ContextClient:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False


class _FakeProjectClient(_ContextClient):
    instance = None

    def __init__(self, *, credential, endpoint):
        self.credential = credential
        self.endpoint = endpoint
        self.connections = SimpleNamespace(
            get=lambda name: SimpleNamespace(id=f"connection:{name}")
        )
        _FakeProjectClient.instance = self


class _FakeAgentsClient(_ContextClient):
    instance = None

    def __init__(self, *, credential, endpoint):
        self.credential = credential
        self.endpoint = endpoint
        self.agent = SimpleNamespace(id="agent-1", name="test-agent")
        self.thread = SimpleNamespace(id="thread-1")
        self.run = SimpleNamespace(id="run-1", status="completed")
        self.list_agents_called = False
        self.threads = SimpleNamespace(create=lambda: self.thread)
        self.messages = SimpleNamespace(
            create=lambda **kwargs: SimpleNamespace(id="message-1"),
            list=lambda **kwargs: [
                SimpleNamespace(
                    id="assistant-message-1",
                    role="assistant",
                    content=[{
                        "text": {
                            "value": "Grounded answer\u30101:0\u2020source\u3011",
                            "annotations": [{
                                "url_citation": {
                                    "title": "Example",
                                    "url": "https://example.com/source",
                                }
                            }],
                        }
                    }],
                    as_dict=lambda: {"id": "assistant-message-1"},
                )
            ],
        )
        self.runs = SimpleNamespace(
            create_and_process=lambda **kwargs: self.run
        )
        _FakeAgentsClient.instance = self

    def list_agents(self):
        self.list_agents_called = True
        return [self.agent]

    def create_agent(self, **kwargs):
        raise AssertionError("The existing fixture agent should be reused.")


class SearchTests(unittest.TestCase):
    def test_search_uses_agents_client_for_classic_agent_operations(self):
        environment = {
            "PROJECT_CONNECTION_STRING": "https://example.test/project",
            "BING_RESOURCE_NAME": "bing-connection",
            "AGENT_NAME": "test-agent",
            "AGENT_INSTRUCTIONS": "Use Bing.",
            "MODEL_DEPLOYMENT_NAME": "test-model",
        }
        credential = object()

        with (
            patch.dict(os.environ, environment, clear=False),
            patch.object(main, "AIProjectClient", _FakeProjectClient),
            patch.object(main, "AgentsClient", _FakeAgentsClient),
            patch.object(main, "DefaultAzureCredential", return_value=credential),
            patch.object(
                main,
                "BingGroundingTool",
                side_effect=lambda connection_id: SimpleNamespace(
                    definitions=[{"connection_id": connection_id}]
                ),
            ),
            patch.object(
                main,
                "load_domain_filters",
                return_value={
                    "whitelist": ["example.com"],
                    "blacklist": [],
                },
            ),
        ):
            result = asyncio.run(main.search("test query"))

        agents_client = _FakeAgentsClient.instance
        self.assertTrue(agents_client.list_agents_called)
        self.assertEqual(agents_client.endpoint, environment["PROJECT_CONNECTION_STRING"])
        self.assertIs(agents_client.credential, credential)
        self.assertEqual(result["agent_id"], "agent-1")
        self.assertEqual(result["assistant_response"], "Grounded answer\u30101:0\u2020source\u3011")
        self.assertEqual(result["citations"], [{
            "title": "Example",
            "url": "https://example.com/source",
        }])
        self.assertEqual(result["filter_mode"], "whitelist")
        self.assertNotIn("raw_message", result)

    def test_home_renders_raw_unfiltered_and_server_filtered_panels(self):
        unfiltered_result = {
            "query": "test query",
            "assistant_response": (
                "Allowed answer\u30101:0\u2020source\u3011. "
                "Blocked answer\u30102:0\u2020source\u3011."
            ),
            "citations": [
                {
                    "title": "Allowed",
                    "url": "https://example.com/source",
                },
                {
                    "title": "Blocked",
                    "url": "https://blocked.test/source",
                },
            ],
            "raw_message": {"content": "RAW UNFILTERED RESPONSE"},
            "unknown_sdk_payload": "UNKNOWN SDK FIELD",
        }

        with (
            patch.object(
                main,
                "run_agent_search",
                new=AsyncMock(return_value=unfiltered_result),
            ) as mocked_search,
            patch.object(
                main,
                "load_domain_filters",
                return_value={
                    "whitelist": [],
                    "blacklist": ["blocked.test"],
                },
            ),
        ):
            response = TestClient(main.app).post(
                "/",
                data={"query": "test query", "use_blacklist": "on"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Allowed answer", response.text)
        self.assertIn("Blocked answer", response.text)
        self.assertIn("RAW UNFILTERED RESPONSE", response.text)
        self.assertNotIn("UNKNOWN SDK FIELD", response.text)
        self.assertNotIn("result-filter.js", response.text)
        self.assertNotIn("domain-filters.json", response.text)
        self.assertIn("Unfiltered Raw JSON", response.text)
        self.assertIn("Unfiltered Formatted Results", response.text)
        self.assertIn("Blacklist Filtered Formatted Results", response.text)
        self.assertRegex(
            response.text,
            r"using\s+1 blacklist domain\(s\)",
        )
        self.assertEqual(response.text.count('class="panel"'), 3)

        filtered_panel = response.text.split(
            "Blacklist Filtered Formatted Results",
            maxsplit=1,
        )[1]
        self.assertIn("Allowed answer", filtered_panel)
        self.assertNotIn("Blocked answer", filtered_panel)
        self.assertNotIn("blocked.test", filtered_panel)
        self.assertNotIn("bing.com/search", filtered_panel)
        mocked_search.assert_awaited_once_with("test query")

    def test_domain_configuration_is_not_web_accessible(self):
        client = TestClient(main.app)

        self.assertEqual(
            client.get("/static/domain-filters.json").status_code,
            404,
        )
        self.assertEqual(
            client.get("/config/domain-filters.json").status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
