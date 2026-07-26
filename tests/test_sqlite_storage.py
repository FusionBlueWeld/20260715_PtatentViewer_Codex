import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.support import build_fixture

import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from patent_viewer.domain import Repository
from patent_viewer.storage import SCHEMA_VERSION, SQLiteStore, decode_vector, encode_vector


class SQLiteStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        build_fixture(self.root)
        manifest_path = self.root / "researches/normal_research/patents.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["patents"][0].update(
            {"title": "Laser processing apparatus", "applicant": "Example Corp"}
        )
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.repository = Repository(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_repository_builds_current_schema_and_lightweight_dashboard(self):
        dashboard = self.repository.dashboard(
            "normal", "normal_research", include_patents=False
        )
        self.assertEqual(dashboard["patents"], [])
        self.assertEqual(dashboard["aggregates"]["metrics"]["total"], 1)
        store = self.repository.store("normal")
        self.assertTrue(store.path.is_file())
        connection = sqlite3.connect(store.path)
        try:
            version = connection.execute(
                "SELECT MAX(version) FROM schema_migrations"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(version, SCHEMA_VERSION)

    def test_pipeline_counts_treat_retryable_failure_as_pending_over_old_result(self):
        self.repository.dashboard("normal", "normal_research")
        store = self.repository.store("normal")
        counts = store.pipeline_counts(
            "normal_research", {"JPA_2026000001-000000"}
        )
        self.assertEqual(counts["total"], 1)
        self.assertEqual(counts["finalized"], 0)
        self.assertEqual(counts["pending"], 1)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(counts["llm_pending"], 1)

    def test_document_query_pages_and_searches_in_sqlite(self):
        page = self.repository.document_page(
            "normal", "normal_research", query="Laser", limit=1, offset=0
        )
        self.assertEqual(page["total"], 1)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["items"][0]["title"], "Laser processing apparatus")
        empty = self.repository.document_page(
            "normal", "normal_research", query="not-present", limit=1, offset=0
        )
        self.assertEqual(empty["total"], 0)

    def test_embeddings_are_float32_blobs(self):
        self.repository.dashboard("normal", "normal_research")
        store: SQLiteStore = self.repository.store("normal")
        patent_id = "JPA_2026000001-000000"
        dimensions = 64
        embedding = {
            "dimensions": dimensions,
            "vectors": {
                "technology": [0.25] * dimensions,
                "problem": [-0.5] * dimensions,
            },
            "input_sha256": ["a", "b"],
            "created_at": "2026-07-25T00:00:00+00:00",
        }
        self.assertTrue(store.put_embedding("normal_research", patent_id, embedding))
        connection = sqlite3.connect(store.path)
        try:
            row = connection.execute(
                """
                SELECT dimensions, length(technology), length(problem)
                FROM embeddings WHERE research_id=? AND patent_id=?
                """,
                ("normal_research", patent_id),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row, (dimensions, dimensions * 4, dimensions * 4))
        loaded = store.get_embedding("normal_research", patent_id)
        self.assertAlmostEqual(loaded["vectors"]["technology"][0], 0.25)
        self.assertEqual(len(encode_vector([1.0, 2.0])), 8)
        self.assertEqual(decode_vector(encode_vector([1.0, 2.0])), [1.0, 2.0])
        store.set_document_state(
            "normal_research",
            patent_id,
            "skipped",
            skip_reason="manual_review_required",
        )
        self.assertEqual(
            store.embedding_summary("normal_research")["count"],
            0,
        )
        self.assertEqual(
            list(store.iter_embedding_blobs("normal_research", "technology")),
            [],
        )

    def test_verified_claim_trace_survives_sqlite_result_round_trip(self):
        self.repository.dashboard("normal", "normal_research")
        trace = {
            "similarity": {
                "criterion": "similarity",
                "verified": True,
                "claim_number": 1,
                "claim_text": "A verified source claim.",
                "claim_sha256": "a" * 64,
            }
        }
        self.repository.store("normal").upsert_analysis_result(
            "normal_research",
            "JPA_2026000001-000000",
            {"claim_traceability": trace},
        )
        page = self.repository.document_page(
            "normal", "normal_research", limit=1, offset=0
        )
        self.assertEqual(page["items"][0]["claim_traceability"], trace)


if __name__ == "__main__":
    unittest.main()
