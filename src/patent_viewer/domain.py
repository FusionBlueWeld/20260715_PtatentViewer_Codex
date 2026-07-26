from __future__ import annotations

import base64
import hashlib
import csv
import json
import re
import shutil
import tempfile
import unicodedata
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .storage import SQLiteStore


SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ANALYSIS_REQUIRED = {"similarity", "concept_level", "tech_summary", "problem_summary", "reasoning", "tech_cluster", "problem_cluster"}
PATENT_LIST_HEADERS = (
    "No", "AIスコア", "出願番号", "出願日", "公開・公表番号", "公開・公表日", "登録番号", "登録日",
    "出願人・権利者名", "発明の名称", "ステイタス",
)
PATENT_LIST_NAME = re.compile(r"^patent_list_(\d{14})\.csv$")
ORGANIZATION_ID = re.compile(r"^org_[0-9a-f]{16}$")
GROUP_ID = re.compile(r"^group_[0-9a-f]{16}$")
DEFAULT_LEGAL_STATUS_RULES = {
    "version": 1,
    "rights_acquired": ["登録（権利有）"],
    "under_examination": ["通常審査中"],
}


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


def applicant_organizations(value: Any) -> list[dict[str, str]]:
    """Keep source wording, splitting only explicit multi-value separators."""
    raw = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not raw or raw in {"未設定", "未調査"}:
        return []
    names: list[str] = []
    for item in re.split(r"[\r\n;；]+", raw):
        name = re.sub(r"\s+", " ", item).strip()
        if name and name not in names:
            names.append(name)
    return [
        {"id": f"org_{hashlib.sha256(name.casefold().encode('utf-8')).hexdigest()[:16]}", "name": name}
        for name in names
    ]


def write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


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


def application_year(value: Any) -> int | None:
    raw = str(value or "").strip()
    match = re.fullmatch(r"((?:19|20)\d{2})[./-]\d{1,2}[./-]\d{1,2}", raw)
    return int(match.group(1)) if match else None


def legal_status_rules(config: dict[str, Any]) -> dict[str, Any]:
    raw = config.get("legal_status_rules", {})
    rules: dict[str, Any] = {"version": 1}
    for key in ("rights_acquired", "under_examination"):
        source = raw.get(key, DEFAULT_LEGAL_STATUS_RULES[key])
        if not isinstance(source, list):
            source = DEFAULT_LEGAL_STATUS_RULES[key]
        rules[key] = list(dict.fromkeys(str(item).strip() for item in source if str(item).strip()))
    return rules


def classify_legal_status(source_status: Any, rules: dict[str, Any]) -> str:
    value = str(source_status or "").strip()
    if value in rules.get("rights_acquired", []):
        return "rights_acquired"
    if value in rules.get("under_examination", []):
        return "under_examination"
    return "published"


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
                    "status": infer_status(display_number), "year": application_year(row["出願日"]),
                    "year_source": "application_date",
                    "pdf_match": {"selected": selected, "candidates": matches, "rule": "registration_then_domestic_then_wo"},
                })
    except UnicodeDecodeError as exc:
        raise DataError(f"CSVをCP932でデコードできません: {csv_path.name}: {exc}") from exc
    return patents, {"selected_csv": csv_path.name, "warnings": warnings, "rows": len(patents)}


def research_patents(research_dir: Path, pool: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Load the document set owned by one research.

    CSV is the production source, a research-level patents.json is the JSON
    source used by fixtures and hand-authored sets, and subresearch manifests
    are a read-only compatibility fallback.
    """
    csv_path, excluded = latest_patent_list(research_dir)
    if csv_path:
        patents, audit = load_patent_list(csv_path, pool)
        for patent in patents:
            patent["_legacy_subresearch_id"] = "patent_list"
        audit["excluded_older_csvs"] = excluded
        audit.update({"selected_manifest": None, "legacy_inputs": []})
        return patents, audit
    manifest_path = research_dir / "patents.json"
    if manifest_path.is_file():
        manifest = read_json(manifest_path)
        patents = manifest.get("patents", [])
        if not isinstance(patents, list):
            raise DataError(f"patentsが配列ではありません: {manifest_path}")
        return patents, {
            "selected_csv": None, "selected_manifest": manifest_path.name,
            "excluded_older_csvs": [], "legacy_inputs": [],
            "warnings": [], "rows": len(patents),
        }
    patents: list[dict[str, Any]] = []
    legacy_subresearches: list[str] = []
    for manifest_path in sorted((research_dir / "subresearches").glob("*/patents.json")):
        manifest = read_json(manifest_path)
        legacy_id = safe_id(manifest_path.parent.name, "legacy group id")
        legacy_name = str(manifest.get("name", legacy_id))
        legacy_subresearches.append(legacy_id)
        for item in manifest.get("patents", []):
            patent = dict(item)
            patent.setdefault("category", legacy_name)
            patent["_legacy_subresearch_id"] = legacy_id
            patents.append(patent)
    return patents, {
        "selected_csv": None, "selected_manifest": None,
        "excluded_older_csvs": [], "legacy_inputs": legacy_subresearches,
        "warnings": ([{"code": "legacy_subresearch_input"}] if legacy_subresearches else []),
        "rows": len(patents),
    }


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
        self._stores: dict[str, SQLiteStore] = {}

    def store(self, environment: str) -> SQLiteStore:
        self.paths(environment)
        if environment not in self._stores:
            self._stores[environment] = SQLiteStore(self.root, environment)
        return self._stores[environment]

    def paths(self, environment: str) -> DataPaths:
        if environment not in {"normal", "debug"}:
            raise DataError("environmentはnormalまたはdebugです")
        return DataPaths(self.root, environment)

    @staticmethod
    def _lifecycle(config: dict[str, Any]) -> dict[str, Any]:
        raw = config.get("lifecycle", {})
        return {
            "status": str(raw.get("status", "active")),
            "created_at": raw.get("created_at"),
            "updated_at": raw.get("updated_at"),
            "archived_at": raw.get("archived_at"),
            "archive_reason": raw.get("archive_reason"),
            "analysis_stale": bool(raw.get("analysis_stale", False)),
        }

    @staticmethod
    def _csv_history(research_dir: Path) -> list[dict[str, Any]]:
        active, _ = latest_patent_list(research_dir)
        items = []
        for path in research_dir.glob("patent_list_*.csv"):
            match = PATENT_LIST_NAME.fullmatch(path.name)
            if not match:
                continue
            try:
                timestamp = datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
            except ValueError:
                continue
            items.append({
                "filename": path.name,
                "timestamp": timestamp.isoformat(timespec="seconds"),
                "active": active == path,
                "bytes": path.stat().st_size,
            })
        return sorted(items, key=lambda item: item["timestamp"], reverse=True)

    def _decode_csv_upload(self, environment: str, filename: str, encoded: str) -> tuple[bytes, list[dict[str, Any]], dict[str, Any]]:
        match = PATENT_LIST_NAME.fullmatch(filename)
        if not match:
            raise DataError("CSV名は patent_list_yyyymmddHHMMSS.csv 形式で指定してください")
        try:
            datetime.strptime(match.group(1), "%Y%m%d%H%M%S")
        except ValueError as exc:
            raise DataError("CSVファイル名の日時が不正です") from exc
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise DataError("CSVアップロードデータが不正です") from exc
        if not content or len(content) > 5_000_000:
            raise DataError("CSVは1バイト以上5MB以下にしてください")
        paths = self.paths(environment)
        paths.runtime.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="csv-validation-", dir=paths.runtime) as temp:
            path = Path(temp) / filename
            path.write_bytes(content)
            patents, audit = load_patent_list(path, paths.pool)
        audit["matched_pdfs"] = sum(bool(item.get("pdf_match", {}).get("selected")) for item in patents)
        audit["missing_pdfs"] = len(patents) - audit["matched_pdfs"]
        audit["multiple_candidates"] = sum(len(item.get("pdf_match", {}).get("candidates", [])) > 1 for item in patents)
        return content, patents, audit

    def validate_research_csv(self, environment: str, filename: str, encoded: str) -> dict[str, Any]:
        _, patents, audit = self._decode_csv_upload(environment, filename, encoded)
        return {"ok": True, "filename": filename, "rows": len(patents), **audit}

    def create_research(self, environment: str, payload: dict[str, Any]) -> dict[str, Any]:
        if environment != "normal":
            raise DataError("UIからのリサーチ作成はNORMAL環境だけで使用できます")
        research_id = safe_id(str(payload.get("id", "")).strip(), "research id")
        name = str(payload.get("name", "")).strip()
        description = str(payload.get("description", "")).strip()
        company_technology = str(payload.get("company_technology", "")).strip()
        if not name or len(name) > 120:
            raise DataError("リサーチ名は1〜120文字で指定してください")
        if len(description) > 2_000:
            raise DataError("説明は2000文字以内で指定してください")
        if not company_technology or len(company_technology) > 20_000:
            raise DataError("自社技術は1〜20000文字で指定してください")
        filename = str(payload.get("csv_filename", ""))
        content, patents, audit = self._decode_csv_upload(environment, filename, str(payload.get("csv_base64", "")))
        if not patents:
            raise DataError("CSVに文献がありません")
        paths = self.paths(environment)
        paths.researches.mkdir(parents=True, exist_ok=True)
        target = (paths.researches / research_id).resolve()
        if target.parent != paths.researches.resolve() or target.exists():
            raise DataError("同じIDのリサーチが既に存在します")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        draft_root = paths.runtime / "research_drafts"
        draft_root.mkdir(parents=True, exist_ok=True)
        draft = draft_root / uuid.uuid4().hex
        try:
            draft.mkdir()
            config = {
                "schema_version": 2,
                "name": name,
                "description": description,
                "legal_status_rules": DEFAULT_LEGAL_STATUS_RULES,
                "lifecycle": {
                    "status": "active", "created_at": now, "updated_at": now,
                    "archived_at": None, "archive_reason": None, "analysis_stale": False,
                },
                "pipeline": {
                    "source_policy": {"extraction": "verify_original", "reading": "adaptive"},
                    "threat_scoring": {
                        "map": "5x5", "x_axis": "similarity", "y_axis": "concept_level",
                        "primary_evidence": "independent_claims", "embedding_is_scoring_evidence": False,
                    },
                },
            }
            write_json_atomic(draft / "research.json", config)
            (draft / "company_tech.txt").write_text(company_technology, encoding="utf-8")
            (draft / filename).write_bytes(content)
            draft.rename(target)
        finally:
            if draft.exists():
                shutil.rmtree(draft)
        self.sync_research(environment, research_id, force=True)
        return {
            "ok": True, "research_id": research_id, "active_csv": filename,
            "document_count": len(patents), "validation": audit,
        }

    def add_research_csv(self, environment: str, research_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        paths = self.paths(environment)
        research_id = safe_id(research_id, "research id")
        research_dir = (paths.researches / research_id).resolve()
        if research_dir.parent != paths.researches.resolve() or not research_dir.is_dir():
            raise DataError("リサーチが見つかりません")
        config = read_research_config(research_dir)
        if self._lifecycle(config)["status"] == "archived":
            raise DataError("アーカイブ済みリサーチは更新できません")
        filename = str(payload.get("csv_filename", ""))
        content, new_patents, audit = self._decode_csv_upload(environment, filename, str(payload.get("csv_base64", "")))
        old_path, _ = latest_patent_list(research_dir)
        old_patents = load_patent_list(old_path, paths.pool)[0] if old_path else []
        target = research_dir / filename
        if target.exists():
            if target.read_bytes() != content:
                raise DataError("同名で内容が異なるCSVは上書きできません。新しいタイムスタンプで作成してください")
            return {
                "ok": True, "research_id": research_id, "active_csv": old_path.name if old_path else filename,
                "active_changed": False, "already_uploaded": True, "validation": audit,
                "history": self._csv_history(research_dir),
            }
        temporary = research_dir / f".{filename}.{uuid.uuid4().hex}.tmp"
        temporary.write_bytes(content)
        temporary.replace(target)
        new_active, _ = latest_patent_list(research_dir)
        active_changed = new_active == target
        old_ids = {str(item.get("publication_number") or item.get("pdf")) for item in old_patents}
        new_ids = {str(item.get("publication_number") or item.get("pdf")) for item in new_patents}
        if active_changed:
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            lifecycle = dict(config.get("lifecycle", {}))
            lifecycle.update({"status": "active", "updated_at": now, "analysis_stale": True})
            config["schema_version"] = max(2, int(config.get("schema_version", 0)))
            config["lifecycle"] = lifecycle
            write_json_atomic(research_dir / "research.json", config)
            self.sync_research(environment, research_id, force=True)
        return {
            "ok": True, "research_id": research_id,
            "active_csv": new_active.name if new_active else None,
            "active_changed": active_changed, "already_uploaded": False,
            "diff": {
                "added": len(new_ids - old_ids), "continued": len(new_ids & old_ids),
                "removed": len(old_ids - new_ids),
            } if active_changed else {"added": 0, "continued": len(old_ids), "removed": 0},
            "validation": audit, "history": self._csv_history(research_dir),
        }

    def set_research_archived(self, environment: str, research_id: str, archived: bool, reason: str = "") -> dict[str, Any]:
        paths = self.paths(environment)
        research_id = safe_id(research_id, "research id")
        research_dir = (paths.researches / research_id).resolve()
        if research_dir.parent != paths.researches.resolve() or not research_dir.is_dir():
            raise DataError("リサーチが見つかりません")
        config = read_research_config(research_dir)
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        lifecycle = dict(config.get("lifecycle", {}))
        lifecycle.update({
            "status": "archived" if archived else "active",
            "updated_at": now,
            "archived_at": now if archived else None,
            "archive_reason": reason.strip()[:1_000] if archived else None,
        })
        lifecycle.setdefault("created_at", now)
        lifecycle.setdefault("analysis_stale", False)
        config["schema_version"] = max(2, int(config.get("schema_version", 0)))
        config["lifecycle"] = lifecycle
        write_json_atomic(research_dir / "research.json", config)
        self.sync_research(environment, research_id, force=True)
        return {"ok": True, "research_id": research_id, "lifecycle": self._lifecycle(config)}

    def save_legal_status_rules(self, environment: str, research_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        paths = self.paths(environment)
        research_id = safe_id(research_id, "research id")
        research_dir = (paths.researches / research_id).resolve()
        if research_dir.parent != paths.researches.resolve() or not research_dir.is_dir():
            raise DataError("リサーチが見つかりません")

        rules: dict[str, Any] = {"version": 1}
        for key, label in (("rights_acquired", "権利化"), ("under_examination", "審査中")):
            raw = payload.get(key, [])
            if not isinstance(raw, list):
                raise DataError(f"{label}の判定テキストは配列で指定してください")
            values = list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))
            if len(values) > 100 or any(len(item) > 200 for item in values):
                raise DataError(f"{label}の判定テキストは100件以内・1件200文字以内で指定してください")
            rules[key] = values
        duplicate = set(rules["rights_acquired"]) & set(rules["under_examination"])
        if duplicate:
            raise DataError(f"同じ判定テキストを権利化と審査中の両方には登録できません: {sorted(duplicate)[0]}")

        config = read_research_config(research_dir)
        config["schema_version"] = max(2, int(config.get("schema_version", 0)))
        config["legal_status_rules"] = rules
        lifecycle = dict(config.get("lifecycle", {}))
        lifecycle["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        config["lifecycle"] = lifecycle
        write_json_atomic(research_dir / "research.json", config)
        self.sync_research(environment, research_id, force=True)
        return {"ok": True, "research_id": research_id, "legal_status_rules": rules}

    def _organization_registry_path(self, environment: str) -> Path:
        self.paths(environment)
        return self.root / ("config/organization_registry.json" if environment == "normal" else "debug_data/config/organization_registry.json")

    def _research_organization_path(self, environment: str, research_id: str) -> Path:
        paths = self.paths(environment)
        research_id = safe_id(research_id, "research id")
        research_dir = (paths.researches / research_id).resolve()
        if research_dir.parent != paths.researches.resolve() or not research_dir.is_dir():
            raise DataError("リサーチが見つかりません")
        return research_dir / "organization_overrides.json"

    @staticmethod
    def _read_groups(path: Path, scope: str) -> tuple[list[dict[str, Any]], str | None]:
        if not path.is_file():
            return [], None
        payload = read_json(path)
        if not isinstance(payload, dict) or not isinstance(payload.get("groups", []), list):
            raise DataError(f"企業グループ設定が不正です: {path}")
        groups = []
        for raw in payload.get("groups", []):
            if not isinstance(raw, dict):
                continue
            group = dict(raw)
            group["scope"] = scope
            groups.append(group)
        return groups, str(payload.get("updated_at") or "") or None

    def organization_registry(self, environment: str, research_id: str, patents: list[dict[str, Any]]) -> dict[str, Any]:
        shared, shared_version = self._read_groups(self._organization_registry_path(environment), "common")
        local, local_version = self._read_groups(self._research_organization_path(environment, research_id), "research")
        organizations: dict[str, str] = {}
        for patent in patents:
            entities = applicant_organizations(patent.get("applicant"))
            patent["applicant_organizations"] = entities
            patent["applicant_organization_ids"] = [item["id"] for item in entities]
            organizations.update({item["id"]: item["name"] for item in entities})
        for group in shared + local:
            for member in group.get("members", []):
                if isinstance(member, dict) and ORGANIZATION_ID.fullmatch(str(member.get("id", ""))):
                    organizations.setdefault(str(member["id"]), str(member.get("name") or member["id"]))
        version_material = f"{shared_version or 'none'}|{local_version or 'none'}"
        return {
            "version": hashlib.sha256(version_material.encode("utf-8")).hexdigest()[:12],
            "organizations": [{"id": key, "name": value} for key, value in sorted(organizations.items(), key=lambda item: item[1].casefold())],
            "groups": shared + local,
        }

    def save_organization_group(self, environment: str, research_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        scope = str(payload.get("scope", "common"))
        if scope not in {"common", "research"}:
            raise DataError("企業グループの適用範囲が不正です")
        target = self._organization_registry_path(environment) if scope == "common" else self._research_organization_path(environment, research_id)
        existing, _ = self._read_groups(target, scope)
        action = str(payload.get("action", "save"))
        group_id = str(payload.get("id") or f"group_{uuid.uuid4().hex[:16]}")
        if not GROUP_ID.fullmatch(group_id):
            raise DataError("企業グループIDが不正です")
        if action == "delete":
            remaining = [group for group in existing if group.get("id") != group_id]
            if len(remaining) == len(existing):
                raise DataError("企業グループが見つかりません")
            saved = None
        elif action == "save":
            name = str(payload.get("name", "")).strip()
            note = str(payload.get("note", "")).strip()
            member_ids = list(dict.fromkeys(str(item) for item in payload.get("member_ids", [])))
            supplied_members = payload.get("members", [])
            if not name or len(name) > 100:
                raise DataError("グループ名は1〜100文字で指定してください")
            if len(note) > 2_000:
                raise DataError("根拠メモは2000文字以内で指定してください")
            if len(member_ids) < 2 or any(not ORGANIZATION_ID.fullmatch(item) for item in member_ids):
                raise DataError("企業グループには2社以上の有効な法人を指定してください")
            member_names = {
                str(item.get("id")): str(item.get("name", "")).strip()
                for item in supplied_members if isinstance(item, dict) and str(item.get("id")) in member_ids
            }
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            old = next((group for group in existing if group.get("id") == group_id), {})
            saved = {
                "id": group_id,
                "name": name,
                "member_ids": member_ids,
                "members": [{"id": item, "name": member_names.get(item, item)} for item in member_ids],
                "note": note,
                "verified": bool(payload.get("verified", False)),
                "created_at": old.get("created_at", now),
                "updated_at": now,
            }
            remaining = [group for group in existing if group.get("id") != group_id] + [saved]
        else:
            raise DataError("企業グループ操作が不正です")
        updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        persisted = [{key: value for key, value in group.items() if key != "scope"} for group in remaining]
        write_json_atomic(target, {"version": 1, "updated_at": updated_at, "groups": persisted})
        return {"ok": True, "scope": scope, "group": saved, "path": str(target.relative_to(self.root))}

    def _research_signature(self, environment: str, research_dir: Path) -> str:
        """Cheap invalidation key for filesystem compatibility inputs.

        The PDF directory mtime changes when files are added or removed, while
        source and configuration files are content hashed. Generated analysis
        is written directly to SQLite by the current pipeline; legacy JSON is
        imported by the explicit migration command.
        """
        digest = hashlib.sha256()
        for path in (
            research_dir / "research.json",
            research_dir / "company_tech.txt",
            research_dir / "patents.json",
            latest_patent_list(research_dir)[0],
        ):
            if path and path.is_file():
                digest.update(path.name.encode("utf-8"))
                digest.update(path.read_bytes())
        pool = self.paths(environment).pool
        try:
            digest.update(str(pool.stat().st_mtime_ns).encode("ascii"))
        except OSError:
            digest.update(b"missing-pool")
        return digest.hexdigest()

    @staticmethod
    def _technology_map(research_dir: Path) -> dict[str, Any]:
        clustering_root = research_dir / "clustering"
        if not clustering_root.is_dir():
            return {}
        for run_dir in sorted((item for item in clustering_root.iterdir() if item.is_dir()), reverse=True):
            clusters_path = run_dir / "clusters.json"
            if not clusters_path.is_file():
                continue
            clusters = read_json(clusters_path)
            ordering = clusters.get("semantic_ordering", {})
            proximity = clusters.get("company_proximity", {})
            technology_names = clusters.get("technology_cluster_names", {})
            problem_names = clusters.get("problem_cluster_names", {})

            def metadata(names: dict, values: dict) -> dict[str, Any]:
                return {
                    str(cluster_id): {
                        "name": name,
                        **values.get(str(cluster_id), values.get(cluster_id, {})),
                    }
                    for cluster_id, name in names.items()
                }

            return {
                "run_id": run_dir.name,
                "algorithm": ordering.get("algorithm"),
                "proximity_scale": proximity.get("scale"),
                "technology_order": [str(item) for item in ordering.get("technology_order", [])],
                "problem_order": [str(item) for item in ordering.get("problem_order", [])],
                "technology_clusters": metadata(technology_names, proximity.get("technology", {})),
                "problem_clusters": metadata(problem_names, proximity.get("problem", {})),
            }
        return {}

    def _filesystem_documents(
        self,
        environment: str,
        research_dir: Path,
        config: dict[str, Any],
        *,
        prefer_database_results: bool = True,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, str]]:
        paths = self.paths(environment)
        status_rules = legal_status_rules(config)
        research_id = safe_id(research_dir.name, "research id")
        existing = {}
        if prefer_database_results and self.store(environment).source_signature(research_id):
            existing = {
                str(item.get("id")): item
                for item in self.store(environment).all_documents(research_id)
            }
        documents: list[dict[str, Any]] = []
        organizations: dict[str, str] = {}
        seen: set[str] = set()
        research_documents, input_audit = research_patents(research_dir, paths.pool)
        for raw in research_documents:
            pdf_name = str(raw.get("pdf", ""))
            if not pdf_name.lower().endswith(".pdf") or Path(pdf_name).name != pdf_name:
                raise DataError(f"不正なPDF参照です: {pdf_name}")
            pdf_path = (paths.pool / pdf_name).resolve()
            if pdf_path.parent != paths.pool.resolve():
                raise DataError(f"不正なPDF参照です: {pdf_name}")
            key = patent_key(pdf_name)
            if key in seen:
                raise DataError(f"同一リサーチ内でPDFが重複しています: {pdf_name}")
            seen.add(key)
            legacy_id = raw.get("_legacy_subresearch_id")
            result_path = research_dir / "results" / f"{key}.json"
            legacy_result = research_dir / "subresearches" / str(legacy_id) / "results" / f"{key}.json"
            if not result_path.exists() and legacy_id and legacy_result.exists():
                result_path = legacy_result
            analysis = read_json(result_path) if result_path.exists() else {
                field: existing.get(key, {}).get(field)
                for field in ANALYSIS_REQUIRED | {"tech_cluster_id", "problem_cluster_id"}
                if existing.get(key, {}).get(field) is not None
            }
            skip_path = research_dir / "pipeline" / key / "skip.json"
            legacy_skip = research_dir / "subresearches" / str(legacy_id) / "pipeline" / key / "skip.json"
            if not skip_path.exists() and legacy_id and legacy_skip.exists():
                skip_path = legacy_skip
            skip = read_json(skip_path) if skip_path.exists() else {}
            pdf_available = pdf_path.is_file()
            if not pdf_available:
                skip = {"reason": "pdf_not_found", "detail": f"PDFがpatent_poolにありません: {pdf_name}"}
            publication_number = str(raw.get("publication_number") or Path(pdf_name).stem)
            source_status = raw.get("source_status")
            derived_application_year = application_year(raw.get("application_date"))
            entities = applicant_organizations(raw.get("applicant"))
            organizations.update({item["id"]: item["name"] for item in entities})
            record = {
                "id": key,
                "pdf": pdf_name,
                "publication_number": publication_number,
                "title": raw.get("title") or publication_number,
                "applicant": raw.get("applicant", "未設定"),
                "applicant_organizations": entities,
                "applicant_organization_ids": [item["id"] for item in entities],
                "category": raw.get("category", "未分類"),
                "year": derived_application_year or raw.get("year") or infer_year(publication_number),
                "year_source": "application_date" if derived_application_year else str(raw.get("year_source") or "legacy"),
                "status": raw.get("status") or infer_status(publication_number),
                "legal_status_category": classify_legal_status(source_status, status_rules),
                "tags": raw.get("tags", []),
                "pdf_available": pdf_available,
                "analysis_state": "skipped" if skip else ("ready" if ANALYSIS_REQUIRED.issubset(analysis) else ("invalid" if analysis else "pending")),
                "skip_reason": skip.get("reason"),
                "skip_detail": skip.get("detail"),
                "source_status": source_status,
                "application_number": raw.get("application_number"),
                "application_date": raw.get("application_date"),
                "registration_number": raw.get("registration_number"),
                "registration_date": raw.get("registration_date"),
                "source_ai_score": raw.get("source_ai_score"),
                "source_no": raw.get("source_no"),
                "pdf_match": raw.get("pdf_match"),
            }
            record.update(analysis)
            documents.append(record)
        return documents, input_audit, organizations

    def _import_artifacts(self, environment: str, research_id: str, research_dir: Path) -> int:
        store = self.store(environment)
        imported = 0
        roots = [research_dir / "pipeline", research_dir / "subresearches"]
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*.json"):
                if path.name == "embeddings.json":
                    try:
                        embedding = read_json(path)
                        patent_id = path.parent.name
                        store.put_embedding(research_id, patent_id, embedding)
                        imported += 1
                    except (DataError, KeyError, ValueError):
                        continue
                    continue
                try:
                    payload = read_json(path)
                except DataError:
                    continue
                relative = path.relative_to(research_dir)
                patent_id = path.parent.name if SAFE_ID.fullmatch(path.parent.name) else "_research"
                run_id = ""
                parts = relative.parts
                if "attempts" in parts:
                    index = parts.index("attempts")
                    if index + 1 < len(parts):
                        run_id = parts[index + 1]
                store.put_artifact(
                    research_id,
                    patent_id,
                    path.stem,
                    payload,
                    run_id=run_id,
                    source_path=str(relative),
                )
                imported += 1
        interpretations = self.paths(environment).runtime / "interpretations" / research_id
        if interpretations.is_dir():
            for path in interpretations.glob("*.json"):
                try:
                    payload = read_json(path)
                    store.save_interpretation(
                        research_id,
                        safe_id(path.stem, "patent id"),
                        str(payload.get("note", "")),
                        str(payload.get("source", "human")),
                    )
                    imported += 1
                except (DataError, ValueError):
                    continue
        return imported

    def sync_research(
        self,
        environment: str,
        research_id: str,
        *,
        force: bool = False,
        import_artifacts: bool = False,
    ) -> dict[str, Any]:
        paths = self.paths(environment)
        research_id = safe_id(research_id, "research id")
        research_dir = (paths.researches / research_id).resolve()
        if research_dir.parent != paths.researches.resolve() or not research_dir.is_dir():
            raise DataError("リサーチが見つかりません")
        signature = self._research_signature(environment, research_dir)
        store = self.store(environment)
        changed = force or store.source_signature(research_id) != signature
        if changed:
            config = read_research_config(research_dir)
            company_technology = read_company_technology(research_dir, config)
            documents, input_audit, organizations = self._filesystem_documents(
                environment, research_dir, config, prefer_database_results=True
            )
            store.replace_research(
                research_id,
                config=config,
                company_technology=company_technology,
                input_audit=input_audit,
                csv_history=self._csv_history(research_dir),
                technology_map=self._technology_map(research_dir),
                source_signature=signature,
                documents=documents,
                organizations=organizations,
            )
        imported = self._import_artifacts(environment, research_id, research_dir) if import_artifacts else 0
        return {"research_id": research_id, "changed": changed, "artifacts_imported": imported}

    def sync_environment(
        self, environment: str, *, force: bool = False, import_artifacts: bool = False
    ) -> dict[str, Any]:
        paths = self.paths(environment)
        paths.researches.mkdir(parents=True, exist_ok=True)
        items = []
        for research_dir in sorted(path for path in paths.researches.iterdir() if path.is_dir()):
            if not (
                (research_dir / "research.json").exists()
                or (research_dir / "company_tech.txt").exists()
                or (research_dir / "patents.json").exists()
                or latest_patent_list(research_dir)[0]
            ):
                continue
            items.append(
                self.sync_research(
                    environment,
                    research_dir.name,
                    force=force,
                    import_artifacts=import_artifacts,
                )
            )
        store = self.store(environment)
        pool_signature = (
            str(paths.pool.stat().st_mtime_ns) if paths.pool.exists() else "missing"
        )
        if force or store.get_metadata("pool_signature") != pool_signature:
            pool_count = (
                sum(1 for _ in paths.pool.glob("*.pdf")) if paths.pool.exists() else 0
            )
            store.set_metadata("pool_count", str(pool_count))
            store.set_metadata("pool_signature", pool_signature)
        else:
            pool_count = store.pool_count()
        return {
            "environment": environment,
            "researches": items,
            "pool_count": pool_count,
            "database": store.statistics(),
        }

    def list_researches(self, environment: str, status: str = "active") -> list[dict[str, Any]]:
        if status not in {"active", "archived", "all"}:
            raise DataError("リサーチ状態の指定が不正です")
        self.sync_environment(environment)
        return self.store(environment).list_researches(status)

    def _stored_organization_registry(self, environment: str, research_id: str) -> dict[str, Any]:
        shared, shared_version = self._read_groups(self._organization_registry_path(environment), "common")
        local, local_version = self._read_groups(self._research_organization_path(environment, research_id), "research")
        version_material = f"{shared_version or 'none'}|{local_version or 'none'}"
        return {
            "version": hashlib.sha256(version_material.encode("utf-8")).hexdigest()[:12],
            "organizations": self.store(environment).organizations(research_id),
            "groups": shared + local,
        }

    def dashboard(
        self, environment: str, research_id: str, *, include_patents: bool = True
    ) -> dict[str, Any]:
        paths = self.paths(environment)
        research_id = safe_id(research_id, "research id")
        research_dir = (paths.researches / research_id).resolve()
        if research_dir.parent != paths.researches.resolve() or not research_dir.is_dir():
            raise DataError("リサーチが見つかりません")
        self.sync_research(environment, research_id)
        stored = self.store(environment).research(research_id)
        if not stored:
            raise DataError("リサーチDBを初期化できません")
        config = stored["config"]
        company_technology = stored["company_technology"]
        status_rules = legal_status_rules(config)
        page = self.store(environment).document_page(research_id, limit=1)
        patents = self.store(environment).all_documents(research_id) if include_patents else []
        return {
            "environment": environment,
            "research": {
                "id": research_id,
                "name": config.get("name", research_id),
                "description": config.get("description", ""),
                "company_technology": company_technology,
                "legal_status_rules": status_rules,
                "lifecycle": self._lifecycle(config),
                "csv_history": self._csv_history(research_dir),
            },
            "pool_count": self.store(environment).pool_count(),
            "input": stored["input"],
            "technology_map": stored["technology_map"],
            "organization_registry": self._stored_organization_registry(environment, research_id),
            "aggregates": page["aggregates"],
            "patents": patents,
        }

    def document_page(self, environment: str, research_id: str, **filters: Any) -> dict[str, Any]:
        self.sync_research(environment, research_id)
        return self.store(environment).document_page(research_id, **filters)

    def cell_trend(self, environment: str, research_id: str, **filters: Any) -> dict[str, Any]:
        self.sync_research(environment, research_id)
        return self.store(environment).cell_trend(research_id, **filters)

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
        self.store(environment).save_interpretation(
            research_id, patent_id, note, str(payload.get("source", "human"))
        )
        return target

    def preflight(self, environment: str, research_id: str, ollama_url: str = "http://127.0.0.1:11434") -> dict[str, Any]:
        dashboard = self.dashboard(environment, research_id, include_patents=False)
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
        selection_fingerprint = hashlib.sha256()
        document_count = 0
        for patent in self.store(environment).iter_documents(research_id):
            document_count += 1
            selection_fingerprint.update(str(patent.get("pdf", "")).encode("utf-8"))
            selection_fingerprint.update(b"\n")
            if not patent.get("pdf_available"):
                continue
            artifact_dir = research_dir / "pipeline" / patent["id"]
            if not artifact_dir.exists():
                legacy_artifacts = list((research_dir / "subresearches").glob(f"*/pipeline/{patent['id']}"))
                if len(legacy_artifacts) == 1:
                    artifact_dir = legacy_artifacts[0]
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
            {"id": "pdf-selection", "ok": document_count > 0, "detail": f"{document_count}件"},
            {"id": "prompts", "ok": prompt_contract_ok, "detail": f"小型プロンプト {len(required_prompt_names & installed_prompt_names)}/{len(required_prompt_names)}・Schema {len(required_schema_names & installed_schema_names)}/{len(required_schema_names)}"},
            {"id": "output-isolation", "ok": self.paths(environment).runtime != self.paths("debug" if environment == "normal" else "normal").runtime, "detail": str(self.paths(environment).runtime)},
            {"id": "ollama", "ok": ollama_ok, "detail": ollama_detail},
            {"id": "models", "ok": models_ok, "detail": "必要モデル導入済み" if models_ok else f"不足: {', '.join(missing_models)}"},
            {"id": "claim-structure", "ok": structure_ok, "detail": f"有効 {structure_valid}/{structure_total}件・未準備 {structure_missing}件・異常 {structure_invalid}件・スキップ {structure_skipped}件"},
        ]
        return {
            "ready": all(c["ok"] for c in checks),
            "checks": checks,
            "selection_fingerprint": selection_fingerprint.hexdigest()[:12],
        }
