import hashlib
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import build_fixture

import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from patent_viewer.domain import DataError, PATENT_LIST_HEADERS, Repository, infer_status, infer_year, patent_key, safe_id


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name); build_fixture(self.root); self.repo = Repository(self.root)

    def tearDown(self): self.temp.cleanup()

    def test_number_metadata(self):
        self.assertEqual(infer_year("JPA 2026000001-000000"), 2026)
        self.assertEqual(infer_status("JPA 2026000001-000000"), "published")
        self.assertEqual(infer_status("JPB 000000001-000000"), "registered")
        self.assertEqual(patent_key("JPA 2026000001-000000.pdf"), "JPA_2026000001-000000")

    def test_production_pdf_filename_formats(self):
        cases = [
            ("WO2024-123456.pdf", "WO_2024_123456", 2024, "published"),
            ("特開2024-123456.pdf", "JP_A_2024_123456", 2024, "published"),
            ("特表2024-123456.pdf", "JP_AT_2024_123456", 2024, "published"),
            ("特許第7654321号.pdf", "JP_B_7654321", None, "registered"),
            ("特開平5-123456.pdf", "JP_A_H05_123456", 1993, "published"),
            ("特表平11-654321.pdf", "JP_AT_H11_654321", 1999, "published"),
        ]
        for filename, key, year, status in cases:
            with self.subTest(filename=filename):
                publication = Path(filename).stem
                self.assertEqual(patent_key(filename), key)
                self.assertEqual(infer_year(publication), year)
                self.assertEqual(infer_status(publication), status)
                self.assertEqual(safe_id(key), key)

    def test_full_width_and_hyphen_variants_are_normalized(self):
        self.assertEqual(patent_key("特開２０２４－１２３４５６.pdf"), "JP_A_2024_123456")
        self.assertEqual(infer_year("特開平３－１２３４５６"), 1991)

    def test_dashboard_accepts_japanese_publication_filename(self):
        filename = "特開平5-123456.pdf"
        (self.root / "patent_pool" / filename).write_bytes(b"%PDF-1.4\n%%EOF")
        manifest_path = self.root / "researches/normal_research/subresearches/sample/patents.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["patents"].append({"pdf": filename})
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        patent = next(item for item in self.repo.dashboard("normal", "normal_research")["patents"] if item["pdf"] == filename)
        self.assertEqual(patent["id"], "JP_A_H05_123456")
        self.assertEqual(patent["year"], 1993)
        self.assertEqual(patent["status"], "published")
        self.assertEqual(self.repo.pdf_path(filename).name, filename)

    def test_company_tech_file_overrides_legacy_research_field(self):
        research_dir = self.root / "researches/normal_research"
        (research_dir / "company_tech.txt").write_text("リサーチ固有の自社技術", encoding="utf-8")
        self.assertEqual(self.repo.list_researches("normal")[0]["company_technology"], "リサーチ固有の自社技術")
        self.assertEqual(self.repo.dashboard("normal", "normal_research")["research"]["company_technology"], "リサーチ固有の自社技術")

    def test_missing_pdf_remains_visible_as_skipped(self):
        manifest_path = self.root / "researches/normal_research/subresearches/sample/patents.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["patents"].append({"pdf": "WO2024-999999.pdf", "title": "missing document"})
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        patent = next(item for item in self.repo.dashboard("normal", "normal_research")["patents"] if item["pdf"] == "WO2024-999999.pdf")
        self.assertFalse(patent["pdf_available"])
        self.assertEqual(patent["analysis_state"], "skipped")
        self.assertEqual(patent["skip_reason"], "pdf_not_found")

    def test_latest_cp932_csv_is_loaded_and_pdf_priority_is_applied(self):
        research = self.root / "researches/production_csv"
        research.mkdir()
        (research / "company_tech.txt").write_text("自社技術", encoding="utf-8")
        for filename in ("特開2024-001234.pdf", "WO2022-0442209.pdf", "特表2024-530685.pdf", "特許第7654321号.pdf"):
            (self.root / "patent_pool" / filename).write_bytes(b"%PDF-1.4\n%%EOF")

        def write_csv(name, rows):
            output = io.StringIO(newline="")
            writer = csv.writer(output, lineterminator="\r\n")
            writer.writerow(PATENT_LIST_HEADERS)
            writer.writerows(rows)
            (research / name).write_bytes(output.getvalue().encode("cp932"))

        write_csv("patent_list_20260715000000.csv", [["1", "1.0", "特願2020-1", "2020.01.01", "特開2020-1", "2020.02.01", "", "", "old", "old", "通常審査中"]])
        write_csv("patent_list_20260716000000.csv", [
            ["1", "85.3", "特願2024-1", "2024.01.01", "特開2024-1234", "2024.02.01", "", "", "A社", "零埋め照合", "通常審査中"],
            ["2", "70.0", "特願2024-2", "2024.01.02", "特表2024-530685,WO2022/0442209", "2023.06.13,2021.11.12", "特許第7654321号", "2026.03.10", "B社", "登録優先", "登録（権利有）"],
            ["3", "10.0", "特願2024-3", "2024.01.03", "特開2024-999999", "2024.02.03", "", "", "C社", "PDFなし", "審査請求無し"],
        ])

        listed = next(item for item in self.repo.list_researches("normal") if item["id"] == "production_csv")
        self.assertEqual(listed["input"]["selected_csv"], "patent_list_20260716000000.csv")
        self.assertEqual(listed["input"]["excluded_older_csvs"], ["patent_list_20260715000000.csv"])
        dashboard = self.repo.dashboard("normal", "production_csv")
        self.assertEqual(len(dashboard["patents"]), 3)
        self.assertEqual(dashboard["patents"][0]["pdf"], "特開2024-001234.pdf")
        self.assertEqual(dashboard["patents"][1]["pdf"], "特許第7654321号.pdf")
        self.assertEqual(dashboard["patents"][1]["source_ai_score"], 70.0)
        self.assertEqual(dashboard["patents"][2]["analysis_state"], "skipped")

    def test_preflight_requires_valid_claim_structure(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self):
                return json.dumps({"models": [{"name": "gemma4:e4b"}, {"name": "qwen3-embedding:8b"}]}).encode()

        with patch("patent_viewer.domain.urllib.request.urlopen", return_value=Response()):
            blocked = self.repo.preflight("normal", "normal_research")
            claim_check = next(item for item in blocked["checks"] if item["id"] == "claim-structure")
            self.assertFalse(claim_check["ok"])
            structure = self.root / "researches/normal_research/subresearches/sample/pipeline/JPA_2026000001-000000/document_structure.json"
            structure.parent.mkdir(parents=True)
            structure.write_text(json.dumps({
                "schema_version": 2, "claims": [{"number": 1, "text": "claim"}],
                "claim_validation": {"valid": True, "errors": []},
            }), encoding="utf-8")
            ready = self.repo.preflight("normal", "normal_research")
            self.assertTrue(next(item for item in ready["checks"] if item["id"] == "claim-structure")["ok"])
            self.assertTrue(ready["ready"])

    def test_preflight_allows_recorded_document_skip(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self):
                return json.dumps({"models": [{"name": "gemma4:e4b"}, {"name": "qwen3-embedding:8b"}]}).encode()

        second_pdf = "JPA 2026000002-000000.pdf"
        (self.root / "patent_pool" / second_pdf).write_bytes(b"%PDF-1.4\n%%EOF")
        manifest_path = self.root / "researches/normal_research/subresearches/sample/patents.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["patents"].append({"pdf": second_pdf, "year": 2026})
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        first = self.root / "researches/normal_research/subresearches/sample/pipeline/JPA_2026000001-000000/document_structure.json"
        first.parent.mkdir(parents=True)
        first.write_text(json.dumps({
            "schema_version": 2, "claims": [{"number": 1, "text": "claim"}],
            "claim_validation": {"valid": True, "errors": []},
        }), encoding="utf-8")
        skipped = self.root / "researches/normal_research/subresearches/sample/pipeline/JPA_2026000002-000000/skip.json"
        skipped.parent.mkdir(parents=True)
        skipped.write_text(json.dumps({"reason": "image_only_or_insufficient_text"}), encoding="utf-8")

        with patch("patent_viewer.domain.urllib.request.urlopen", return_value=Response()):
            result = self.repo.preflight("normal", "normal_research")
        self.assertTrue(result["ready"])
        detail = next(item for item in result["checks"] if item["id"] == "claim-structure")["detail"]
        self.assertIn("スキップ 1件", detail)

    def test_environment_researches_are_separate(self):
        self.assertEqual(self.repo.list_researches("normal")[0]["id"], "normal_research")
        self.assertEqual(self.repo.list_researches("debug")[0]["id"], "debug_research")
        self.assertEqual(self.repo.dashboard("normal", "normal_research")["patents"][0]["status"], "published")
        self.assertEqual(self.repo.dashboard("debug", "debug_research")["patents"][0]["status"], "registered")

    def test_debug_write_does_not_change_normal(self):
        normal = self.root / "runtime/normal"
        before = hashlib.sha256(b"").hexdigest()
        self.repo.save_interpretation("debug", {"research_id":"debug_research","patent_id":"JPB_000000001-000000","note":"debug only"})
        after = hashlib.sha256(b"" if not normal.exists() else b"unexpected").hexdigest()
        self.assertEqual(before, after)
        self.assertTrue((self.root / "runtime/debug/interpretations/debug_research/JPB_000000001-000000.json").exists())

    def test_path_traversal_rejected(self):
        with self.assertRaises(DataError): self.repo.pdf_path("../secret.pdf")
        manifest = self.root / "researches/normal_research/subresearches/sample/patents.json"
        manifest.write_text(json.dumps({"patents":[{"pdf":"../outside.pdf"}]}), encoding="utf-8")
        with self.assertRaises(DataError): self.repo.dashboard("normal", "normal_research")


if __name__ == "__main__": unittest.main()
