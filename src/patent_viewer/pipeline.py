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
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from pypdf import PdfReader

from .domain import DataError, patent_key, read_company_technology, read_research_config, research_patents, read_json, safe_id
from .storage import SQLiteStore


READING_MODES = {"cache", "verify_original", "reextract"}
PIPELINE_STAGES = (
    "extract",
    "structure",
    "company_profile",
    "similarity",
    "concept_level",
    "problem_summary",
    "technology_summary",
    "embeddings",
    "company_embedding",
    "clustering",
    "cluster_names",
    "semantic_ordering",
    "finalize",
)
ANALYSIS_PIPELINE_VERSION = "semantic-map-v11-shared-semantic-cache"
STRUCTURED_DOCUMENT_VERSION = 2
CLAIM_STRUCTURE_VERSION = 4
EVIDENCE_PACK_VERSION = 2
SHARED_PREPROCESSING_VERSION = "pdf-structure-v2-claim-v4-page-artifacts"
SHARED_SEMANTIC_CACHE_VERSION = "semantic-analysis-v1"
CACHE_FILENAME_FINGERPRINT_LENGTH = 32
SHARED_ANALYSIS_TASKS = frozenset({
    "concept_level",
    "problem_summary",
    "technology_summary",
})


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


def cache_record_filename(fingerprint: str) -> str:
    """Return a Windows-safe cache filename while retaining a 128-bit key."""
    return f"{fingerprint[:CACHE_FILENAME_FINGERPRINT_LENGTH]}.json"


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Do not repeat a potentially long content-addressed destination basename.
    # The old temporary name pushed shared-cache writes past MAX_PATH on Windows.
    temporary = path.parent / f".tmp-{os.getpid():x}-{threading.get_ident():x}"
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


PATENT_PAGE_HEADER_LINE = re.compile(
    r"""
    (?:\(\s*\d+\s*\)\s*)?
    (?:JP\s*)?
    [0-9][0-9\s-]{2,20}
    \s+[AB]\d?
    \s+\d{1,4}\.\d{1,2}\.\d{1,2}
    """,
    re.IGNORECASE | re.VERBOSE,
)


def strip_patent_page_headers(text: str) -> tuple[str, int]:
    """Remove JPO page headers without changing substantive line boundaries."""
    lines: list[str] = []
    removed = 0
    for line in text.splitlines():
        normalized = unicodedata.normalize("NFKC", line).strip()
        if normalized and PATENT_PAGE_HEADER_LINE.fullmatch(normalized):
            removed += 1
            continue
        lines.append(line)
    return "\n".join(lines), removed


def clean_text(text: str) -> str:
    text, _ = strip_patent_page_headers(text)
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
    text, removed_page_headers = strip_patent_page_headers(text)
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
        "removed_page_headers": removed_page_headers,
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

SECTION_LABELS = {
    label: key
    for key, labels in SECTION_HEADINGS.items()
    for label in labels
}
SECTION_LABELS.update({
    "発明の概要": "summary",
    "発明の詳細な説明": "description",
    "図面の簡単な説明": "drawings",
    "符号の説明": "reference_signs",
    "要約": "abstract",
})
SECTION_HEADING_PATTERN = re.compile(
    r"^[ \t]*(?:【\s*)?("
    + "|".join(
        re.escape(label)
        for label in sorted(SECTION_LABELS, key=len, reverse=True)
    )
    + r")(?:\s*】)?[ \t]*"
)
PARAGRAPH_NUMBER = re.compile(r"^[ \t]*【\s*([0-9０-９]{4})\s*】[ \t]*")
BOILERPLATE_PATTERNS = (
    re.compile(r"本発明は(?:上記|上述)の実施形態に限定されるものではない"),
    re.compile(r"当業者であれば.*適宜変更"),
    re.compile(r"図面において同一.*同一の符号"),
    re.compile(r"以下、図面を参照して.*説明"),
)
ROLE_PATTERNS = {
    "prior_art_limitation": re.compile(r"(従来|従来技術|しかしながら|問題があった|困難であった|十分でなかった)"),
    "problem_condition": re.compile(r"(課題|問題|ばらつき|変動|欠陥|低下|抑制|防止|改善)"),
    "input": re.compile(r"(入力|検出|測定|取得|センサ|信号|データ|画像|情報)"),
    "processing": re.compile(r"(算出|演算|推定|判定|識別|予測|処理|解析|生成|補正)"),
    "control": re.compile(r"(制御|調整|変更|駆動|フィードバック|出力を設定)"),
    "output": re.compile(r"(出力|表示|通知|加工|照射|送信|記録)"),
    "component": re.compile(r"(装置|システム|部|手段|回路|機構|モジュール|プロセッサ|メモリ)"),
    "definition": re.compile(r"(本明細書において|とは、|を意味する|と定義|という。)"),
    "parameter": re.compile(r"(\d+(?:\.\d+)?\s*(?:%|℃|mm|μm|nm|Hz|kW|W|秒|分)|以上|以下|未満|範囲)"),
    "effect": re.compile(r"(効果|可能となる|できる|向上する|低減する|抑制できる)"),
    "example": re.compile(r"(実施例|実施形態|具体例|図\d+|図[０-９]+)"),
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


def _normalized_duplicate_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = PARAGRAPH_NUMBER.sub("", value)
    return re.sub(r"[\s、。・,.;；:：()（）「」『』【】\[\]]+", "", value).lower()


def _line_is_complete(value: str) -> bool:
    return bool(re.search(r"[。！？!?；;：:]$|[】)]$", value.strip()))


def _page_units(text: str) -> list[str]:
    """Rejoin PDF display lines without losing patent paragraph boundaries."""
    output: list[str] = []
    current = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if current:
                output.append(current)
                current = ""
            continue
        starts_boundary = bool(PARAGRAPH_NUMBER.match(line) or SECTION_HEADING_PATTERN.match(line))
        if starts_boundary and current:
            output.append(current)
            current = ""
        current = f"{current}{line}" if current else line
        if _line_is_complete(current):
            output.append(current)
            current = ""
    if current:
        output.append(current)
    return output


def _repeated_page_lines(pages: list[dict[str, Any]]) -> set[str]:
    """Identify short headers/footers repeated on at least half the pages."""
    if len(pages) < 3:
        return set()
    occurrences: Counter[str] = Counter()
    originals: dict[str, str] = {}
    for page in pages:
        seen: set[str] = set()
        lines = [line.strip() for line in str(page.get("text", "")).splitlines() if line.strip()]
        for line in lines[:3] + lines[-3:]:
            key = _normalized_duplicate_key(line)
            if (
                not key
                or len(key) > 80
                or key in seen
                or re.search(r"[。！？!?]$", line)
            ):
                continue
            seen.add(key)
            occurrences[key] += 1
            originals[key] = line
    threshold = max(2, math.ceil(len(pages) * 0.5))
    return {key for key, count in occurrences.items() if count >= threshold}


def paragraph_roles(text: str, section: str) -> list[str]:
    roles = [role for role, pattern in ROLE_PATTERNS.items() if pattern.search(text)]
    section_role = {
        "background": "prior_art",
        "problem": "problem",
        "solution": "solution",
        "effects": "effect",
        "embodiments": "example",
        "claims": "claim",
    }.get(section)
    if section_role and section_role not in roles:
        roles.insert(0, section_role)
    return roles or ["context"]


def build_structured_document(
    pages: list[dict[str, Any]],
    *,
    pdf_name: str = "",
    pdf_sha256: str = "",
) -> dict[str, Any]:
    """Create stable paragraph IDs, roles, and duplicate/boilerplate annotations."""
    repeated = _repeated_page_lines(pages)
    paragraphs: list[dict[str, Any]] = []
    first_by_hash: dict[str, str] = {}
    current_section = "unclassified"
    removed_repeated = 0
    removed_page_headers = 0
    for page in pages:
        page_number = int(page.get("page") or len(paragraphs) + 1)
        unit_index = 0
        page_text, page_removed = strip_patent_page_headers(
            str(page.get("text", ""))
        )
        removed_page_headers += page_removed
        for unit in _page_units(page_text):
            duplicate_key = _normalized_duplicate_key(unit)
            if duplicate_key in repeated:
                removed_repeated += 1
                continue
            heading = SECTION_HEADING_PATTERN.match(unit)
            heading_label = heading.group(1) if heading else ""
            if heading_label:
                current_section = SECTION_LABELS[heading_label]
                unit = unit[heading.end():].strip()
                if not unit:
                    continue
            number_match = PARAGRAPH_NUMBER.match(unit)
            paragraph_number = (
                unicodedata.normalize("NFKC", number_match.group(1))
                if number_match else None
            )
            if number_match:
                unit = unit[number_match.end():].strip()
            if not unit:
                continue
            unit_index += 1
            paragraph_id = f"P{page_number:04d}-N{unit_index:04d}"
            content_hash = sha256_bytes(unit.encode("utf-8"))
            normalized_key = _normalized_duplicate_key(unit)
            duplicate_of = first_by_hash.get(normalized_key)
            if not duplicate_of and normalized_key:
                first_by_hash[normalized_key] = paragraph_id
            boilerplate = any(pattern.search(unit) for pattern in BOILERPLATE_PATTERNS)
            paragraphs.append({
                "id": paragraph_id,
                "page": page_number,
                "paragraph_number": paragraph_number,
                "section": current_section,
                "text": unit,
                "roles": paragraph_roles(unit, current_section),
                "boilerplate": boilerplate,
                "duplicate_of": duplicate_of,
                "characters": len(unit),
                "sha256": content_hash,
            })
    return {
        "schema_version": STRUCTURED_DOCUMENT_VERSION,
        "pdf": pdf_name,
        "pdf_sha256": pdf_sha256,
        "paragraphs": paragraphs,
        "statistics": {
            "pages": len(pages),
            "paragraphs": len(paragraphs),
            "unique_paragraphs": sum(not item["duplicate_of"] for item in paragraphs),
            "duplicate_paragraphs": sum(bool(item["duplicate_of"]) for item in paragraphs),
            "boilerplate_paragraphs": sum(item["boilerplate"] for item in paragraphs),
            "removed_repeated_headers_footers": removed_repeated,
            "removed_page_headers": removed_page_headers,
            "characters": sum(item["characters"] for item in paragraphs),
        },
        "created_at": utc_now(),
    }


def _claim_category(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text)
    if re.search(r"(プログラム|命令).{0,30}(コンピュータ|プロセッサ)", normalized):
        return "program"
    if re.search(r"(方法|工程)[。.]?$", normalized):
        return "method"
    if re.search(r"(システム|装置|機器|ユニット|モジュール)[。.]?$", normalized):
        return "apparatus"
    if re.search(r"(組成物|材料|化合物|合金)[。.]?$", normalized):
        return "material"
    return "other"


def _claim_element_type(text: str) -> str:
    for role in ("control", "processing", "input", "component", "output", "parameter"):
        if ROLE_PATTERNS[role].search(text):
            return role
    if "工程" in text:
        return "step"
    return "limitation"


def decompose_claim_elements(text: str, claim_number: int) -> list[dict[str, Any]]:
    """Conservatively split enumerated Japanese claim clauses into source-bound elements."""
    normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).strip()
    candidates = re.split(r"(?<=と)、|(?<=工程)、|[；;]\s*|\n+", normalized)
    candidates = [item.strip(" 、") for item in candidates if item.strip(" 、")]
    if len(candidates) == 1:
        candidates = [
            item.strip()
            for item in re.split(r"(?<=。)", normalized)
            if item.strip()
        ]
    elements: list[dict[str, Any]] = []
    for index, value in enumerate(candidates, 1):
        limitations = [
            kind for kind, pattern in {
                "numeric": ROLE_PATTERNS["parameter"],
                "material": re.compile(r"(材料|樹脂|金属|合金|ガラス|半導体)"),
                "shape": re.compile(r"(形状|円形|矩形|層状|筒状|凹|凸)"),
                "position": re.compile(r"(上方|下方|内部|外部|間|隣接|対向|接続)"),
                "sequence": re.compile(r"(前|後|次いで|順に|同時に)"),
                "condition": re.compile(r"(場合|とき|条件|に応じて|に基づいて)"),
            }.items()
            if pattern.search(value)
        ]
        elements.append({
            "id": f"C{claim_number}-E{index}",
            "text": value,
            "type": _claim_element_type(value),
            "limitations": limitations,
            "sha256": sha256_bytes(value.encode("utf-8")),
        })
    return elements


def enrich_claim_structure(claims: list[dict[str, Any]]) -> dict[str, Any]:
    enriched: list[dict[str, Any]] = []
    for claim in claims:
        number = int(claim["number"])
        enriched.append({
            **claim,
            "category": _claim_category(claim["text"]),
            "elements": decompose_claim_elements(claim["text"], number),
            "inherits_from": list(claim.get("depends_on", [])),
        })
    return {
        "schema_version": CLAIM_STRUCTURE_VERSION,
        "claims": enriched,
        "statistics": {
            "claims": len(enriched),
            "independent_claims": sum(item["type"] == "independent" for item in enriched),
            "elements": sum(len(item["elements"]) for item in enriched),
        },
        "created_at": utc_now(),
    }


def _character_ngrams(value: str, width: int = 2) -> set[str]:
    normalized = _normalized_duplicate_key(value)
    return {
        normalized[index:index + width]
        for index in range(max(0, len(normalized) - width + 1))
    }


def _company_overlap(text: str, company_technology: str) -> float:
    if not company_technology:
        return 0.0
    company = _character_ngrams(company_technology)
    candidate = _character_ngrams(text)
    if not company or not candidate:
        return 0.0
    return len(company & candidate) / max(1, len(company))


EVIDENCE_TASK_CONFIG = {
    "similarity": {
        "maximum": 12_000,
        "claim_ratio": 0.45,
        "allowed_sections": {"solution", "technical_field", "embodiments", "problem", "summary", "description", "abstract", "unclassified"},
        "sections": {"solution": 10, "technical_field": 7, "embodiments": 4, "summary": 4, "description": 2},
        "roles": {"control": 6, "processing": 6, "input": 4, "output": 4, "component": 4, "definition": 5, "parameter": 2},
        "company_weight": 18,
        "section_limits": {"embodiments": 18, "problem": 8, "technical_field": 8, "summary": 8, "abstract": 5, "unclassified": 8},
    },
    "concept_level": {
        "maximum": 10_000,
        "claim_ratio": 0.9,
        "allowed_sections": set(),
        "sections": {"claims": 10, "description": 1},
        "roles": {"parameter": 5, "definition": 2},
        "company_weight": 0,
        "section_limits": {},
    },
    "problem_summary": {
        "maximum": 12_000,
        "claim_ratio": 0,
        "allowed_sections": {"problem", "background", "effects", "summary", "description", "abstract", "unclassified"},
        "sections": {"problem": 12, "background": 10, "effects": 3, "summary": 2, "description": 1},
        "roles": {"prior_art_limitation": 8, "problem_condition": 8, "effect": 1, "definition": 1},
        "company_weight": 0,
        "section_limits": {"background": 18, "problem": 18, "effects": 8, "summary": 8, "abstract": 5, "unclassified": 5},
    },
    "technology_summary": {
        "maximum": 12_000,
        "claim_ratio": 0.4,
        "allowed_sections": {"solution", "technical_field", "embodiments", "summary", "description", "abstract", "unclassified"},
        "sections": {"solution": 11, "technical_field": 7, "embodiments": 5, "summary": 4, "description": 2},
        "roles": {"control": 6, "processing": 6, "input": 4, "output": 4, "component": 5, "definition": 4, "parameter": 2},
        "company_weight": 0,
        "section_limits": {"embodiments": 20, "technical_field": 8, "summary": 8, "abstract": 5, "unclassified": 8},
    },
}


def _truncate_at_sentence(value: str, maximum: int) -> str:
    if len(value) <= maximum:
        return value
    if maximum <= 1:
        return ""
    clipped = value[:maximum]
    boundary = max(clipped.rfind("。"), clipped.rfind("；"), clipped.rfind(";"))
    return clipped[:boundary + 1] if boundary >= maximum // 2 else clipped


def _render_claim_evidence(
    claim_structure: dict[str, Any],
    maximum: int,
    *,
    include_element_index: bool,
) -> tuple[str, list[dict[str, Any]]]:
    claims = [
        item for item in claim_structure.get("claims", [])
        if item.get("type") == "independent"
    ] or claim_structure.get("claims", [])[:1]
    output: list[str] = []
    sources: list[dict[str, Any]] = []
    remaining = maximum
    for claim in claims:
        header = f"【請求項{claim['number']}】"
        body = str(claim.get("text", "")).strip()
        elements = claim.get("elements", [])
        if include_element_index and elements:
            element_lines = []
            for item in elements:
                attributes = [str(item.get("type") or "limitation")]
                attributes.extend(str(value) for value in item.get("limitations", []))
                element_lines.append(
                    f"[{item['id']} {'/'.join(attributes)}] {item.get('text', '')}"
                )
            block = f"{header}\n" + "\n".join(element_lines)
        else:
            block = f"{header}{body}"
        block = _truncate_at_sentence(block, remaining)
        if not block:
            break
        included_element_ids = [
            item["id"]
            for item in elements
            if f"[{item['id']} " in block
        ]
        output.append(block)
        sources.append({
            "source_type": "claim",
            "claim_number": claim["number"],
            "element_ids": included_element_ids,
            "characters": len(block),
        })
        remaining -= len(block) + 2
        if remaining <= 0:
            break
    return "\n\n".join(output), sources


def _paragraph_score(
    paragraph: dict[str, Any],
    config: dict[str, Any],
    company_technology: str,
) -> tuple[float, list[str]]:
    if paragraph.get("section") not in config["allowed_sections"]:
        return -100.0, ["penalty:task_irrelevant_section"]
    score = float(config["sections"].get(paragraph.get("section"), 0))
    reasons: list[str] = []
    if score:
        reasons.append(f"section:{paragraph['section']}")
    for role in paragraph.get("roles", []):
        weight = config["roles"].get(role, 0)
        if weight:
            score += weight
            reasons.append(f"role:{role}")
    overlap = _company_overlap(paragraph.get("text", ""), company_technology)
    if overlap and config["company_weight"]:
        score += overlap * config["company_weight"]
        reasons.append(f"company_overlap:{overlap:.3f}")
    if paragraph.get("paragraph_number"):
        score += 0.5
    if paragraph.get("boilerplate"):
        score -= 20
        reasons.append("penalty:boilerplate")
    if paragraph.get("duplicate_of"):
        score -= 30
        reasons.append("penalty:duplicate")
    return score, reasons


def build_evidence_pack(
    structured_document: dict[str, Any],
    claim_structure: dict[str, Any],
    company_technology: str,
    *,
    reading_policy: str = "adaptive",
    fallback_text: str = "",
) -> dict[str, Any]:
    """Build deterministic, source-traceable task inputs within existing budgets."""
    tasks: dict[str, Any] = {}
    paragraphs = structured_document.get("paragraphs", [])
    for task, config in EVIDENCE_TASK_CONFIG.items():
        maximum = int(config["maximum"])
        claim_budget = round(maximum * float(config["claim_ratio"]))
        claim_text, claim_sources = _render_claim_evidence(
            claim_structure,
            claim_budget,
            include_element_index=task in {"similarity", "concept_level", "technology_summary"},
        )
        prefix = "## 独立請求項\n" + claim_text if claim_text else ""
        remaining = maximum - len(prefix) - (2 if prefix else 0)
        ranked: list[tuple[float, int, dict[str, Any], list[str]]] = []
        for position, paragraph in enumerate(paragraphs):
            if paragraph.get("section") == "claims":
                continue
            score, reasons = _paragraph_score(paragraph, config, company_technology)
            if score > 0 and not paragraph.get("duplicate_of") and not paragraph.get("boilerplate"):
                ranked.append((score, position, paragraph, reasons))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        selected: list[dict[str, Any]] = []
        rendered: list[str] = [prefix] if prefix else []
        selected_sections: Counter[str] = Counter()
        selected_ngrams: list[set[str]] = []
        near_duplicates_excluded = 0
        for score, _, paragraph, reasons in ranked:
            section = paragraph["section"]
            section_limit = config["section_limits"].get(section)
            if section_limit is not None and selected_sections[section] >= section_limit:
                continue
            paragraph_ngrams = _character_ngrams(paragraph["text"], width=3)
            if any(
                len(paragraph_ngrams & existing)
                / max(1, len(paragraph_ngrams | existing))
                >= 0.82
                for existing in selected_ngrams
            ):
                near_duplicates_excluded += 1
                continue
            label = f"[{paragraph['id']} p.{paragraph['page']} {paragraph['section']}]"
            block = f"{label}\n{paragraph['text']}"
            if len(block) + 2 > remaining:
                continue
            rendered.append(block)
            remaining -= len(block) + 2
            selected_sections[paragraph["section"]] += 1
            selected_ngrams.append(paragraph_ngrams)
            selected.append({
                "source_type": "paragraph",
                "paragraph_id": paragraph["id"],
                "page": paragraph["page"],
                "paragraph_number": paragraph.get("paragraph_number"),
                "section": paragraph["section"],
                "roles": paragraph["roles"],
                "score": round(score, 4),
                "reasons": reasons,
                "characters": len(paragraph["text"]),
                "sha256": paragraph["sha256"],
            })
        rendered_text = "\n\n".join(item for item in rendered if item)
        if not rendered_text:
            rendered_text = _truncate_at_sentence(fallback_text, maximum)
        tasks[task] = {
            "maximum_characters": maximum,
            "rendered_characters": len(rendered_text),
            "rendered_text": rendered_text,
            "sources": claim_sources + selected,
            "statistics": {
                "claim_sources": len(claim_sources),
                "paragraph_sources": len(selected),
                "candidate_paragraphs": len(ranked),
                "near_duplicates_excluded": near_duplicates_excluded,
                "selected_sections": dict(selected_sections),
                "unused_characters": max(0, maximum - len(rendered_text)),
            },
        }
    return {
        "schema_version": EVIDENCE_PACK_VERSION,
        "reading_policy": reading_policy,
        "company_technology_sha256": sha256_bytes(
            company_technology.encode("utf-8")
        ),
        "selection_algorithm": "role-section-overlap-v1",
        "structured_document_sha256": sha256_bytes(
            json.dumps(
                structured_document.get("paragraphs", []),
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ),
        "claim_structure_sha256": sha256_bytes(
            json.dumps(
                claim_structure.get("claims", []),
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ),
        "tasks": tasks,
        "created_at": utc_now(),
    }


def validate_score(value: dict[str, Any]) -> dict[str, Any]:
    for key in ("similarity", "concept_level"):
        if type(value.get(key)) is not int or not 1 <= value[key] <= 5:
            raise ValueError(f"{key} must be an integer from 1 to 5")
    for key in ("similarity_reason", "concept_level_reason"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"{key} is required")
    return value


def verify_claim_reference(
    structure: dict[str, Any], reported_number: Any, criterion: str
) -> dict[str, Any]:
    """Bind an LLM-selected number to Python-extracted source text.

    The model never supplies the displayed quotation. Only an integer is
    accepted, and Python copies the exact claim text from document_structure.
    """
    claims = structure.get("claims", [])
    eligible = [
        claim for claim in claims
        if claim.get("type") == "independent"
    ] or claims[:1]
    base = {
        "criterion": criterion,
        "verified": False,
        "source": "document_structure.json",
    }
    if type(reported_number) is not int:
        return {**base, "rejection_reason": "claim_number_not_integer"}
    claim = next(
        (item for item in eligible if item.get("number") == reported_number),
        None,
    )
    if claim is None:
        return {
            **base,
            "reported_claim_number": reported_number,
            "rejection_reason": "claim_number_not_in_scoring_input",
        }
    text = claim.get("text")
    if not isinstance(text, str) or not text.strip():
        return {
            **base,
            "reported_claim_number": reported_number,
            "rejection_reason": "claim_text_missing",
        }
    exact_text = text.strip()
    return {
        **base,
        "verified": True,
        "claim_number": reported_number,
        "claim_type": claim.get("type", "unknown"),
        "claim_text": exact_text,
        "claim_sha256": sha256_bytes(exact_text.encode("utf-8")),
    }


def verify_evidence_references(
    prepared: dict[str, Any],
    task: str,
    reported_ids: Any,
) -> dict[str, Any]:
    """Validate source IDs selected by the model against the exact task input."""
    evidence_pack = prepared.get("evidence_pack", {})
    task_evidence = evidence_pack.get("tasks", {}).get(task, {})
    allowed_element_ids = {
        str(element_id)
        for source in task_evidence.get("sources", [])
        if source.get("source_type") == "claim"
        for element_id in source.get("element_ids", [])
    }
    allowed_paragraph_ids = {
        str(source.get("paragraph_id"))
        for source in task_evidence.get("sources", [])
        if source.get("source_type") == "paragraph"
        and source.get("paragraph_id")
    }
    expected_type = {
        "similarity": "element",
        "concept_level": "element",
        "problem_summary": "paragraph",
        "technology_summary": "element_or_paragraph",
    }.get(task, "paragraph")
    allowed_ids = {
        "element": allowed_element_ids,
        "paragraph": allowed_paragraph_ids,
        "element_or_paragraph": allowed_element_ids | allowed_paragraph_ids,
    }[expected_type]
    base = {
        "criterion": task,
        "source": "evidence_pack.json",
        "source_type": expected_type,
        "verified": False,
    }
    if not isinstance(reported_ids, list) or any(
        not isinstance(item, str) for item in reported_ids
    ):
        return {**base, "rejection_reason": "evidence_ids_not_string_array"}

    reported_values = list(
        dict.fromkeys(item.strip() for item in reported_ids if item.strip())
    )
    reported = []
    for value in reported_values:
        match = re.search(r"\b(?:C\d+-E\d+|P\d{4}-N\d{4})\b", value)
        normalized = match.group(0) if match else value
        if normalized not in reported:
            reported.append(normalized)
    verified_ids = [item for item in reported if item in allowed_ids]
    rejected_ids = [item for item in reported if item not in allowed_ids]
    element_lookup = {
        str(element.get("id")): {
            "id": str(element.get("id")),
            "source_type": "element",
            "claim_number": claim.get("number"),
            "type": element.get("type"),
            "limitations": list(element.get("limitations", [])),
            "sha256": element.get("sha256"),
        }
        for claim in prepared.get("claim_structure", {}).get("claims", [])
        for element in claim.get("elements", [])
        if element.get("id")
    }
    paragraph_lookup = {
        str(paragraph.get("id")): {
            "id": str(paragraph.get("id")),
            "source_type": "paragraph",
            "page": paragraph.get("page"),
            "paragraph_number": paragraph.get("paragraph_number"),
            "section": paragraph.get("section"),
            "roles": list(paragraph.get("roles", [])),
            "sha256": paragraph.get("sha256"),
        }
        for paragraph in prepared.get("structured_document", {}).get(
            "paragraphs", []
        )
        if paragraph.get("id")
    }
    source_lookup = {
        **(
            element_lookup
            if expected_type in {"element", "element_or_paragraph"}
            else {}
        ),
        **(
            paragraph_lookup
            if expected_type in {"paragraph", "element_or_paragraph"}
            else {}
        ),
    }
    verified_sources = [
        source_lookup[item]
        for item in verified_ids
        if item in source_lookup
    ]
    verified = bool(verified_ids) and not rejected_ids
    result = {
        **base,
        "verified": verified,
        "reported_values": reported_values,
        "reported_ids": reported,
        "verified_ids": verified_ids,
        "verified_sources": verified_sources,
        "rejected_ids": rejected_ids,
    }
    if not reported:
        result["rejection_reason"] = "evidence_ids_empty"
    elif rejected_ids:
        result["rejection_reason"] = "evidence_id_not_in_task_input"
    elif not verified_ids:
        result["rejection_reason"] = "no_verified_evidence_ids"
    return result


def summarize_evidence_validation(
    scores: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate grounding validity so a run can decide whether task splitting is needed."""
    by_task = {
        task: {"verified": 0, "invalid": 0}
        for task in (
            "similarity",
            "concept_level",
            "problem_summary",
            "technology_summary",
        )
    }
    for score in scores:
        trace = score.get("evidence_traceability", {})
        for task, counts in by_task.items():
            if trace.get(task, {}).get("verified") is True:
                counts["verified"] += 1
            else:
                counts["invalid"] += 1
    verified = sum(item["verified"] for item in by_task.values())
    invalid = sum(item["invalid"] for item in by_task.values())
    return {
        "total_checks": verified + invalid,
        "verified": verified,
        "invalid": invalid,
        "verification_rate": (
            round(verified / (verified + invalid), 6)
            if verified + invalid
            else 0.0
        ),
        "by_task": by_task,
    }


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


def scalable_clusters(items: dict[str, list[float]], cluster_count: int | None = None) -> dict[str, int]:
    """Deterministic bounded-memory spherical mini-batch clustering.

    Exact average linkage is useful for small review sets but its all-pairs
    distance table cannot represent production collections. Large sets use a
    structured projection and mini-batch cosine K-means instead.
    """
    if len(items) <= 500:
        return deterministic_clusters(items, cluster_count)
    try:
        import numpy as np
    except ImportError as exc:
        raise DataError("numpy is required for production-scale clustering") from exc
    keys = sorted(items)
    matrix = np.asarray([items[key] for key in keys], dtype=np.float32)
    if matrix.ndim != 2 or not matrix.shape[1]:
        raise DataError("embedding matrix is invalid")
    target_dimensions = min(256, matrix.shape[1])
    padded_dimensions = math.ceil(matrix.shape[1] / target_dimensions) * target_dimensions
    if padded_dimensions != matrix.shape[1]:
        matrix = np.pad(matrix, ((0, 0), (0, padded_dimensions - matrix.shape[1])))
    group_width = padded_dimensions // target_dimensions
    signs = np.where(np.arange(group_width) % 2, -1.0, 1.0).astype(np.float32)
    projected = (matrix.reshape(len(keys), target_dimensions, group_width) * signs).sum(axis=2)
    del matrix
    norms = np.linalg.norm(projected, axis=1, keepdims=True)
    projected /= np.maximum(norms, np.float32(1e-12))
    k = min(len(keys), cluster_count or max(1, round(math.sqrt(len(keys)))))
    rng = np.random.default_rng(0)
    initial = np.sort(rng.choice(len(keys), size=k, replace=False))
    centroids = projected[initial].copy()
    counts = np.zeros(k, dtype=np.int64)
    batch_size = min(1024, max(128, len(keys) // max(1, k)))
    for _ in range(8):
        for offset in range(0, len(keys), batch_size):
            batch = projected[offset:offset + batch_size]
            assigned = np.argmax(batch @ centroids.T, axis=1)
            for cluster_id in np.unique(assigned):
                members = batch[assigned == cluster_id]
                previous = counts[cluster_id]
                current = len(members)
                centroids[cluster_id] = (
                    centroids[cluster_id] * previous + members.sum(axis=0)
                ) / max(1, previous + current)
                counts[cluster_id] += current
            centroid_norms = np.linalg.norm(centroids, axis=1, keepdims=True)
            centroids /= np.maximum(centroid_norms, np.float32(1e-12))
    assignments: dict[str, int] = {}
    for offset in range(0, len(keys), batch_size):
        assigned = np.argmax(projected[offset:offset + batch_size] @ centroids.T, axis=1)
        assignments.update({key: int(cluster_id) for key, cluster_id in zip(keys[offset:offset + batch_size], assigned)})
    return assignments


def scalable_clusters_from_blobs(
    items: Iterable[tuple[str, int, bytes]],
    item_count: int,
    dimensions: int,
    cluster_count: int | None = None,
) -> tuple[list[str], Any, dict[str, int]]:
    """Project float32 SQLite BLOBs incrementally before clustering.

    Only the bounded 256-dimensional projection is retained in memory. The
    original 4096-dimensional vectors never become Python float objects.
    """
    if not item_count:
        return [], None, {}
    try:
        import numpy as np
    except ImportError as exc:
        raise DataError("numpy is required for production-scale clustering") from exc
    if dimensions < 1:
        raise DataError("embedding dimensions are invalid")
    target_dimensions = min(256, dimensions)
    padded_dimensions = math.ceil(dimensions / target_dimensions) * target_dimensions
    group_width = padded_dimensions // target_dimensions
    signs = np.where(np.arange(group_width) % 2, -1.0, 1.0).astype(np.float32)
    projected = np.empty((item_count, target_dimensions), dtype=np.float32)
    keys: list[str] = []
    for index, (key, row_dimensions, blob) in enumerate(items):
        if index >= item_count or row_dimensions != dimensions:
            raise DataError("embedding dimensions are inconsistent")
        vector = np.frombuffer(blob, dtype="<f4")
        if vector.size != dimensions:
            raise DataError(f"embedding BLOB length is invalid: {key}")
        if padded_dimensions == dimensions:
            padded = vector
        else:
            padded = np.pad(vector, (0, padded_dimensions - dimensions))
        projected[index] = (padded.reshape(target_dimensions, group_width) * signs).sum(axis=1)
        keys.append(key)
    if len(keys) != item_count:
        raise DataError(f"embedding count changed during clustering: {len(keys)}/{item_count}")
    norms = np.linalg.norm(projected, axis=1, keepdims=True)
    projected /= np.maximum(norms, np.float32(1e-12))
    if item_count <= 500:
        assignments = deterministic_clusters(
            {key: projected[index].tolist() for index, key in enumerate(keys)},
            cluster_count,
        )
        return keys, projected, assignments
    k = min(item_count, cluster_count or max(1, round(math.sqrt(item_count))))
    rng = np.random.default_rng(0)
    initial = np.sort(rng.choice(item_count, size=k, replace=False))
    centroids = projected[initial].copy()
    counts = np.zeros(k, dtype=np.int64)
    batch_size = min(1024, max(128, item_count // max(1, k)))
    for _ in range(8):
        for offset in range(0, item_count, batch_size):
            batch = projected[offset:offset + batch_size]
            assigned = np.argmax(batch @ centroids.T, axis=1)
            for cluster_id in np.unique(assigned):
                members = batch[assigned == cluster_id]
                previous = counts[cluster_id]
                current = len(members)
                centroids[cluster_id] = (
                    centroids[cluster_id] * previous + members.sum(axis=0)
                ) / max(1, previous + current)
                counts[cluster_id] += current
            centroid_norms = np.linalg.norm(centroids, axis=1, keepdims=True)
            centroids /= np.maximum(centroid_norms, np.float32(1e-12))
    assigned_all: dict[str, int] = {}
    for offset in range(0, item_count, batch_size):
        assigned = np.argmax(projected[offset:offset + batch_size] @ centroids.T, axis=1)
        assigned_all.update(
            {
                key: int(cluster_id)
                for key, cluster_id in zip(keys[offset:offset + batch_size], assigned)
            }
        )
    return keys, projected, assigned_all


def projected_centroids(
    keys: list[str], matrix: Any, assignments: dict[str, int]
) -> dict[int, list[float]]:
    try:
        import numpy as np
    except ImportError as exc:
        raise DataError("numpy is required for production-scale clustering") from exc
    output: dict[int, list[float]] = {}
    by_cluster: dict[int, list[int]] = {}
    for index, key in enumerate(keys):
        by_cluster.setdefault(assignments[key], []).append(index)
    for cluster_id, indexes in by_cluster.items():
        centroid = matrix[np.asarray(indexes, dtype=np.int64)].mean(axis=0)
        norm = float(np.linalg.norm(centroid))
        if norm:
            centroid /= norm
        output[cluster_id] = centroid.astype(np.float32).tolist()
    return output


def project_reference_vector(values: list[float], target_dimensions: int) -> list[float]:
    try:
        import numpy as np
    except ImportError as exc:
        raise DataError("numpy is required for production-scale clustering") from exc
    vector = np.asarray(values, dtype=np.float32)
    padded_dimensions = math.ceil(len(vector) / target_dimensions) * target_dimensions
    if padded_dimensions != len(vector):
        vector = np.pad(vector, (0, padded_dimensions - len(vector)))
    group_width = padded_dimensions // target_dimensions
    signs = np.where(np.arange(group_width) % 2, -1.0, 1.0).astype(np.float32)
    projected = (vector.reshape(target_dimensions, group_width) * signs).sum(axis=1)
    norm = float(np.linalg.norm(projected))
    if norm:
        projected /= norm
    return projected.tolist()


def normalized_mean(vectors: Iterable[list[float]]) -> list[float]:
    """Return the unit-normalized mean used as a cosine cluster centroid."""
    rows = list(vectors)
    if not rows:
        raise ValueError("cannot calculate an empty centroid")
    dimensions = {len(row) for row in rows}
    if len(dimensions) != 1 or not next(iter(dimensions)):
        raise ValueError("centroid vectors have inconsistent dimensions")
    normalized: list[list[float]] = []
    for row in rows:
        norm = math.sqrt(sum(value * value for value in row))
        normalized.append([value / norm for value in row] if norm else [0.0] * len(row))
    mean = [sum(row[index] for row in normalized) / len(normalized) for index in range(len(normalized[0]))]
    norm = math.sqrt(sum(value * value for value in mean))
    return [value / norm for value in mean] if norm else mean


def cluster_centroids(items: dict[str, list[float]], assignments: dict[str, int]) -> dict[int, list[float]]:
    grouped: dict[int, list[list[float]]] = {}
    for key, cluster_id in assignments.items():
        grouped.setdefault(cluster_id, []).append(items[key])
    return {cluster_id: normalized_mean(vectors) for cluster_id, vectors in grouped.items()}


def semantic_cluster_order(centroids: dict[int, list[float]]) -> list[int]:
    """Linearize an average-linkage cosine hierarchy while preserving close neighbours.

    Embedding space has no intrinsic left or right edge.  This deterministic leaf
    seriation chooses the orientation of every hierarchy branch that minimizes
    adjacent cosine distance; callers may reverse the final list for presentation.
    """
    keys = sorted(centroids)
    if len(keys) < 2:
        return keys
    pair_distance = {
        tuple(sorted((left, right))): cosine_distance(centroids[left], centroids[right])
        for index, left in enumerate(keys) for right in keys[index + 1:]
    }

    def distance(left: int, right: int) -> float:
        return 0.0 if left == right else pair_distance[tuple(sorted((left, right)))]

    nodes: list[tuple[tuple[int, ...], tuple | None]] = [((key,), None) for key in keys]
    while len(nodes) > 1:
        candidates = []
        for left_index, (left_leaves, _) in enumerate(nodes):
            for right_index in range(left_index + 1, len(nodes)):
                right_leaves = nodes[right_index][0]
                average = sum(distance(a, b) for a in left_leaves for b in right_leaves) / (len(left_leaves) * len(right_leaves))
                candidates.append((average, tuple(sorted(left_leaves + right_leaves)), left_index, right_index))
        _, merged_leaves, left_index, right_index = min(candidates)
        left_node, right_node = nodes[left_index], nodes[right_index]
        merged = (merged_leaves, (left_node, right_node))
        nodes = [node for index, node in enumerate(nodes) if index not in {left_index, right_index}]
        nodes.append(merged)
        nodes.sort(key=lambda node: node[0])

    def ordered(node: tuple[tuple[int, ...], tuple | None]) -> tuple[int, ...]:
        leaves, children = node
        if children is None:
            return leaves
        left = ordered(children[0])
        right = ordered(children[1])
        variants = {
            left + right, left[::-1] + right, left + right[::-1], left[::-1] + right[::-1],
            right + left, right[::-1] + left, right + left[::-1], right[::-1] + left[::-1],
        }
        return min(variants, key=lambda order: (sum(distance(a, b) for a, b in zip(order, order[1:])), order))

    return list(ordered(nodes[0]))


def relative_proximity(reference: list[float], centroids: dict[int, list[float]]) -> dict[int, dict[str, float]]:
    similarities = {cluster_id: 1.0 - cosine_distance(reference, centroid) for cluster_id, centroid in centroids.items()}
    if not similarities:
        return {}
    low, high = min(similarities.values()), max(similarities.values())
    spread = high - low
    return {
        cluster_id: {
            "cosine_similarity": round(similarity, 6),
            "relative_strength": round((similarity - low) / spread, 6) if spread > 1e-12 else 1.0,
        }
        for cluster_id, similarity in similarities.items()
    }


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
        if self.research.get("lifecycle", {}).get("status", "active") == "archived":
            raise DataError("アーカイブ済みリサーチは分析できません")
        self.research["company_technology"] = read_company_technology(self.research_dir, self.research)
        self.documents, self.input_audit = research_patents(self.research_dir, self.root / "patent_pool")
        self.legacy_locations = {
            patent_key(str(patent.get("pdf", ""))): str(patent["_legacy_subresearch_id"])
            for patent in self.documents if patent.get("_legacy_subresearch_id")
        }
        self.cache = SharedExtractionCache(self.root / "runtime/shared/extractions")
        self.store = SQLiteStore(self.root, environment)

    def patents(self) -> list[tuple[Path, dict[str, Any]]]:
        output: list[tuple[Path, dict[str, Any]]] = []
        seen: set[str] = set()
        for patent in self.documents:
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
            output.append((pdf_path, patent))
        return output

    def artifact_dir(self, pdf_name: str) -> Path:
        return self.research_dir / "pipeline" / patent_key(pdf_name)

    def persist_status_artifact(
        self, pdf_name: str, artifact_type: str, payload: dict[str, Any]
    ) -> None:
        key = patent_key(pdf_name)
        self.store.put_artifact(
            self.research_id,
            key,
            artifact_type,
            payload,
            source_path=str(
                (self.artifact_dir(pdf_name) / f"{artifact_type}.json").relative_to(
                    self.root
                )
            ),
        )
        if artifact_type == "skip":
            self.store.set_document_state(
                self.research_id,
                key,
                "skipped",
                skip_reason=str(payload.get("reason") or ""),
                skip_detail=str(payload.get("detail") or ""),
            )
        elif artifact_type == "analysis_error":
            self.store.set_document_state(self.research_id, key, "invalid")

    def _legacy_artifact_dir(self, pdf_name: str) -> Path | None:
        legacy_id = self.legacy_locations.get(patent_key(pdf_name))
        return self.research_dir / "subresearches" / legacy_id / "pipeline" / patent_key(pdf_name) if legacy_id else None

    def _read_artifact_dir(self, pdf_name: str) -> Path:
        current = self.artifact_dir(pdf_name)
        legacy = self._legacy_artifact_dir(pdf_name)
        return current if current.exists() or not legacy or not legacy.exists() else legacy

    def result_path(self, pdf_name: str) -> Path:
        return self.research_dir / "results" / f"{patent_key(pdf_name)}.json"

    def existing_result_path(self, pdf_name: str) -> Path:
        current = self.result_path(pdf_name)
        legacy_id = self.legacy_locations.get(patent_key(pdf_name))
        legacy = self.research_dir / "subresearches" / str(legacy_id) / "results" / f"{patent_key(pdf_name)}.json"
        return current if current.exists() or not legacy_id or not legacy.exists() else legacy

    def analysis_retry_required(self, pdf_name: str) -> bool:
        error_path = self._read_artifact_dir(pdf_name) / "analysis_error.json"
        if not error_path.is_file():
            return False
        try:
            error = read_json(error_path)
        except DataError:
            return True
        return error.get("retry_on_next_run", True) is not False

    def manual_review_required(self, pdf_name: str) -> bool:
        skip_path = self._read_artifact_dir(pdf_name) / "skip.json"
        if not skip_path.is_file():
            return False
        try:
            skip = read_json(skip_path)
        except DataError:
            return False
        return skip.get("reason") == "manual_review_required"

    def result_exists(self, pdf_name: str) -> bool:
        if self.analysis_retry_required(pdf_name):
            return False
        return self.store.analysis_result_exists(
            self.research_id, patent_key(pdf_name)
        ) or self.existing_result_path(pdf_name).exists()

    def load_analysis_artifacts(self, pdf_name: str) -> dict[str, Any]:
        """Reload completed stage artifacts without invoking an LLM again."""
        artifact = self._read_artifact_dir(pdf_name)
        key = patent_key(pdf_name)
        stored_score = self.store.get_artifact(self.research_id, key, "threat_score")
        stored_summaries = self.store.get_artifact(self.research_id, key, "summaries")
        stored_embedding = self.store.get_embedding(self.research_id, key)
        if stored_score and stored_summaries and stored_embedding:
            return {
                "artifact_dir": artifact,
                "score": stored_score,
                "summaries": stored_summaries,
                "embedding": stored_embedding,
            }
        required = {
            "score": "threat_score.json",
            "summaries": "summaries.json",
            "embedding": "embeddings.json",
        }
        missing = [filename for filename in required.values() if not (artifact / filename).is_file()]
        if missing:
            raise DataError(f"処理済み結果の段階成果物が不足しています: {patent_key(pdf_name)} ({', '.join(missing)})")
        return {"artifact_dir": artifact, **{key: read_json(artifact / filename) for key, filename in required.items()}}

    def load_prepared_document(self, pdf_name: str) -> dict[str, Any]:
        """Reload preparation artifacts so production shards need not retain document text in RAM."""
        artifact = self._read_artifact_dir(pdf_name)
        source_decision = read_json(artifact / "source_decision.json")
        cache_dir = (self.root / source_decision["shared_cache"]).resolve()
        if not (cache_dir / "extracted_text.txt").is_file():
            raise DataError(f"prepared extraction cache is missing: {patent_key(pdf_name)}")
        shared = self._load_or_build_shared_preprocessing(
            cache_dir, source_decision, pdf_name
        )
        structure = shared["structure"]
        structured = shared["structured_document"]
        claim_structure = shared["claim_structure"]
        if not structure.get("claim_validation", {}).get("valid"):
            errors = structure.get("claim_validation", {}).get("errors", [])
            raise DocumentSkipped("invalid_claim_structure", ", ".join(errors))
        evidence_path = artifact / "evidence_pack.json"
        evidence_current = False
        if evidence_path.is_file():
            try:
                evidence_pack = read_json(evidence_path)
                evidence_current = (
                    evidence_pack.get("schema_version") == EVIDENCE_PACK_VERSION
                    and evidence_pack.get("reading_policy")
                    == str(source_decision.get("reading_policy") or "adaptive")
                    and evidence_pack.get("company_technology_sha256")
                    == sha256_bytes(
                        self.research.get("company_technology", "").encode(
                            "utf-8"
                        )
                    )
                    and evidence_pack.get("structured_document_sha256")
                    == self._structured_document_sha256(structured)
                    and evidence_pack.get("claim_structure_sha256")
                    == self._claim_structure_sha256(claim_structure)
                )
            except DataError:
                evidence_current = False
        if not evidence_current:
            evidence_pack = self._build_research_evidence_pack(
                cache_dir,
                artifact,
                source_decision,
                structured,
                claim_structure,
            )
        self._write_research_structure_compatibility(
            artifact, structure, structured, claim_structure
        )
        return {
            "artifact_dir": artifact,
            "cache_dir": cache_dir,
            "source_decision": source_decision,
            "structure": structure,
            "structured_document": structured,
            "claim_structure": claim_structure,
            "evidence_pack": evidence_pack,
            "shared_preprocessing": shared,
        }

    def analysis_checkpoint_current(self, pdf_name: str) -> bool:
        if self.analysis_retry_required(pdf_name):
            return False
        stored = self.store.latest_artifact(
            self.research_id, patent_key(pdf_name), "analysis_complete"
        )
        if stored is not None:
            return stored.get("pipeline_version") == ANALYSIS_PIPELINE_VERSION
        checkpoint = self._read_artifact_dir(pdf_name) / "analysis_complete.json"
        if not checkpoint.is_file():
            return False
        try:
            return read_json(checkpoint).get("pipeline_version") == ANALYSIS_PIPELINE_VERSION
        except DataError:
            return False

    def overview(self, compact: bool = False) -> dict[str, Any]:
        if compact:
            retryable_ids = {
                patent_key(str(patent.get("pdf", "")))
                for patent in self.documents
                if self.analysis_retry_required(str(patent.get("pdf", "")))
            }
            return {
                "research_id": self.research_id,
                "environment": self.environment,
                "source_policy": self.research.get("pipeline", {}).get("source_policy", {}),
                "threat_map": self.research.get("pipeline", {}).get("threat_scoring", {}),
                "input": self.input_audit,
                "analysis_stale": bool(self.research.get("lifecycle", {}).get("analysis_stale", False)),
                "stages": list(PIPELINE_STAGES),
                "documents": [],
                "documents_omitted": True,
                "counts": self.store.pipeline_counts(
                    self.research_id, retryable_ids
                ),
            }
        documents = self.patents()
        items = []
        for pdf_path, patent in documents:
            artifact = self._read_artifact_dir(pdf_path.name)
            skip_path = artifact / "skip.json"
            skip = read_json(skip_path) if skip_path.exists() else {}
            error_path = artifact / "analysis_error.json"
            analysis_error = read_json(error_path) if error_path.exists() else {}
            if not pdf_path.is_file():
                skip = {"reason": "pdf_not_found", "detail": f"PDFがpatent_poolにありません: {pdf_path.name}"}
            files = {stage: (artifact / filename).exists() for stage, filename in {
                "source": "source_decision.json", "structure": "document_structure.json",
                "similarity": "similarity.json", "concept_level": "concept_level.json",
                "threat_score": "threat_score.json", "summaries": "summaries.json",
            }.items()}
            files["embeddings"] = self.store.embedding_exists(
                self.research_id, patent_key(pdf_path.name)
            ) or (artifact / "embeddings.json").exists()
            checkpoint = self.analysis_checkpoint_current(pdf_path.name)
            result_exists = self.result_exists(pdf_path.name)
            retry_required = bool(analysis_error) and self.analysis_retry_required(
                pdf_path.name
            )
            items.append({
                "patent_id": patent_key(pdf_path.name), "pdf": pdf_path.name,
                "source_policy": SourcePolicy.from_values(self.research, patent).__dict__, "stages": files, "finalized": bool(not skip and result_exists and not retry_required),
                "analysis_checkpoint": checkpoint,
                "analysis_error": analysis_error,
                "pdf_available": pdf_path.is_file(), "analysis_state": "skipped" if skip else ("pending" if retry_required else ("ready" if result_exists else "pending")),
                "skip_reason": skip.get("reason"), "skip_detail": skip.get("detail"),
            })
        return {
            "research_id": self.research_id,
            "environment": self.environment,
            "source_policy": self.research.get("pipeline", {}).get("source_policy", {}),
            "threat_map": self.research.get("pipeline", {}).get("threat_scoring", {}),
            "input": self.input_audit,
            "analysis_stale": bool(self.research.get("lifecycle", {}).get("analysis_stale", False)),
            "stages": list(PIPELINE_STAGES),
            "documents": items,
            "counts": {
                "total": len(items),
                "available": sum(item["pdf_available"] for item in items),
                "prepared": sum(item["stages"]["structure"] for item in items),
                "analyzed": sum(item["stages"]["threat_score"] and item["stages"]["summaries"] and not item["analysis_error"] for item in items),
                "finalized": sum(item["finalized"] for item in items),
                "pending": sum(item["analysis_state"] == "pending" for item in items),
                "llm_pending": sum(item["analysis_state"] == "pending" and not item["analysis_checkpoint"] for item in items),
                "skipped": sum(item["analysis_state"] == "skipped" for item in items),
                "failed": sum(
                    bool(item["analysis_error"])
                    and item["analysis_state"] != "skipped"
                    for item in items
                ),
            },
        }

    def mark_analysis_current(self) -> None:
        config = read_research_config(self.research_dir)
        lifecycle = dict(config.get("lifecycle", {}))
        lifecycle["analysis_stale"] = False
        lifecycle["updated_at"] = utc_now()
        lifecycle.setdefault("status", "active")
        lifecycle.setdefault("created_at", lifecycle["updated_at"])
        config["schema_version"] = max(2, int(config.get("schema_version", 0)))
        config["lifecycle"] = lifecycle
        atomic_json(self.research_dir / "research.json", config)

    @staticmethod
    def _structured_document_sha256(structured: dict[str, Any]) -> str:
        return sha256_bytes(
            json.dumps(
                structured.get("paragraphs", []),
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        )

    @staticmethod
    def _claim_structure_sha256(claim_structure: dict[str, Any]) -> str:
        return sha256_bytes(
            json.dumps(
                claim_structure.get("claims", []),
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        )

    @staticmethod
    def _shared_preprocessing_paths(cache_dir: Path) -> dict[str, Path]:
        return {
            "manifest": cache_dir / "preprocessing_manifest.json",
            "document_structure": cache_dir / "document_structure.json",
            "structured_document": cache_dir / "structured_document.json",
            "claim_structure": cache_dir / "claim_structure.json",
        }

    def _load_or_build_shared_preprocessing(
        self,
        cache_dir: Path,
        source_decision: dict[str, Any],
        pdf_name: str,
    ) -> dict[str, Any]:
        """Load PDF-only preprocessing from the content-addressed shared cache."""
        paths = self._shared_preprocessing_paths(cache_dir)
        text = (cache_dir / "extracted_text.txt").read_text(encoding="utf-8")
        text_sha256 = sha256_bytes(text.encode("utf-8"))
        pdf_sha256 = str(source_decision.get("pdf_sha256") or cache_dir.name)
        if all(path.is_file() for path in paths.values()):
            try:
                manifest = read_json(paths["manifest"])
                structure = read_json(paths["document_structure"])
                structured = read_json(paths["structured_document"])
                claim_structure = read_json(paths["claim_structure"])
                current = (
                    manifest.get("preprocessing_version")
                    == SHARED_PREPROCESSING_VERSION
                    and manifest.get("pdf_sha256") == pdf_sha256
                    and manifest.get("text_sha256") == text_sha256
                    and structure.get("schema_version") == CLAIM_STRUCTURE_VERSION
                    and structured.get("schema_version")
                    == STRUCTURED_DOCUMENT_VERSION
                    and claim_structure.get("schema_version")
                    == CLAIM_STRUCTURE_VERSION
                    and manifest.get("structured_document_sha256")
                    == self._structured_document_sha256(structured)
                    and manifest.get("claim_structure_sha256")
                    == self._claim_structure_sha256(claim_structure)
                )
                if current:
                    return {
                        "cache_decision": "reused",
                        "manifest": manifest,
                        "structure": structure,
                        "structured_document": structured,
                        "claim_structure": claim_structure,
                    }
            except DataError:
                pass

        pages = read_json(cache_dir / "pages.json")
        derived_text, _ = strip_patent_page_headers(text)
        sections = split_sections(derived_text)
        claims, claim_validation = extract_claims_with_metadata(derived_text)
        claim_structure = enrich_claim_structure(claims)
        enriched_claims = claim_structure["claims"]
        structure = {
            "schema_version": CLAIM_STRUCTURE_VERSION,
            "pdf": pdf_name,
            "pdf_sha256": pdf_sha256,
            "claims": enriched_claims,
            "claim_validation": claim_validation,
            "sections": {
                key: value for key, value in sections.items() if key != "claims"
            },
            "section_characters": {
                **{
                    key: len(value)
                    for key, value in sections.items()
                    if key != "claims"
                },
                "claims": sum(len(item["text"]) for item in enriched_claims),
            },
            "created_at": utc_now(),
        }
        structured = build_structured_document(
            pages,
            pdf_name=pdf_name,
            pdf_sha256=pdf_sha256,
        )
        manifest = {
            "schema_version": 1,
            "preprocessing_version": SHARED_PREPROCESSING_VERSION,
            "pdf_sha256": pdf_sha256,
            "text_sha256": text_sha256,
            "structured_document_version": STRUCTURED_DOCUMENT_VERSION,
            "claim_structure_version": CLAIM_STRUCTURE_VERSION,
            "structured_document_sha256": self._structured_document_sha256(
                structured
            ),
            "claim_structure_sha256": self._claim_structure_sha256(
                claim_structure
            ),
            "artifacts": {
                key: path.name for key, path in paths.items() if key != "manifest"
            },
            "created_at": utc_now(),
        }
        atomic_json(paths["document_structure"], structure)
        atomic_json(paths["structured_document"], structured)
        atomic_json(paths["claim_structure"], claim_structure)
        # The manifest is the commit marker and must be written last.
        atomic_json(paths["manifest"], manifest)
        return {
            "cache_decision": "created",
            "manifest": manifest,
            "structure": structure,
            "structured_document": structured,
            "claim_structure": claim_structure,
        }

    def _write_research_structure_compatibility(
        self,
        artifact: Path,
        structure: dict[str, Any],
        structured: dict[str, Any],
        claim_structure: dict[str, Any],
    ) -> None:
        """Keep legacy per-research paths while shared cache remains authoritative."""
        outputs = {
            "document_structure": (structure, artifact / "document_structure.json"),
            "structured_document": (
                structured,
                artifact / "structured_document.json",
            ),
            "claim_structure": (
                claim_structure,
                artifact / "claim_structure.json",
            ),
        }
        for artifact_type, (payload, path) in outputs.items():
            atomic_json(path, payload)
            self.store.put_artifact(
                self.research_id,
                artifact.name,
                artifact_type,
                payload,
                source_path=str(path.relative_to(self.root)),
            )

    def _build_research_evidence_pack(
        self,
        cache_dir: Path,
        artifact: Path,
        source_decision: dict[str, Any],
        structured: dict[str, Any],
        claim_structure: dict[str, Any],
    ) -> dict[str, Any]:
        text = (cache_dir / "extracted_text.txt").read_text(encoding="utf-8")
        evidence_pack = build_evidence_pack(
            structured,
            claim_structure,
            self.research.get("company_technology", ""),
            reading_policy=str(
                source_decision.get("reading_policy") or "adaptive"
            ),
            fallback_text=text,
        )
        path = artifact / "evidence_pack.json"
        atomic_json(path, evidence_pack)
        self.store.put_artifact(
            self.research_id,
            artifact.name,
            "evidence_pack",
            evidence_pack,
            source_path=str(path.relative_to(self.root)),
        )
        return evidence_pack

    def _build_preprocessing_artifacts(
        self,
        cache_dir: Path,
        artifact: Path,
        source_decision: dict[str, Any],
        pdf_name: str,
    ) -> dict[str, Any]:
        shared = self._load_or_build_shared_preprocessing(
            cache_dir, source_decision, pdf_name
        )
        structure = shared["structure"]
        structured = shared["structured_document"]
        claim_structure = shared["claim_structure"]
        claim_validation = structure.get("claim_validation", {})
        if not claim_validation["valid"]:
            raise DocumentSkipped(
                "invalid_claim_structure",
                ", ".join(claim_validation.get("errors", [])),
            )
        evidence_pack = self._build_research_evidence_pack(
            cache_dir,
            artifact,
            source_decision,
            structured,
            claim_structure,
        )
        self._write_research_structure_compatibility(
            artifact, structure, structured, claim_structure
        )
        return {
            "artifact_dir": artifact,
            "cache_dir": cache_dir,
            "source_decision": source_decision,
            "structure": structure,
            "structured_document": structured,
            "claim_structure": claim_structure,
            "evidence_pack": evidence_pack,
            "shared_preprocessing": shared,
        }

    def prepare_document(self, pdf_path: Path, patent: dict[str, Any]) -> dict[str, Any]:
        policy = SourcePolicy.from_values(self.research, patent)
        cache_dir, extraction = self.cache.obtain(pdf_path, policy.extraction)
        artifact = self.artifact_dir(pdf_path.name)
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
        self.store.put_artifact(
            self.research_id, artifact.name, "source_decision", source_decision,
            source_path=str((artifact / "source_decision.json").relative_to(self.root)),
        )
        return self._build_preprocessing_artifacts(
            cache_dir, artifact, source_decision, pdf_path.name
        )

    def write_run_manifest(self, run_id: str, status: str, stages: Iterable[str], detail: dict[str, Any] | None = None) -> Path:
        unknown = set(stages) - set(PIPELINE_STAGES)
        if unknown:
            raise DataError(f"unknown pipeline stages: {sorted(unknown)}")
        path = self.research_dir / "runs" / run_id / "run_manifest.json"
        payload = {
            "schema_version": 1,
            "run_id": run_id,
            "environment": self.environment,
            "research_id": self.research_id,
            "status": status,
            "stages": list(stages),
            "detail": {"input": self.input_audit, **(detail or {})},
            "updated_at": utc_now(),
        }
        atomic_json(path, payload)
        self.store.put_artifact(
            self.research_id, "_research", "run_manifest", payload,
            run_id=run_id, source_path=str(path.relative_to(self.root)),
        )
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
            "algorithm": "adaptive-exact-or-minibatch-cosine",
            "cluster_count_requested": cluster_count,
            "technology_assignments": scalable_clusters(technology, cluster_count),
            "problem_assignments": scalable_clusters(problem, cluster_count),
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

    @staticmethod
    def analysis_task_file(task: str) -> str:
        files = {
            "similarity": "similarity.json",
            "concept_level": "concept_level.json",
            "problem_summary": "problem_summary.json",
            "technology_summary": "technology_summary.json",
        }
        if task not in files:
            raise ValueError(f"unknown document analysis task: {task}")
        return files[task]

    def analysis_task_current(self, prepared: dict[str, Any], task: str) -> bool:
        artifact: Path = prepared["artifact_dir"]
        progress_path = artifact / "analysis_progress.json"
        if (
            not progress_path.is_file()
            or not (artifact / self.analysis_task_file(task)).is_file()
            or not (artifact / f"{task}_grounding.json").is_file()
        ):
            return False
        try:
            progress = read_json(progress_path)
        except DataError:
            return False
        return (
            progress.get("pipeline_version") == ANALYSIS_PIPELINE_VERSION
            and task in progress.get("completed_tasks", [])
        )

    @staticmethod
    def analysis_cache_summary(
        prepared: dict[str, Any],
    ) -> dict[str, Any]:
        artifact: Path = prepared["artifact_dir"]
        try:
            progress = read_json(artifact / "analysis_progress.json")
        except DataError:
            progress = {}
        try:
            embedding_manifest = read_json(
                artifact / "embedding_manifest.json"
            )
        except DataError:
            embedding_manifest = {}
        task_cache = progress.get("shared_cache", {})
        return {
            "tasks": task_cache,
            "task_hits": sorted(
                task
                for task, status in task_cache.items()
                if status.get("hit") is True
            ),
            "task_writes": sorted(
                task
                for task, status in task_cache.items()
                if status.get("hit") is False
            ),
            "embedding": embedding_manifest.get("shared_cache"),
        }

    @staticmethod
    def _task_reported_evidence_ids(
        task: str, response: dict[str, Any]
    ) -> Any:
        return response.get(
            {
                "similarity": "evidence_element_ids",
                "concept_level": "evidence_element_ids",
                "problem_summary": "source_paragraph_ids",
                "technology_summary": "source_ids",
            }[task]
        )

    @staticmethod
    def _task_allowed_evidence_ids(
        prepared: dict[str, Any], task: str
    ) -> list[str]:
        sources = (
            prepared.get("evidence_pack", {})
            .get("tasks", {})
            .get(task, {})
            .get("sources", [])
        )
        element_ids = {
            str(element_id)
            for source in sources
            if source.get("source_type") == "claim"
            for element_id in source.get("element_ids", [])
        }
        paragraph_ids = {
            str(source.get("paragraph_id"))
            for source in sources
            if source.get("source_type") == "paragraph"
            and source.get("paragraph_id")
        }
        if task in {"similarity", "concept_level"}:
            return sorted(element_ids)
        if task == "problem_summary":
            return sorted(paragraph_ids)
        return sorted(element_ids | paragraph_ids)

    @staticmethod
    def _task_allowed_claim_numbers(
        prepared: dict[str, Any], task: str
    ) -> list[int]:
        if task not in {"similarity", "concept_level"}:
            return []
        claims = prepared.get("structure", {}).get("claims", [])
        eligible = [
            claim for claim in claims
            if claim.get("type") == "independent"
        ] or claims[:1]
        return list(dict.fromkeys(
            claim["number"]
            for claim in eligible
            if type(claim.get("number")) is int
        ))

    def _validate_task_grounding(
        self,
        prepared: dict[str, Any],
        task: str,
        response: dict[str, Any],
    ) -> dict[str, Any]:
        evidence = verify_evidence_references(
            prepared,
            task,
            self._task_reported_evidence_ids(task, response),
        )
        validation: dict[str, Any] = {
            "task": task,
            "verified": evidence["verified"],
            "evidence": evidence,
        }
        if task in {"similarity", "concept_level"}:
            claim = verify_claim_reference(
                prepared["structure"],
                response.get("claim_number"),
                task,
            )
            validation["claim"] = claim
            validation["verified"] = (
                validation["verified"] and claim["verified"]
            )
        return validation

    @staticmethod
    def _task_response_shape_valid(
        task: str, response: Any
    ) -> bool:
        if not isinstance(response, dict):
            return False
        if task == "concept_level":
            return (
                type(response.get("concept_level")) is int
                and 1 <= response["concept_level"] <= 5
                and isinstance(response.get("reason"), str)
                and bool(response["reason"].strip())
                and type(response.get("claim_number")) is int
                and isinstance(response.get("evidence_element_ids"), list)
                and all(
                    isinstance(item, str)
                    for item in response["evidence_element_ids"]
                )
            )
        if task == "problem_summary":
            return (
                isinstance(response.get("problem_summary"), str)
                and bool(response["problem_summary"].strip())
                and isinstance(response.get("source_paragraph_ids"), list)
                and all(
                    isinstance(item, str)
                    for item in response["source_paragraph_ids"]
                )
            )
        if task == "technology_summary":
            return (
                isinstance(response.get("tech_summary"), str)
                and bool(response["tech_summary"].strip())
                and isinstance(response.get("source_ids"), list)
                and all(
                    isinstance(item, str)
                    for item in response["source_ids"]
                )
            )
        return False

    def _shared_task_cache_descriptor(
        self,
        prepared: dict[str, Any],
        task: str,
        task_data: dict[str, Any],
        cache_identity: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if (
            task not in SHARED_ANALYSIS_TASKS
            or not isinstance(cache_identity, dict)
        ):
            return None
        patent_text = task_data.get("patent_text")
        constraints = task_data.get("grounding_constraints")
        if not isinstance(patent_text, str) or not isinstance(
            constraints, dict
        ):
            return None
        shared_manifest = prepared.get("shared_preprocessing", {}).get(
            "manifest", {}
        )
        key_material = {
            "cache_version": SHARED_SEMANTIC_CACHE_VERSION,
            "environment": self.environment,
            "task": task,
            "pdf_sha256": prepared.get("source_decision", {}).get(
                "pdf_sha256"
            ),
            "preprocessing_version": shared_manifest.get(
                "preprocessing_version"
            ),
            "structured_document_sha256": shared_manifest.get(
                "structured_document_sha256"
            ),
            "claim_structure_sha256": shared_manifest.get(
                "claim_structure_sha256"
            ),
            "evidence_pack_version": EVIDENCE_PACK_VERSION,
            "reading_policy": prepared.get("source_decision", {}).get(
                "reading_policy"
            ),
            "input_sha256": sha256_bytes(patent_text.encode("utf-8")),
            "grounding_constraints": constraints,
            "generator": cache_identity,
        }
        canonical = json.dumps(
            key_material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fingerprint = sha256_bytes(canonical)
        path = (
            prepared["cache_dir"]
            / SHARED_SEMANTIC_CACHE_VERSION
            / "tasks"
            / task
            / cache_record_filename(fingerprint)
        )
        return {
            "fingerprint": fingerprint,
            "key_material": key_material,
            "path": path,
        }

    def _load_shared_task_cache(
        self,
        prepared: dict[str, Any],
        task: str,
        descriptor: dict[str, Any] | None,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        if not descriptor or not descriptor["path"].is_file():
            return None
        try:
            record = read_json(descriptor["path"])
        except DataError:
            return None
        if (
            record.get("cache_version") != SHARED_SEMANTIC_CACHE_VERSION
            or record.get("fingerprint") != descriptor["fingerprint"]
        ):
            return None
        response = record.get("response")
        if not self._task_response_shape_valid(task, response):
            return None
        grounding = self._validate_task_grounding(
            prepared, task, response
        )
        if not grounding["verified"]:
            return None
        return response, {
            "schema_version": 1,
            "task": task,
            "verified": True,
            "attempt_count": 0,
            "attempts": [],
            "shared_cache": {
                "hit": True,
                "fingerprint": descriptor["fingerprint"],
                "source_created_at": record.get("created_at"),
                "path": str(descriptor["path"].relative_to(self.root)),
            },
            "validation": grounding,
            "updated_at": utc_now(),
        }

    def _commit_analysis_task(
        self,
        prepared: dict[str, Any],
        task: str,
        response: dict[str, Any],
        grounding_record: dict[str, Any],
        cache_status: dict[str, Any] | None = None,
    ) -> None:
        artifact: Path = prepared["artifact_dir"]
        grounding_path = artifact / f"{task}_grounding.json"
        atomic_json(grounding_path, grounding_record)
        self.store.put_artifact(
            self.research_id,
            artifact.name,
            f"{task}_grounding",
            grounding_record,
            source_path=str(grounding_path.relative_to(self.root)),
        )
        task_path = artifact / self.analysis_task_file(task)
        atomic_json(task_path, response)
        self.store.put_artifact(
            self.research_id,
            artifact.name,
            task,
            response,
            source_path=str(task_path.relative_to(self.root)),
        )
        progress_path = artifact / "analysis_progress.json"
        try:
            progress = (
                read_json(progress_path) if progress_path.exists() else {}
            )
        except DataError:
            progress = {}
        if progress.get("pipeline_version") != ANALYSIS_PIPELINE_VERSION:
            progress = {}
        completed = set(progress.get("completed_tasks", []))
        completed.add(task)
        shared_cache = dict(progress.get("shared_cache", {}))
        if cache_status is not None:
            shared_cache[task] = cache_status
        progress_record = {
            "schema_version": 1,
            "pipeline_version": ANALYSIS_PIPELINE_VERSION,
            "completed_tasks": sorted(completed),
            "shared_cache": shared_cache,
            "updated_at": utc_now(),
        }
        atomic_json(progress_path, progress_record)
        self.store.put_artifact(
            self.research_id,
            artifact.name,
            "analysis_progress",
            progress_record,
            source_path=str(progress_path.relative_to(self.root)),
        )

    def _analysis_task_data(self, prepared: dict[str, Any]) -> dict[str, dict[str, Any]]:
        artifact: Path = prepared["artifact_dir"]
        text = (prepared["cache_dir"] / "extracted_text.txt").read_text(encoding="utf-8")
        evidence_pack = prepared.get("evidence_pack")
        if not isinstance(evidence_pack, dict) or evidence_pack.get("schema_version") != EVIDENCE_PACK_VERSION:
            structure = prepared["structure"]
            claims = structure.get("claims", [])
            if claims and not isinstance(claims[0].get("elements"), list):
                claim_structure = enrich_claim_structure(claims)
            else:
                claim_structure = {
                    "schema_version": CLAIM_STRUCTURE_VERSION,
                    "claims": claims,
                    "statistics": {
                        "claims": len(claims),
                        "independent_claims": sum(item.get("type") == "independent" for item in claims),
                        "elements": sum(len(item.get("elements", [])) for item in claims),
                    },
                }
            structured = prepared.get("structured_document") or build_structured_document(
                [{"page": 1, "text": text}],
                pdf_name=str(prepared.get("source_decision", {}).get("pdf") or ""),
                pdf_sha256=str(prepared.get("source_decision", {}).get("pdf_sha256") or ""),
            )
            evidence_pack = build_evidence_pack(
                structured,
                claim_structure,
                self.research.get("company_technology", ""),
                reading_policy=str(prepared.get("source_decision", {}).get("reading_policy") or "adaptive"),
                fallback_text=text,
            )
            prepared["structured_document"] = structured
            prepared["claim_structure"] = claim_structure
            prepared["evidence_pack"] = evidence_pack
            atomic_json(artifact / "evidence_pack.json", evidence_pack)
        inputs = {
            task: evidence_pack["tasks"][task]["rendered_text"]
            for task in EVIDENCE_TASK_CONFIG
        }
        analysis_inputs = {
            "pipeline_version": ANALYSIS_PIPELINE_VERSION,
            "evidence_pack_version": EVIDENCE_PACK_VERSION,
            "characters": {task: len(value) for task, value in inputs.items()},
            "sha256": {task: sha256_bytes(value.encode("utf-8")) for task, value in inputs.items()},
            "sources": {
                task: evidence_pack["tasks"][task]["statistics"]
                for task in EVIDENCE_TASK_CONFIG
            },
        }
        if not prepared.get("_analysis_inputs_persisted"):
            atomic_json(artifact / "analysis_inputs.json", analysis_inputs)
            self.store.put_artifact(
                self.research_id,
                artifact.name,
                "analysis_inputs",
                analysis_inputs,
                source_path=str((artifact / "analysis_inputs.json").relative_to(self.root)),
            )
            prepared["_analysis_inputs_persisted"] = True
        return {
            "similarity": {
                "company_technology": self.research.get("company_technology", ""),
                "patent_text": inputs["similarity"],
            },
            **{
                task: {"patent_text": inputs[task]}
                for task in ("concept_level", "problem_summary", "technology_summary")
            },
        }

    def analyze_task(
        self,
        prepared: dict[str, Any],
        task: str,
        generate_json: "GenerateJson",
        *,
        cache_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        artifact: Path = prepared["artifact_dir"]
        grounding_constraints = {
            "allowed_evidence_ids": self._task_allowed_evidence_ids(
                prepared, task
            ),
        }
        if task in {"similarity", "concept_level"}:
            grounding_constraints["allowed_claim_numbers"] = (
                self._task_allowed_claim_numbers(prepared, task)
            )
        task_data = {
            **self._analysis_task_data(prepared)[task],
            "grounding_constraints": grounding_constraints,
        }
        cache_descriptor = self._shared_task_cache_descriptor(
            prepared,
            task,
            task_data,
            cache_identity,
        )
        cached = self._load_shared_task_cache(
            prepared,
            task,
            cache_descriptor,
        )
        if cached is not None:
            response, grounding_record = cached
            self._commit_analysis_task(
                prepared,
                task,
                response,
                grounding_record,
                cache_status=grounding_record["shared_cache"],
            )
            return response
        grounding_attempts: list[dict[str, Any]] = []
        response = generate_json(task, task_data)
        grounding = self._validate_task_grounding(prepared, task, response)
        grounding_attempts.append(
            {
                "attempt": 1,
                "response": response,
                "validation": grounding,
            }
        )
        if not grounding["verified"]:
            repair_data = {
                **task_data,
                "grounding_repair": {
                    "allowed_ids": self._task_allowed_evidence_ids(
                        prepared, task
                    ),
                    "previous_ids": self._task_reported_evidence_ids(
                        task, response
                    ),
                    "validation": grounding,
                },
            }
            response = generate_json(task, repair_data)
            grounding = self._validate_task_grounding(
                prepared, task, response
            )
            grounding_attempts.append(
                {
                    "attempt": 2,
                    "response": response,
                    "validation": grounding,
                }
            )
        grounding_record = {
            "schema_version": 1,
            "task": task,
            "verified": grounding["verified"],
            "attempt_count": len(grounding_attempts),
            "attempts": grounding_attempts,
            "updated_at": utc_now(),
        }
        if not grounding["verified"]:
            grounding_path = artifact / f"{task}_grounding.json"
            atomic_json(grounding_path, grounding_record)
            self.store.put_artifact(
                self.research_id,
                artifact.name,
                f"{task}_grounding",
                grounding_record,
                source_path=str(grounding_path.relative_to(self.root)),
            )
            raise ValueError(
                f"{task} grounding validation failed after "
                f"{len(grounding_attempts)} attempts"
            )

        cache_status = None
        if cache_descriptor is not None:
            cache_record = {
                "schema_version": 1,
                "cache_version": SHARED_SEMANTIC_CACHE_VERSION,
                "fingerprint": cache_descriptor["fingerprint"],
                "task": task,
                "key_material": cache_descriptor["key_material"],
                "response": response,
                "grounding": grounding,
                "created_at": utc_now(),
            }
            atomic_json(cache_descriptor["path"], cache_record)
            cache_status = {
                "hit": False,
                "fingerprint": cache_descriptor["fingerprint"],
                "path": str(
                    cache_descriptor["path"].relative_to(self.root)
                ),
            }
            grounding_record["shared_cache"] = cache_status
        self._commit_analysis_task(
            prepared,
            task,
            response,
            grounding_record,
            cache_status=cache_status,
        )
        return response

    def analysis_task_characters(self, prepared: dict[str, Any], task: str) -> int:
        return len(self._analysis_task_data(prepared)[task]["patent_text"])

    def assemble_generated_analysis(self, prepared: dict[str, Any]) -> dict[str, Any]:
        artifact: Path = prepared["artifact_dir"]
        similarity = read_json(artifact / self.analysis_task_file("similarity"))
        concept = read_json(artifact / self.analysis_task_file("concept_level"))
        problem = read_json(artifact / self.analysis_task_file("problem_summary"))
        technology = read_json(artifact / self.analysis_task_file("technology_summary"))
        score = validate_score({
            "similarity": similarity["similarity"],
            "concept_level": concept["concept_level"],
            "similarity_reason": similarity["reason"],
            "concept_level_reason": concept["reason"],
            "claim_traceability": {
                "schema_version": 1,
                "similarity": verify_claim_reference(
                    prepared["structure"],
                    similarity.get("claim_number"),
                    "similarity",
                ),
                "concept_level": verify_claim_reference(
                    prepared["structure"],
                    concept.get("claim_number"),
                    "concept_level",
                ),
            },
            "evidence_traceability": {
                "schema_version": 1,
                "similarity": verify_evidence_references(
                    prepared,
                    "similarity",
                    similarity.get("evidence_element_ids"),
                ),
                "concept_level": verify_evidence_references(
                    prepared,
                    "concept_level",
                    concept.get("evidence_element_ids"),
                ),
                "problem_summary": verify_evidence_references(
                    prepared,
                    "problem_summary",
                    problem.get("source_paragraph_ids"),
                ),
                "technology_summary": verify_evidence_references(
                    prepared,
                    "technology_summary",
                    technology.get("source_ids"),
                ),
            },
        })
        summaries = {
            "tech_summary": technology["tech_summary"].strip(),
            "problem_summary": problem["problem_summary"].strip(),
        }
        self._require_strings(summaries, ("tech_summary", "problem_summary"))
        atomic_json(artifact / "threat_score.json", score)
        atomic_json(artifact / "summaries.json", summaries)
        self.store.put_artifact(
            self.research_id, artifact.name, "threat_score", score,
            source_path=str((artifact / "threat_score.json").relative_to(self.root)),
        )
        self.store.put_artifact(
            self.research_id, artifact.name, "summaries", summaries,
            source_path=str((artifact / "summaries.json").relative_to(self.root)),
        )
        return {"artifact_dir": artifact, "score": score, "summaries": summaries}

    def _shared_embedding_cache_descriptor(
        self,
        prepared: dict[str, Any],
        summaries: dict[str, str],
        cache_identity: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(cache_identity, dict):
            return None
        inputs = [
            summaries["tech_summary"],
            summaries["problem_summary"],
        ]
        key_material = {
            "cache_version": SHARED_SEMANTIC_CACHE_VERSION,
            "environment": self.environment,
            "kind": "summary_embeddings",
            "pdf_sha256": prepared.get("source_decision", {}).get(
                "pdf_sha256"
            ),
            "input_order": ["technology", "problem"],
            "input_sha256": [
                sha256_bytes(item.encode("utf-8")) for item in inputs
            ],
            "embedder": cache_identity,
        }
        fingerprint = sha256_bytes(
            json.dumps(
                key_material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        path = (
            prepared["cache_dir"]
            / SHARED_SEMANTIC_CACHE_VERSION
            / "embeddings"
            / cache_record_filename(fingerprint)
        )
        return {
            "fingerprint": fingerprint,
            "key_material": key_material,
            "path": path,
        }

    def _persist_embedding(
        self,
        prepared: dict[str, Any],
        generated: dict[str, Any],
        embedding: dict[str, Any],
        shared_cache: dict[str, Any] | None,
    ) -> dict[str, Any]:
        artifact: Path = prepared["artifact_dir"]
        stored = self.store.put_embedding(self.research_id, artifact.name, embedding)
        if not stored:
            # Compatibility for isolated pipeline callers that have not indexed
            # the research yet. Normal application runs always use SQLite.
            atomic_json(artifact / "embeddings.json", embedding)
        atomic_json(artifact / "embedding_manifest.json", {
            "storage": "sqlite-float32",
            "input_order": embedding["input_order"],
            "input_sha256": embedding["input_sha256"],
            "dimensions": embedding["dimensions"],
            "shared_cache": shared_cache,
            "created_at": embedding["created_at"],
        })
        return {**generated, "embedding": embedding}

    def reuse_shared_embedding(
        self,
        prepared: dict[str, Any],
        cache_identity: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        generated = self.assemble_generated_analysis(prepared)
        descriptor = self._shared_embedding_cache_descriptor(
            prepared,
            generated["summaries"],
            cache_identity,
        )
        if descriptor is None or not descriptor["path"].is_file():
            return None
        try:
            record = read_json(descriptor["path"])
            if (
                record.get("cache_version")
                != SHARED_SEMANTIC_CACHE_VERSION
                or record.get("fingerprint") != descriptor["fingerprint"]
            ):
                return None
            embedding = record.get("embedding")
            if not isinstance(embedding, dict):
                return None
            vectors = embedding.get("vectors", {})
            validated = validate_vectors(
                [vectors.get("technology"), vectors.get("problem")],
                2,
            )
            if embedding.get("input_sha256") != descriptor[
                "key_material"
            ]["input_sha256"]:
                return None
            embedding = {
                **embedding,
                "dimensions": len(validated[0]),
                "vectors": {
                    "technology": validated[0],
                    "problem": validated[1],
                },
            }
        except (DataError, KeyError, TypeError, ValueError):
            return None
        return self._persist_embedding(
            prepared,
            generated,
            embedding,
            {
                "hit": True,
                "fingerprint": descriptor["fingerprint"],
                "source_created_at": record.get("created_at"),
                "path": str(descriptor["path"].relative_to(self.root)),
            },
        )

    def write_embedding(
        self,
        prepared: dict[str, Any],
        vectors: list[list[float]],
        *,
        cache_identity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        generated = self.assemble_generated_analysis(prepared)
        summaries = generated["summaries"]
        embedding_inputs = [
            summaries["tech_summary"],
            summaries["problem_summary"],
        ]
        vectors = validate_vectors(vectors, 2)
        embedding = {
            "input_order": ["technology", "problem"],
            "input_sha256": [
                sha256_bytes(item.encode("utf-8"))
                for item in embedding_inputs
            ],
            "dimensions": len(vectors[0]),
            "vectors": {
                "technology": vectors[0],
                "problem": vectors[1],
            },
            "created_at": utc_now(),
        }
        descriptor = self._shared_embedding_cache_descriptor(
            prepared,
            summaries,
            cache_identity,
        )
        cache_status = None
        if descriptor is not None:
            cache_record = {
                "schema_version": 1,
                "cache_version": SHARED_SEMANTIC_CACHE_VERSION,
                "fingerprint": descriptor["fingerprint"],
                "key_material": descriptor["key_material"],
                "embedding": embedding,
                "created_at": utc_now(),
            }
            atomic_json(descriptor["path"], cache_record)
            cache_status = {
                "hit": False,
                "fingerprint": descriptor["fingerprint"],
                "path": str(descriptor["path"].relative_to(self.root)),
            }
        return self._persist_embedding(
            prepared,
            generated,
            embedding,
            cache_status,
        )

    @classmethod
    def build_company_reference(cls, profile: dict[str, Any], vectors: list[list[float]]) -> dict[str, Any]:
        cls._require_strings(profile, ("technology_summary", "problem_summary"))
        inputs = [profile["technology_summary"], profile["problem_summary"]]
        vectors = validate_vectors(vectors, 2)
        return {
            "schema_version": 1,
            "input_order": ["technology", "problem"],
            "summaries": {"technology": inputs[0], "problem": inputs[1]},
            "input_sha256": [sha256_bytes(item.encode("utf-8")) for item in inputs],
            "dimensions": len(vectors[0]),
            "vectors": {"technology": vectors[0], "problem": vectors[1]},
            "created_at": utc_now(),
        }

    def analyze_document(
        self,
        prepared: dict[str, Any],
        generate_json: "GenerateJson",
        embed_texts: "EmbedTexts",
    ) -> dict[str, Any]:
        """Run four small schema-constrained LLM jobs and two summary embeddings."""
        for task in ("concept_level", "problem_summary", "similarity", "technology_summary"):
            self.analyze_task(prepared, task, generate_json)
        generated = self.assemble_generated_analysis(prepared)
        inputs = [generated["summaries"]["tech_summary"], generated["summaries"]["problem_summary"]]
        return self.write_embedding(prepared, embed_texts(inputs))

        # Legacy in-method implementation retained below temporarily for
        # artifact compatibility documentation; execution returns above.
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
        stored = self.store.put_embedding(self.research_id, artifact.name, embedding)
        if not stored:
            atomic_json(artifact / "embeddings.json", embedding)
        atomic_json(artifact / "embedding_manifest.json", {
            "storage": "sqlite-float32",
            "input_order": embedding["input_order"],
            "input_sha256": embedding["input_sha256"],
            "dimensions": embedding["dimensions"],
            "created_at": embedding["created_at"],
        })
        return {
            "artifact_dir": artifact,
            "score": score,
            "summaries": summaries,
            "embedding": embedding,
        }

    def finalize_research(
        self,
        analyses: dict[str, dict[str, Any]],
        generate_json: "GenerateJson",
        run_id: str,
        cluster_count: int | None = None,
        company_reference: dict[str, Any] | None = None,
        progress: Callable[..., None] | None = None,
    ) -> dict[str, Any]:
        vectors = {key: value["embedding"]["vectors"] for key, value in analyses.items()}
        clustering = self.cluster(vectors, cluster_count)

        def names(kind: str, assignments: dict[str, int]) -> dict[int, str]:
            output: dict[int, str] = {}
            summary_key = "tech_summary" if kind == "technology" else "problem_summary"
            for cluster_id in sorted(set(assignments.values())):
                members = [key for key, assigned in assignments.items() if assigned == cluster_id]
                # Bound the naming prompt for production clusters. Deterministic
                # sampling keeps reruns reproducible while avoiding 16K overflow.
                sampled_members = members[:20]
                response = generate_json("cluster_name", {
                    "kind": kind,
                    "cluster_id": cluster_id,
                    "member_count": len(members),
                    "summaries": [{"patent_id": key, "summary": analyses[key]["summaries"][summary_key]} for key in sampled_members],
                })
                self._require_strings(response, ("name",))
                output[cluster_id] = response["name"]
            return output

        tech_names = names("technology", clustering["technology_assignments"])
        problem_names = names("problem", clustering["problem_assignments"])
        clustering["technology_cluster_names"] = tech_names
        clustering["problem_cluster_names"] = problem_names
        if progress:
            progress("semantic_ordering", documents=len(analyses))
        technology_vectors = {key: value["technology"] for key, value in vectors.items()}
        problem_vectors = {key: value["problem"] for key, value in vectors.items()}
        technology_centroids = cluster_centroids(technology_vectors, clustering["technology_assignments"])
        problem_centroids = cluster_centroids(problem_vectors, clustering["problem_assignments"])
        technology_order = semantic_cluster_order(technology_centroids)
        problem_order = semantic_cluster_order(problem_centroids)
        if company_reference:
            technology_proximity = relative_proximity(company_reference["vectors"]["technology"], technology_centroids)
            problem_proximity = relative_proximity(company_reference["vectors"]["problem"], problem_centroids)
            # Reversal does not change semantic adjacency. Use it only to put the
            # more company-relevant endpoint on the conventional top/left side.
            if len(technology_order) > 1 and technology_proximity[technology_order[-1]]["cosine_similarity"] > technology_proximity[technology_order[0]]["cosine_similarity"]:
                technology_order.reverse()
            if len(problem_order) > 1 and problem_proximity[problem_order[-1]]["cosine_similarity"] > problem_proximity[problem_order[0]]["cosine_similarity"]:
                problem_order.reverse()
        else:
            technology_proximity = {}
            problem_proximity = {}
        clustering["semantic_ordering"] = {
            "algorithm": "average-linkage-cosine-leaf-seriation",
            "technology_order": technology_order,
            "problem_order": problem_order,
        }
        clustering["company_proximity"] = {
            "scale": "research-relative-minmax",
            "technology": technology_proximity,
            "problem": problem_proximity,
        }
        clustering["evidence_validation"] = summarize_evidence_validation(
            analysis["score"] for analysis in analyses.values()
        )
        cluster_dir = self.research_dir / "clustering" / run_id
        atomic_json(cluster_dir / "clusters.json", clustering)
        if company_reference:
            atomic_json(cluster_dir / "company_reference.json", company_reference)

        results: dict[str, str] = {}
        for key, analysis in analyses.items():
            score = analysis["score"]
            summary = analysis["summaries"]
            result = {
                "similarity": score["similarity"],
                "concept_level": score["concept_level"],
                "tech_summary": summary["tech_summary"],
                "problem_summary": summary["problem_summary"],
                "reasoning": f"類似度: {score['similarity_reason']}\n権利範囲の広さ: {score['concept_level_reason']}",
                "claim_traceability": score.get("claim_traceability", {}),
                "evidence_traceability": score.get("evidence_traceability", {}),
                "tech_cluster": tech_names[clustering["technology_assignments"][key]],
                "problem_cluster": problem_names[clustering["problem_assignments"][key]],
                "tech_cluster_id": clustering["technology_assignments"][key],
                "problem_cluster_id": clustering["problem_assignments"][key],
            }
            result_path = self.research_dir / "results" / f"{key}.json"
            atomic_json(result_path, result)
            self.store.upsert_analysis_result(self.research_id, key, result)
            results[key] = str(result_path.relative_to(self.root))
        self.store.put_artifact(
            self.research_id,
            "_research",
            "clusters",
            clustering,
            run_id=run_id,
            source_path=str((cluster_dir / "clusters.json").relative_to(self.root)),
        )
        self.store.update_technology_map(
            self.research_id,
            {
                "run_id": run_id,
                "algorithm": clustering["semantic_ordering"].get("algorithm"),
                "proximity_scale": clustering["company_proximity"].get("scale"),
                "technology_order": [str(item) for item in clustering["semantic_ordering"].get("technology_order", [])],
                "problem_order": [str(item) for item in clustering["semantic_ordering"].get("problem_order", [])],
                "technology_clusters": {
                    str(cluster_id): {
                        "name": name,
                        **clustering["company_proximity"].get("technology", {}).get(
                            str(cluster_id),
                            clustering["company_proximity"].get("technology", {}).get(cluster_id, {}),
                        ),
                    }
                    for cluster_id, name in tech_names.items()
                },
                "problem_clusters": {
                    str(cluster_id): {
                        "name": name,
                        **clustering["company_proximity"].get("problem", {}).get(
                            str(cluster_id),
                            clustering["company_proximity"].get("problem", {}).get(cluster_id, {}),
                        ),
                    }
                    for cluster_id, name in problem_names.items()
                },
            },
        )
        return {
            "clustering": str((cluster_dir / "clusters.json").relative_to(self.root)),
            "company_reference": str((cluster_dir / "company_reference.json").relative_to(self.root)) if company_reference else None,
            "results": results,
            "evidence_validation": clustering["evidence_validation"],
        }

    def finalize_persisted_research(
        self,
        generate_json: "GenerateJson",
        run_id: str,
        cluster_count: int | None = None,
        company_reference: dict[str, Any] | None = None,
        progress: Callable[..., None] | None = None,
    ) -> dict[str, Any]:
        """Finalize a large research without loading JSON vectors into Python.

        Embeddings stream from SQLite as float32 BLOBs and are reduced to a
        bounded 256-dimensional matrix one kind at a time.
        """
        summary = self.store.embedding_summary(self.research_id)
        if not summary["count"] or summary["dimensions"] != summary["minimum_dimensions"]:
            raise DataError("SQLite embeddings are missing or have inconsistent dimensions")
        materials = self.store.analysis_materials(self.research_id)
        if len(materials) != summary["count"]:
            raise DataError(
                f"SQLite analysis artifacts are incomplete: {len(materials)}/{summary['count']}"
            )
        if progress:
            progress("loading_embeddings", documents=summary["count"])
        tech_keys, tech_matrix, technology_assignments = scalable_clusters_from_blobs(
            self.store.iter_embedding_blobs(self.research_id, "technology"),
            summary["count"],
            summary["dimensions"],
            cluster_count,
        )
        problem_keys, problem_matrix, problem_assignments = scalable_clusters_from_blobs(
            self.store.iter_embedding_blobs(self.research_id, "problem"),
            summary["count"],
            summary["dimensions"],
            cluster_count,
        )
        if tech_keys != problem_keys:
            raise DataError("technology/problem embedding order differs")

        def names(kind: str, assignments: dict[str, int]) -> dict[int, str]:
            output: dict[int, str] = {}
            summary_key = "tech_summary" if kind == "technology" else "problem_summary"
            for cluster_id in sorted(set(assignments.values())):
                members = [key for key in tech_keys if assignments[key] == cluster_id]
                response = generate_json(
                    "cluster_name",
                    {
                        "kind": kind,
                        "cluster_id": cluster_id,
                        "member_count": len(members),
                        "summaries": [
                            {
                                "patent_id": key,
                                "summary": materials[key]["summaries"][summary_key],
                            }
                            for key in members[:20]
                        ],
                    },
                )
                self._require_strings(response, ("name",))
                output[cluster_id] = response["name"]
            return output

        technology_names = names("technology", technology_assignments)
        problem_names = names("problem", problem_assignments)
        technology_centroids = projected_centroids(
            tech_keys, tech_matrix, technology_assignments
        )
        problem_centroids = projected_centroids(
            problem_keys, problem_matrix, problem_assignments
        )
        del tech_matrix, problem_matrix
        technology_order = semantic_cluster_order(technology_centroids)
        problem_order = semantic_cluster_order(problem_centroids)
        if company_reference:
            target_dimensions = len(next(iter(technology_centroids.values())))
            technology_reference = project_reference_vector(
                company_reference["vectors"]["technology"], target_dimensions
            )
            problem_reference = project_reference_vector(
                company_reference["vectors"]["problem"], target_dimensions
            )
            technology_proximity = relative_proximity(
                technology_reference, technology_centroids
            )
            problem_proximity = relative_proximity(problem_reference, problem_centroids)
            if (
                len(technology_order) > 1
                and technology_proximity[technology_order[-1]]["cosine_similarity"]
                > technology_proximity[technology_order[0]]["cosine_similarity"]
            ):
                technology_order.reverse()
            if (
                len(problem_order) > 1
                and problem_proximity[problem_order[-1]]["cosine_similarity"]
                > problem_proximity[problem_order[0]]["cosine_similarity"]
            ):
                problem_order.reverse()
        else:
            technology_proximity = {}
            problem_proximity = {}
        clustering = {
            "schema_version": 2,
            "scope": "research",
            "research_id": self.research_id,
            "algorithm": "sqlite-streamed-projection-minibatch-cosine",
            "cluster_count_requested": cluster_count,
            "technology_assignments": technology_assignments,
            "problem_assignments": problem_assignments,
            "technology_cluster_names": technology_names,
            "problem_cluster_names": problem_names,
            "semantic_ordering": {
                "algorithm": "average-linkage-cosine-leaf-seriation",
                "technology_order": technology_order,
                "problem_order": problem_order,
            },
            "company_proximity": {
                "scale": "research-relative-minmax",
                "technology": technology_proximity,
                "problem": problem_proximity,
            },
            "created_at": utc_now(),
        }
        clustering["evidence_validation"] = summarize_evidence_validation(
            item["score"] for item in materials.values()
        )
        cluster_dir = self.research_dir / "clustering" / run_id
        atomic_json(cluster_dir / "clusters.json", clustering)
        if company_reference:
            # The research-level reference is one vector pair, so JSON remains
            # a compact human-auditable artifact.
            atomic_json(cluster_dir / "company_reference.json", company_reference)

        def results() -> Iterator[tuple[str, dict[str, Any]]]:
            for key in tech_keys:
                score = materials[key]["score"]
                summaries = materials[key]["summaries"]
                yield key, {
                    "similarity": score["similarity"],
                    "concept_level": score["concept_level"],
                    "tech_summary": summaries["tech_summary"],
                    "problem_summary": summaries["problem_summary"],
                    "reasoning": (
                        f"類似度: {score['similarity_reason']}\n"
                        f"権利範囲の広さ: {score['concept_level_reason']}"
                    ),
                    "claim_traceability": score.get("claim_traceability", {}),
                    "evidence_traceability": score.get("evidence_traceability", {}),
                    "tech_cluster": technology_names[technology_assignments[key]],
                    "problem_cluster": problem_names[problem_assignments[key]],
                    "tech_cluster_id": technology_assignments[key],
                    "problem_cluster_id": problem_assignments[key],
                }

        result_count = self.store.bulk_upsert_analysis_results(
            self.research_id, results()
        )
        self.store.put_artifact(
            self.research_id,
            "_research",
            "clusters",
            clustering,
            run_id=run_id,
            source_path=str((cluster_dir / "clusters.json").relative_to(self.root)),
        )
        technology_map = {
            "run_id": run_id,
            "algorithm": clustering["semantic_ordering"]["algorithm"],
            "proximity_scale": clustering["company_proximity"]["scale"],
            "technology_order": [str(item) for item in technology_order],
            "problem_order": [str(item) for item in problem_order],
            "technology_clusters": {
                str(cluster_id): {
                    "name": name,
                    **technology_proximity.get(cluster_id, {}),
                }
                for cluster_id, name in technology_names.items()
            },
            "problem_clusters": {
                str(cluster_id): {
                    "name": name,
                    **problem_proximity.get(cluster_id, {}),
                }
                for cluster_id, name in problem_names.items()
            },
        }
        self.store.update_technology_map(self.research_id, technology_map)
        return {
            "clustering": str((cluster_dir / "clusters.json").relative_to(self.root)),
            "company_reference": (
                str((cluster_dir / "company_reference.json").relative_to(self.root))
                if company_reference
                else None
            ),
            "results": {},
            "results_count": result_count,
            "storage": "sqlite",
            "evidence_validation": clustering["evidence_validation"],
        }


GenerateJson = Callable[[str, dict[str, Any]], dict[str, Any]]
EmbedTexts = Callable[[list[str]], list[list[float]]]
