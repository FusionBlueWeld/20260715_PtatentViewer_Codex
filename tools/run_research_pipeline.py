from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from patent_viewer.domain import DataError, patent_key
from patent_viewer.pipeline import (
    ANALYSIS_PIPELINE_VERSION,
    DocumentSkipped,
    PIPELINE_STAGES,
    ResearchPipeline,
    atomic_json,
    sha256_bytes,
)


class DocumentAnalysisError(RuntimeError):
    """A model output failure that is isolated to one patent document."""

    def __init__(self, task: str, detail: str, attempts: int = 1):
        super().__init__(f"{task}: {detail}")
        self.task = task
        self.detail = detail
        self.attempts = attempts


class PipelineCancelled(RuntimeError):
    """User-requested cancellation; never convert this into a document skip."""


SCHEMA_FILES = {
    "similarity": "llm-similarity.schema.json",
    "concept_level": "llm-concept-level.schema.json",
    "problem_summary": "llm-problem-summary.schema.json",
    "technology_summary": "llm-technology-summary.schema.json",
    "cluster_name": "llm-cluster-name.schema.json",
}
STAGE_SCHEMAS = {
    task: json.loads((ROOT / "schemas" / filename).read_text(encoding="utf-8"))
    for task, filename in SCHEMA_FILES.items()
}
TASK_OUTPUT_TOKENS = {
    "similarity": (384, 512, 768),
    "concept_level": (384, 512, 768),
    "problem_summary": (384, 512, 768),
    "technology_summary": (384, 512, 768),
    "cluster_name": (128, 192, 256),
}
DOCUMENT_TASK_INDEX = {
    "similarity": 1,
    "concept_level": 2,
    "problem_summary": 3,
    "technology_summary": 4,
}


def validate_schema_value(value, schema: dict, path: str = "$") -> None:
    """Validate the deliberately small JSON-Schema subset used by local LLM calls."""
    expected = schema.get("type")
    if expected == "object":
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        properties = schema.get("properties", {})
        missing = [key for key in schema.get("required", []) if key not in value]
        if missing:
            raise ValueError(f"{path} missing required keys: {', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(value) - set(properties))
            if extra:
                raise ValueError(f"{path} has unexpected keys: {', '.join(extra)}")
        for key, item in value.items():
            if key in properties:
                validate_schema_value(item, properties[key], f"{path}.{key}")
        return
    if expected == "string":
        if not isinstance(value, str):
            raise ValueError(f"{path} must be a string")
        if len(value) < schema.get("minLength", 0):
            raise ValueError(f"{path} is too short")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ValueError(f"{path} exceeds {schema['maxLength']} characters")
    elif expected == "integer":
        if type(value) is not int:
            raise ValueError(f"{path} must be an integer")
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            raise ValueError(f"{path} is outside the permitted range")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is not an allowed value")


def extraction_index_entry(root: Path, subresearch_id: str, pdf_name: str, prepared: dict) -> dict:
    cache_dir = prepared["cache_dir"]
    artifact_dir = prepared["artifact_dir"]
    manifest = json.loads((cache_dir / "extraction_manifest.json").read_text(encoding="utf-8"))
    return {
        "patent_id": patent_key(pdf_name),
        "subresearch_id": subresearch_id,
        "pdf": pdf_name,
        "pdf_sha256": manifest["pdf_sha256"],
        "pages": manifest["pages"],
        "nonempty_pages": manifest["nonempty_pages"],
        "characters": manifest["characters"],
        "extractor": manifest["extractor"],
        "ocr_used": manifest["ocr_used"],
        "cache_decision": prepared["source_decision"]["cache_decision"],
        "extracted_text": str((cache_dir / "extracted_text.txt").relative_to(root)),
        "pages_json": str((cache_dir / "pages.json").relative_to(root)),
        "extraction_manifest": str((cache_dir / "extraction_manifest.json").relative_to(root)),
        "document_structure": str((artifact_dir / "document_structure.json").relative_to(root)),
        "source_decision": str((artifact_dir / "source_decision.json").relative_to(root)),
    }


def write_extraction_indexes(root: Path, pipeline: ResearchPipeline, entries: dict[str, list[dict]]) -> list[Path]:
    paths = []
    for subresearch_id, documents in entries.items():
        path = pipeline.research_dir / "subresearches" / subresearch_id / "pipeline" / "extraction_index.json"
        atomic_json(path, {
            "schema_version": 1,
            "research_id": pipeline.research_id,
            "environment": pipeline.environment,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "document_count": len(documents),
            "total_pages": sum(item["pages"] for item in documents),
            "total_characters": sum(item["characters"] for item in documents),
            "documents": documents,
        })
        paths.append(path)
    return paths


def context_window_for(prompt: str, output_tokens: int = 1200) -> int:
    # Actual Japanese patent prompts measured near 0.6 tokens/character. Use 0.8 plus
    # output and framing headroom, then select the smallest GPU-conscious power of two.
    estimated_input_tokens = math.ceil(len(prompt) * 0.8)
    required = estimated_input_tokens + output_tokens + 512
    for window in (4096, 8192, 16384):
        if required <= window:
            return window
    raise RuntimeError(
        "prompt exceeds the 16K context budget and must be split further: "
        f"{len(prompt)} characters, approximately {required} tokens including output"
    )


def write_progress_event(path: Path, payload: dict) -> bool:
    """Best-effort telemetry: a locked UI progress file must never stop analysis."""
    try:
        atomic_json(path, payload)
        return True
    except OSError as exc:
        print(f"warning: progress update skipped: {exc}", file=sys.stderr, flush=True)
        return False


class OllamaStages:
    def __init__(self, model: str, embedding_model: str, timeout: int, checkpoint=lambda: None, progress=lambda stage, **detail: None):
        self.model = model
        self.embedding_model = embedding_model
        self.timeout = timeout
        self.audit_dir: Path | None = None
        self.counter = 0
        self.checkpoint = checkpoint
        self.progress = progress

    def _post(self, endpoint: str, payload: dict) -> dict:
        request = urllib.request.Request(
            "http://127.0.0.1:11434" + endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Ollama {endpoint} failed: {exc}") from exc
        if result.get("error"):
            raise RuntimeError(f"Ollama {endpoint}: {result['error']}")
        return result

    def generate_json(self, task: str, data: dict) -> dict:
        self.checkpoint()
        template = (ROOT / "src/patent_viewer/prompts/stages" / f"{task}.txt").read_text(encoding="utf-8")
        schema = STAGE_SCHEMAS[task]
        if task == "similarity":
            input_text = (
                f"## 自社技術\n{data['company_technology']}\n\n"
                f"## 市場特許\n{data['patent_text']}"
            )
        elif task in {"concept_level", "problem_summary", "technology_summary"}:
            input_text = f"## 市場特許\n{data['patent_text']}"
        elif task == "cluster_name":
            summaries = "\n".join(
                f"- {item['patent_id']}: {item['summary']}" for item in data["summaries"]
            )
            input_text = f"## 種類\n{data['kind']}\n\n## 要約群\n{summaries}"
        else:
            raise ValueError(f"unknown local LLM task: {task}")
        base_prompt = (
            f"{template}\n\n## 出力JSON Schema\n{json.dumps(schema, ensure_ascii=False)}"
            f"\n\n{input_text}"
        )
        audit = self.audit_dir / "llm_calls" if self.audit_dir else None
        last_error = "unknown error"
        token_limits = TASK_OUTPUT_TOKENS[task]
        for attempt, num_predict in enumerate(token_limits, 1):
            self.checkpoint()
            retry_note = (
                "\n\n前回の応答は途中終了または不正JSONでした。説明を短くし、必ず完結したJSONオブジェクトだけを返してください。"
                if attempt > 1 else ""
            )
            prompt = base_prompt + retry_note
            num_ctx = context_window_for(prompt, num_predict)
            payload = {
                "model": self.model, "prompt": prompt, "stream": False, "format": schema, "keep_alive": "10m",
                "options": {"temperature": 0, "top_p": 0.9, "num_ctx": num_ctx, "num_predict": num_predict},
            }
            if task == "cluster_name":
                self.progress(
                    "cluster_naming", task=task, attempt=attempt,
                    max_attempts=len(token_limits),
                )
            else:
                self.progress(
                    "llm_task", task=task, task_index=DOCUMENT_TASK_INDEX[task], task_total=5,
                    attempt=attempt, max_attempts=len(token_limits),
                )
            self.counter += 1
            call_number = self.counter
            if audit:
                atomic_json(audit / f"{call_number:03d}-{task}-request.json", {
                    "task": task, "attempt": attempt, "model": self.model,
                    "prompt_sha256": sha256_bytes(prompt.encode("utf-8")),
                    "prompt_characters": len(prompt), "num_ctx": num_ctx, "num_predict": num_predict, "input": data,
                })
            response = self._post("/api/generate", payload)
            if audit:
                atomic_json(audit / f"{call_number:03d}-{task}-response.json", response)
            raw = response.get("response")
            if response.get("done") is False or response.get("done_reason") == "length":
                last_error = f"response ended before completion ({response.get('done_reason', 'done=false')})"
                continue
            if not isinstance(raw, str) or not raw.strip():
                last_error = "empty response"
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                last_error = f"invalid JSON: {exc}"
                continue
            try:
                validate_schema_value(parsed, schema)
            except ValueError as exc:
                last_error = str(exc)
                continue
            return parsed
        raise DocumentAnalysisError(task, f"failed after {len(token_limits)} JSON attempts: {last_error}", len(token_limits))

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.checkpoint()
        self.progress("llm_task", task="embeddings", task_index=5, task_total=5, attempt=1, max_attempts=1)
        # Never keep the generation and embedding models resident together on a 16 GiB GPU.
        self.unload_generation()
        response = self._post("/api/embed", {
            "model": self.embedding_model, "input": texts, "keep_alive": 0, "options": {"num_ctx": 4096},
        })
        vectors = response.get("embeddings")
        if not isinstance(vectors, list):
            raise DocumentAnalysisError("embeddings", "Ollama embedding response is invalid")
        return vectors

    def unload_generation(self) -> None:
        try:
            self._post("/api/generate", {"model": self.model, "keep_alive": 0})
        except RuntimeError:
            # Cleanup must not replace the actual pipeline result or error.
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="リサーチ単位の段階パイプラインを準備・実行する")
    parser.add_argument("research_id")
    parser.add_argument("--environment", choices=("normal", "debug"), default="normal")
    parser.add_argument("--stage", choices=("prepare", "plan", "execute"), default="prepare")
    parser.add_argument("--generation-model", default="gemma4:e4b")
    parser.add_argument("--embedding-model", default="qwen3-embedding:8b")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--cluster-count", type=int)
    parser.add_argument("--limit", type=int, help="先頭から処理する文献数（ローカル検証用）")
    parser.add_argument("--cooldown-seconds", type=int, default=0, help="各文献のLLM分析後に待機する秒数")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--events-file", type=Path)
    parser.add_argument("--control-file", type=Path)
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be at least 1")
    if not 0 <= args.cooldown_seconds <= 3600:
        parser.error("--cooldown-seconds must be between 0 and 3600")

    last_progress: dict = {}

    def emit(stage: str, **detail):
        last_progress.clear()
        last_progress.update({"stage": stage, "detail": detail})
        if args.events_file:
            write_progress_event(args.events_file, {
                "stage": stage, "detail": detail,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

    def checkpoint():
        while args.control_file and args.control_file.exists():
            try:
                value = json.loads(args.control_file.read_text(encoding="utf-8")).get("control", "run")
            except (OSError, json.JSONDecodeError):
                value = "run"
            if value == "cancel":
                raise PipelineCancelled("pipeline job cancelled")
            if value == "run":
                return
            time.sleep(0.25)

    def cooldown(patent_id: str, completed: int, total: int) -> None:
        remaining = args.cooldown_seconds
        while remaining > 0:
            checkpoint()
            emit(
                "cooldown", patent_id=patent_id, completed=completed, current=completed,
                total=total, remaining_seconds=remaining,
                percent=round(completed / max(1, total) * 100, 1),
            )
            time.sleep(1)
            remaining -= 1

    model = None
    try:
        emit("initializing", mode=args.stage, research_id=args.research_id)
        pipeline = ResearchPipeline(ROOT, args.environment, args.research_id)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        documents = []
        progress_context: dict = {}

        def model_progress(stage: str, **detail) -> None:
            emit(stage, **progress_context, **detail)

        model = OllamaStages(args.generation_model, args.embedding_model, args.timeout, checkpoint, model_progress)
        analyses = {}
        locations = {}
        extraction_entries: dict[str, list[dict]] = {}
        document_failures: list[dict] = []
        document_skips: list[dict] = []
        if args.stage == "plan":
            for subresearch_id, pdf_path, patent in pipeline.patents():
                checkpoint()
                documents.append({
                    "patent_id": patent_key(pdf_path.name),
                    "subresearch_id": subresearch_id,
                    "pdf": pdf_path.name,
                    "analysis_state": "pending" if pdf_path.is_file() else "skipped",
                    "skip_reason": None if pdf_path.is_file() else "pdf_not_found",
                    "source_policy": patent.get("source_policy", pipeline.research.get("pipeline", {}).get("source_policy", {})),
                })
        if args.stage in {"prepare", "execute"}:
            patents = pipeline.patents()
            if args.limit is not None:
                patents = patents[:args.limit]
            pending_total = sum(
                not pipeline.result_path(subresearch_id, pdf_path.name).exists() or args.overwrite
                for subresearch_id, pdf_path, _ in patents
                if pdf_path.is_file()
            ) if args.stage == "execute" else len(patents)
            llm_total = sum(
                (args.overwrite or not pipeline.result_path(subresearch_id, pdf_path.name).exists())
                and (args.overwrite or not pipeline.analysis_checkpoint_current(subresearch_id, pdf_path.name))
                for subresearch_id, pdf_path, _ in patents if pdf_path.is_file()
            ) if args.stage == "execute" else 0
            llm_completed = 0
            llm_attempted = 0
            pending_index = 0
            existing_analyses = {}
            for document_index, (subresearch_id, pdf_path, patent) in enumerate(patents, 1):
                checkpoint()
                key = patent_key(pdf_path.name)
                final_path = pipeline.result_path(subresearch_id, pdf_path.name)
                if args.stage == "execute" and final_path.exists() and not args.overwrite:
                    if pending_total:
                        existing_analyses[key] = pipeline.load_analysis_artifacts(subresearch_id, pdf_path.name)
                        locations[key] = subresearch_id
                    documents.append({
                        "patent_id": key, "subresearch_id": subresearch_id, "pdf": pdf_path.name,
                        "analysis_state": "finalized", "skip_reason": "already_processed",
                    })
                    emit("already_processed", patent_id=key, current=pending_index, total=pending_total)
                    continue
                if args.stage == "execute" and pdf_path.is_file():
                    pending_index += 1
                    if pipeline.analysis_checkpoint_current(subresearch_id, pdf_path.name) and not args.overwrite:
                        analyses[key] = pipeline.load_analysis_artifacts(subresearch_id, pdf_path.name)
                        locations[key] = subresearch_id
                        documents.append({
                            "patent_id": key, "subresearch_id": subresearch_id, "pdf": pdf_path.name,
                            "analysis_state": "analyzed", "skip_reason": "analysis_checkpoint_reused",
                        })
                        emit(
                            "checkpoint_reused", patent_id=key, completed=pending_index,
                            current=pending_index, total=pending_total,
                            percent=round(pending_index / max(1, pending_total) * 100, 1),
                        )
                        continue
                progress_index = pending_index if args.stage == "execute" else document_index
                emit("preparing", patent_id=key, current=progress_index, total=pending_total)
                if not pdf_path.is_file():
                    skip = {
                        "schema_version": 1, "patent_id": key, "pdf": pdf_path.name,
                        "reason": "pdf_not_found", "detail": f"PDFがpatent_poolにありません: {pdf_path.name}",
                        "skipped_at": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
                    }
                    atomic_json(pipeline.artifact_dir(subresearch_id, pdf_path.name) / "skip.json", skip)
                    documents.append({"patent_id": key, "subresearch_id": subresearch_id, "pdf": pdf_path.name, "analysis_state": "skipped", **skip})
                    document_skips.append(skip)
                    emit("skipped", patent_id=key, reason=skip["reason"], current=progress_index, total=pending_total)
                    continue
                try:
                    prepared = pipeline.prepare_document(subresearch_id, pdf_path, patent)
                except DocumentSkipped as exc:
                    skip = {
                        "schema_version": 1, "patent_id": key, "pdf": pdf_path.name,
                        "reason": exc.reason, "detail": exc.detail,
                        "skipped_at": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
                    }
                    atomic_json(pipeline.artifact_dir(subresearch_id, pdf_path.name) / "skip.json", skip)
                    documents.append({"patent_id": key, "subresearch_id": subresearch_id, "pdf": pdf_path.name, "analysis_state": "skipped", **skip})
                    document_skips.append(skip)
                    emit("skipped", patent_id=key, reason=exc.reason, current=progress_index, total=pending_total)
                    continue
                except DataError:
                    raise
                except Exception as exc:
                    failure = {
                        "schema_version": 1,
                        "pipeline_version": ANALYSIS_PIPELINE_VERSION,
                        "run_id": run_id,
                        "patent_id": key,
                        "pdf": pdf_path.name,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                        "failed_stage": "prepare",
                        "attempts": 1,
                        "audit_dir": None,
                        "completed_artifacts": [],
                        "failed_at": datetime.now(timezone.utc).isoformat(),
                        "retry_on_next_run": True,
                    }
                    atomic_json(pipeline.artifact_dir(subresearch_id, pdf_path.name) / "analysis_error.json", failure)
                    documents.append({
                        "patent_id": key, "subresearch_id": subresearch_id, "pdf": pdf_path.name,
                        "analysis_state": "failed", "analysis_error": failure,
                    })
                    document_failures.append(failure)
                    emit(
                        "document_failed", patent_id=key, error=str(exc), failed=len(document_failures),
                        completed=progress_index, current=progress_index, total=pending_total,
                        percent=round(progress_index / max(1, pending_total) * 100, 1),
                    )
                    continue
                skip_path = prepared["artifact_dir"] / "skip.json"
                if skip_path.exists():
                    skip_path.unlink()
                document_record = {
                    "patent_id": key,
                    "subresearch_id": subresearch_id,
                    "pdf": pdf_path.name,
                    "artifact_dir": str(prepared["artifact_dir"].relative_to(ROOT)),
                    "source_decision": prepared["source_decision"],
                    "claims": len(prepared["structure"]["claims"]),
                    "analysis_state": "ready" if args.stage == "execute" else "prepared",
                }
                documents.append(document_record)
                extraction_entries.setdefault(subresearch_id, []).append(
                    extraction_index_entry(ROOT, subresearch_id, pdf_path.name, prepared)
                )
                if args.stage == "execute":
                    emit("analyzing", patent_id=key, current=progress_index, total=pending_total)
                    progress_context.clear()
                    progress_context.update({
                        "patent_id": key, "current": progress_index, "total": pending_total,
                        "processed": max(0, progress_index - 1), "succeeded": llm_completed,
                        "failed": len(document_failures), "skipped": len(document_skips),
                        "percent": round(max(0, progress_index - 1) / max(1, pending_total) * 100, 1),
                    })
                    model.audit_dir = prepared["artifact_dir"] / "attempts" / run_id
                    model.counter = 0
                    llm_attempted += 1
                    try:
                        analyses[key] = pipeline.analyze_document(prepared, model.generate_json, model.embed)
                    except PipelineCancelled:
                        raise
                    except (DocumentAnalysisError, ValueError, RuntimeError, OSError) as exc:
                        # Malformed or semantically invalid model output is local to this PDF.
                        # Preserve an audit record, unload the model for cooling, and continue.
                        model.unload_generation()
                        failure = {
                            "schema_version": 1,
                            "pipeline_version": ANALYSIS_PIPELINE_VERSION,
                            "run_id": run_id,
                            "patent_id": key,
                            "pdf": pdf_path.name,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "failed_stage": getattr(exc, "task", "python_validation"),
                            "attempts": getattr(exc, "attempts", 1),
                            "audit_dir": str(model.audit_dir.relative_to(ROOT)),
                            "completed_artifacts": sorted(
                                path.name for path in prepared["artifact_dir"].glob("*.json")
                                if path.name not in {"analysis_error.json", "analysis_complete.json"}
                            ),
                            "failed_at": datetime.now(timezone.utc).isoformat(),
                            "retry_on_next_run": True,
                        }
                        atomic_json(prepared["artifact_dir"] / "analysis_error.json", failure)
                        document_record.update({"analysis_state": "failed", "analysis_error": failure})
                        document_failures.append(failure)
                        emit(
                            "document_failed", patent_id=key, error=str(exc), failed=len(document_failures),
                            task=getattr(exc, "task", "runtime"),
                            task_index=DOCUMENT_TASK_INDEX.get(getattr(exc, "task", ""), 5), task_total=5,
                            completed=pending_index, processed=pending_index, succeeded=llm_completed,
                            skipped=len(document_skips), current=pending_index, total=pending_total,
                            percent=round(pending_index / max(1, pending_total) * 100, 1),
                        )
                        if llm_attempted < llm_total and args.cooldown_seconds:
                            cooldown(key, pending_index, pending_total)
                        continue
                    locations[key] = subresearch_id
                    llm_completed += 1
                    error_path = prepared["artifact_dir"] / "analysis_error.json"
                    if error_path.exists():
                        error_path.unlink()
                    document_record["analysis_state"] = "analyzed"
                    atomic_json(prepared["artifact_dir"] / "analysis_complete.json", {
                        "schema_version": 1, "patent_id": key, "pdf_sha256": prepared["source_decision"]["pdf_sha256"],
                        "pipeline_version": ANALYSIS_PIPELINE_VERSION, "run_id": run_id,
                        "generation_model": args.generation_model, "embedding_model": args.embedding_model,
                        "audit_dir": str(model.audit_dir.relative_to(ROOT)),
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    })
                    emit(
                        "document_completed", patent_id=key, completed=pending_index,
                        processed=pending_index, succeeded=llm_completed, failed=len(document_failures),
                        skipped=len(document_skips), current=pending_index, total=pending_total,
                        llm_completed=llm_completed, llm_total=llm_total,
                        percent=round(pending_index / max(1, pending_total) * 100, 1),
                    )
                    if llm_attempted < llm_total and args.cooldown_seconds:
                        cooldown(key, pending_index, pending_total)
        extraction_indexes = write_extraction_indexes(ROOT, pipeline, extraction_entries) if extraction_entries else []
        finalized = None
        if args.stage == "execute":
            if analyses:
                checkpoint()
                processed_count = llm_completed
                analyses = {**existing_analyses, **analyses}
                progress_context.clear()
                progress_context.update({
                    "processed": pending_total, "succeeded": llm_completed,
                    "failed": len(document_failures), "skipped": len(document_skips),
                    "current": pending_total, "total": pending_total, "percent": 100,
                })
                emit("clustering", documents=len(analyses), processed=processed_count, completed=pending_total, current=pending_total, total=pending_total, percent=100)
                model.audit_dir = pipeline.research_dir / "clustering" / run_id
                model.counter = 0
                finalized = pipeline.finalize_research(analyses, locations, model.generate_json, run_id, args.cluster_count)
                finalized["processed"] = processed_count
                finalized["failed"] = len(document_failures)
                finalized["failed_documents"] = [item["patent_id"] for item in document_failures]
            else:
                finalized = {
                    "clustering": None, "results": {}, "processed": 0,
                    "failed": len(document_failures),
                    "failed_documents": [item["patent_id"] for item in document_failures],
                }
                emit("nothing_to_process", documents=0)
        manifest = pipeline.write_run_manifest(
            run_id,
            "completed" if args.stage == "execute" else ("prepared" if args.stage == "prepare" else "planned"),
            PIPELINE_STAGES,
            {
                "documents": documents,
                "document_failures": document_failures,
                "document_skips": document_skips,
                "extraction_indexes": [str(path.relative_to(ROOT)) for path in extraction_indexes],
                "llm_execution": (
                    "completed_with_document_failures" if args.stage == "execute" and document_failures
                    else ("completed" if args.stage == "execute" else "not_started")
                ),
                "outputs": finalized,
            },
        )
        completed_total = pending_total if args.stage == "execute" else len(documents)
        emit(
            "completed", run_id=run_id, documents=len(documents), failed=len(document_failures),
            completed=completed_total, processed=completed_total, succeeded=llm_completed,
            skipped=len(document_skips), current=completed_total, total=completed_total, percent=100,
        )
        model.unload_generation()
        print(json.dumps({
            "status": "completed",
            "mode": args.stage,
            "research_id": args.research_id,
            "pipeline_stages": PIPELINE_STAGES,
            "documents": documents,
            "document_failures": document_failures,
            "document_skips": document_skips,
            "extraction_indexes": [str(path.relative_to(ROOT)) for path in extraction_indexes],
            "outputs": finalized,
            "run_manifest": str(manifest.relative_to(ROOT)),
        }, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        if model is not None:
            model.unload_generation()
        previous = dict(last_progress)
        failure_detail = dict(previous.get("detail", {}))
        failure_detail.update({"error": str(exc), "failed_at_stage": previous.get("stage", "")})
        emit("failed", **failure_detail)
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
