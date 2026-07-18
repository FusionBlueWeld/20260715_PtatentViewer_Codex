from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from pypdf import PdfReader

from .domain import DataError, patent_key, read_company_technology, read_research_config, research_sources, read_json, safe_id


READING_MODES = {"cache", "verify_original", "reextract"}
PIPELINE_STAGES = (
    "extract",
    "structure",
    "similarity",
    "concept_level",
    "problem_summary",
    "technology_summary",
    "embeddings",
    "clustering",
    "cluster_names",
    "finalize",
)
ANALYSIS_PIPELINE_VERSION = "simple-schema-v1"


class DocumentSkipped(RuntimeError):
    """A document-level condition that must not fail the whole research run."""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        for attempt in range(12):
            try:
                temporary.write_text(serialized, encoding="utf-8")
                os.replace(temporary, path)
                return
            except OSError as exc:
                transient = isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in {5, 32, 33}
                if not transient or attempt == 11:
                    raise
                # Windows can briefly deny replacement while the UI server or
                # security scanner is reading the destination file.
                time.sleep(min(0.025 * (attempt + 1), 0.25))
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def clean_text(text: str) -> str:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if re.fullmatch(r"\(?\d+\)?", stripped):
            continue
        lines.append(stripped)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def chunk_text(text: str, maximum: int = 12_000) -> list[str]:
    if maximum < 1000:
        raise ValueError("chunk maximum must be at least 1000 characters")
    paragraphs = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) > maximum:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(paragraph[i:i + maximum] for i in range(0, len(paragraph), maximum))
        elif len(current) + len(paragraph) + 2 > maximum:
            chunks.append(current)
            current = paragraph
        else:
            current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    return chunks


CLAIM_HEADER = re.compile(
    r"(?m)^[ \t]*[【\[［]\s*請求項\s*([0-9０-９]+)\s*[】\]］][ \t]*"
)
POST_CLAIMS_HEADING = re.compile(
    r"(?m)^[ \t]*【\s*(?:発明の詳細な説明|技術分野|背景技術|図面の簡単な説明|符号の説明|要約)\s*】"
)


def _claim_number(value: str) -> int:
    return int(unicodedata.normalize("NFKC", value))


def _claim_header_run(matches: list[re.Match[str]]) -> list[re.Match[str]]:
    """Choose the longest 1..N block when a PDF contains duplicated claim sets."""
    runs: list[list[re.Match[str]]] = []
    current: list[re.Match[str]] = []
    for match in matches:
        number = _claim_number(match.group(1))
        if number == 1:
            if current:
                runs.append(current)
            current = [match]
        elif current and number == _claim_number(current[-1].group(1)) + 1:
            current.append(match)
        else:
            if current:
                runs.append(current)
            current = []
    if current:
        runs.append(current)
    if not runs:
        return []
    return max(runs, key=lambda run: (len(run), -run[0].start()))


def _claim_dependencies(value: str, number: int) -> list[int]:
    normalized = unicodedata.normalize("NFKC", value)
    dependencies: set[int] = set()
    for start, end in re.findall(r"請求項\s*(\d+)\s*(?:から|[\-~〜～])\s*(?:請求項\s*)?(\d+)", normalized):
        lower, upper = sorted((int(start), int(end)))
        dependencies.update(range(lower, upper + 1))
    reference_clause = re.compile(
        r"請求項\s*([0-9\s、,又はまた若しく及びから乃至\-~〜～請求項]+?)(?:\s*に)?記載"
    )
    for reference in reference_clause.finditer(normalized):
        dependencies.update(int(item) for item in re.findall(r"\d+", reference.group(1)))
    dependencies.update(int(item) for item in re.findall(r"請求項\s*(\d+)", normalized))
    if "先行する請求項" in normalized:
        dependencies.update(range(1, number))
    if "前項" in normalized and number > 1:
        dependencies.add(number - 1)
    return sorted(item for item in dependencies if 1 <= item < number)


def extract_claims_with_metadata(text: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    matches = list(CLAIM_HEADER.finditer(text))
    selected = _claim_header_run(matches)
    claims: list[dict[str, Any]] = []
    selected_indexes = {id(match): index for index, match in enumerate(matches)}
    for index, match in enumerate(selected):
        if index + 1 < len(selected):
            end = selected[index + 1].start()
        else:
            global_index = selected_indexes[id(match)]
            later_header = matches[global_index + 1].start() if global_index + 1 < len(matches) else len(text)
            later_section = POST_CLAIMS_HEADING.search(text, match.end())
            section_start = later_section.start() if later_section else len(text)
            end = min(later_header, section_start)
        value = text[match.end():end].strip()
        if not value:
            continue
        number = _claim_number(match.group(1))
        dependencies = _claim_dependencies(value, number)
        claims.append({
            "number": number,
            "text": value,
            "type": "dependent" if dependencies else "independent",
            "depends_on": dependencies,
        })
    validation = validate_claim_structure(claims)
    validation.update({
        "detected_headers": len(matches),
        "selected_headers": len(selected),
        "ignored_headers": len(matches) - len(selected),
    })
    return claims, validation


def extract_claims(text: str) -> list[dict[str, Any]]:
    return extract_claims_with_metadata(text)[0]


def validate_claim_structure(claims: list[dict[str, Any]]) -> dict[str, Any]:
    numbers = [item.get("number") for item in claims]
    errors: list[str] = []
    if not claims:
        errors.append("claims_not_found")
    elif numbers != list(range(1, len(claims) + 1)):
        errors.append("claim_numbers_not_sequential")
    if any(not isinstance(item.get("text"), str) or not item["text"].strip() for item in claims):
        errors.append("empty_claim_text")
    if any(any(not isinstance(dep, int) or dep < 1 or dep >= item["number"] for dep in item.get("depends_on", [])) for item in claims):
        errors.append("invalid_dependency")
    return {"valid": not errors, "errors": errors, "claim_count": len(claims), "numbers": numbers}


SECTION_HEADINGS = {
    "technical_field": ("技術分野",),
    "background": ("背景技術", "従来の技術"),
    "problem": ("発明が解決しようとする課題", "解決しようとする課題"),
    "solution": ("課題を解決するための手段",),
    "effects": ("発明の効果",),
    "embodiments": ("発明を実施するための形態", "実施形態"),
    "claims": ("特許請求の範囲",),
}


def split_sections(text: str) -> dict[str, str]:
    headings: list[tuple[int, int, str]] = []
    for key, labels in SECTION_HEADINGS.items():
        for label in labels:
            match = re.search(rf"(?:【\s*)?{re.escape(label)}(?:\s*】)?", text)
            if match:
                headings.append((match.start(), match.end(), key))
                break
    headings.sort()
    sections: dict[str, str] = {}
    for index, (_, end, key) in enumerate(headings):
        next_start = headings[index + 1][0] if index + 1 < len(headings) else len(text)
        sections[key] = text[end:next_start].strip()
    return sections


def validate_score(value: dict[str, Any]) -> dict[str, Any]:
    for key in ("similarity", "concept_level"):
        if type(value.get(key)) is not int or not 1 <= value[key] <= 5:
            raise ValueError(f"{key} must be an integer from 1 to 5")
    for key in ("similarity_reason", "concept_level_reason"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"{key} is required")
    return value


def validate_vectors(vectors: list[list[float]], expected: int) -> list[list[float]]:
    if len(vectors) != expected or not vectors:
        raise ValueError(f"expected {expected} embeddings")
    dimensions = {len(vector) for vector in vectors}
    if len(dimensions) != 1 or next(iter(dimensions)) < 128:
        raise ValueError(f"invalid embedding dimensions: {dimensions}")
    if any(not all(isinstance(item, (int, float)) for item in vector) for vector in vectors):
        raise ValueError("embedding contains non-numeric values")
    return vectors


@dataclass(frozen=True)
class SourcePolicy:
    extraction: str = "verify_original"
    reading: str = "adaptive"

    @classmethod
    def from_values(cls, research: dict[str, Any], patent: dict[str, Any]) -> "SourcePolicy":
        defaults = research.get("pipeline", {}).get("source_policy", {})
        override = patent.get("source_policy", {})
        extraction = override.get("extraction", defaults.get("extraction", "verify_original"))
        reading = override.get("reading", defaults.get("reading", "adaptive"))
        if extraction not in READING_MODES:
            raise DataError(f"unsupported extraction policy: {extraction}")
        if reading not in {"targeted", "hierarchical_full", "adaptive"}:
            raise DataError(f"unsupported reading policy: {reading}")
        return cls(extraction=extraction, reading=reading)


class SharedExtractionCache:
    """Content-addressed extraction cache. The PDF remains the source of truth."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def obtain(self, pdf_path: Path, policy: str = "verify_original") -> tuple[Path, dict[str, Any]]:
        if policy not in READING_MODES:
            raise DataError(f"unsupported extraction policy: {policy}")
        pdf_bytes = pdf_path.read_bytes()
        pdf_hash = sha256_bytes(pdf_bytes)
        target = self.root / pdf_hash
        manifest_path = target / "extraction_manifest.json"
        reusable = manifest_path.exists() and (target / "extracted_text.txt").exists() and (target / "pages.json").exists()
        if reusable and policy != "reextract":
            manifest = read_json(manifest_path)
            if manifest.get("pdf_sha256") == pdf_hash:
                return target, {**manifest, "cache_decision": "reused", "source_policy": policy}

        reader = PdfReader(pdf_path)
        pages = [{"page": index + 1, "text": clean_text(page.extract_text() or "")} for index, page in enumerate(reader.pages)]
        text = clean_text("\n\n".join(item["text"] for item in pages))
        if len(text) < 500:
            raise DocumentSkipped(
                "image_only_or_insufficient_text",
                f"抽出文字数が少なすぎます: {len(text)}",
            )
        manifest = {
            "schema_version": 1,
            "pdf": pdf_path.name,
            "pdf_sha256": pdf_hash,
            "pages": len(pages),
            "nonempty_pages": sum(bool(item["text"]) for item in pages),
            "characters": len(text),
            "text_sha256": sha256_bytes(text.encode("utf-8")),
            "extractor": "pypdf",
            "ocr_used": False,
            "created_at": utc_now(),
        }
        target.mkdir(parents=True, exist_ok=True)
        (target / "extracted_text.txt").write_text(text, encoding="utf-8")
        atomic_json(target / "pages.json", pages)
        atomic_json(manifest_path, manifest)
        return target, {**manifest, "cache_decision": "created", "source_policy": policy}


def cosine_distance(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    return 1.0 if not left_norm or not right_norm else 1.0 - dot / (left_norm * right_norm)


def deterministic_clusters(items: dict[str, list[float]], cluster_count: int | None = None) -> dict[str, int]:
    """Dependency-free deterministic average-linkage clustering for local research sets."""
    if not items:
        return {}
    keys = sorted(items)
    k = min(len(keys), cluster_count or max(1, round(math.sqrt(len(keys)))))
    distances = {
        (left, right): cosine_distance(items[left], items[right])
        for index, left in enumerate(keys) for right in keys[index + 1:]
    }

    def item_distance(left: str, right: str) -> float:
        if left == right:
            return 0.0
        return distances[tuple(sorted((left, right)))]

    clusters: list[tuple[str, ...]] = [(key,) for key in keys]
    while len(clusters) > k:
        candidates: list[tuple[float, tuple[str, ...], int, int]] = []
        for left_index, left in enumerate(clusters):
            for right_index in range(left_index + 1, len(clusters)):
                right = clusters[right_index]
                average = sum(item_distance(a, b) for a in left for b in right) / (len(left) * len(right))
                candidates.append((average, tuple(sorted(left + right)), left_index, right_index))
        _, merged, left_index, right_index = min(candidates)
        clusters = [cluster for index, cluster in enumerate(clusters) if index not in {left_index, right_index}]
        clusters.append(merged)
        clusters.sort()
    return {key: cluster_id for cluster_id, cluster in enumerate(sorted(clusters)) for key in cluster}


class ResearchPipeline:
    """Filesystem contracts and deterministic stages for one research boundary."""

    def __init__(self, root: Path, environment: str, research_id: str):
        self.root = root.resolve()
        if environment not in {"normal", "debug"}:
            raise DataError("environmentはnormalまたはdebugです")
        self.environment = environment
        self.research_id = safe_id(research_id, "research id")
        base = self.root / ("researches" if environment == "normal" else "debug_data/researches")
        self.research_dir = (base / self.research_id).resolve()
        if self.research_dir.parent != base.resolve() or not self.research_dir.is_dir():
            raise DataError("リサーチが見つかりません")
        self.research = read_research_config(self.research_dir)
        self.research["company_technology"] = read_company_technology(self.research_dir, self.research)
        self.sources, self.input_audit = research_sources(self.research_dir, self.root / "patent_pool")
        self.cache = SharedExtractionCache(self.root / "runtime/shared/extractions")

    def patents(self) -> list[tuple[str, Path, dict[str, Any]]]:
        output: list[tuple[str, Path, dict[str, Any]]] = []
        seen: set[str] = set()
        for source in self.sources:
            sub_id = source["id"]
            for patent in source["patents"]:
                pdf_name = str(patent.get("pdf", ""))
                if Path(pdf_name).name != pdf_name or not pdf_name.lower().endswith(".pdf"):
                    raise DataError(f"不正なPDF参照です: {pdf_name}")
                key = patent_key(pdf_name)
                if key in seen:
                    raise DataError(f"同一リサーチ内でPDFが重複しています: {pdf_name}")
                pdf_path = (self.root / "patent_pool" / pdf_name).resolve()
                if pdf_path.parent != (self.root / "patent_pool").resolve():
                    raise DataError(f"不正なPDF参照です: {pdf_name}")
                seen.add(key)
                output.append((sub_id, pdf_path, patent))
        return output

    def artifact_dir(self, subresearch_id: str, pdf_name: str) -> Path:
        return self.research_dir / "subresearches" / subresearch_id / "pipeline" / patent_key(pdf_name)

    def result_path(self, subresearch_id: str, pdf_name: str) -> Path:
        return self.research_dir / "subresearches" / subresearch_id / "results" / f"{patent_key(pdf_name)}.json"

    def load_analysis_artifacts(self, subresearch_id: str, pdf_name: str) -> dict[str, Any]:
        """Reload completed stage artifacts without invoking an LLM again."""
        artifact = self.artifact_dir(subresearch_id, pdf_name)
        required = {
            "score": "threat_score.json",
            "summaries": "summaries.json",
            "embedding": "embeddings.json",
        }
        missing = [filename for filename in required.values() if not (artifact / filename).is_file()]
        if missing:
            raise DataError(f"処理済み結果の段階成果物が不足しています: {patent_key(pdf_name)} ({', '.join(missing)})")
        return {"artifact_dir": artifact, **{key: read_json(artifact / filename) for key, filename in required.items()}}

    def analysis_checkpoint_current(self, subresearch_id: str, pdf_name: str) -> bool:
        checkpoint = self.artifact_dir(subresearch_id, pdf_name) / "analysis_complete.json"
        if not checkpoint.is_file():
            return False
        try:
            return read_json(checkpoint).get("pipeline_version") == ANALYSIS_PIPELINE_VERSION
        except DataError:
            return False

    def overview(self) -> dict[str, Any]:
        documents = self.patents()
        items = []
        for subresearch_id, pdf_path, patent in documents:
            artifact = self.artifact_dir(subresearch_id, pdf_path.name)
            skip_path = artifact / "skip.json"
            skip = read_json(skip_path) if skip_path.exists() else {}
            error_path = artifact / "analysis_error.json"
            analysis_error = read_json(error_path) if error_path.exists() else {}
            if not pdf_path.is_file():
                skip = {"reason": "pdf_not_found", "detail": f"PDFがpatent_poolにありません: {pdf_path.name}"}
            files = {stage: (artifact / filename).exists() for stage, filename in {
                "source": "source_decision.json", "structure": "document_structure.json",
                "similarity": "similarity.json", "concept_level": "concept_level.json",
                "threat_score": "threat_score.json", "summaries": "summaries.json", "embeddings": "embeddings.json",
            }.items()}
            checkpoint = self.analysis_checkpoint_current(subresearch_id, pdf_path.name)
            result = self.result_path(subresearch_id, pdf_path.name)
            items.append({
                "patent_id": patent_key(pdf_path.name), "pdf": pdf_path.name, "subresearch_id": subresearch_id,
                "source_policy": SourcePolicy.from_values(self.research, patent).__dict__, "stages": files, "finalized": result.exists(),
                "analysis_checkpoint": checkpoint,
                "analysis_error": analysis_error,
                "pdf_available": pdf_path.is_file(), "analysis_state": "skipped" if skip else ("ready" if result.exists() else "pending"),
                "skip_reason": skip.get("reason"), "skip_detail": skip.get("detail"),
            })
        return {
            "research_id": self.research_id,
            "environment": self.environment,
            "source_policy": self.research.get("pipeline", {}).get("source_policy", {}),
            "threat_map": self.research.get("pipeline", {}).get("threat_scoring", {}),
            "input": self.input_audit,
            "stages": list(PIPELINE_STAGES),
            "documents": items,
            "counts": {
                "total": len(items),
                "prepared": sum(item["stages"]["structure"] for item in items),
                "analyzed": sum(item["stages"]["threat_score"] and item["stages"]["summaries"] for item in items),
                "finalized": sum(item["finalized"] for item in items),
                "pending": sum(item["analysis_state"] == "pending" for item in items),
                "llm_pending": sum(item["analysis_state"] == "pending" and not item["analysis_checkpoint"] for item in items),
                "skipped": sum(item["analysis_state"] == "skipped" for item in items),
                "failed": sum(bool(item["analysis_error"]) for item in items),
            },
        }

    def prepare_document(self, subresearch_id: str, pdf_path: Path, patent: dict[str, Any]) -> dict[str, Any]:
        policy = SourcePolicy.from_values(self.research, patent)
        cache_dir, extraction = self.cache.obtain(pdf_path, policy.extraction)
        text = (cache_dir / "extracted_text.txt").read_text(encoding="utf-8")
        artifact = self.artifact_dir(subresearch_id, pdf_path.name)
        sections = split_sections(text)
        claims, claim_validation = extract_claims_with_metadata(text)
        if not claim_validation["valid"]:
            raise DocumentSkipped("invalid_claim_structure", ", ".join(claim_validation["errors"]))
        structure = {
            "schema_version": 2,
            "pdf": pdf_path.name,
            "pdf_sha256": extraction["pdf_sha256"],
            "claims": claims,
            "claim_validation": claim_validation,
            "sections": {key: value for key, value in sections.items() if key != "claims"},
            "section_characters": {
                **{key: len(value) for key, value in sections.items() if key != "claims"},
                "claims": sum(len(item["text"]) for item in claims),
            },
            "created_at": utc_now(),
        }
        source_decision = {
            "schema_version": 1,
            "pdf": pdf_path.name,
            "shared_cache": str(cache_dir.relative_to(self.root)),
            "extraction_policy": policy.extraction,
            "reading_policy": policy.reading,
            "cache_decision": extraction["cache_decision"],
            "decision_reason": "explicit_reextract" if policy.extraction == "reextract" else "matching_pdf_hash_and_complete_cache_or_new_extraction",
            "pdf_sha256": extraction["pdf_sha256"],
            "original_pdf_remains_authoritative": True,
            "created_at": utc_now(),
        }
        atomic_json(artifact / "source_decision.json", source_decision)
        atomic_json(artifact / "document_structure.json", structure)
        return {"artifact_dir": artifact, "cache_dir": cache_dir, "source_decision": source_decision, "structure": structure}

    def write_run_manifest(self, run_id: str, status: str, stages: Iterable[str], detail: dict[str, Any] | None = None) -> Path:
        unknown = set(stages) - set(PIPELINE_STAGES)
        if unknown:
            raise DataError(f"unknown pipeline stages: {sorted(unknown)}")
        path = self.research_dir / "runs" / run_id / "run_manifest.json"
        atomic_json(path, {
            "schema_version": 1,
            "run_id": run_id,
            "environment": self.environment,
            "research_id": self.research_id,
            "status": status,
            "stages": list(stages),
            "detail": {"input": self.input_audit, **(detail or {})},
            "updated_at": utc_now(),
        })
        # Execution history is diagnostic data, not a durable result. Keep it bounded.
        for history_name in ("runs", "clustering"):
            history_root = self.research_dir / history_name
            if not history_root.is_dir():
                continue
            histories = sorted((item for item in history_root.iterdir() if item.is_dir()), reverse=True)
            for stale in histories[5:]:
                shutil.rmtree(stale)
        return path

    def cluster(self, vectors_by_patent: dict[str, dict[str, list[float]]], cluster_count: int | None = None) -> dict[str, Any]:
        technology = {key: value["technology"] for key, value in vectors_by_patent.items()}
        problem = {key: value["problem"] for key, value in vectors_by_patent.items()}
        result = {
            "schema_version": 1,
            "scope": "research",
            "research_id": self.research_id,
            "algorithm": "agglomerative-average-linkage-cosine",
            "cluster_count_requested": cluster_count,
            "technology_assignments": deterministic_clusters(technology, cluster_count),
            "problem_assignments": deterministic_clusters(problem, cluster_count),
            "created_at": utc_now(),
        }
        return result

    @staticmethod
    def _require_strings(value: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
        for key in keys:
            if not isinstance(value.get(key), str) or not value[key].strip():
                raise ValueError(f"{key} is required")
            value[key] = value[key].strip()
        return value

    def analyze_document(
        self,
        prepared: dict[str, Any],
        generate_json: "GenerateJson",
        embed_texts: "EmbedTexts",
    ) -> dict[str, Any]:
        """Run four small schema-constrained LLM jobs and two summary embeddings."""
        artifact: Path = prepared["artifact_dir"]
        structure = prepared["structure"]
        cache_dir: Path = prepared["cache_dir"]
        text = (cache_dir / "extracted_text.txt").read_text(encoding="utf-8")
        sections = structure["sections"]
        independent = [claim for claim in structure["claims"] if claim["type"] == "independent"] or structure["claims"][:1]
        claims_text = "\n\n".join(f"【請求項{claim['number']}】{claim['text']}" for claim in independent)

        def bounded(parts: Iterable[tuple[str, str]], maximum: int = 12_000) -> str:
            output: list[str] = []
            remaining = maximum
            for label, value in parts:
                value = (value or "").strip()
                if not value or remaining <= 0:
                    continue
                block = f"## {label}\n{value}"
                block = block[:remaining]
                output.append(block)
                remaining -= len(block) + 2
            return "\n\n".join(output) or text[:maximum]

        similarity_input = bounded((
            ("独立請求項", claims_text), ("技術分野", sections.get("technical_field", "")),
            ("課題を解決するための手段", sections.get("solution", "")),
            ("実施形態", sections.get("embodiments", "")),
        ))
        concept_input = bounded((("独立請求項", claims_text),), maximum=10_000)
        problem_input = bounded((
            ("背景技術", sections.get("background", "")), ("発明が解決しようとする課題", sections.get("problem", "")),
            ("発明の効果", sections.get("effects", "")),
        ))
        technology_input = bounded((
            ("独立請求項", claims_text), ("技術分野", sections.get("technical_field", "")),
            ("課題を解決するための手段", sections.get("solution", "")),
            ("実施形態", sections.get("embodiments", "")),
        ))
        atomic_json(artifact / "analysis_inputs.json", {
            "pipeline_version": ANALYSIS_PIPELINE_VERSION,
            "characters": {
                "similarity": len(similarity_input), "concept_level": len(concept_input),
                "problem_summary": len(problem_input), "technology_summary": len(technology_input),
            },
            "sha256": {
                "similarity": sha256_bytes(similarity_input.encode("utf-8")),
                "concept_level": sha256_bytes(concept_input.encode("utf-8")),
                "problem_summary": sha256_bytes(problem_input.encode("utf-8")),
                "technology_summary": sha256_bytes(technology_input.encode("utf-8")),
            },
        })

        similarity = generate_json("similarity", {
            "company_technology": self.research.get("company_technology", ""),
            "patent_text": similarity_input,
        })
        atomic_json(artifact / "similarity.json", similarity)
        concept = generate_json("concept_level", {"patent_text": concept_input})
        atomic_json(artifact / "concept_level.json", concept)
        problem = generate_json("problem_summary", {"patent_text": problem_input})
        technology = generate_json("technology_summary", {"patent_text": technology_input})

        score = validate_score({
            "similarity": similarity["similarity"],
            "concept_level": concept["concept_level"],
            "similarity_reason": similarity["reason"],
            "concept_level_reason": concept["reason"],
        })
        atomic_json(artifact / "threat_score.json", score)
        summaries = {
            "tech_summary": technology["tech_summary"].strip(),
            "problem_summary": problem["problem_summary"].strip(),
        }
        self._require_strings(summaries, ("tech_summary", "problem_summary"))
        atomic_json(artifact / "summaries.json", summaries)

        embedding_inputs = [summaries["tech_summary"], summaries["problem_summary"]]
        vectors = validate_vectors(embed_texts(embedding_inputs), 2)
        embedding = {
            "input_order": ["technology", "problem"],
            "input_sha256": [sha256_bytes(item.encode("utf-8")) for item in embedding_inputs],
            "dimensions": len(vectors[0]),
            "vectors": {"technology": vectors[0], "problem": vectors[1]},
            "created_at": utc_now(),
        }
        atomic_json(artifact / "embeddings.json", embedding)
        return {
            "artifact_dir": artifact,
            "score": score,
            "summaries": summaries,
            "embedding": embedding,
        }

    def finalize_research(
        self,
        analyses: dict[str, dict[str, Any]],
        locations: dict[str, str],
        generate_json: "GenerateJson",
        run_id: str,
        cluster_count: int | None = None,
    ) -> dict[str, Any]:
        vectors = {key: value["embedding"]["vectors"] for key, value in analyses.items()}
        clustering = self.cluster(vectors, cluster_count)

        def names(kind: str, assignments: dict[str, int]) -> dict[int, str]:
            output: dict[int, str] = {}
            summary_key = "tech_summary" if kind == "technology" else "problem_summary"
            for cluster_id in sorted(set(assignments.values())):
                members = [key for key, assigned in assignments.items() if assigned == cluster_id]
                response = generate_json("cluster_name", {
                    "kind": kind,
                    "cluster_id": cluster_id,
                    "summaries": [{"patent_id": key, "summary": analyses[key]["summaries"][summary_key]} for key in members],
                })
                self._require_strings(response, ("name",))
                output[cluster_id] = response["name"]
            return output

        tech_names = names("technology", clustering["technology_assignments"])
        problem_names = names("problem", clustering["problem_assignments"])
        clustering["technology_cluster_names"] = tech_names
        clustering["problem_cluster_names"] = problem_names
        cluster_dir = self.research_dir / "clustering" / run_id
        atomic_json(cluster_dir / "clusters.json", clustering)

        results: dict[str, str] = {}
        for key, analysis in analyses.items():
            score = analysis["score"]
            summary = analysis["summaries"]
            result = {
                "similarity": score["similarity"],
                "concept_level": score["concept_level"],
                "tech_summary": summary["tech_summary"],
                "problem_summary": summary["problem_summary"],
                "reasoning": f"類似度: {score['similarity_reason']}\n概念レベル: {score['concept_level_reason']}",
                "tech_cluster": tech_names[clustering["technology_assignments"][key]],
                "problem_cluster": problem_names[clustering["problem_assignments"][key]],
            }
            result_path = self.research_dir / "subresearches" / locations[key] / "results" / f"{key}.json"
            atomic_json(result_path, result)
            results[key] = str(result_path.relative_to(self.root))
        return {"clustering": str((cluster_dir / "clusters.json").relative_to(self.root)), "results": results}


GenerateJson = Callable[[str, dict[str, Any]], dict[str, Any]]
EmbedTexts = Callable[[list[str]], list[list[float]]]
