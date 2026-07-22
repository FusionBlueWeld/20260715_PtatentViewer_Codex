from __future__ import annotations

import hashlib
import csv
import json
import re
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ANALYSIS_REQUIRED = {"similarity", "concept_level", "tech_summary", "problem_summary", "reasoning", "tech_cluster", "problem_cluster"}
PATENT_LIST_HEADERS = (
    "No", "AIスコア", "出願番号", "出願日", "公開・公表番号", "公開・公表日", "登録番号", "登録日",
    "出願人・権利者名", "発明の名称", "ステイタス",
)
PATENT_LIST_NAME = re.compile(r"^patent_list_(\d{14})\.csv$")


class DataError(ValueError):
    pass


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DataError(f"JSONを読み込めません: {path}: {exc}") from exc


def read_company_technology(research_dir: Path, config: dict[str, Any]) -> str:
    prompt_path = research_dir / "company_tech.txt"
    if prompt_path.exists():
        try:
            value = prompt_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            raise DataError(f"自社技術プロンプトを読み込めません: {prompt_path}: {exc}") from exc
        if not value:
            raise DataError(f"自社技術プロンプトが空です: {prompt_path}")
        return value
    return str(config.get("company_technology", "")).strip()


def safe_id(value: str, label: str = "id") -> str:
    if not SAFE_ID.fullmatch(value or ""):
        raise DataError(f"不正な{label}です")
    return value


def patent_key(pdf_name: str) -> str:
    stem = normalize_publication_number(Path(pdf_name).stem)
    compact = stem.replace(" ", "")
    patterns = (
        (r"^WO(\d{4})-(\d+)$", lambda m: f"WO_{m[1]}_{m[2]}"),
        (r"^特開(\d{4})-(\d+)$", lambda m: f"JP_A_{m[1]}_{m[2]}"),
        (r"^特表(\d{4})-(\d+)$", lambda m: f"JP_AT_{m[1]}_{m[2]}"),
        (r"^特許第?(\d+)号$", lambda m: f"JP_B_{m[1]}"),
        (r"^特開平(\d+)-(\d+)$", lambda m: f"JP_A_H{int(m[1]):02d}_{m[2]}"),
        (r"^特表平(\d+)-(\d+)$", lambda m: f"JP_AT_H{int(m[1]):02d}_{m[2]}"),
    )
    for pattern, build in patterns:
        match = re.fullmatch(pattern, compact, re.IGNORECASE)
        if match:
            return build(match)
    legacy = re.sub(r"\s+", "_", stem)
    ascii_key = re.sub(r"[^A-Za-z0-9._-]+", "_", legacy).strip("._-")
    if not ascii_key or ascii_key != legacy:
        digest = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:12]
        ascii_key = f"{ascii_key or 'PATENT'}_{digest}"
    return ascii_key


def normalize_publication_number(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).strip()
    return re.sub(r"[‐‑‒–—―ー−]", "-", normalized)


def infer_year(publication_number: str) -> int | None:
    normalized = normalize_publication_number(publication_number)
    compact = normalized.replace(" ", "")
    heisei = re.match(r"^(?:特開|特表)平(\d{1,2})-", compact)
    if heisei:
        era_year = int(heisei.group(1))
        return 1988 + era_year if 1 <= era_year <= 31 else None
    if re.match(r"^特許第?\d+号$", compact) or compact.upper().startswith("JPB"):
        return None
    match = re.search(r"(?:19|20)\d{2}", compact)
    return int(match.group(0)) if match else None


def infer_status(publication_number: str) -> str:
    upper = normalize_publication_number(publication_number).replace(" ", "").upper()
    if upper.startswith("JPB") or upper.startswith("特許"):
        return "registered"
    if upper.startswith("JPA") or upper.startswith("WO") or upper.startswith("特開") or upper.startswith("特表"):
        return "published"
    return "unknown"


def read_research_config(research_dir: Path) -> dict[str, Any]:
    path = research_dir / "research.json"
    if path.exists():
        return read_json(path)
    return {"name": research_dir.name, "description": ""}


def latest_patent_list(research_dir: Path) -> tuple[Path | None, list[str]]:
    candidates: list[tuple[datetime, Path]] = []
    for path in research_dir.glob("patent_list_*.csv"):
        match = PATENT_LIST_NAME.fullmatch(path.name)
        if not match:
            continue
        try:
            timestamp = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
        except ValueError:
            continue
        candidates.append((timestamp, path))
    candidates.sort(key=lambda item: (item[0], item[1].name))
    if not candidates:
        return None, []
    return candidates[-1][1], [path.name for _, path in candidates[:-1]]


def _clean_csv_value(value: str | None) -> tuple[str, bool]:
    original = value or ""
    cleaned = "".join(char for char in original if char in "\t\r\n" or unicodedata.category(char) != "Cc")
    return cleaned, cleaned != original


def _number_match_key(value: str) -> str | None:
    compact = normalize_publication_number(value).replace(" ", "").replace("/", "-")
    patterns = (
        (r"^WO(\d{4})-(\d+)$", "WO"),
        (r"^特開(\d{4})-(\d+)$", "JP_A"),
        (r"^特表(\d{4})-(\d+)$", "JP_AT"),
        (r"^特開平(\d+)-(\d+)$", "JP_A_H"),
        (r"^特表平(\d+)-(\d+)$", "JP_AT_H"),
    )
    for pattern, prefix in patterns:
        match = re.fullmatch(pattern, compact, re.IGNORECASE)
        if match:
            return f"{prefix}:{int(match.group(1))}:{int(match.group(2))}"
    registration = re.fullmatch(r"^特許第?(\d+)号$", compact)
    if registration:
        return f"JP_B:{int(registration.group(1))}"
    return None


def load_patent_list(csv_path: Path, pool: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    pdf_index: dict[str, list[str]] = {}
    for pdf in sorted(pool.glob("*.pdf")):
        key = _number_match_key(pdf.stem)
        if key:
            pdf_index.setdefault(key, []).append(pdf.name)

    patents: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen_numbers: set[str] = set()
    try:
        handle = csv_path.open("r", encoding="cp932", newline="")
    except OSError as exc:
        raise DataError(f"CSVを読み込めません: {csv_path}: {exc}") from exc
    try:
        with handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != PATENT_LIST_HEADERS:
                raise DataError(f"CSVヘッダーが仕様と一致しません: {csv_path.name}")
            for line_number, source in enumerate(reader, 2):
                if None in source:
                    raise DataError(f"CSV {line_number}行目の列数がヘッダーと一致しません: {csv_path.name}")
                row: dict[str, str] = {}
                changed_fields: list[str] = []
                for header in PATENT_LIST_HEADERS:
                    row[header], changed = _clean_csv_value(source.get(header))
                    if changed:
                        changed_fields.append(header)
                row_number = row["No"]
                if row_number in seen_numbers:
                    warnings.append({"line": line_number, "code": "duplicate_no", "value": row_number})
                seen_numbers.add(row_number)
                if changed_fields:
                    warnings.append({"line": line_number, "code": "control_characters_removed", "fields": changed_fields})

                publication_values = [item.strip() for item in row["公開・公表番号"].split(",") if item.strip()]
                publication_dates = [item.strip().replace(".", "-") for item in row["公開・公表日"].split(",") if item.strip()]
                if len(publication_values) != len(publication_dates):
                    warnings.append({"line": line_number, "code": "publication_date_count_mismatch"})
                publications = [
                    {"number": number, "date": publication_dates[index] if index < len(publication_dates) else None,
                     "kind": "wo" if number.upper().startswith("WO") else "domestic"}
                    for index, number in enumerate(publication_values)
                ]
                ranked_numbers: list[tuple[int, str]] = []
                if row["登録番号"]:
                    ranked_numbers.append((0, row["登録番号"]))
                ranked_numbers.extend((1 if item["kind"] == "domestic" else 2, item["number"]) for item in publications)
                matches: list[dict[str, Any]] = []
                for priority, number in ranked_numbers:
                    key = _number_match_key(number)
                    for filename in pdf_index.get(key or "", []):
                        matches.append({"priority": priority, "number": number, "pdf": filename, "match_key": key})
                matches.sort(key=lambda item: (item["priority"], item["pdf"]))
                selected = matches[0] if matches else None
                display_number = selected["number"] if selected else (row["登録番号"] or (publication_values[0] if publication_values else row["出願番号"]))
                try:
                    ai_score = float(row["AIスコア"]) if row["AIスコア"] else None
                except ValueError:
                    ai_score = None
                    warnings.append({"line": line_number, "code": "invalid_ai_score", "value": row["AIスコア"]})
                patents.append({
                    "pdf": selected["pdf"] if selected else f"UNRESOLVED_{row_number or 'ROW'}_{line_number}.pdf",
                    "publication_number": display_number,
                    "publication_numbers": publications,
                    "application_number": row["出願番号"], "application_date": row["出願日"].replace(".", "-"),
                    "registration_number": row["登録番号"], "registration_date": row["登録日"].replace(".", "-"),
                    "applicant": row["出願人・権利者名"], "title": row["発明の名称"],
                    "source_status": row["ステイタス"], "source_ai_score": ai_score, "source_no": row_number,
                    "status": infer_status(display_number), "year": infer_year(display_number),
                    "pdf_match": {"selected": selected, "candidates": matches, "rule": "registration_then_domestic_then_wo"},
                })
    except UnicodeDecodeError as exc:
        raise DataError(f"CSVをCP932でデコードできません: {csv_path.name}: {exc}") from exc
    return patents, {"selected_csv": csv_path.name, "warnings": warnings, "rows": len(patents)}


def research_sources(research_dir: Path, pool: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    csv_path, excluded = latest_patent_list(research_dir)
    if csv_path:
        patents, audit = load_patent_list(csv_path, pool)
        audit["excluded_older_csvs"] = excluded
        return [{"id": "patent_list", "name": "特許リスト", "patents": patents}], audit
    sources = []
    for manifest_path in sorted((research_dir / "subresearches").glob("*/patents.json")):
        manifest = read_json(manifest_path)
        sources.append({
            "id": safe_id(manifest_path.parent.name, "subresearch id"),
            "name": manifest.get("name", manifest_path.parent.name),
            "description": manifest.get("description", ""),
            "patents": manifest.get("patents", []),
        })
    return sources, {"selected_csv": None, "excluded_older_csvs": [], "warnings": [], "rows": sum(len(item["patents"]) for item in sources)}


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
        for research_dir in sorted(path for path in base.iterdir() if path.is_dir()):
            if not ((research_dir / "research.json").exists() or (research_dir / "company_tech.txt").exists() or latest_patent_list(research_dir)[0]):
                continue
            config = read_research_config(research_dir)
            company_technology = read_company_technology(research_dir, config)
            research_id = safe_id(research_dir.name, "research id")
            sources, input_audit = research_sources(research_dir, self.paths(environment).pool)
            subs = [{
                "id": source["id"], "name": source["name"], "description": source.get("description", ""),
                "count": len(source["patents"]),
            } for source in sources]
            items.append({
                "id": research_id,
                "name": config.get("name", research_id),
                "description": config.get("description", ""),
                "company_technology": company_technology,
                "subresearches": subs,
                "input": input_audit,
            })
        return items

    def dashboard(self, environment: str, research_id: str) -> dict[str, Any]:
        paths = self.paths(environment)
        research_id = safe_id(research_id, "research id")
        research_dir = (paths.researches / research_id).resolve()
        if research_dir.parent != paths.researches.resolve() or not research_dir.is_dir():
            raise DataError("リサーチが見つかりません")
        config = read_research_config(research_dir)
        company_technology = read_company_technology(research_dir, config)
        patents: list[dict[str, Any]] = []
        seen: set[str] = set()
        sources, input_audit = research_sources(research_dir, paths.pool)
        for source in sources:
            sub_id = source["id"]
            result_dir = research_dir / "subresearches" / sub_id / "results"
            for raw in source["patents"]:
                pdf_name = str(raw.get("pdf", ""))
                if not pdf_name.lower().endswith(".pdf") or Path(pdf_name).name != pdf_name:
                    raise DataError(f"不正なPDF参照です: {pdf_name}")
                pdf_path = (paths.pool / pdf_name).resolve()
                if pdf_path.parent != paths.pool.resolve():
                    raise DataError(f"不正なPDF参照です: {pdf_name}")
                pdf_available = pdf_path.is_file()
                key = patent_key(pdf_name)
                if key in seen:
                    raise DataError(f"同一リサーチ内でPDFが重複しています: {pdf_name}")
                seen.add(key)
                analysis_path = result_dir / f"{key}.json"
                analysis = read_json(analysis_path) if analysis_path.exists() else {}
                skip_path = research_dir / "subresearches" / sub_id / "pipeline" / key / "skip.json"
                skip = read_json(skip_path) if skip_path.exists() else {}
                if not pdf_available:
                    skip = {"reason": "pdf_not_found", "detail": f"PDFがpatent_poolにありません: {pdf_name}"}
                publication_number = str(raw.get("publication_number") or Path(pdf_name).stem)
                record = {
                    "id": key,
                    "pdf": pdf_name,
                    "publication_number": publication_number,
                    "title": raw.get("title") or publication_number,
                    "applicant": raw.get("applicant", "未設定"),
                    "category": raw.get("category", source.get("name", sub_id)),
                    "subresearch_id": sub_id,
                    "subresearch_name": source.get("name", sub_id),
                    "year": raw.get("year") or infer_year(publication_number),
                    "status": raw.get("status") or infer_status(publication_number),
                    "tags": raw.get("tags", []),
                    "pdf_available": pdf_available,
                    "analysis_state": "skipped" if skip else ("ready" if ANALYSIS_REQUIRED.issubset(analysis) else ("invalid" if analysis else "pending")),
                    "skip_reason": skip.get("reason"),
                    "skip_detail": skip.get("detail"),
                    "source_status": raw.get("source_status"),
                    "source_ai_score": raw.get("source_ai_score"),
                    "source_no": raw.get("source_no"),
                    "pdf_match": raw.get("pdf_match"),
                }
                record.update(analysis)
                patents.append(record)
        technology_map: dict[str, Any] = {}
        clustering_root = research_dir / "clustering"
        if clustering_root.is_dir():
            for run_dir in sorted((item for item in clustering_root.iterdir() if item.is_dir()), reverse=True):
                clusters_path = run_dir / "clusters.json"
                if not clusters_path.is_file():
                    continue
                clusters = read_json(clusters_path)
                ordering = clusters.get("semantic_ordering", {})
                proximity = clusters.get("company_proximity", {})
                technology_names = clusters.get("technology_cluster_names", {})
                problem_names = clusters.get("problem_cluster_names", {})

                def cluster_metadata(names: dict, values: dict) -> dict[str, Any]:
                    return {
                        str(cluster_id): {
                            "name": name,
                            **values.get(str(cluster_id), values.get(cluster_id, {})),
                        }
                        for cluster_id, name in names.items()
                    }

                technology_map = {
                    "run_id": run_dir.name,
                    "algorithm": ordering.get("algorithm"),
                    "proximity_scale": proximity.get("scale"),
                    "technology_order": [str(item) for item in ordering.get("technology_order", [])],
                    "problem_order": [str(item) for item in ordering.get("problem_order", [])],
                    "technology_clusters": cluster_metadata(technology_names, proximity.get("technology", {})),
                    "problem_clusters": cluster_metadata(problem_names, proximity.get("problem", {})),
                }
                break
        return {
            "environment": environment,
            "research": {
                "id": research_id,
                "name": config.get("name", research_id),
                "description": config.get("description", ""),
                "company_technology": company_technology,
            },
            "pool_count": len(list(paths.pool.glob("*.pdf"))),
            "input": input_audit,
            "technology_map": technology_map,
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

    def preflight(self, environment: str, research_id: str, ollama_url: str = "http://127.0.0.1:11434") -> dict[str, Any]:
        dashboard = self.dashboard(environment, research_id)
        paths = self.paths(environment)
        research_dir = paths.researches / safe_id(research_id, "research id")
        required_prompt_names = {
            "similarity.txt", "concept_level.txt", "problem_summary.txt",
            "technology_summary.txt", "cluster_name.txt", "company_profile.txt",
        }
        stage_prompt_dir = self.root / "src/patent_viewer/prompts/stages"
        installed_prompt_names = {
            path.name for path in stage_prompt_dir.glob("*.txt")
            if path.read_text(encoding="utf-8").strip()
        }
        required_schema_names = {
            "llm-similarity.schema.json", "llm-concept-level.schema.json",
            "llm-problem-summary.schema.json", "llm-technology-summary.schema.json",
            "llm-cluster-name.schema.json", "llm-company-profile.schema.json",
        }
        schema_dir = self.root / "schemas"
        installed_schema_names: set[str] = set()
        for path in schema_dir.glob("llm-*.schema.json"):
            try:
                schema = read_json(path)
            except DataError:
                continue
            if isinstance(schema, dict) and schema.get("type") == "object" and schema.get("required"):
                installed_schema_names.add(path.name)
        prompt_contract_ok = required_prompt_names.issubset(installed_prompt_names) and required_schema_names.issubset(installed_schema_names)
        ollama_ok = False
        installed_models: set[str] = set()
        ollama_detail = "Ollamaへ接続できません"
        try:
            with urllib.request.urlopen(ollama_url.rstrip("/") + "/api/tags", timeout=2) as response:
                tags = json.loads(response.read().decode("utf-8"))
            installed_models = {str(model.get("name", "")) for model in tags.get("models", [])}
            ollama_ok = True
            ollama_detail = f"接続済み・{len(installed_models)}モデル（推論は未実行）"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            pass
        required_models = {"gemma4:e4b", "qwen3-embedding:8b"}
        models_ok = required_models.issubset(installed_models)
        missing_models = sorted(required_models - installed_models)
        structure_total = 0
        structure_valid = 0
        structure_missing = 0
        structure_invalid = 0
        structure_skipped = 0
        for patent in dashboard["patents"]:
            if not patent.get("pdf_available"):
                continue
            artifact_dir = research_dir / "subresearches" / patent["subresearch_id"] / "pipeline" / patent["id"]
            skip_path = artifact_dir / "skip.json"
            if skip_path.exists():
                structure_skipped += 1
                continue
            structure_total += 1
            structure_path = artifact_dir / "document_structure.json"
            if not structure_path.exists():
                structure_missing += 1
                continue
            structure = read_json(structure_path)
            validation = structure.get("claim_validation", {})
            if structure.get("schema_version", 0) >= 2 and validation.get("valid") is True and structure.get("claims"):
                structure_valid += 1
            else:
                structure_invalid += 1
        structure_ok = structure_total > 0 and structure_valid == structure_total
        checks = [
            {"id": "pdf-selection", "ok": bool(dashboard["patents"]), "detail": f"{len(dashboard['patents'])}件"},
            {"id": "prompts", "ok": prompt_contract_ok, "detail": f"小型プロンプト {len(required_prompt_names & installed_prompt_names)}/{len(required_prompt_names)}・Schema {len(required_schema_names & installed_schema_names)}/{len(required_schema_names)}"},
            {"id": "output-isolation", "ok": self.paths(environment).runtime != self.paths("debug" if environment == "normal" else "normal").runtime, "detail": str(self.paths(environment).runtime)},
            {"id": "ollama", "ok": ollama_ok, "detail": ollama_detail},
            {"id": "models", "ok": models_ok, "detail": "必要モデル導入済み" if models_ok else f"不足: {', '.join(missing_models)}"},
            {"id": "claim-structure", "ok": structure_ok, "detail": f"有効 {structure_valid}/{structure_total}件・未準備 {structure_missing}件・異常 {structure_invalid}件・スキップ {structure_skipped}件"},
        ]
        fingerprint = hashlib.sha256("\n".join(p["pdf"] for p in dashboard["patents"]).encode()).hexdigest()[:12]
        return {"ready": all(c["ok"] for c in checks), "checks": checks, "selection_fingerprint": fingerprint}
