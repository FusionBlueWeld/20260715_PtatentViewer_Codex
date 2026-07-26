import json
from pathlib import Path


def build_fixture(root: Path):
    (root / "public/assets").mkdir(parents=True)
    (root / "public/index.html").write_text("<!doctype html><title>fixture</title>", encoding="utf-8")
    (root / "patent_pool").mkdir()
    (root / "patent_pool/JPA 2026000001-000000.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    (root / "patent_pool/JPB 000000001-000000.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    for env_root, research_id in [("researches", "normal_research"), ("debug_data/researches", "debug_research")]:
        base = root / env_root / research_id
        (base / "results").mkdir(parents=True)
        (base / "research.json").write_text(json.dumps({"name": research_id, "description": "fixture", "company_technology": "test"}), encoding="utf-8")
        pdf = "JPA 2026000001-000000.pdf" if research_id.startswith("normal") else "JPB 000000001-000000.pdf"
        manifest = {"name": "sample", "patents": [{"pdf": pdf, "year": 2026, "applicant": "Company A"}]}
        (base / "patents.json").write_text(json.dumps(manifest), encoding="utf-8")
        key = Path(pdf).stem.replace(" ", "_")
        result = {
            "similarity": 4, "concept_level": 5, "tech_cluster": "A", "problem_cluster": "B",
            "tech_cluster_id": 0, "problem_cluster_id": 0,
            "tech_summary": "fixture tech", "problem_summary": "fixture problem", "reasoning": "fixture reason"
        }
        (base / f"results/{key}.json").write_text(json.dumps(result), encoding="utf-8")
    prompt_dir = root / "src/patent_viewer/prompts/stages"; prompt_dir.mkdir(parents=True)
    for name in ("similarity", "concept_level", "problem_summary", "technology_summary", "cluster_name", "company_profile"):
        (prompt_dir / f"{name}.txt").write_text("prompt", encoding="utf-8")
    schema_dir = root / "schemas"; schema_dir.mkdir(parents=True)
    for name in ("similarity", "concept-level", "problem-summary", "technology-summary", "cluster-name", "company-profile"):
        (schema_dir / f"llm-{name}.schema.json").write_text(json.dumps({
            "type": "object", "required": ["value"], "properties": {"value": {"type": "string"}}
        }), encoding="utf-8")
