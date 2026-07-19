import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

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

    def test_pipeline_overview_and_execute_confirmation_guard(self):
        overview = self.request("/api/researches/normal_research/pipeline?environment=normal")
        self.assertEqual(overview["counts"]["total"], 1)
        self.assertEqual(overview["counts"]["pending"], 0)
        self.assertEqual(overview["threat_map"], {})
        next((self.root / "researches/normal_research/subresearches/sample/results").glob("*.json")).unlink()
        overview = self.request("/api/researches/normal_research/pipeline?environment=normal")
        self.assertEqual(overview["counts"]["pending"], 1)
        self.request("/api/researches/normal_research/pipeline/jobs", {
            "environment": "normal", "mode": "execute", "source": "human"
        }, expected=400)

    def test_pipeline_job_start_and_control_contract(self):
        with patch("patent_viewer.server.subprocess.Popen") as popen:
            process = popen.return_value
            process.poll.return_value = None
            job = self.request("/api/researches/normal_research/pipeline/jobs", {
                "environment": "normal", "mode": "prepare", "source": "human", "cooldown_seconds": 30
            }, expected=202)
            self.assertEqual(job["status"], "running")
            self.assertEqual(job["cooldown_seconds"], 0)
            paused = self.request(f"/api/pipeline/jobs/{job['id']}/control", {"control": "pause"})
            self.assertEqual(paused["status"], "paused")
            resumed = self.request(f"/api/pipeline/jobs/{job['id']}/control", {"control": "run"})
            self.assertEqual(resumed["status"], "running")
            process.poll.return_value = 0
            self.assertEqual(self.request(f"/api/pipeline/jobs/{job['id']}")["status"], "completed")

    def test_execute_job_passes_cooldown_to_local_pipeline(self):
        with patch("patent_viewer.server.subprocess.Popen") as popen, patch.object(self.server.app_state.repo, "preflight", return_value={"ready": True}):
            process = popen.return_value
            process.poll.return_value = None
            job = self.request("/api/researches/normal_research/pipeline/jobs", {
                "environment": "normal", "mode": "execute", "source": "human",
                "confirmation": "RUN_LOCAL_LLM", "cooldown_seconds": 30,
            }, expected=202)
            self.assertEqual(job["cooldown_seconds"], 30)
            command = popen.call_args.args[0]
            self.assertIn("--cooldown-seconds", command)
            self.assertEqual(command[command.index("--cooldown-seconds") + 1], "30")
            self.assertIn("--ollama-url", command)
            self.assertIn("--generation-workers", command)
            self.assertIn("--embedding-batch-size", command)
            self.assertIn("--shard-size", command)
            self.assertIn("--cooldown-every-documents", command)
            self.assertIn("runtime_config", job)
            process.poll.return_value = 0
            self.assertEqual(self.request(f"/api/pipeline/jobs/{job['id']}")["status"], "completed")

    def test_debug_switch_and_isolated_human_save(self):
        self.request("/api/environment", {"environment":"debug"})
        self.assertEqual(self.request("/api/researches")["items"][0]["id"], "debug_research")
        payload={"environment":"debug","research_id":"debug_research","patent_id":"JPB_000000001-000000","note":"debug human note","source":"human"}
        result=self.request("/api/interpretations", payload, expected=201)
        self.assertTrue(result["path"].startswith("runtime\\debug") or result["path"].startswith("runtime/debug"))
        self.request("/api/environment", {"environment":"normal"})


if __name__ == "__main__": unittest.main()
