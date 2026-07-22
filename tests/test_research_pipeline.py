import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from patent_viewer.pipeline import (
    ANALYSIS_PIPELINE_VERSION,
    ResearchPipeline,
    SourcePolicy,
    atomic_json,
    chunk_text,
    deterministic_clusters,
    cluster_centroids,
    relative_proximity,
    scalable_clusters,
    semantic_cluster_order,
    extract_claims,
    extract_claims_with_metadata,
    split_sections,
    validate_claim_structure,
    validate_score,
)


class ResearchPipelineTests(unittest.TestCase):
    def test_large_clustering_is_bounded_and_deterministic(self):
        items = {
            f"P{index:04d}": [float((index + dimension) % 17) for dimension in range(32)]
            for index in range(501)
        }
        first = scalable_clusters(items, 12)
        second = scalable_clusters(items, 12)
        self.assertEqual(first, second)
        self.assertEqual(set(first), set(items))
        self.assertLessEqual(len(set(first.values())), 12)

    def test_atomic_json_retries_a_transient_windows_file_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "progress.json"
            with patch("patent_viewer.pipeline.os.replace", side_effect=[PermissionError(5, "locked"), None]) as replace:
                atomic_json(path, {"stage": "cooldown"})
            self.assertEqual(replace.call_count, 2)

    def test_rule_based_structure_extracts_sections_and_claims(self):
        text = """
【技術分野】センサを用いる加工技術である。
【発明が解決しようとする課題】加工品質の変動を抑える。
【特許請求の範囲】
【請求項1】センサと、制御部と、を備える加工装置。
【請求項2】請求項1に記載の加工装置であって、レーザーを備えるもの。
"""
        sections = split_sections(text)
        claims = extract_claims(sections["claims"])
        self.assertIn("加工技術", sections["technical_field"])
        self.assertEqual([item["number"] for item in claims], [1, 2])
        self.assertEqual(claims[0]["type"], "independent")
        self.assertEqual(claims[1]["type"], "dependent")
        self.assertEqual(claims[1]["depends_on"], [1])

    def test_claim_references_are_not_treated_as_headers(self):
        text = """
【特許請求の範囲】
【請求項１】センサを備える加工装置。
【請求項２】
請求項１に記載の加工装置であって、制御部を備えるもの。
【発明の詳細な説明】請求項１は例示である。
"""
        claims, metadata = extract_claims_with_metadata(text)
        self.assertEqual([item["number"] for item in claims], [1, 2])
        self.assertEqual(claims[1]["depends_on"], [1])
        self.assertNotIn("発明の詳細な説明", claims[-1]["text"])
        self.assertTrue(metadata["valid"])

    def test_longest_sequential_claim_block_is_selected(self):
        text = "\n".join([
            "【請求項１】A", "【請求項２】B", "【請求項３】C",
            "【請求項１】X", "【請求項２】Y",
        ])
        claims, metadata = extract_claims_with_metadata(text)
        self.assertEqual([item["number"] for item in claims], [1, 2, 3])
        self.assertEqual(metadata["ignored_headers"], 2)
        self.assertFalse(validate_claim_structure([])["valid"])

    def test_source_policy_allows_per_patent_override(self):
        research = {"pipeline": {"source_policy": {"extraction": "cache", "reading": "targeted"}}}
        patent = {"source_policy": {"extraction": "reextract", "reading": "hierarchical_full"}}
        policy = SourcePolicy.from_values(research, patent)
        self.assertEqual(policy.extraction, "reextract")
        self.assertEqual(policy.reading, "hierarchical_full")

    def test_threat_map_contract_is_five_by_five(self):
        value = validate_score({
            "similarity": 5, "concept_level": 1,
            "similarity_reason": "構成が一致", "concept_level_reason": "限定が多い",
        })
        self.assertEqual((value["similarity"], value["concept_level"]), (5, 1))
        with self.assertRaises(ValueError):
            validate_score({"similarity": 6, "concept_level": 1, "similarity_reason": "x", "concept_level_reason": "y"})

    def test_chunks_and_clusters_are_deterministic(self):
        chunks = chunk_text(("a" * 700 + "\n\n") * 4, maximum=1000)
        self.assertGreater(len(chunks), 1)
        vectors = {"a": [1.0] * 128, "b": [0.9] * 128, "c": [-1.0] * 128}
        first = deterministic_clusters(vectors, 2)
        self.assertEqual(first, deterministic_clusters(vectors, 2))
        self.assertEqual(first["a"], first["b"])
        self.assertNotEqual(first["a"], first["c"])

    def test_semantic_order_uses_cosine_direction_and_company_proximity(self):
        vectors = {"a": [1.0, 0.0], "b": [0.9, 0.1], "c": [-1.0, 0.0]}
        assignments = {"a": 0, "b": 1, "c": 2}
        centroids = cluster_centroids(vectors, assignments)
        order = semantic_cluster_order(centroids)
        self.assertEqual(set(order), {0, 1, 2})
        self.assertEqual(abs(order.index(0) - order.index(1)), 1)
        proximity = relative_proximity([1.0, 0.0], centroids)
        self.assertGreater(proximity[0]["relative_strength"], proximity[2]["relative_strength"])

    def test_run_history_is_limited_to_five(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            research_dir = root / "researches/sample"
            research_dir.mkdir(parents=True)
            (research_dir / "research.json").write_text("{}", encoding="utf-8")
            pipeline = ResearchPipeline(root, "normal", "sample")
            for index in range(7):
                run_id = f"20260716T00000{index}Z-test"
                pipeline.write_run_manifest(run_id, "planned", (), {})
                cluster_dir = research_dir / "clustering" / run_id
                cluster_dir.mkdir(parents=True)
                (cluster_dir / "clusters.json").write_text("{}", encoding="utf-8")
            pipeline.write_run_manifest("20260716T000007Z-test", "planned", (), {})
            self.assertEqual(len(list((research_dir / "runs").iterdir())), 5)
            self.assertEqual(len(list((research_dir / "clustering").iterdir())), 5)

    def test_only_current_simple_schema_checkpoint_is_reused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            research_dir = root / "researches/sample"
            artifact = research_dir / "subresearches/group/pipeline/P1"
            artifact.mkdir(parents=True)
            (research_dir / "research.json").write_text("{}", encoding="utf-8")
            pipeline = ResearchPipeline(root, "normal", "sample")
            checkpoint = artifact / "analysis_complete.json"
            checkpoint.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
            self.assertFalse(pipeline.analysis_checkpoint_current("group", "P1.pdf"))
            checkpoint.write_text(json.dumps({"pipeline_version": ANALYSIS_PIPELINE_VERSION}), encoding="utf-8")
            self.assertTrue(pipeline.analysis_checkpoint_current("group", "P1.pdf"))

    def test_prepared_document_can_be_reloaded_for_a_durable_shard(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            research_dir = root / "researches/sample"
            artifact = research_dir / "subresearches/group/pipeline/P1"
            cache = root / "runtime/shared/extractions/hash"
            artifact.mkdir(parents=True)
            cache.mkdir(parents=True)
            (research_dir / "research.json").write_text("{}", encoding="utf-8")
            (cache / "extracted_text.txt").write_text("patent text", encoding="utf-8")
            atomic_json(artifact / "source_decision.json", {
                "shared_cache": str(cache.relative_to(root)), "pdf_sha256": "hash",
            })
            atomic_json(artifact / "document_structure.json", {
                "claims": [{"number": 1, "type": "independent", "text": "claim"}], "sections": {},
            })
            pipeline = ResearchPipeline(root, "normal", "sample")
            prepared = pipeline.load_prepared_document("group", "P1.pdf")
            self.assertEqual(prepared["artifact_dir"], artifact)
            self.assertEqual(prepared["cache_dir"], cache.resolve())
            self.assertEqual(prepared["structure"]["claims"][0]["number"], 1)

    def test_staged_analysis_finalizes_ui_compatible_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            research_dir = root / "researches/sample"
            artifact = research_dir / "subresearches/group/pipeline/P1"
            cache = root / "runtime/shared/extractions/hash"
            artifact.mkdir(parents=True)
            cache.mkdir(parents=True)
            (research_dir / "research.json").write_text(json.dumps({
                "company_technology": "自社技術", "pipeline": {"source_policy": {"reading": "targeted"}}
            }), encoding="utf-8")
            (cache / "extracted_text.txt").write_text("技術分野の説明。" * 100, encoding="utf-8")
            prepared = {
                "artifact_dir": artifact,
                "cache_dir": cache,
                "source_decision": {"reading_policy": "targeted"},
                "structure": {
                    "claims": [{"number": 1, "type": "independent", "text": "装置。"}],
                    "sections": {"technical_field": "加工技術", "problem": "品質変動"},
                },
            }

            def generate(task, data):
                return {
                    "similarity": {"similarity": 3, "reason": "一部一致"},
                    "concept_level": {"concept_level": 4, "reason": "限定が少ない"},
                    "problem_summary": {"problem_summary": "課題"},
                    "technology_summary": {"tech_summary": "技術"},
                    "cluster_name": {"name": "分類"},
                }[task]

            pipeline = ResearchPipeline(root, "normal", "sample")
            analysis = pipeline.analyze_document(prepared, generate, lambda texts: [[float(index)] * 128 for index, _ in enumerate(texts, 1)])
            for task in ("similarity", "concept_level", "problem_summary", "technology_summary"):
                self.assertTrue(pipeline.analysis_task_current(prepared, task))
            company_reference = pipeline.build_company_reference(
                {"technology_summary": "自社技術", "problem_summary": "自社課題"},
                [[1.0] * 128, [1.0] * 128],
            )
            finalized = pipeline.finalize_research({"P1": analysis}, {"P1": "group"}, generate, "run-1", company_reference=company_reference)
            result_path = root / finalized["results"]["P1"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["similarity"], 3)
            self.assertEqual(result["concept_level"], 4)
            self.assertEqual(result["tech_cluster"], "分類")
            self.assertEqual(result["tech_cluster_id"], 0)
            self.assertEqual(result["problem_cluster_id"], 0)
            clusters = json.loads((research_dir / "clustering/run-1/clusters.json").read_text(encoding="utf-8"))
            self.assertEqual(clusters["semantic_ordering"]["technology_order"], [0])
            self.assertIn("company_proximity", clusters)
            reloaded = pipeline.load_analysis_artifacts("group", "P1.pdf")
            self.assertEqual(reloaded["score"]["similarity"], 3)
            self.assertEqual(reloaded["summaries"]["tech_summary"], "技術")
            self.assertEqual(reloaded["embedding"]["input_order"], ["technology", "problem"])

            error_path = artifact / "analysis_error.json"
            error_path.write_text(json.dumps({"error": "invalid JSON"}), encoding="utf-8")
            pdf_path = root / "patent_pool/P1.pdf"
            pdf_path.parent.mkdir(parents=True)
            pdf_path.write_bytes(b"pdf")
            pipeline.patents = lambda: [("group", pdf_path, {})]
            overview = pipeline.overview()
            self.assertEqual(overview["counts"]["failed"], 1)
            self.assertEqual(overview["documents"][0]["analysis_error"]["error"], "invalid JSON")


if __name__ == "__main__":
    unittest.main()
