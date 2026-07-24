import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tests.support import build_fixture

import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
sys.path.append(str(Path(__file__).parents[1] / "tools"))

from patent_viewer.collaboration import AuditStore, BlockPlanner, CollaborationError, estimate_workload
from patent_viewer.collaboration_client import PatentViewerClient
from patent_viewer.server import create_server
from browser_smoke import select_client


class CollaborationUnitTests(unittest.TestCase):
    def test_rule_blocks_compile_to_existing_semantic_targets(self):
        planner = BlockPlanner()
        plan = planner.plan("set_filters", {
            "research_id": "normal_research", "year_from": 2024, "year_to": 2026,
            "query": "laser", "statuses": ["rights_acquired", "published"],
        })
        targets = [action.get("target", {}).get("agentId") for action in plan["actions"]]
        self.assertIn("research-select", targets)
        self.assertIn("patent-search", targets)
        self.assertIn("status-under-examination", targets)
        self.assertEqual(plan["execution_path"], "visible_ui")

    def test_heavy_block_requires_explicit_confirmation_and_reports_load(self):
        planner = BlockPlanner()
        with self.assertRaises(CollaborationError) as raised:
            planner.plan("execute_pipeline", {})
        self.assertEqual(raised.exception.code, "HUMAN_CONFIRMATION_REQUIRED")
        plan = planner.plan("execute_pipeline", {"confirmation": "RUN_LOCAL_LLM"})
        self.assertTrue(plan["definition"]["heavy"])
        self.assertEqual(estimate_workload("execute_pipeline")["weight"], "heavy")

    def test_audit_is_append_only_and_idempotency_survives_reload(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = AuditStore(root)
            store.append("command_created", command_id="abc", idempotency_key="same")
            self.assertEqual(store.find_idempotent("same")["command_id"], "abc")
            reloaded = AuditStore(root)
            self.assertEqual(reloaded.find_idempotent("same")["command_id"], "abc")
            self.assertEqual(reloaded.recent(1)[0]["event"], "command_created")

    def test_stdio_mcp_advertises_lightweight_tools(self):
        root = Path(__file__).parents[1]
        messages = "\n".join([
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
            "",
        ])
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(root / "tools/collaboration_mcp.py")],
            input=messages, text=True, encoding="utf-8", capture_output=True, cwd=root, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "patent-viewer")
        names = {item["name"] for item in replies[1]["result"]["tools"]}
        self.assertIn("search_documents", names)
        self.assertIn("execute_ui_block", names)

    def test_browser_smoke_requires_explicit_client_when_multiple_are_connected(self):
        status = {
            "clients": [
                {"clientId": "browser-a", "environment": "normal"},
                {"clientId": "browser-b", "environment": "debug"},
            ],
        }
        with self.assertRaises(RuntimeError):
            select_client(status, None)
        self.assertEqual(select_client(status, "browser-b")["environment"], "debug")


class CollaborationApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        build_fixture(cls.root)
        cls.server = create_server(cls.root, port=0, quiet=True, control_token="collaboration-token")
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def request(self, path, body=None, headers=None, expected=200):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urlopen(request) as response:
                status, payload = response.status, json.loads(response.read())
        except HTTPError as error:
            status, payload = error.code, json.loads(error.read())
        self.assertEqual(status, expected, payload)
        return payload

    def heartbeat(self, targets=None):
        return self.request("/api/ui/clients/heartbeat", {
            "clientId": "browser-one", "module": "patent-viewer",
            "capabilities": ["filter", "map", "pdf-preview", "interpretation", "llm-preflight", "research-pipeline", "pipeline-control"],
            "targets": targets if targets is not None else [{"agentId": "filters-reset"}],
            "targetSnapshotHash": "fixture-hash",
        })

    def test_status_plan_authorization_idempotency_and_lifecycle(self):
        self.heartbeat()
        status = self.request("/api/collaboration/status")
        self.assertEqual(status["protocol_version"], 3)
        browser = next(item for item in status["clients"] if item["clientId"] == "browser-one")
        self.assertEqual(browser["target_count"], 1)
        plan = self.request("/api/collaboration/plan", {"block": "reset_filters", "arguments": {}})
        self.assertEqual(plan["actions"][0]["target"]["agentId"], "filters-reset")
        self.request("/api/collaboration/execute", {
            "block": "reset_filters", "idempotency_key": "reset-once",
        }, expected=403)
        created = self.request("/api/collaboration/execute", {
            "block": "reset_filters", "idempotency_key": "reset-once",
            "targetClientId": "browser-one",
        }, headers={"X-PatentViewer-Collaboration-Token": "collaboration-token"}, expected=201)
        command_id = created["id"]
        claimed = self.request("/api/ui/next?clientId=browser-one")["command"]
        self.assertEqual(claimed["id"], command_id)
        self.request(f"/api/ui/commands/{command_id}/events", {"clientId": "browser-one", "type": "completed"})
        reused = self.request("/api/collaboration/execute", {
            "block": "reset_filters", "idempotency_key": "reset-once",
            "targetClientId": "browser-one",
        }, headers={"X-PatentViewer-Collaboration-Token": "collaboration-token"})
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["command"]["status"], "completed")
        audit = self.request("/api/collaboration/audit?limit=20")
        self.assertTrue(any(item["event"] == "command_created" for item in audit["items"]))

    def test_target_snapshot_delta_retains_server_registry(self):
        self.heartbeat([{"agentId": "filters-reset"}, {"agentId": "patent-search"}])
        self.heartbeat([])
        clients = self.request("/api/ui/clients")["items"]
        client = next(item for item in clients if item["clientId"] == "browser-one")
        self.assertEqual(len(client["targets"]), 2)

    def test_all_codex_mutations_require_visible_command(self):
        payload = {"environment": "debug", "source": "codex"}
        result = self.request("/api/environment", payload, expected=403)
        self.assertEqual(result["code"], "VISIBLE_UI_COMMAND_REQUIRED")

    def test_debug_environment_is_scoped_to_one_browser_client(self):
        for client_id in ("browser-debug-a", "browser-debug-b"):
            self.request("/api/ui/clients/heartbeat", {
                "clientId": client_id, "module": "patent-viewer", "environment": "normal",
                "capabilities": ["filter"], "targets": [{"agentId": "filters-reset"}],
            })
        changed = self.request(
            "/api/environment", {"environment": "debug"},
            headers={"X-PatentViewer-UI-Client": "browser-debug-a"},
        )
        self.assertTrue(changed["client_scoped"])
        first = self.request("/api/environment", headers={"X-PatentViewer-UI-Client": "browser-debug-a"})
        second = self.request("/api/environment", headers={"X-PatentViewer-UI-Client": "browser-debug-b"})
        self.assertEqual(first["environment"], "debug")
        self.assertEqual(second["environment"], "normal")
        heartbeat = self.request("/api/ui/clients/heartbeat", {
            "clientId": "browser-debug-a", "module": "patent-viewer", "environment": "normal",
            "capabilities": ["filter"], "targets": [], "targetSnapshotHash": "",
        })
        self.assertEqual(heartbeat["environment"], "debug", "pending client-scoped DEBUG must survive the reload heartbeat")

    def test_rule_client_searches_without_llm(self):
        client = PatentViewerClient(self.root, f"http://127.0.0.1:{self.port}", "collaboration-token")
        result = client.search_documents("normal_research", analysis_states=["ready"])
        self.assertEqual(result["execution_path"], "rule_api")
        self.assertEqual(result["count"], 1)


if __name__ == "__main__":
    unittest.main()
