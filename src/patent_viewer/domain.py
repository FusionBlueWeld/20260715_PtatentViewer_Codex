from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
YEAR = re.compile(r"\b(19|20)\d{2}")
ANALYSIS_REQUIRED = {"similarity", "concept_level", "tech_summary", "problem_summary", "reasoning", "tech_cluster", "problem_cluster"}


class DataError(ValueError):
    pass


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataError(f"JSONを読み込めません: {path}: {exc}") from exc


def safe_id(value: str, label: str = "id") -> str:
    if not SAFE_ID.fullmatch(value or ""):
        raise DataError(f"不正な{label}です")
    return value


def patent_key(pdf_name: str) -> str:
    return Path(pdf_name).stem.replace(" ", "_")


def infer_year(publication_number: str) -> int | None:
    match = YEAR.search(publication_number)
    return int(match.group(0)) if match else None


def infer_status(publication_number: str) -> str:
    upper = publication_number.upper()
    if upper.startswith("JPB"):
        return "registered"
    if upper.startswith("JPA"):
        return "published"
    return "unknown"


@dataclass(frozen=True)
class DataPaths:
    root: Path
    environment: str

    @property
    def researches(self) -> Path:
        return self.root / ("researches" if self.environment == "normal" else "debug_data/researches")

    @property
    def runtime(self) -> Path:
        return self.root / "runtime" / self.environment

    @property
    def pool(self) -> Path:
        return self.root / "patent_pool"


class Repository:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def paths(self, environment: str) -> DataPaths:
        if environment not in {"normal", "debug"}:
            raise DataError("environmentはnormalまたはdebugです")
        return DataPaths(self.root, environment)

    def list_researches(self, environment: str) -> list[dict[str, Any]]:
        base = self.paths(environment).researches
        items: list[dict[str, Any]] = []
        if not base.exists():
            return items
        for config_path in sorted(base.glob("*/research.json")):
            config = read_json(config_path)
            research_id = safe_id(config_path.parent.name, "research id")
            subs = []
            sub_root = config_path.parent / "subresearches"
            for manifest in sorted(sub_root.glob("*/patents.json")):
                data = read_json(manifest)
                subs.append({
                    "id": safe_id(manifest.parent.name, "subresearch id"),
                    "name": data.get("name", manifest.parent.name),
                    "description": data.get("description", ""),
                    "count": len(data.get("patents", [])),
                })
            items.append({
                "id": research_id,
                "name": config.get("name", research_id),
                "description": config.get("description", ""),
                "company_technology": config.get("company_technology", ""),
                "subresearches": subs,
            })
        return items

    def dashboard(self, environment: str, research_id: str) -> dict[str, Any]:
        paths = self.paths(environment)
        research_id = safe_id(research_id, "research id")
        research_dir = (paths.researches / research_id).resolve()
        if research_dir.parent != paths.researches.resolve() or not research_dir.is_dir():
            raise DataError("リサーチが見つかりません")
        config = read_json(research_dir / "research.json")
        patents: list[dict[str, Any]] = []
        seen: set[str] = set()
        for manifest_path in sorted((research_dir / "subresearches").glob("*/patents.json")):
            manifest = read_json(manifest_path)
            sub_id = safe_id(manifest_path.parent.name, "subresearch id")
            result_dir = manifest_path.parent / "results"
            for raw in manifest.get("patents", []):
                pdf_name = str(raw.get("pdf", ""))
                if not pdf_name.lower().endswith(".pdf") or Path(pdf_name).name != pdf_name:
                    raise DataError(f"不正なPDF参照です: {pdf_name}")
                pdf_path = (paths.pool / pdf_name).resolve()
                if pdf_path.parent != paths.pool.resolve() or not pdf_path.is_file():
                    raise DataError(f"PDFがpatent_poolにありません: {pdf_name}")
                key = patent_key(pdf_name)
                if key in seen:
                    raise DataError(f"同一リサーチ内でPDFが重複しています: {pdf_name}")
                seen.add(key)
                analysis_path = result_dir / f"{key}.json"
                analysis = read_json(analysis_path) if analysis_path.exists() else {}
                publication_number = str(raw.get("publication_number") or Path(pdf_name).stem)
                record = {
                    "id": key,
                    "pdf": pdf_name,
                    "publication_number": publication_number,
                    "title": raw.get("title") or publication_number,
                    "applicant": raw.get("applicant", "未設定"),
                    "category": raw.get("category", manifest.get("name", sub_id)),
                    "subresearch_id": sub_id,
                    "subresearch_name": manifest.get("name", sub_id),
                    "year": raw.get("year") or infer_year(publication_number),
                    "status": raw.get("status") or infer_status(publication_number),
                    "tags": raw.get("tags", []),
                    "analysis_state": "ready" if ANALYSIS_REQUIRED.issubset(analysis) else ("invalid" if analysis else "pending"),
                }
                record.update(analysis)
                patents.append(record)
        return {
            "environment": environment,
            "research": {
                "id": research_id,
                "name": config.get("name", research_id),
                "description": config.get("description", ""),
                "company_technology": config.get("company_technology", ""),
            },
            "pool_count": len(list(paths.pool.glob("*.pdf"))),
            "patents": patents,
        }

    def pdf_path(self, pdf_name: str) -> Path:
        if Path(pdf_name).name != pdf_name or not pdf_name.lower().endswith(".pdf"):
            raise DataError("不正なPDF名です")
        pool = (self.root / "patent_pool").resolve()
        target = (pool / pdf_name).resolve()
        if target.parent != pool or not target.is_file():
            raise DataError("PDFが見つかりません")
        return target

    def save_interpretation(self, environment: str, payload: dict[str, Any]) -> Path:
        paths = self.paths(environment)
        research_id = safe_id(str(payload.get("research_id", "")), "research id")
        patent_id = safe_id(str(payload.get("patent_id", "")), "patent id")
        note = str(payload.get("note", "")).strip()
        if not note or len(note) > 20_000:
            raise DataError("解釈メモは1〜20000文字で指定してください")
        target_dir = paths.runtime / "interpretations" / research_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{patent_id}.json"
        data = {
            "research_id": research_id,
            "patent_id": patent_id,
            "note": note,
            "source": payload.get("source", "human"),
        }
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return target

    def preflight(self, environment: str, research_id: str) -> dict[str, Any]:
        dashboard = self.dashboard(environment, research_id)
        prompts = sorted((self.root / "src/patent_viewer/prompts").glob("*.txt"))
        ollama_ok = False
        installed_models: set[str] = set()
        ollama_detail = "Ollamaへ接続できません"
        try:
            with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as response:
                tags = json.loads(response.read().decode("utf-8"))
            installed_models = {str(model.get("name", "")) for model in tags.get("models", [])}
            ollama_ok = True
            ollama_detail = f"接続済み・{len(installed_models)}モデル（推論は未実行）"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            pass
        required_models = {"gemma4:e4b", "qwen3-embedding:8b"}
        models_ok = required_models.issubset(installed_models)
        missing_models = sorted(required_models - installed_models)
        checks = [
            {"id": "pdf-selection", "ok": bool(dashboard["patents"]), "detail": f"{len(dashboard['patents'])}件"},
            {"id": "prompts", "ok": len(prompts) >= 6, "detail": f"{len(prompts)}ファイル"},
            {"id": "output-isolation", "ok": self.paths(environment).runtime != self.paths("debug" if environment == "normal" else "normal").runtime, "detail": str(self.paths(environment).runtime)},
            {"id": "ollama", "ok": ollama_ok, "detail": ollama_detail},
            {"id": "models", "ok": models_ok, "detail": "必要モデル導入済み" if models_ok else f"不足: {', '.join(missing_models)}"},
        ]
        fingerprint = hashlib.sha256("\n".join(p["pdf"] for p in dashboard["patents"]).encode()).hexdigest()[:12]
        return {"ready": all(c["ok"] for c in checks), "checks": checks, "selection_fingerprint": fingerprint}
