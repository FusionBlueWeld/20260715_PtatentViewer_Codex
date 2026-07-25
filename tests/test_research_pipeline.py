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
    build_evidence_pack,
    build_structured_document,
    chunk_text,
    deterministic_clusters,
    enrich_claim_structure,
    cluster_centroids,
    relative_proximity,
    scalable_clusters,
    semantic_cluster_order,
    extract_claims,
    extract_claims_with_metadata,
    split_sections,
    strip_patent_page_headers,
    validate_claim_structure,
    validate_score,
    verify_claim_reference,
    verify_evidence_references,
)


class ResearchPipelineTests(unittest.TestCase):
    def _grounding_prepared(self, root: Path) -> tuple[ResearchPipeline, dict]:
        research_dir = root / "researches/sample"
        artifact = research_dir / "pipeline/P1"
        cache = root / "runtime/shared/extractions/hash"
        artifact.mkdir(parents=True)
        cache.mkdir(parents=True)
        (research_dir / "research.json").write_text(
            json.dumps({
                "company_technology": "画像検査",
                "pipeline": {"source_policy": {"reading": "targeted"}},
            }),
            encoding="utf-8",
        )
        (cache / "extracted_text.txt").write_text("検査装置。", encoding="utf-8")
        tasks = {
            task: {
                "rendered_text": "[C1-E1 component] 検査装置",
                "sources": [{
                    "source_type": "claim",
                    "claim_number": 1,
                    "element_ids": ["C1-E1"],
                }],
                "statistics": {},
            }
            for task in (
                "similarity",
                "concept_level",
                "problem_summary",
                "technology_summary",
            )
        }
        tasks["problem_summary"]["rendered_text"] = "[P0001-N0001] 検査精度が低い"
        tasks["problem_summary"]["sources"] = [{
            "source_type": "paragraph",
            "paragraph_id": "P0001-N0001",
        }]
        tasks["technology_summary"]["sources"].append({
            "source_type": "paragraph",
            "paragraph_id": "P0001-N0002",
        })
        prepared = {
            "artifact_dir": artifact,
            "cache_dir": cache,
            "source_decision": {"reading_policy": "targeted"},
            "structure": {
                "claims": [{
                    "number": 1,
                    "type": "independent",
                    "text": "検査装置。",
                    "elements": [{
                        "id": "C1-E1",
                        "text": "検査装置",
                        "type": "component",
                        "limitations": [],
                    }],
                }],
            },
            "evidence_pack": {
                "schema_version": 2,
                "tasks": tasks,
            },
        }
        return ResearchPipeline(root, "normal", "sample"), prepared

    def test_grounding_mismatch_automatically_retries_only_the_task(self):
        with tempfile.TemporaryDirectory() as temp:
            pipeline, prepared = self._grounding_prepared(Path(temp))
            calls = []

            def generate(task, data):
                calls.append(data)
                evidence_ids = ["C9-E9"] if len(calls) == 1 else ["C1-E1"]
                return {
                    "similarity": 4,
                    "reason": "構成が一致する",
                    "claim_number": 1,
                    "evidence_element_ids": evidence_ids,
                }

            response = pipeline.analyze_task(prepared, "similarity", generate)

            self.assertEqual(response["evidence_element_ids"], ["C1-E1"])
            self.assertEqual(len(calls), 2)
            self.assertNotIn("grounding_repair", calls[0])
            self.assertEqual(calls[0]["grounding_constraints"], {
                "allowed_evidence_ids": ["C1-E1"],
                "allowed_claim_numbers": [1],
            })
            self.assertEqual(
                calls[1]["grounding_repair"]["allowed_ids"],
                ["C1-E1"],
            )
            self.assertEqual(
                calls[1]["grounding_constraints"],
                calls[0]["grounding_constraints"],
            )
            grounding = json.loads(
                (prepared["artifact_dir"] / "similarity_grounding.json")
                .read_text(encoding="utf-8")
            )
            self.assertTrue(grounding["verified"])
            self.assertEqual(grounding["attempt_count"], 2)
            self.assertTrue(pipeline.analysis_task_current(prepared, "similarity"))

    def test_grounding_second_mismatch_fails_without_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            pipeline, prepared = self._grounding_prepared(Path(temp))
            calls = []

            def generate(task, data):
                calls.append(data)
                return {
                    "similarity": 4,
                    "reason": "構成が一致する",
                    "claim_number": 1,
                    "evidence_element_ids": ["C9-E9"],
                }

            with self.assertRaisesRegex(ValueError, "grounding validation failed"):
                pipeline.analyze_task(prepared, "similarity", generate)

            self.assertEqual(len(calls), 2)
            self.assertFalse(
                (prepared["artifact_dir"] / "similarity.json").exists()
            )
            grounding = json.loads(
                (prepared["artifact_dir"] / "similarity_grounding.json")
                .read_text(encoding="utf-8")
            )
            self.assertFalse(grounding["verified"])
            self.assertEqual(grounding["attempt_count"], 2)
            self.assertFalse(pipeline.analysis_task_current(prepared, "similarity"))

    def test_intrinsic_llm_result_is_reused_from_pdf_shared_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pipeline, prepared = self._grounding_prepared(root)
            identity = {
                "model": "model-a",
                "prompt_sha256": "p",
                "schema_sha256": "s",
            }
            calls = []

            def generate(task, data):
                calls.append(task)
                return {
                    "concept_level": 3,
                    "reason": "限定事項がある",
                    "claim_number": 1,
                    "evidence_element_ids": ["C1-E1"],
                }

            (prepared["artifact_dir"] / "analysis_progress.json").write_text(
                json.dumps({
                    "pipeline_version": "stale-version",
                    "completed_tasks": [
                        "problem_summary",
                        "similarity",
                        "technology_summary",
                    ],
                }),
                encoding="utf-8",
            )
            first = pipeline.analyze_task(
                prepared,
                "concept_level",
                generate,
                cache_identity=identity,
            )
            first_progress = json.loads(
                (
                    prepared["artifact_dir"]
                    / "analysis_progress.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                first_progress["completed_tasks"],
                ["concept_level"],
            )
            second_artifact = (
                root / "researches/other/pipeline/P1"
            )
            second_artifact.mkdir(parents=True)
            second = {
                **prepared,
                "artifact_dir": second_artifact,
            }
            second.pop("_analysis_inputs_persisted", None)
            reused = pipeline.analyze_task(
                second,
                "concept_level",
                lambda task, data: self.fail(
                    "shared cache should avoid LLM generation"
                ),
                cache_identity=identity,
            )

            self.assertEqual(reused, first)
            self.assertEqual(calls, ["concept_level"])
            grounding = json.loads(
                (second_artifact / "concept_level_grounding.json")
                .read_text(encoding="utf-8")
            )
            self.assertTrue(grounding["shared_cache"]["hit"])
            progress = json.loads(
                (second_artifact / "analysis_progress.json")
                .read_text(encoding="utf-8")
            )
            self.assertTrue(
                progress["shared_cache"]["concept_level"]["hit"]
            )
            cache_status = progress["shared_cache"]["concept_level"]
            cache_path = root / cache_status["path"]
            self.assertEqual(len(cache_status["fingerprint"]), 64)
            self.assertEqual(len(cache_path.stem), 32)
            self.assertTrue(cache_path.is_file())

            third_artifact = root / "researches/third/pipeline/P1"
            third_artifact.mkdir(parents=True)
            third = {**prepared, "artifact_dir": third_artifact}
            third.pop("_analysis_inputs_persisted", None)
            pipeline.analyze_task(
                third,
                "concept_level",
                generate,
                cache_identity={**identity, "model": "model-b"},
            )
            self.assertEqual(
                calls,
                ["concept_level", "concept_level"],
            )

    def test_summary_embeddings_are_reused_from_pdf_shared_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pipeline, prepared = self._grounding_prepared(root)
            responses = {
                "similarity": {
                    "similarity": 3,
                    "reason": "一部一致",
                    "claim_number": 1,
                    "evidence_element_ids": ["C1-E1"],
                },
                "concept_level": {
                    "concept_level": 3,
                    "reason": "限定事項がある",
                    "claim_number": 1,
                    "evidence_element_ids": ["C1-E1"],
                },
                "problem_summary": {
                    "problem_summary": "検査精度を改善する課題",
                    "source_paragraph_ids": ["P0001-N0001"],
                },
                "technology_summary": {
                    "tech_summary": "画像検査装置",
                    "source_ids": ["C1-E1"],
                },
            }
            for task, response in responses.items():
                (prepared["artifact_dir"] / pipeline.analysis_task_file(task)) \
                    .write_text(
                        json.dumps(response, ensure_ascii=False),
                        encoding="utf-8",
                    )
            identity = {"model": "embedding-model", "num_ctx": 4096}
            vectors = [[0.25] * 128, [-0.5] * 128]
            first = pipeline.write_embedding(
                prepared,
                vectors,
                cache_identity=identity,
            )

            second_artifact = root / "researches/other/pipeline/P1"
            second_artifact.mkdir(parents=True)
            for task, response in responses.items():
                (second_artifact / pipeline.analysis_task_file(task)) \
                    .write_text(
                        json.dumps(response, ensure_ascii=False),
                        encoding="utf-8",
                    )
            second = {
                **prepared,
                "artifact_dir": second_artifact,
            }
            reused = pipeline.reuse_shared_embedding(second, identity)

            self.assertIsNotNone(reused)
            self.assertEqual(
                reused["embedding"]["vectors"],
                first["embedding"]["vectors"],
            )
            manifest = json.loads(
                (second_artifact / "embedding_manifest.json")
                .read_text(encoding="utf-8")
            )
            self.assertTrue(manifest["shared_cache"]["hit"])
            self.assertIsNone(
                pipeline.reuse_shared_embedding(
                    second,
                    {"model": "different-embedding-model"},
                )
            )

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

    def test_atomic_json_uses_short_temporary_name_near_windows_max_path(self):
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            segment = "x" * 32
            while len(str(parent)) < 210:
                parent /= segment
            target = parent / ("f" * 32 + ".json")
            legacy_temporary = target.with_name(
                f".{target.name}.16964.17736.tmp"
            )
            self.assertLess(len(str(target)), 260)
            self.assertGreaterEqual(len(str(legacy_temporary)), 260)

            atomic_json(target, {"status": "cached"})

            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8")),
                {"status": "cached"},
            )
            self.assertEqual(list(parent.glob(".tmp-*")), [])

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

    def test_jpo_page_headers_are_removed_before_claim_decomposition(self):
        text = "\n".join([
            "【請求項１】",
            "加工条件を生成",
            "(4) JP  7126639  B1  2022.8.26",
            "するステップを含む加工方法。",
            "【発明の詳細な説明】",
        ])
        cleaned, removed = strip_patent_page_headers(text)
        self.assertEqual(removed, 1)
        self.assertNotIn("7126639", cleaned)

        claims, metadata = extract_claims_with_metadata(text)
        self.assertEqual(metadata["removed_page_headers"], 1)
        self.assertEqual(len(claims), 1)
        self.assertNotIn("7126639", claims[0]["text"])
        enriched = enrich_claim_structure(claims)
        self.assertTrue(enriched["claims"][0]["elements"])
        self.assertTrue(
            all(
                "7126639" not in element["text"]
                for element in enriched["claims"][0]["elements"]
            )
        )

        structured = build_structured_document([
            {
                "page": 4,
                "text": "\n".join([
                    "(4) JP  7126639  B1  2022.8.26",
                    "【０００５】加工条件の調整が困難である。",
                ]),
            }
        ])
        self.assertEqual(structured["statistics"]["removed_page_headers"], 1)
        self.assertTrue(
            all(
                "7126639" not in paragraph["text"]
                for paragraph in structured["paragraphs"]
            )
        )

    def test_structured_document_removes_repeated_headers_and_marks_duplicates(self):
        pages = [
            {
                "page": page,
                "text": "\n".join([
                    "公開特許公報",
                    "【技術分野】",
                    f"【00{page:02d}】センサから加工状態を取得する。",
                    "同じ説明を繰り返す。",
                ]),
            }
            for page in range(1, 4)
        ]
        structured = build_structured_document(
            pages, pdf_name="P1.pdf", pdf_sha256="hash"
        )
        self.assertEqual(structured["schema_version"], 2)
        self.assertGreater(
            structured["statistics"]["removed_repeated_headers_footers"], 0
        )
        self.assertTrue(
            all(item["id"].startswith("P") for item in structured["paragraphs"])
        )
        repeated = [
            item
            for item in structured["paragraphs"]
            if item["text"] == "同じ説明を繰り返す。"
        ]
        self.assertEqual(len(repeated), 3)
        self.assertIsNone(repeated[0]["duplicate_of"])
        self.assertTrue(
            all(item["duplicate_of"] == repeated[0]["id"] for item in repeated[1:])
        )

    def test_claim_elements_and_evidence_pack_raise_information_density(self):
        claims, metadata = extract_claims_with_metadata(
            "【請求項1】センサと、センサ出力から加工状態を推定する推定部と、"
            "推定結果に基づいてレーザー出力を制御する制御部と、を備えるレーザー加工装置。"
        )
        self.assertTrue(metadata["valid"])
        claim_structure = enrich_claim_structure(claims)
        elements = claim_structure["claims"][0]["elements"]
        self.assertGreaterEqual(len(elements), 3)
        self.assertIn("processing", {item["type"] for item in elements})
        self.assertIn("control", {item["type"] for item in elements})
        structured = build_structured_document([
            {
                "page": 1,
                "text": "\n".join([
                    "【背景技術】従来は加工中の品質変動を検出できなかった。",
                    "【発明が解決しようとする課題】加工状態の変動を抑制する。",
                    "【課題を解決するための手段】センサ信号から状態を推定しレーザー出力を制御する。",
                    "本発明は上述の実施形態に限定されるものではない。",
                ]),
            }
        ])
        pack = build_evidence_pack(
            structured,
            claim_structure,
            "センサから加工状態を推定しレーザー出力を制御する",
        )
        similarity = pack["tasks"]["similarity"]
        problem = pack["tasks"]["problem_summary"]
        self.assertLessEqual(similarity["rendered_characters"], 12_000)
        self.assertIn("[C1-E1", similarity["rendered_text"])
        self.assertIn("センサ", similarity["rendered_text"])
        self.assertIn("レーザー出力を制御", similarity["rendered_text"])
        self.assertIn("従来", problem["rendered_text"])
        self.assertNotIn("限定されるものではない", similarity["rendered_text"])
        self.assertNotEqual(
            similarity["rendered_text"],
            pack["tasks"]["technology_summary"]["rendered_text"],
        )

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

    def test_claim_reference_is_bound_to_python_extracted_source(self):
        structure = {
            "claims": [
                {"number": 1, "type": "independent", "text": "A laser processing apparatus."},
                {"number": 2, "type": "dependent", "text": "The apparatus of claim 1."},
            ]
        }
        verified = verify_claim_reference(structure, 1, "similarity")
        self.assertTrue(verified["verified"])
        self.assertEqual(verified["claim_text"], "A laser processing apparatus.")
        self.assertEqual(len(verified["claim_sha256"]), 64)
        hallucinated = verify_claim_reference(structure, 99, "similarity")
        self.assertFalse(hallucinated["verified"])
        self.assertNotIn("claim_text", hallucinated)
        dependent = verify_claim_reference(structure, 2, "concept_level")
        self.assertFalse(dependent["verified"])
        boolean = verify_claim_reference(structure, True, "concept_level")
        self.assertEqual(boolean["rejection_reason"], "claim_number_not_integer")

    def test_evidence_reference_normalizes_labels_and_accepts_mixed_technology_sources(self):
        prepared = {
            "evidence_pack": {
                "tasks": {
                    "problem_summary": {
                        "sources": [
                            {
                                "source_type": "paragraph",
                                "paragraph_id": "P0003-N0003",
                            }
                        ]
                    },
                    "technology_summary": {
                        "sources": [
                            {
                                "source_type": "claim",
                                "claim_number": 1,
                                "element_ids": ["C1-E1"],
                            },
                            {
                                "source_type": "paragraph",
                                "paragraph_id": "P0003-N0005",
                            },
                        ]
                    },
                }
            },
            "claim_structure": {
                "claims": [
                    {
                        "number": 1,
                        "elements": [
                            {
                                "id": "C1-E1",
                                "type": "input",
                                "limitations": [],
                                "sha256": "element-hash",
                            }
                        ],
                    }
                ]
            },
            "structured_document": {
                "paragraphs": [
                    {
                        "id": "P0003-N0003",
                        "page": 3,
                        "section": "problem",
                        "roles": ["problem"],
                        "sha256": "problem-hash",
                    },
                    {
                        "id": "P0003-N0005",
                        "page": 3,
                        "section": "solution",
                        "roles": ["solution"],
                        "sha256": "solution-hash",
                    },
                ]
            },
        }
        problem = verify_evidence_references(
            prepared,
            "problem_summary",
            ["P0003-N0003 p.3 problem"],
        )
        self.assertTrue(problem["verified"])
        self.assertEqual(problem["verified_ids"], ["P0003-N0003"])
        technology = verify_evidence_references(
            prepared,
            "technology_summary",
            ["C1-E1 input", "P0003-N0005 p.3 solution"],
        )
        self.assertTrue(technology["verified"])
        self.assertEqual(
            technology["verified_ids"], ["C1-E1", "P0003-N0005"]
        )
        self.assertEqual(
            [item["source_type"] for item in technology["verified_sources"]],
            ["element", "paragraph"],
        )

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
            artifact = research_dir / "pipeline/P1"
            artifact.mkdir(parents=True)
            (research_dir / "research.json").write_text("{}", encoding="utf-8")
            pipeline = ResearchPipeline(root, "normal", "sample")
            checkpoint = artifact / "analysis_complete.json"
            checkpoint.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
            self.assertFalse(pipeline.analysis_checkpoint_current("P1.pdf"))
            checkpoint.write_text(json.dumps({"pipeline_version": ANALYSIS_PIPELINE_VERSION}), encoding="utf-8")
            self.assertTrue(pipeline.analysis_checkpoint_current("P1.pdf"))

    def test_prepared_document_can_be_reloaded_for_a_durable_shard(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            research_dir = root / "researches/sample"
            artifact = research_dir / "pipeline/P1"
            cache = root / "runtime/shared/extractions/hash"
            artifact.mkdir(parents=True)
            cache.mkdir(parents=True)
            (research_dir / "research.json").write_text("{}", encoding="utf-8")
            patent_text = "【特許請求の範囲】\n【請求項1】センサを備える装置。"
            (cache / "extracted_text.txt").write_text(patent_text, encoding="utf-8")
            atomic_json(cache / "pages.json", [{"page": 1, "text": patent_text}])
            atomic_json(artifact / "source_decision.json", {
                "shared_cache": str(cache.relative_to(root)), "pdf_sha256": "hash",
                "pdf": "P1.pdf", "reading_policy": "targeted",
            })
            atomic_json(artifact / "document_structure.json", {
                "claims": [{"number": 1, "type": "independent", "text": "claim"}], "sections": {},
            })
            pipeline = ResearchPipeline(root, "normal", "sample")
            prepared = pipeline.load_prepared_document("P1.pdf")
            self.assertEqual(prepared["artifact_dir"], artifact)
            self.assertEqual(prepared["cache_dir"], cache.resolve())
            self.assertEqual(prepared["structure"]["claims"][0]["number"], 1)
            self.assertEqual(
                prepared["shared_preprocessing"]["cache_decision"], "created"
            )
            self.assertTrue((cache / "preprocessing_manifest.json").is_file())
            self.assertTrue((cache / "document_structure.json").is_file())
            self.assertTrue((cache / "structured_document.json").is_file())
            self.assertTrue((cache / "claim_structure.json").is_file())
            self.assertTrue((artifact / "structured_document.json").is_file())
            self.assertTrue((artifact / "claim_structure.json").is_file())
            self.assertTrue((artifact / "evidence_pack.json").is_file())
            self.assertIn("similarity", prepared["evidence_pack"]["tasks"])

    def test_pdf_only_preprocessing_is_reused_across_researches(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cache = root / "runtime/shared/extractions/hash"
            cache.mkdir(parents=True)
            patent_text = (
                "【特許請求の範囲】\n"
                "【請求項1】センサ出力から加工状態を推定する装置。\n"
                "【技術分野】レーザー加工状態の推定に関する。"
            )
            (cache / "extracted_text.txt").write_text(
                patent_text, encoding="utf-8"
            )
            atomic_json(
                cache / "pages.json", [{"page": 1, "text": patent_text}]
            )

            prepared_items = []
            for research_id, company_technology in (
                ("first", "レーザー加工状態を推定する"),
                ("second", "光学部材の温度を制御する"),
            ):
                research_dir = root / "researches" / research_id
                artifact = research_dir / "pipeline/P1"
                artifact.mkdir(parents=True)
                (research_dir / "research.json").write_text(
                    json.dumps({"company_technology": company_technology}),
                    encoding="utf-8",
                )
                atomic_json(
                    artifact / "source_decision.json",
                    {
                        "shared_cache": str(cache.relative_to(root)),
                        "pdf_sha256": "hash",
                        "pdf": "P1.pdf",
                        "reading_policy": "targeted",
                    },
                )
                pipeline = ResearchPipeline(root, "normal", research_id)
                prepared_items.append(pipeline.load_prepared_document("P1.pdf"))

            first, second = prepared_items
            self.assertEqual(
                first["shared_preprocessing"]["cache_decision"], "created"
            )
            self.assertEqual(
                second["shared_preprocessing"]["cache_decision"], "reused"
            )
            self.assertEqual(first["structure"], second["structure"])
            self.assertEqual(
                first["structured_document"], second["structured_document"]
            )
            self.assertEqual(
                first["claim_structure"], second["claim_structure"]
            )
            self.assertNotEqual(
                first["evidence_pack"]["company_technology_sha256"],
                second["evidence_pack"]["company_technology_sha256"],
            )
            self.assertTrue(
                (
                    root
                    / "researches/first/pipeline/P1/evidence_pack.json"
                ).is_file()
            )
            self.assertTrue(
                (
                    root
                    / "researches/second/pipeline/P1/evidence_pack.json"
                ).is_file()
            )

    def test_staged_analysis_finalizes_ui_compatible_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            research_dir = root / "researches/sample"
            artifact = research_dir / "pipeline/P1"
            cache = root / "runtime/shared/extractions/hash"
            artifact.mkdir(parents=True)
            cache.mkdir(parents=True)
            (research_dir / "research.json").write_text(json.dumps({
                "company_technology": "自社技術", "pipeline": {"source_policy": {"reading": "targeted"}}
            }), encoding="utf-8")
            (cache / "extracted_text.txt").write_text(
                "\n".join([
                    "【技術分野】レーザー加工技術に関する。",
                    "【発明が解決しようとする課題】加工品質の変動を抑える。",
                    "【課題を解決するための手段】センサ信号から状態を推定する。",
                ]),
                encoding="utf-8",
            )
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
                    "similarity": {
                        "similarity": 3,
                        "reason": "一部一致",
                        "claim_number": 1,
                        "evidence_element_ids": ["C1-E1"],
                    },
                    "concept_level": {
                        "concept_level": 4,
                        "reason": "限定が少ない",
                        "claim_number": 1,
                        "evidence_element_ids": ["C1-E1"],
                    },
                    "problem_summary": {
                        "problem_summary": "課題",
                        "source_paragraph_ids": ["P0001-N0002"],
                    },
                    "technology_summary": {
                        "tech_summary": "技術",
                        "source_ids": ["C1-E1", "P0001-N0001", "P0001-N0003"],
                    },
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
            finalized = pipeline.finalize_research({"P1": analysis}, generate, "run-1", company_reference=company_reference)
            result_path = root / finalized["results"]["P1"]
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["similarity"], 3)
            self.assertEqual(result["concept_level"], 4)
            self.assertEqual(result["tech_cluster"], "分類")
            self.assertEqual(result["tech_cluster_id"], 0)
            self.assertEqual(result["problem_cluster_id"], 0)
            self.assertTrue(result["claim_traceability"]["similarity"]["verified"])
            self.assertEqual(result["claim_traceability"]["similarity"]["claim_number"], 1)
            self.assertEqual(result["claim_traceability"]["similarity"]["claim_text"], "装置。")
            self.assertTrue(result["evidence_traceability"]["similarity"]["verified"])
            self.assertEqual(
                result["evidence_traceability"]["similarity"]["verified_ids"],
                ["C1-E1"],
            )
            self.assertTrue(
                result["evidence_traceability"]["problem_summary"]["verified"]
            )
            clusters = json.loads((research_dir / "clustering/run-1/clusters.json").read_text(encoding="utf-8"))
            self.assertEqual(clusters["semantic_ordering"]["technology_order"], [0])
            self.assertIn("company_proximity", clusters)
            reloaded = pipeline.load_analysis_artifacts("P1.pdf")
            self.assertEqual(reloaded["score"]["similarity"], 3)
            self.assertEqual(reloaded["summaries"]["tech_summary"], "技術")
            self.assertEqual(reloaded["embedding"]["input_order"], ["technology", "problem"])

            error_path = artifact / "analysis_error.json"
            error_path.write_text(json.dumps({"error": "invalid JSON"}), encoding="utf-8")
            pdf_path = root / "patent_pool/P1.pdf"
            pdf_path.parent.mkdir(parents=True)
            pdf_path.write_bytes(b"pdf")
            pipeline.patents = lambda: [(pdf_path, {})]
            overview = pipeline.overview()
            self.assertEqual(overview["counts"]["failed"], 1)
            self.assertEqual(overview["counts"]["finalized"], 0)
            self.assertEqual(overview["counts"]["pending"], 1)
            self.assertEqual(overview["counts"]["llm_pending"], 1)
            self.assertEqual(overview["documents"][0]["analysis_state"], "pending")
            self.assertEqual(overview["documents"][0]["analysis_error"]["error"], "invalid JSON")
            self.assertFalse(pipeline.result_exists("P1.pdf"))
            self.assertFalse(pipeline.analysis_checkpoint_current("P1.pdf"))

            error_path.write_text(json.dumps({
                "error": "rescue failed",
                "retry_on_next_run": False,
                "manual_review_required": True,
            }), encoding="utf-8")
            (artifact / "skip.json").write_text(json.dumps({
                "reason": "manual_review_required",
                "detail": "Ask Codex to review manually.",
            }), encoding="utf-8")
            overview = pipeline.overview()
            self.assertTrue(pipeline.manual_review_required("P1.pdf"))
            self.assertEqual(overview["counts"]["pending"], 0)
            self.assertEqual(overview["counts"]["failed"], 0)
            self.assertEqual(overview["counts"]["skipped"], 1)
            self.assertEqual(overview["counts"]["finalized"], 0)
            self.assertEqual(
                overview["documents"][0]["analysis_state"],
                "skipped",
            )


if __name__ == "__main__":
    unittest.main()
