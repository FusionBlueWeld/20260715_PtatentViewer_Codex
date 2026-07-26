import json
import shutil
import subprocess
import unittest
from pathlib import Path


class WhitespaceAnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]
        cls.node = shutil.which("node")

    def analyze(self, payload):
        if not self.node:
            self.skipTest("Node.js is not installed")
        script = (
            "const engine=require('./public/assets/whitespace-analysis.js');"
            f"process.stdout.write(JSON.stringify(engine.analyze({json.dumps(payload)})));"
        )
        result = subprocess.run(
            [self.node, "-e", script],
            cwd=self.root,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        return json.loads(result.stdout)

    def test_isolated_gap_uses_row_column_and_adjacent_evidence(self):
        result = self.analyze({
            "tech": "b",
            "problem": "y",
            "techs": ["a", "b", "c"],
            "problems": ["x", "y", "z"],
            "counts": {
                "a|x": 2, "b|x": 3, "c|x": 1,
                "a|y": 4, "b|y": 0, "c|y": 5,
                "a|z": 1, "b|z": 2, "c|z": 1,
            },
            "technologyFit": 0.9,
            "problemFit": 0.8,
        })
        self.assertEqual(result["pattern"], "孤立した空白")
        self.assertEqual(result["blockSize"], 1)
        self.assertEqual(result["rowTotal"], 9)
        self.assertEqual(result["columnTotal"], 5)
        self.assertEqual(result["occupiedNeighbors"], 4)
        self.assertGreater(result["opportunityScore"], 0.6)

    def test_large_zero_area_is_reported_as_structural_risk(self):
        result = self.analyze({
            "tech": "b",
            "problem": "y",
            "techs": ["a", "b", "c"],
            "problems": ["x", "y", "z"],
            "counts": {"a|x": 2},
            "technologyFit": 0.2,
            "problemFit": 0.2,
        })
        self.assertEqual(result["pattern"], "大きな空白領域")
        self.assertEqual(result["blockSize"], 8)
        self.assertEqual(result["opportunityLevel"], "低")

    def test_non_empty_cell_is_not_a_whitespace(self):
        self.assertIsNone(self.analyze({
            "tech": "a",
            "problem": "x",
            "techs": ["a"],
            "problems": ["x"],
            "counts": {"a|x": 1},
        }))

    def test_analysis_engine_is_retained_without_diagnosis_ui(self):
        html = (self.root / "public/index.html").read_text(encoding="utf-8")
        self.assertNotIn('id="whitespace-insight"', html)
        self.assertIn('id="cell-trend-panel"', html)
        self.assertIn('data-trend-mode="year"', html)
        self.assertIn('data-trend-mode="organization"', html)
        self.assertLess(
            html.index('id="cell-trend-panel"'),
            html.index('class="lower-grid"'),
        )
        self.assertLess(
            html.index("/assets/whitespace-analysis.js"),
            html.index("/assets/app.js"),
        )

    def test_status_chart_css_matches_api_status_keys(self):
        css = (self.root / "public/assets/app.css").read_text(encoding="utf-8")
        for status in ("rights_acquired", "under_examination", "published"):
            self.assertIn(f".status-segment.{status}", css)
            self.assertIn(f".trend-legend i.{status}", css)
        self.assertIn(".status-segment.unclassified", css)


if __name__ == "__main__":
    unittest.main()
