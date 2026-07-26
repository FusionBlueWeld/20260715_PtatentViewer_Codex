from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pypdf import PdfReader


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
from patent_viewer.domain import ANALYSIS_REQUIRED, Repository, patent_key, read_json


GENERATION_MODEL = "gemma4:e4b"
EMBEDDING_MODEL = "qwen3-embedding:8b"
OLLAMA_URL = "http://127.0.0.1:11434"

ANALYSIS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": sorted(ANALYSIS_REQUIRED),
    "properties": {
        "similarity": {"type": "integer", "minimum": 1, "maximum": 5},
        "concept_level": {
            "description": (
                "独立請求項の文言上の権利範囲の広さ。"
                "1は非常に狭く、5は非常に広い。"
            ),
            "type": "integer",
            "minimum": 1,
            "maximum": 5,
        },
        "tech_summary": {"type": "string", "minLength": 1, "maxLength": 2000},
        "problem_summary": {"type": "string", "minLength": 1, "maxLength": 2000},
        "reasoning": {"type": "string", "minLength": 1, "maxLength": 4000},
        "tech_cluster": {"type": "string", "minLength": 1, "maxLength": 120},
        "problem_cluster": {"type": "string", "minLength": 1, "maxLength": 120},
    },
}


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ollama_post(endpoint: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(
        OLLAMA_URL + endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except OSError:
            detail = ""
        raise RuntimeError(f"Ollama {endpoint} HTTP {exc.code}: {detail or exc.reason}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Ollama {endpoint} failed: {exc}") from exc
    if result.get("error"):
        raise RuntimeError(f"Ollama {endpoint}: {result['error']}")
    return result


def clean_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"\(?\d+\)?", stripped):
            continue
        lines.append(stripped)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def extract_pdf(pdf_path: Path) -> tuple[str, dict[str, Any]]:
    reader = PdfReader(pdf_path)
    page_texts = [page.extract_text() or "" for page in reader.pages]
    text = clean_text("\n".join(page_texts))
    if len(text) < 500:
        raise RuntimeError(f"抽出文字数が少なすぎます: {len(text)}")
    meta = {
        "pdf": pdf_path.name,
        "pdf_sha256": sha256_bytes(pdf_path.read_bytes()),
        "pages": len(reader.pages),
        "nonempty_pages": sum(bool(value.strip()) for value in page_texts),
        "characters": len(text),
        "text_sha256": sha256_bytes(text.encode("utf-8")),
    }
    return text, meta


def validate_analysis(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("analysis response is not an object")
    extra = set(value) - ANALYSIS_REQUIRED
    missing = ANALYSIS_REQUIRED - set(value)
    if missing or extra:
        raise ValueError(f"analysis keys mismatch; missing={sorted(missing)}, extra={sorted(extra)}")
    for key in ("similarity", "concept_level"):
        if type(value[key]) is not int or not 1 <= value[key] <= 5:
            raise ValueError(f"{key} must be an integer from 1 to 5")
    limits = {"tech_summary": 2000, "problem_summary": 2000, "reasoning": 4000, "tech_cluster": 120, "problem_cluster": 120}
    for key, limit in limits.items():
        if not isinstance(value[key], str) or not value[key].strip() or len(value[key]) > limit:
            raise ValueError(f"{key} is empty or too long")
        value[key] = value[key].strip()
    return value


def validate_embeddings(response: dict[str, Any], expected_count: int) -> list[list[float]]:
    vectors = response.get("embeddings")
    if not isinstance(vectors, list) or len(vectors) != expected_count:
        raise ValueError(f"expected {expected_count} embeddings")
    dimensions = {len(vector) for vector in vectors if isinstance(vector, list)}
    if len(dimensions) != 1 or not dimensions or next(iter(dimensions)) < 128:
        raise ValueError(f"invalid embedding dimensions: {dimensions}")
    if any(not all(isinstance(item, (int, float)) for item in vector) for vector in vectors):
        raise ValueError("embedding contains non-numeric values")
    return vectors


def resolve_target(research_id: str) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    base = ROOT / "debug_data/researches" / research_id
    research = read_json(base / "research.json")
    manifest_path = base / "patents.json"
    manifest = read_json(manifest_path)
    patents = manifest.get("patents", [])
    if len(patents) != 1:
        raise RuntimeError("single-document test manifest must contain exactly one patent")
    pdf_path = ROOT / "patent_pool" / patents[0]["pdf"]
    Repository(ROOT).pdf_path(pdf_path.name)
    return base, research, patents[0]


def run(args: argparse.Namespace) -> dict[str, Any]:
    research_dir, research, patent = resolve_target(args.research_id)
    pdf_path = ROOT / "patent_pool" / patent["pdf"]
    key = patent_key(pdf_path.name)
    final_path = research_dir / "results" / f"{key}.json"
    if final_path.exists() and not args.overwrite:
        raise RuntimeError(f"result already exists: {final_path}; use --overwrite explicitly")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    run_dir = research_dir / "runs" / run_id
    started = time.perf_counter()
    text, extraction = extract_pdf(pdf_path)
    atomic_json(run_dir / "extraction.json", extraction)
    (run_dir / "extracted_text.txt").write_text(text, encoding="utf-8")

    prompt_template = (ROOT / "src/patent_viewer/prompts/analysis_combined.txt").read_text(encoding="utf-8")
    prompt = (
        f"{prompt_template}\n\n"
        f"## 自社技術定義\n{research['company_technology']}\n\n"
        f"## 対象特許明細書\n{text[:100_000]}"
    )
    (run_dir / "analysis_prompt.txt").write_text(prompt, encoding="utf-8")
    generate_payload = {
        "model": args.generation_model,
        "prompt": prompt,
        "stream": False,
        # gemma4:e4b rejects Ollama's JSON-Schema grammar initialization.
        # Use JSON mode, then enforce the stricter contract in Python before saving.
        "format": "json",
        "keep_alive": 0,
        "options": {"temperature": 0, "top_p": 0.9, "num_ctx": 32768, "num_predict": 1600},
    }
    request_audit = dict(generate_payload)
    request_audit["validation_schema"] = ANALYSIS_SCHEMA
    request_audit["prompt"] = {"characters": len(prompt), "sha256": sha256_bytes(prompt.encode("utf-8"))}
    atomic_json(run_dir / "generate_request.json", request_audit)
    generated = ollama_post("/api/generate", generate_payload, args.timeout)
    atomic_json(run_dir / "generate_response.json", generated)
    response_text = generated.get("response")
    if not isinstance(response_text, str) or not response_text.strip():
        raise RuntimeError("Ollama returned an empty analysis response")
    try:
        analysis = validate_analysis(json.loads(response_text))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"analysis response is not valid JSON: {exc}") from exc

    embed_inputs = [analysis["problem_summary"], analysis["tech_summary"]]
    embed_payload = {"model": args.embedding_model, "input": embed_inputs, "keep_alive": 0, "options": {"num_ctx": 4096}}
    atomic_json(run_dir / "embedding_request.json", {"model": args.embedding_model, "input": embed_inputs, "keep_alive": 0})
    embedded = ollama_post("/api/embed", embed_payload, args.timeout)
    atomic_json(run_dir / "embedding_response.json", embedded)
    vectors = validate_embeddings(embedded, 2)

    atomic_json(final_path, analysis)
    elapsed = round(time.perf_counter() - started, 3)
    manifest = {
        "run_id": run_id,
        "status": "completed",
        "environment": "debug",
        "research_id": args.research_id,
        "result": str(final_path.relative_to(ROOT)),
        "generation_model": args.generation_model,
        "embedding_model": args.embedding_model,
        "embedding_dimensions": len(vectors[0]),
        "pdf_sha256": extraction["pdf_sha256"],
        "prompt_sha256": request_audit["prompt"]["sha256"],
        "elapsed_seconds": elapsed,
        "generate_metrics": {key: generated.get(key) for key in ("total_duration", "load_duration", "prompt_eval_count", "eval_count", "eval_duration")},
        "embedding_metrics": {key: embedded.get(key) for key in ("total_duration", "load_duration", "prompt_eval_count")},
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    atomic_json(run_dir / "run_manifest.json", manifest)
    return {"analysis": analysis, "manifest": manifest, "run_dir": str(run_dir.relative_to(ROOT))}


def main() -> int:
    parser = argparse.ArgumentParser(description="DEBUG環境で実PDF 1件をOllama分析する")
    parser.add_argument(
        "--research-id",
        required=True,
        help="debug_data/researches/ 配下にローカル作成した検証用リサーチID",
    )
    parser.add_argument("--generation-model", default=GENERATION_MODEL)
    parser.add_argument("--embedding-model", default=EMBEDDING_MODEL)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    try:
        result = run(args)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps({"status": "completed", **result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
