import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from tests.support import build_fixture

import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from patent_viewer.server import create_server


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(); cls.root = Path(cls.temp.name); build_fixture(cls.root)
        cls.server = create_server(cls.root, port=0, quiet=True); cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls): cls.server.shutdown(); cls.server.server_close(); cls.thread.join(); cls.temp.cleanup()

    def request(self, path, body=None, headers=None, expected=200):
        data = json.dumps(body).encode() if body is not None else None
        req = Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers={"Content-Type":"application/json", **(headers or {})})
        try:
            with urlopen(req) as response: status=response.status; payload=json.loads(response.read()) if response.headers.get_content_type()=="application/json" else response.read()
        except HTTPError as error: status=error.code; payload=json.loads(error.read())
        self.assertEqual(status, expected, payload); return payload

    def test_health_research_dashboard_and_pdf(self):
        self.assertTrue(self.request("/api/health")["ok"])
        self.assertEqual(self.request("/api/researches")["items"][0]["id"], "normal_research")
        dashboard=self.request("/api/researches/normal_research/dashboard")
        self.assertEqual(dashboard["patents"][0]["analysis_state"], "ready")
        req=Request(f"http://127.0.0.1:{self.port}/api/pdfs/JPA%202026000001-000000.pdf")
        with urlopen(req) as response: self.assertEqual(response.headers.get_content_type(), "application/pdf")

    def test_protocol_command_lifecycle_and_codex_gate(self):
        self.assertIn("assert", self.request("/api/ui-protocol")["actions"])
        client="test-client"
        self.request("/api/ui/clients/heartbeat", {"clientId":client,"module":"patent-viewer","targets":[{"agentId":"interpretation-save"}]})
        command=self.request("/api/ui/commands", {"actor":"codex","intent":"fixture save","targetClientId":client,"actions":[{"type":"click","target":{"agentId":"interpretation-save"}}]}, expected=201)
        payload={"environment":"normal","research_id":"normal_research","patent_id":"JPA_2026000001-000000","note":"codex note","source":"codex"}
        self.request("/api/interpretations", payload, expected=403)
        claimed=self.request(f"/api/ui/next?clientId={client}")["command"]
        self.assertEqual(claimed["status"], "running")
        headers={"X-PatentViewer-UI-Command":command["id"],"X-PatentViewer-UI-Client":client}
        self.request("/api/interpretations", payload, headers=headers, expected=201)
        self.request(f"/api/ui/commands/{command['id']}/events", {"clientId":client,"type":"completed"})
        self.assertEqual(self.request(f"/api/ui/commands/{command['id']}")["status"], "completed")

    def test_debug_switch_and_isolated_human_save(self):
        self.request("/api/environment", {"environment":"debug"})
        self.assertEqual(self.request("/api/researches")["items"][0]["id"], "debug_research")
        payload={"environment":"debug","research_id":"debug_research","patent_id":"JPB_000000001-000000","note":"debug human note","source":"human"}
        result=self.request("/api/interpretations", payload, expected=201)
        self.assertTrue(result["path"].startswith("runtime\\debug") or result["path"].startswith("runtime/debug"))
        self.request("/api/environment", {"environment":"normal"})


if __name__ == "__main__": unittest.main()
