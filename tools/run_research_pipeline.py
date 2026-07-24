from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import sys
import threading
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
    "company_profile": "llm-company-profile.schema.json",
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
    "company_profile": (384, 512, 768),
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


def extraction_index_entry(root: Path, pdf_name: str, prepared: dict) -> dict:
    cache_dir = prepared["cache_dir"]
    artifact_dir = prepared["artifact_dir"]
    manifest = json.loads((cache_dir / "extraction_manifest.json").read_text(encoding="utf-8"))
    return {
        "patent_id": patent_key(pdf_name),
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


def write_extraction_index(root: Path, pipeline: ResearchPipeline, documents: list[dict]) -> Path:
    path = pipeline.research_dir / "pipeline" / "extraction_index.json"
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
    return path


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
    def __init__(self, model: str, embedding_model: str, timeout: int, checkpoint=lambda: None, progress=lambda stage, **detail: None, ollama_url: str = "http://127.0.0.1:11434", keep_alive: str = "1h"):
        self.model = model
        self.embedding_model = embedding_model
        self.timeout = timeout
        self.audit_dir: Path | None = None
        self.counter = 0
        self.checkpoint = checkpoint
        self.progress = progress
        self.ollama_url = ollama_url.rstrip("/")
        self.keep_alive = keep_alive
        self._audit_lock = threading.Lock()
        self._audit_counters: dict[str, int] = {}

    def _post(self, endpoint: str, payload: dict) -> dict:
        request = urllib.request.Request(
            self.ollama_url + endpoint,
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

    def generate_json(self, task: str, data: dict, audit_dir: Path | None = None) -> dict:
        self.checkpoint()
        template = (ROOT / "src/patent_viewer/prompts/stages" / f"{task}.txt").read_text(encoding="utf-8")
        schema = STAGE_SCHEMAS[task]
        if task == "company_profile":
            input_text = f"## 自社技術定義\n{data['company_technology']}"
        elif task == "similarity":
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
        selected_audit = audit_dir or self.audit_dir
        audit = selected_audit / "llm_calls" if selected_audit else None
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
                "model": self.model, "prompt": prompt, "stream": False, "format": schema, "keep_alive": self.keep_alive,
                "options": {"temperature": 0, "top_p": 0.9, "num_ctx": num_ctx, "num_predict": num_predict},
            }
            if task == "cluster_name":
                self.progress(
                    "cluster_naming", task=task, attempt=attempt,
                    max_attempts=len(token_limits),
                )
            elif task == "company_profile":
                self.progress(
                    "company_profile", task=task, attempt=attempt,
                    max_attempts=len(token_limits),
                )
            else:
                self.progress(
                    "llm_task", task=task, task_index=DOCUMENT_TASK_INDEX[task], task_total=5,
                    attempt=attempt, max_attempts=len(token_limits),
                )
            with self._audit_lock:
                audit_key = str(audit or "__default__")
                call_number = self._audit_counters.get(audit_key, 0) + 1
                self._audit_counters[audit_key] = call_number
                if audit is None:
                    self.counter = call_number
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
        response = self._post("/api/embed", {
            "model": self.embedding_model, "input": texts, "keep_alive": self.keep_alive, "options": {"num_ctx": 4096},
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

    def unload_embedding(self) -> None:
        try:
            self._post("/api/generate", {"model": self.embedding_model, "keep_alive": 0})
        except RuntimeError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="リサーチ単位の段階パイプラインを準備・実行する")
    parser.add_argument("research_id")
    parser.add_argument("--environment", choices=("normal", "debug"), default="normal")
    parser.add_argument("--stage", choices=("prepare", "plan", "execute"), default="prepare")
    parser.add_argument("--generation-model", default="gemma4:e4b")
    parser.add_argument("--embedding-model", default="qwen3-embedding:8b")
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--generation-workers", type=int, default=1)
    parser.add_argument("--embedding-batch-size", type=int, default=16)
    parser.add_argument("--shard-size", type=int, default=250)
    parser.add_argument("--durable-shards", action="store_true", help="persist and release each shard before global clustering")
    parser.add_argument("--cooldown-every-documents", type=int, default=50)
    parser.add_argument("--keep-alive", default="1h")
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
    if not 1 <= args.generation_workers <= 8:
        parser.error("--generation-workers must be between 1 and 8")
    if not 2 <= args.embedding_batch_size <= 1024:
        parser.error("--embedding-batch-size must be between 2 and 1024")
    if args.embedding_batch_size % 2:
        parser.error("--embedding-batch-size must be even")
    if args.shard_size < 1:
        parser.error("--shard-size must be at least 1")
    if args.cooldown_every_documents < 1:
        parser.error("--cooldown-every-documents must be at least 1")
    if not 0 <= args.cooldown_seconds <= 180:
        parser.error("--cooldown-seconds must be between 0 and 180")

    last_progress: dict = {}
    progress_lock = threading.Lock()
    timing = {"started": time.monotonic(), "phase": None, "phase_started": time.monotonic(), "durations": {}}

    def timing_phase(stage: str, detail: dict) -> str:
        if stage == "cooldown":
            return "cooldown"
        if stage in {"initializing", "preparing", "skipped", "already_processed", "checkpoint_reused"}:
            return "preparation"
        if stage == "company_profile":
            return "company_profile"
        if stage in {"llm_batch", "llm_task"}:
            task = detail.get("task", "generation")
            return "embeddings" if task == "embeddings" else f"generation:{task}"
        if stage == "company_embedding":
            return "company_embedding"
        if stage in {"clustering", "cluster_naming"}:
            return "clustering_and_naming"
        if stage == "semantic_ordering":
            return "semantic_ordering"
        if stage in {"completed", "failed", "nothing_to_process"}:
            return "finalization"
        return stage

    def emit(stage: str, **detail):
        with progress_lock:
            now = time.monotonic()
            phase = timing_phase(stage, detail)
            if timing["phase"] is None:
                timing["phase"] = phase
                timing["phase_started"] = now
            elif phase != timing["phase"]:
                previous = timing["phase"]
                timing["durations"][previous] = timing["durations"].get(previous, 0.0) + now - timing["phase_started"]
                timing["phase"] = phase
                timing["phase_started"] = now
            durations = dict(timing["durations"])
            durations[phase] = durations.get(phase, 0.0) + now - timing["phase_started"]
            timing_snapshot = {
                "elapsed_seconds": round(now - timing["started"], 1),
                "current_phase": phase,
                "current_phase_elapsed_seconds": round(now - timing["phase_started"], 1),
                "phase_durations_seconds": {key: round(value, 1) for key, value in durations.items()},
            }
            last_progress.clear()
            last_progress.update({"stage": stage, "detail": detail, "timing": timing_snapshot})
            if args.events_file:
                write_progress_event(args.events_file, {
                    "stage": stage, "detail": detail, "timing": timing_snapshot,
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

        model = OllamaStages(
            args.generation_model, args.embedding_model, args.timeout, checkpoint, model_progress,
            ollama_url=args.ollama_url, keep_alive=args.keep_alive,
        )
        analyses = {}
        company_profile = None
        company_reference = None
        work_items: list[dict] = []
        extraction_entries: list[dict] = []
        document_failures: list[dict] = []
        document_skips: list[dict] = []
        if args.stage == "plan":
            for pdf_path, patent in pipeline.patents():
                checkpoint()
                documents.append({
                    "patent_id": patent_key(pdf_path.name),
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
                not pipeline.existing_result_path(pdf_path.name).exists() or args.overwrite
                for pdf_path, _ in patents
                if pdf_path.is_file()
            ) if args.stage == "execute" else len(patents)
            llm_total = sum(
                (args.overwrite or not pipeline.existing_result_path(pdf_path.name).exists())
                and (args.overwrite or not pipeline.analysis_checkpoint_current(pdf_path.name))
                for pdf_path, _ in patents if pdf_path.is_file()
            ) if args.stage == "execute" else 0
            llm_completed = 0
            llm_attempted = 0
            pending_index = 0
            existing_analyses = {}
            for document_index, (pdf_path, patent) in enumerate(patents, 1):
                checkpoint()
                key = patent_key(pdf_path.name)
                final_path = pipeline.existing_result_path(pdf_path.name)
                if args.stage == "execute" and final_path.exists() and not args.overwrite:
                    if pending_total:
                        if not args.durable_shards:
                            existing_analyses[key] = pipeline.load_analysis_artifacts(pdf_path.name)
                    documents.append({
                        "patent_id": key, "pdf": pdf_path.name,
                        "analysis_state": "finalized", "skip_reason": "already_processed",
                    })
                    emit("already_processed", patent_id=key, current=pending_index, total=pending_total)
                    continue
                if args.stage == "execute" and pdf_path.is_file():
                    pending_index += 1
                    if pipeline.analysis_checkpoint_current(pdf_path.name) and not args.overwrite:
                        if not args.durable_shards:
                            analyses[key] = pipeline.load_analysis_artifacts(pdf_path.name)
                        documents.append({
                            "patent_id": key, "pdf": pdf_path.name,
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
                    atomic_json(pipeline.artifact_dir(pdf_path.name) / "skip.json", skip)
                    documents.append({"patent_id": key, "pdf": pdf_path.name, "analysis_state": "skipped", **skip})
                    document_skips.append(skip)
                    emit("skipped", patent_id=key, reason=skip["reason"], current=progress_index, total=pending_total)
                    continue
                try:
                    prepared = pipeline.prepare_document(pdf_path, patent)
                except DocumentSkipped as exc:
                    skip = {
                        "schema_version": 1, "patent_id": key, "pdf": pdf_path.name,
                        "reason": exc.reason, "detail": exc.detail,
                        "skipped_at": datetime.now(timezone.utc).isoformat(), "run_id": run_id,
                    }
                    atomic_json(pipeline.artifact_dir(pdf_path.name) / "skip.json", skip)
                    documents.append({"patent_id": key, "pdf": pdf_path.name, "analysis_state": "skipped", **skip})
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
                    atomic_json(pipeline.artifact_dir(pdf_path.name) / "analysis_error.json", failure)
                    documents.append({
                        "patent_id": key, "pdf": pdf_path.name,
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
                    "pdf": pdf_path.name,
                    "artifact_dir": str(prepared["artifact_dir"].relative_to(ROOT)),
                    "source_decision": prepared["source_decision"],
                    "claims": len(prepared["structure"]["claims"]),
                    "analysis_state": "ready" if args.stage == "execute" else "prepared",
                }
                documents.append(document_record)
                extraction_entries.append(extraction_index_entry(ROOT, pdf_path.name, prepared))
                if args.stage == "execute":
                    work_items.append({
                        "key": key, "pdf_path": pdf_path,
                        "prepared": None if args.durable_shards else prepared,
                        "record": document_record, "index": progress_index,
                        "audit_dir": prepared["artifact_dir"] / "attempts" / run_id,
                    })

        if args.stage == "execute" and (work_items or analyses):
            emit("company_profile", current=0, total=pending_total)
            model.audit_dir = pipeline.research_dir / "clustering" / run_id / "company_profile"
            company_profile = model.generate_json("company_profile", {
                "company_technology": pipeline.research["company_technology"],
            })

        if args.stage == "execute" and work_items:
            llm_attempted = len(work_items)
            task_order = ("concept_level", "problem_summary", "similarity", "technology_summary")
            calls_per_cooling_period = max(1, min(args.shard_size, args.cooldown_every_documents * len(task_order)))
            shards = (
                [work_items]
                if not args.durable_shards
                else [work_items[offset:offset + args.shard_size] for offset in range(0, len(work_items), args.shard_size)]
            )
            for shard_index, shard_items in enumerate(shards, 1):
                checkpoint()
                if args.durable_shards:
                    for item in shard_items:
                        item["prepared"] = pipeline.load_prepared_document(item["pdf_path"].name)
                    emit(
                        "shard_started", shard=shard_index, shards=len(shards),
                        batch_size=len(shard_items), current=llm_completed, total=pending_total,
                    )
                active = {item["key"]: item for item in shard_items}
                shard_analyses: dict[str, dict] = {}

                def record_failure(item: dict, exc: Exception, task: str) -> None:
                    failure = {
                        "schema_version": 1, "pipeline_version": ANALYSIS_PIPELINE_VERSION,
                        "run_id": run_id, "patent_id": item["key"], "pdf": item["pdf_path"].name,
                        "error_type": type(exc).__name__, "error": str(exc),
                        "failed_stage": getattr(exc, "task", task),
                        "attempts": getattr(exc, "attempts", 1),
                        "audit_dir": str(item["audit_dir"].relative_to(ROOT)),
                        "completed_artifacts": sorted(
                            path.name for path in item["prepared"]["artifact_dir"].glob("*.json")
                            if path.name not in {"analysis_error.json", "analysis_complete.json"}
                        ),
                        "failed_at": datetime.now(timezone.utc).isoformat(), "retry_on_next_run": True,
                    }
                    atomic_json(item["prepared"]["artifact_dir"] / "analysis_error.json", failure)
                    item["record"].update({"analysis_state": "failed", "analysis_error": failure})
                    document_failures.append(failure)
                    active.pop(item["key"], None)
                    emit(
                        "document_failed", patent_id=item["key"], error=str(exc),
                        task=task, task_index=DOCUMENT_TASK_INDEX.get(task, 5), task_total=5,
                        completed=llm_completed + len(document_failures), processed=llm_completed,
                        succeeded=llm_completed, failed=len(document_failures), skipped=len(document_skips),
                        current=llm_completed + len(document_failures), total=pending_total,
                        percent=round((llm_completed + len(document_failures)) / max(1, pending_total) * 100, 1),
                    )

                # In legacy mode this loop still sees one shard containing every work item.
                for task in task_order:
                    pending = [
                        item for item in active.values()
                        if args.overwrite or not pipeline.analysis_task_current(item["prepared"], task)
                    ]
                    pending.sort(
                        key=lambda item: pipeline.analysis_task_characters(item["prepared"], task),
                        reverse=task == "technology_summary",
                    )
                    for offset in range(0, len(pending), calls_per_cooling_period):
                        checkpoint()
                        group = pending[offset:offset + calls_per_cooling_period]
                        progress_context.clear()
                        progress_context.update({
                            "current": llm_completed, "total": pending_total,
                            "processed": llm_completed, "succeeded": llm_completed,
                            "failed": len(document_failures), "skipped": len(document_skips),
                        })
                        emit(
                            "llm_batch", **progress_context, task=task,
                            task_index=DOCUMENT_TASK_INDEX[task], task_total=5,
                            batch_size=len(group), workers=args.generation_workers,
                            shard=shard_index, shards=len(shards),
                        )

                        def run_task(item: dict) -> dict:
                            return pipeline.analyze_task(
                                item["prepared"], task,
                                lambda name, data: model.generate_json(name, data, audit_dir=item["audit_dir"]),
                            )

                        with concurrent.futures.ThreadPoolExecutor(max_workers=args.generation_workers) as executor:
                            futures = {executor.submit(run_task, item): item for item in group if item["key"] in active}
                            for future in concurrent.futures.as_completed(futures):
                                item = futures[future]
                                try:
                                    future.result()
                                except PipelineCancelled:
                                    raise
                                except (DocumentAnalysisError, ValueError, RuntimeError, OSError) as exc:
                                    record_failure(item, exc, task)
                        if offset + len(group) < len(pending) and args.cooldown_seconds:
                            cooldown(f"{task}-batch", llm_completed, pending_total)

                model.unload_generation()
                embedding_items = list(active.values())
                documents_per_embedding_batch = max(1, args.embedding_batch_size // 2)
                for offset in range(0, len(embedding_items), documents_per_embedding_batch):
                    checkpoint()
                    group = [item for item in embedding_items[offset:offset + documents_per_embedding_batch] if item["key"] in active]
                    if not group:
                        continue
                    flat_inputs: list[str] = []
                    for item in group:
                        generated = pipeline.assemble_generated_analysis(item["prepared"])
                        flat_inputs.extend([generated["summaries"]["tech_summary"], generated["summaries"]["problem_summary"]])
                    emit(
                        "llm_batch", task="embeddings", task_index=5, task_total=5,
                        batch_size=len(group), current=llm_completed + offset, total=pending_total,
                        processed=llm_completed, succeeded=llm_completed,
                        failed=len(document_failures), skipped=len(document_skips),
                        shard=shard_index, shards=len(shards),
                    )
                    try:
                        vectors = model.embed(flat_inputs)
                        if len(vectors) != len(flat_inputs):
                            raise ValueError(f"expected {len(flat_inputs)} embeddings, got {len(vectors)}")
                        for index, item in enumerate(group):
                            shard_analyses[item["key"]] = pipeline.write_embedding(
                                item["prepared"], vectors[index * 2:index * 2 + 2],
                            )
                    except PipelineCancelled:
                        raise
                    except (DocumentAnalysisError, ValueError, RuntimeError, OSError):
                        for item in group:
                            try:
                                generated = pipeline.assemble_generated_analysis(item["prepared"])
                                inputs = [generated["summaries"]["tech_summary"], generated["summaries"]["problem_summary"]]
                                shard_analyses[item["key"]] = pipeline.write_embedding(item["prepared"], model.embed(inputs))
                            except (DocumentAnalysisError, ValueError, RuntimeError, OSError) as exc:
                                record_failure(item, exc, "embeddings")

                for item in shard_items:
                    if item["key"] not in shard_analyses:
                        continue
                    llm_completed += 1
                    error_path = item["prepared"]["artifact_dir"] / "analysis_error.json"
                    if error_path.exists():
                        error_path.unlink()
                    item["record"]["analysis_state"] = "analyzed"
                    atomic_json(item["prepared"]["artifact_dir"] / "analysis_complete.json", {
                        "schema_version": 1, "patent_id": item["key"],
                        "pdf_sha256": item["prepared"]["source_decision"]["pdf_sha256"],
                        "pipeline_version": ANALYSIS_PIPELINE_VERSION, "run_id": run_id,
                        "generation_model": args.generation_model, "embedding_model": args.embedding_model,
                        "audit_dir": str(item["audit_dir"].relative_to(ROOT)),
                        "runtime": {
                            "generation_workers": args.generation_workers,
                            "embedding_batch_size": args.embedding_batch_size,
                            "shard_size": args.shard_size, "durable_shards": args.durable_shards,
                            "keep_alive": args.keep_alive,
                        },
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                    })
                    emit(
                        "document_completed", patent_id=item["key"], completed=llm_completed,
                        processed=llm_completed, succeeded=llm_completed, failed=len(document_failures),
                        skipped=len(document_skips), current=llm_completed, total=pending_total,
                        llm_completed=llm_completed, llm_total=llm_total,
                        percent=round(llm_completed / max(1, pending_total) * 100, 1),
                    )
                if args.durable_shards:
                    model.unload_embedding()
                    for item in shard_items:
                        item["prepared"] = None
                    shard_analyses.clear()
                    active.clear()
                    emit(
                        "shard_completed", shard=shard_index, shards=len(shards),
                        completed=llm_completed, current=llm_completed, total=pending_total,
                        percent=round(llm_completed / max(1, pending_total) * 100, 1),
                    )
                else:
                    analyses.update(shard_analyses)

            if company_profile:
                emit("company_embedding", current=pending_total, total=pending_total, percent=100)
                company_inputs = [company_profile["technology_summary"], company_profile["problem_summary"]]
                company_reference = pipeline.build_company_reference(company_profile, model.embed(company_inputs))
            model.unload_embedding()
        if company_profile and company_reference is None:
            model.unload_generation()
            emit("company_embedding", current=pending_total, total=pending_total, percent=100)
            company_inputs = [company_profile["technology_summary"], company_profile["problem_summary"]]
            company_reference = pipeline.build_company_reference(company_profile, model.embed(company_inputs))
            model.unload_embedding()
        extraction_indexes = [write_extraction_index(ROOT, pipeline, extraction_entries)] if extraction_entries else []
        finalized = None
        if args.stage == "execute":
            should_finalize = bool(analyses or existing_analyses or (args.durable_shards and patents))
            if should_finalize:
                checkpoint()
                processed_count = llm_completed
                if args.durable_shards:
                    analyses = {}
                    failed_keys = {item["patent_id"] for item in document_failures}
                    emit(
                        "loading_embeddings", documents=len(patents), processed=processed_count,
                        current=pending_total, total=pending_total, percent=100,
                    )
                    for pdf_path, _ in patents:
                        key = patent_key(pdf_path.name)
                        if key in failed_keys:
                            continue
                        if (
                            pipeline.analysis_checkpoint_current(pdf_path.name)
                            or pipeline.existing_result_path(pdf_path.name).exists()
                        ):
                            analyses[key] = pipeline.load_analysis_artifacts(pdf_path.name)
                else:
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
                finalized = pipeline.finalize_research(
                    analyses, model.generate_json, run_id, args.cluster_count, company_reference,
                    lambda stage, **detail: emit(
                        stage, **progress_context, **detail,
                    ),
                )
                finalized["processed"] = processed_count
                finalized["failed"] = len(document_failures)
                pipeline.mark_analysis_current()
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
                "runtime": {
                    "ollama_url": args.ollama_url,
                    "generation_workers": args.generation_workers,
                    "embedding_batch_size": args.embedding_batch_size,
                    "shard_size": args.shard_size,
                    "durable_shards": args.durable_shards,
                    "cooldown_seconds": args.cooldown_seconds,
                    "cooldown_every_documents": args.cooldown_every_documents,
                    "keep_alive": args.keep_alive,
                },
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
        model.unload_embedding()
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
            model.unload_embedding()
        previous = dict(last_progress)
        failure_detail = dict(previous.get("detail", {}))
        failure_detail.update({"error": str(exc), "failed_at_stage": previous.get("stage", "")})
        emit("failed", **failure_detail)
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
