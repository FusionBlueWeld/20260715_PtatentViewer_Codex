import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests.support import build_fixture

import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from patent_viewer.domain import DataError, Repository, infer_status, infer_year, patent_key


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
