import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
from run_single_llm_test import atomic_json, validate_analysis, validate_embeddings
from run_research_pipeline import (
    DocumentAnalysisError,
    OllamaStages,
    STAGE_SCHEMAS,
    context_window_for,
    extraction_index_entry,
    validate_schema_value,
    write_progress_event,
    write_extraction_index,
)


class SingleLlmPipelineTests(unittest.TestCase):
    def test_progress_file_failure_does_not_stop_pipeline(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "progress.json"
            with patch("run_research_pipeline.atomic_json", side_effect=PermissionError(5, "locked")):
                self.assertFalse(write_progress_event(path, {"stage": "cooldown"}))

    def test_analysis_contract(self):
        value = {
            "similarity": 1, "concept_level": 3,
            "tech_summary": "技術", "problem_summary": "課題", "reasoning": "根拠",
            "tech_cluster": "技術分類", "problem_cluster": "課題分類",
        }
        self.assertEqual(validate_analysis(value)["similarity"], 1)
        with self.assertRaises(ValueError): validate_analysis({**value, "similarity": 0})
        with self.assertRaises(ValueError): validate_analysis({**value, "unexpected": True})

    def test_embedding_contract(self):
        vectors = validate_embeddings({"embeddings": [[0.0] * 128, [1.0] * 128]}, 2)
        self.assertEqual(len(vectors[0]), 128)
        with self.assertRaises(ValueError): validate_embeddings({"embeddings": [[]]}, 2)

    def test_atomic_json(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "result.json"; atomic_json(path, {"日本語": "正常"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["日本語"], "正常")
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_context_window_is_selected_from_prompt_size(self):
        self.assertEqual(context_window_for("短い入力"), 4096)
        self.assertEqual(context_window_for("文" * 4_000), 8192)
        self.assertEqual(context_window_for("文" * 10_000), 16384)
        with self.assertRaises(RuntimeError): context_window_for("文" * 20_000)

    def test_extraction_index_is_human_traceable(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / "runtime/shared/extractions/hash"
            artifact = root / "researches/r/pipeline/JP_A_2026_1"
            cache.mkdir(parents=True)
            artifact.mkdir(parents=True)
            (cache / "extraction_manifest.json").write_text(json.dumps({
                "pdf_sha256": "hash", "pages": 2, "nonempty_pages": 2, "characters": 123,
                "extractor": "pypdf", "ocr_used": False,
            }), encoding="utf-8")
            entry = extraction_index_entry(root, "特開2026-1.pdf", {
                "cache_dir": cache, "artifact_dir": artifact,
                "source_decision": {"cache_decision": "reused"},
            })
            self.assertEqual(entry["pdf"], "特開2026-1.pdf")
            self.assertEqual(entry["characters"], 123)
            self.assertEqual(entry["extracted_text"], str(cache.relative_to(root) / "extracted_text.txt"))

            class Pipeline:
                research_dir = root / "researches/r"
                research_id = "r"
                environment = "normal"

            path = write_extraction_index(root, Pipeline(), [entry])
            index = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(index["document_count"], 1)
            self.assertEqual(index["total_pages"], 2)
            self.assertEqual(index["total_characters"], 123)

    def test_ollama_json_retries_an_incomplete_response(self):
        progress = []
        stages = OllamaStages("model", "embedding", 1, progress=lambda stage, **detail: progress.append((stage, detail)))
        payloads = []
        responses = iter([
            {"response": '{"partial":', "done": False},
            {"response": json.dumps({"similarity": 3, "reason": "一部一致"}), "done": True},
        ])
        def post(endpoint, payload):
            payloads.append(payload)
            return next(responses)
        stages._post = post
        result = stages.generate_json("similarity", {"company_technology": "自社", "patent_text": "特許"})
        self.assertEqual(result["similarity"], 3)
        self.assertEqual(stages.counter, 2)
        self.assertIsInstance(payloads[0]["format"], dict)
        self.assertEqual(payloads[0]["format"]["required"], ["similarity", "reason"])
        self.assertIn(payloads[0]["options"]["num_ctx"], (4096, 8192, 16384))
        self.assertEqual([item[1]["attempt"] for item in progress], [1, 2])
        self.assertTrue(all(item[0] == "llm_task" for item in progress))
        self.assertEqual(progress[-1][1]["task_index"], 1)

    def test_embedding_reports_fifth_document_task(self):
        progress = []
        stages = OllamaStages("model", "embedding", 1, progress=lambda stage, **detail: progress.append((stage, detail)))
        stages.unload_generation = lambda: None
        stages._post = lambda endpoint, payload: {"embeddings": [[0.0] * 128, [1.0] * 128]}
        stages.embed(["技術", "課題"])
        self.assertEqual(progress, [("llm_task", {
            "task": "embeddings", "task_index": 5, "task_total": 5,
            "attempt": 1, "max_attempts": 1,
        })])

    def test_ollama_json_exhaustion_is_a_document_error(self):
        stages = OllamaStages("model", "embedding", 1)
        stages._post = lambda endpoint, payload: {"response": '{"partial":', "done": True, "done_reason": "length"}
        with self.assertRaises(DocumentAnalysisError):
            stages.generate_json("similarity", {"company_technology": "自社", "patent_text": "特許"})
        self.assertEqual(stages.counter, 3)

    def test_small_schema_rejects_extra_keys_and_long_text(self):
        schema = STAGE_SCHEMAS["similarity"]
        validate_schema_value({"similarity": 5, "reason": "一致"}, schema)
        with self.assertRaises(ValueError):
            validate_schema_value({"similarity": 5, "reason": "一致", "extra": True}, schema)
        with self.assertRaises(ValueError):
            validate_schema_value({"similarity": 0, "reason": "不正"}, schema)
        with self.assertRaises(ValueError):
            validate_schema_value({"similarity": 3, "reason": "x" * 801}, schema)


if __name__ == "__main__": unittest.main()
