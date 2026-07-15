import json
import tempfile
import unittest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
from run_single_llm_test import atomic_json, validate_analysis, validate_embeddings


class SingleLlmPipelineTests(unittest.TestCase):
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


if __name__ == "__main__": unittest.main()
