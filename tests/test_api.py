import base64
import json
import tempfile
import threading
import unittest
from dataclasses import replace
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
        self.assertNotIn("subresearches", self.request("/api/researches")["items"][0])
        dashboard=self.request("/api/researches/normal_research/dashboard")
        self.assertEqual(dashboard["patents"][0]["analysis_state"], "ready")
        self.assertNotIn("subresearch_id", dashboard["patents"][0])
        req=Request(f"http://127.0.0.1:{self.port}/api/pdfs/JPA%202026000001-000000.pdf")
        with urlopen(req) as response: self.assertEqual(response.headers.get_content_type(), "application/pdf")

    def test_organization_group_api(self):
        manifest_path = self.root / "researches/normal_research/patents.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["patents"][0]["applicant"] = "Company A;Company B"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        dashboard = self.request("/api/researches/normal_research/dashboard")
        organizations = dashboard["organization_registry"]["organizations"]
        saved = self.request("/api/researches/normal_research/organization-groups", {
            "environment": "normal", "scope": "common", "name": "Company Group",
            "member_ids": [item["id"] for item in organizations], "members": organizations,
        }, expected=201)
        self.assertTrue(saved["ok"])
        refreshed = self.request("/api/researches/normal_research/dashboard")
        self.assertEqual(refreshed["organization_registry"]["groups"][0]["name"], "Company Group")
        self.request("/api/researches/normal_research/organization-groups", {
            "environment": "normal", "scope": "common", "action": "delete", "id": saved["group"]["id"],
        })

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
        self.assertEqual(overview["counts"]["available"], 1)
        self.assertEqual(overview["counts"]["pending"], 0)
        self.assertEqual(overview["threat_map"], {})
        next((self.root / "researches/normal_research/results").glob("*.json")).unlink()
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
        original_config = self.server.app_state.runtime_config
        self.server.app_state.runtime_config = replace(original_config, durable_shards=True)
        try:
            with patch("patent_viewer.server.subprocess.Popen") as popen, patch.object(self.server.app_state.repo, "preflight", return_value={"ready": True}):
                process = popen.return_value
                process.poll.return_value = None
                job = self.request("/api/researches/normal_research/pipeline/jobs", {
                    "environment": "normal", "mode": "execute", "source": "human",
                    "confirmation": "RUN_LOCAL_LLM", "cooldown_seconds": 30, "overwrite": True,
                }, expected=202)
                self.assertEqual(job["cooldown_seconds"], 30)
                self.assertTrue(job["overwrite"])
                command = popen.call_args.args[0]
                self.assertIn("--overwrite", command)
                self.assertIn("--cooldown-seconds", command)
                self.assertEqual(command[command.index("--cooldown-seconds") + 1], "30")
                self.assertIn("--ollama-url", command)
                self.assertIn("--generation-workers", command)
                self.assertIn("--embedding-batch-size", command)
                self.assertIn("--shard-size", command)
                self.assertIn("--cooldown-every-documents", command)
                self.assertIn("--durable-shards", command)
                self.assertTrue(job["runtime_config"]["durable_shards"])
                process.poll.return_value = 0
                self.assertEqual(self.request(f"/api/pipeline/jobs/{job['id']}")["status"], "completed")
        finally:
            self.server.app_state.runtime_config = original_config

    def test_execute_job_rejects_cooldown_above_ui_limit(self):
        with patch.object(self.server.app_state.repo, "preflight", return_value={"ready": True}):
            self.request("/api/researches/normal_research/pipeline/jobs", {
                "environment": "normal", "mode": "execute", "source": "human",
                "confirmation": "RUN_LOCAL_LLM", "cooldown_seconds": 181,
            }, expected=400)

    def test_ui_exposes_full_reanalysis_control(self):
        project = Path(__file__).parents[1]
        html = (project / "public/index.html").read_text(encoding="utf-8")
        script = (project / "public/assets/app.js").read_text(encoding="utf-8")
        self.assertIn('id="pipeline-overwrite"', html)
        self.assertIn("全件を再分析", html)
        self.assertIn("const overwrite=mode==='execute'&&$('#pipeline-overwrite').checked", script)
        self.assertIn('id="pipeline-research-select"', html)
        self.assertIn('id="research-create-form"', html)
        self.assertIn('id="year-from"', html)
        self.assertIn('id="year-to"', html)
        self.assertIn('id="legal-status-settings"', html)
        self.assertIn('id="rights-acquired-tags"', html)
        self.assertIn('id="sidebar-resizer"', html)
        self.assertIn("SIDEBAR_MAX_WIDTH=500", script)
        self.assertIn("p.legal_status_category||'published'", script)

    def test_legal_status_rule_api_uses_exact_match_and_application_year(self):
        manifest_path = self.root / "researches/normal_research/patents.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["patents"][0].update({
            "application_date": "2021-04-05",
            "source_status": "通常審査中",
        })
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        dashboard = self.request("/api/researches/normal_research/dashboard")
        self.assertEqual(dashboard["patents"][0]["year"], 2021)
        self.assertEqual(dashboard["patents"][0]["legal_status_category"], "under_examination")
        result = self.request("/api/researches/normal_research/legal-status-rules", {
            "environment": "normal",
            "rights_acquired": ["通常審査中"],
            "under_examination": ["早期審査中"],
        })
        self.assertTrue(result["ok"])
        refreshed = self.request("/api/researches/normal_research/dashboard")
        self.assertEqual(refreshed["patents"][0]["legal_status_category"], "rights_acquired")
        self.request("/api/researches/normal_research/legal-status-rules", {
            "environment": "normal",
            "rights_acquired": ["重複"],
            "under_examination": ["重複"],
        }, expected=400)

    def test_research_create_csv_history_archive_and_restore(self):
        header = "No,AIスコア,出願番号,出願日,公開・公表番号,公開・公表日,登録番号,登録日,出願人・権利者名,発明の名称,ステイタス\r\n"
        row = "1,,,,特開2026-1,2026.01.01,,,Company,Managed patent,通常審査中\r\n"

        def upload(filename, text=row):
            return {
                "environment": "normal", "csv_filename": filename,
                "csv_base64": base64.b64encode((header + text).encode("cp932")).decode(),
            }

        created = self.request("/api/researches", {
            **upload("patent_list_20260724090000.csv"),
            "id": "z_managed_research", "name": "Managed Research",
            "description": "created from UI", "company_technology": "managed technology",
        }, expected=201)
        self.assertEqual(created["document_count"], 1)
        all_items = self.request("/api/researches?status=all")["items"]
        managed = next(item for item in all_items if item["id"] == "z_managed_research")
        self.assertEqual(managed["csv_history"][0]["filename"], "patent_list_20260724090000.csv")

        older = self.request("/api/researches/z_managed_research/csvs", upload("patent_list_20260101000000.csv"), expected=201)
        self.assertFalse(older["active_changed"])
        newer = self.request("/api/researches/z_managed_research/csvs", upload("patent_list_20260725090000.csv"), expected=201)
        self.assertTrue(newer["active_changed"])
        managed = next(item for item in self.request("/api/researches?status=all")["items"] if item["id"] == "z_managed_research")
        self.assertTrue(managed["lifecycle"]["analysis_stale"])
        self.request("/api/researches/z_managed_research/pipeline/jobs", {
            "environment": "normal", "mode": "execute", "source": "human",
            "confirmation": "RUN_LOCAL_LLM",
        }, expected=400)

        self.request("/api/researches/z_managed_research/archive", {"environment": "normal"})
        self.assertNotIn("z_managed_research", [item["id"] for item in self.request("/api/researches")["items"]])
        self.assertIn("z_managed_research", [item["id"] for item in self.request("/api/researches?status=archived")["items"]])
        self.request("/api/researches/z_managed_research/restore", {"environment": "normal"})
        self.assertIn("z_managed_research", [item["id"] for item in self.request("/api/researches")["items"]])

    def test_debug_switch_and_isolated_human_save(self):
        self.request("/api/environment", {"environment":"debug"})
        self.assertEqual(self.request("/api/researches")["items"][0]["id"], "debug_research")
        payload={"environment":"debug","research_id":"debug_research","patent_id":"JPB_000000001-000000","note":"debug human note","source":"human"}
        result=self.request("/api/interpretations", payload, expected=201)
        self.assertTrue(result["path"].startswith("runtime\\debug") or result["path"].startswith("runtime/debug"))
        self.request("/api/environment", {"environment":"normal"})


if __name__ == "__main__": unittest.main()
