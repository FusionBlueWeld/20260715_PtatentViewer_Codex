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
    constrained_stage_schema,
    context_window_for,
    extraction_index_entry,
    main as run_pipeline_main,
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
            {
                "response": json.dumps({
                    "similarity": 3,
                    "reason": "一部一致",
                    "claim_number": 1,
                    "evidence_element_ids": ["C1-E1"],
                }),
                "done": True,
            },
        ])
        def post(endpoint, payload):
            payloads.append(payload)
            return next(responses)
        stages._post = post
        result = stages.generate_json("similarity", {"company_technology": "自社", "patent_text": "特許"})
        self.assertEqual(result["similarity"], 3)
        self.assertEqual(stages.counter, 2)
        self.assertIsInstance(payloads[0]["format"], dict)
        self.assertEqual(
            payloads[0]["format"]["required"],
            ["similarity", "reason", "claim_number", "evidence_element_ids"],
        )
        self.assertIn(payloads[0]["options"]["num_ctx"], (4096, 8192, 16384))
        self.assertEqual([item[1]["attempt"] for item in progress], [1, 2])
        self.assertTrue(all(item[0] == "llm_task" for item in progress))
        self.assertEqual(progress[-1][1]["task_index"], 1)

    def test_grounding_repair_prompt_restricts_evidence_candidates(self):
        stages = OllamaStages("model", "embedding", 1)
        payloads = []

        def post(endpoint, payload):
            payloads.append(payload)
            return {
                "response": json.dumps({
                    "similarity": 3,
                    "reason": "構成が一致する",
                    "claim_number": 1,
                    "evidence_element_ids": ["C1-E1"],
                }),
                "done": True,
            }

        stages._post = post
        stages.generate_json("similarity", {
            "company_technology": "画像検査",
            "patent_text": "[C1-E1] 検査装置",
            "grounding_repair": {
                "allowed_ids": ["C1-E1"],
                "previous_ids": ["C9-E9"],
            },
        })

        self.assertIn("Python検証による根拠ID再生成", payloads[0]["prompt"])
        self.assertIn('["C1-E1"]', payloads[0]["prompt"])

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

    def test_document_constraints_narrow_schema_without_mutating_base(self):
        schema = constrained_stage_schema("similarity", {
            "allowed_evidence_ids": ["C1-E1", "C2-E1"],
            "allowed_claim_numbers": [1, 2],
        })
        valid = {
            "similarity": 4,
            "reason": "一致する",
            "claim_number": 2,
            "evidence_element_ids": ["C2-E1"],
        }
        validate_schema_value(valid, schema)
        with self.assertRaises(ValueError):
            validate_schema_value(
                {**valid, "evidence_element_ids": ["C9-E9"]},
                schema,
            )
        with self.assertRaises(ValueError):
            validate_schema_value({**valid, "claim_number": 3}, schema)
        self.assertNotIn(
            "enum",
            STAGE_SCHEMAS["similarity"]["properties"]["claim_number"],
        )
        self.assertNotIn(
            "enum",
            STAGE_SCHEMAS["similarity"]["properties"]
            ["evidence_element_ids"]["items"],
        )

    def test_initial_ollama_request_uses_document_constrained_schema(self):
        stages = OllamaStages("model", "embedding", 1)
        payloads = []

        def post(endpoint, payload):
            payloads.append(payload)
            return {
                "response": json.dumps({
                    "similarity": 3,
                    "reason": "構成が一致する",
                    "claim_number": 2,
                    "evidence_element_ids": ["C2-E1"],
                }),
                "done": True,
            }

        stages._post = post
        stages.generate_json("similarity", {
            "company_technology": "画像検査",
            "patent_text": "[C2-E1] 検査装置",
            "grounding_constraints": {
                "allowed_evidence_ids": ["C2-E1"],
                "allowed_claim_numbers": [2],
            },
        })

        schema = payloads[0]["format"]
        self.assertEqual(
            schema["properties"]["evidence_element_ids"]["items"]["enum"],
            ["C2-E1"],
        )
        self.assertEqual(
            schema["properties"]["claim_number"]["enum"],
            [2],
        )
        self.assertIn("Strict grounding constraints", payloads[0]["prompt"])

    def test_rescue_model_uses_prompt_and_python_schema_validation(self):
        stages = OllamaStages(
            "gpt-oss:20b",
            "embedding",
            1,
            use_native_schema=False,
            output_token_scale=2,
        )
        payloads = []

        def post(endpoint, payload):
            payloads.append(payload)
            return {
                "response": json.dumps({
                    "similarity": 3,
                    "reason": "構成が一致する",
                    "claim_number": 2,
                    "evidence_element_ids": ["C2-E1"],
                }),
                "done": True,
            }

        stages._post = post
        stages.generate_json("similarity", {
            "company_technology": "画像検査",
            "patent_text": "[C2-E1] 検査装置",
            "grounding_constraints": {
                "allowed_evidence_ids": ["C2-E1"],
                "allowed_claim_numbers": [2],
            },
        })

        self.assertNotIn("format", payloads[0])
        self.assertEqual(payloads[0]["options"]["num_predict"], 768)
        self.assertIn("Python validation", payloads[0]["prompt"])

    def test_ollama_json_exhaustion_is_a_document_error(self):
        stages = OllamaStages("model", "embedding", 1)
        stages._post = lambda endpoint, payload: {"response": '{"partial":', "done": True, "done_reason": "length"}
        with self.assertRaises(DocumentAnalysisError):
            stages.generate_json("similarity", {"company_technology": "自社", "patent_text": "特許"})
        self.assertEqual(stages.counter, 3)

    def test_small_schema_rejects_extra_keys_and_long_text(self):
        schema = STAGE_SCHEMAS["similarity"]
        valid = {
            "similarity": 5,
            "reason": "一致",
            "claim_number": 1,
            "evidence_element_ids": ["C1-E1"],
        }
        validate_schema_value(valid, schema)
        with self.assertRaises(ValueError):
            validate_schema_value({**valid, "extra": True}, schema)
        with self.assertRaises(ValueError):
            validate_schema_value({**valid, "similarity": 0}, schema)
        with self.assertRaises(ValueError):
            validate_schema_value({**valid, "reason": "x" * 801}, schema)
        with self.assertRaises(ValueError):
            validate_schema_value(
                {**valid, "evidence_element_ids": ["C1-E1", "C1-E1"]},
                schema,
            )
        with self.assertRaises(ValueError):
            validate_schema_value(
                {
                    **valid,
                    "evidence_element_ids": [
                        "C1-E1",
                        "C1-E2",
                        "C1-E3",
                        "C1-E4",
                        "C1-E5",
                        "C1-E6",
                    ],
                },
                schema,
            )

    def _run_two_stage_scenario(self, rescue_fails: bool):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdf_path = root / "patent_pool/P1.pdf"
            pdf_path.parent.mkdir(parents=True)
            pdf_path.write_bytes(b"pdf")
            artifact = root / "researches/sample/pipeline/P1"
            artifact.mkdir(parents=True)
            cache = root / "runtime/shared/extractions/hash"
            cache.mkdir(parents=True)
            prepared = {
                "artifact_dir": artifact,
                "cache_dir": cache,
                "source_decision": {"pdf_sha256": "hash"},
                "structure": {"claims": []},
            }
            pipeline_instances = []
            model_instances = []

            class Store:
                def put_artifact(self, *args, **kwargs):
                    return None

            class Pipeline:
                def __init__(self, selected_root, environment, research_id):
                    self.root = selected_root
                    self.research_id = research_id
                    self.research_dir = (
                        selected_root / "researches" / research_id
                    )
                    self.research = {"company_technology": "laser"}
                    self.store = Store()
                    self.completed = set()
                    self.finalized = {}
                    pipeline_instances.append(self)

                def patents(self):
                    return [(pdf_path, {})]

                def manual_review_required(self, pdf_name):
                    return False

                def result_exists(self, pdf_name):
                    return False

                def analysis_checkpoint_current(self, pdf_name):
                    return False

                def existing_result_path(self, pdf_name):
                    return root / "missing.json"

                def prepare_document(self, selected_pdf, patent):
                    return prepared

                def artifact_dir(self, pdf_name):
                    return artifact

                def persist_status_artifact(
                    self, pdf_name, artifact_type, payload
                ):
                    return None

                def analysis_task_current(self, selected, task):
                    return task in self.completed

                def analysis_task_characters(self, selected, task):
                    return 1

                def analysis_cache_summary(self, selected):
                    return {
                        "tasks": {},
                        "task_hits": [],
                        "task_writes": [],
                        "embedding": None,
                    }

                def analyze_task(
                    self,
                    selected,
                    task,
                    generate,
                    *,
                    cache_identity=None,
                ):
                    result = generate(task, {"grounding_constraints": None})
                    self.completed.add(task)
                    return result

                def assemble_generated_analysis(self, selected):
                    return {
                        "summaries": {
                            "tech_summary": "technology",
                            "problem_summary": "problem",
                        }
                    }

                def reuse_shared_embedding(
                    self, selected, cache_identity
                ):
                    return None

                def write_embedding(
                    self,
                    selected,
                    vectors,
                    *,
                    cache_identity=None,
                ):
                    return {
                        "score": {},
                        "summaries": {
                            "tech_summary": "technology",
                            "problem_summary": "problem",
                        },
                        "embedding": {"vectors": vectors},
                    }

                def build_company_reference(self, profile, vectors):
                    return {"profile": profile, "vectors": vectors}

                def finalize_research(self, analyses, *args, **kwargs):
                    self.finalized = dict(analyses)
                    return {"clustering": "clusters.json", "results": {}}

                def mark_analysis_current(self):
                    return None

                def write_run_manifest(self, *args, **kwargs):
                    path = self.research_dir / "manifest.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("{}", encoding="utf-8")
                    return path

            class Model:
                def __init__(self, model, embedding_model, *args, **kwargs):
                    self.model = model
                    self.embedding_model = embedding_model
                    self.audit_dir = None
                    self.counter = 0
                    self.calls = []
                    model_instances.append(self)

                def generate_json(self, task, data, audit_dir=None):
                    self.calls.append(task)
                    if task == "company_profile":
                        return {
                            "technology_summary": "company technology",
                            "problem_summary": "company problem",
                        }
                    if self.model == "gemma4:e4b" or rescue_fails:
                        raise DocumentAnalysisError(
                            task,
                            f"{self.model} failed",
                            attempts=3,
                        )
                    return {}

                def task_cache_identity(self, task):
                    return {"model": self.model, "task": task}

                def embedding_cache_identity(self):
                    return {"model": self.embedding_model}

                def embed(self, texts):
                    return [[0.0] * 8 for _ in texts]

                def unload_generation(self):
                    return None

                def unload_embedding(self):
                    return None

            index_path = root / "extraction-index.json"
            argv = [
                "run_research_pipeline.py",
                "sample",
                "--stage",
                "execute",
            ]
            with (
                patch("run_research_pipeline.ROOT", root),
                patch("run_research_pipeline.ResearchPipeline", Pipeline),
                patch("run_research_pipeline.OllamaStages", Model),
                patch(
                    "run_research_pipeline.extraction_index_entry",
                    return_value={},
                ),
                patch(
                    "run_research_pipeline.write_extraction_index",
                    return_value=index_path,
                ),
                patch.object(sys, "argv", argv),
                patch("builtins.print") as print_mock,
            ):
                result = run_pipeline_main()
            self.assertEqual(result, 0, print_mock.call_args_list)

            self.assertEqual(
                [item.model for item in model_instances],
                ["gemma4:e4b", "gpt-oss:20b"],
            )
            if rescue_fails:
                self.assertEqual(
                    model_instances[1].calls,
                    ["concept_level"],
                )
                self.assertEqual(pipeline_instances[0].finalized, {})
                skip = json.loads(
                    (artifact / "skip.json").read_text(encoding="utf-8")
                )
                error = json.loads(
                    (artifact / "analysis_error.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    skip["reason"],
                    "manual_review_required",
                )
                self.assertFalse(error["retry_on_next_run"])
                self.assertTrue(error["manual_review_required"])
                self.assertFalse(
                    (artifact / "analysis_complete.json").exists()
                )
            else:
                self.assertEqual(
                    model_instances[1].calls,
                    [
                        "concept_level",
                        "problem_summary",
                        "similarity",
                        "technology_summary",
                    ],
                )
                self.assertIn("P1", pipeline_instances[0].finalized)
                completion = json.loads(
                    (artifact / "analysis_complete.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertTrue(completion["rescued"])
                self.assertEqual(
                    completion["rescue_model"],
                    "gpt-oss:20b",
                )

    def test_primary_failure_is_rescued_after_primary_pass(self):
        self._run_two_stage_scenario(rescue_fails=False)

    def test_rescue_failure_becomes_non_pending_manual_skip(self):
        self._run_two_stage_scenario(rescue_fails=True)


if __name__ == "__main__": unittest.main()
