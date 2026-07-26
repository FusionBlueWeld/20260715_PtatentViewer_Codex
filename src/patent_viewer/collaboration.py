from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = 3
ACTION_TYPES = ("click", "input", "check", "keydown", "select", "wait", "assert")
TERMINAL_STATES = {"completed", "failed", "cancelled"}


class CollaborationError(ValueError):
    def __init__(self, code: str, message: str, *, retryable: bool = False, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.detail = detail or {}

    def payload(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "code": self.code,
            "retryable": self.retryable,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class BlockDefinition:
    name: str
    description: str
    risk: str
    capability: str
    writes: bool = False
    heavy: bool = False
    confirmation: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk,
            "capability": self.capability,
            "writes": self.writes,
            "heavy": self.heavy,
            "confirmation": self.confirmation,
        }


BLOCKS = {
    item.name: item
    for item in (
        BlockDefinition("select_research", "表示するリサーチを選択", "low", "filter"),
        BlockDefinition("set_filters", "年、法的状態、検索語をまとめて設定", "low", "filter"),
        BlockDefinition("reset_filters", "表示条件を初期状態へ戻す", "low", "filter"),
        BlockDefinition("set_view", "脅威マップまたは技術マップを表示", "low", "map"),
        BlockDefinition("select_threat_cell", "脅威マップのセルを選択", "low", "map"),
        BlockDefinition("clear_cell", "マップセル選択を解除", "low", "map"),
        BlockDefinition("set_cluster_min_size", "技術マップの最小クラスタ件数を設定", "low", "map"),
        BlockDefinition("open_patent", "文献を選択して詳細を表示", "low", "map"),
        BlockDefinition("preview_pdf", "選択文献のPDFを可視UIで開く", "low", "pdf-preview"),
        BlockDefinition("close_pdf", "PDFプレビューを閉じる", "low", "pdf-preview"),
        BlockDefinition("save_interpretation", "選択文献へ解釈メモを保存", "medium", "interpretation", writes=True),
        BlockDefinition("open_preflight", "選択リサーチのLLM事前診断を表示", "low", "llm-preflight"),
        BlockDefinition("open_research_manager", "リサーチ管理画面を開く", "low", "filter"),
        BlockDefinition("open_legal_status_settings", "法的状態の判定設定を開く", "low", "filter"),
        BlockDefinition("open_organization_groups", "企業グループ管理を開く", "low", "map"),
        BlockDefinition("open_pipeline", "夜間一括分析画面を開く", "low", "research-pipeline"),
        BlockDefinition("prepare_pipeline", "PDF抽出と規則ベース前処理を開始", "medium", "research-pipeline", writes=True),
        BlockDefinition(
            "execute_pipeline", "ローカルLLM一括分析を開始", "high", "research-pipeline",
            writes=True, heavy=True, confirmation="RUN_LOCAL_LLM",
        ),
        BlockDefinition("pipeline_control", "実行中の分析をpause/resume/cancel", "medium", "pipeline-control", writes=True),
    )
}


def _target(agent_id: str) -> dict[str, str]:
    return {"agentId": agent_id}


def _action(kind: str, agent_id: str | None = None, **values: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"type": kind, **values}
    if agent_id:
        result["target"] = _target(agent_id)
    return result


def validate_actions(actions: Any) -> list[dict[str, Any]]:
    if not isinstance(actions, list) or not 1 <= len(actions) <= 100:
        raise CollaborationError("INVALID_ACTIONS", "actionsは1〜100件です")
    validated: list[dict[str, Any]] = []
    for index, raw in enumerate(actions):
        if not isinstance(raw, dict) or raw.get("type") not in ACTION_TYPES:
            raise CollaborationError("UNSUPPORTED_ACTION", f"action {index} は未対応です")
        action = dict(raw)
        kind = action["type"]
        if kind != "wait":
            target = action.get("target")
            if not isinstance(target, dict) or not (target.get("agentId") or target.get("selector")):
                raise CollaborationError("INVALID_TARGET", f"action {index} のtargetが不正です")
        if kind == "wait":
            action["ms"] = max(0, min(int(action.get("ms", 500)), 10_000))
        validated.append(action)
    return validated


class BlockPlanner:
    def __init__(self, catalog_path: Path | None = None):
        self.catalog = BLOCKS
        if catalog_path and catalog_path.exists():
            try:
                items = json.loads(catalog_path.read_text(encoding="utf-8")).get("blocks", [])
                loaded = {
                    str(item["name"]): BlockDefinition(
                        str(item["name"]), str(item["description"]), str(item["risk"]),
                        str(item["capability"]), bool(item.get("writes")), bool(item.get("heavy")),
                        item.get("confirmation"),
                    )
                    for item in items
                }
                if set(loaded) == set(BLOCKS):
                    self.catalog = loaded
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                self.catalog = BLOCKS

    def list_blocks(self) -> list[dict[str, Any]]:
        return [definition.as_dict() for definition in self.catalog.values()]

    def plan(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = arguments or {}
        definition = self.catalog.get(name)
        if not definition:
            raise CollaborationError("UNKNOWN_BLOCK", f"未対応のblockです: {name}")
        method = getattr(self, f"_plan_{name}")
        actions = validate_actions(method(args))
        estimated_ms = sum(
            int(action.get("ms", 250 if action["type"] in {"click", "select", "check"} else 100))
            for action in actions
        )
        return {
            "protocol_version": PROTOCOL_VERSION,
            "block": name,
            "definition": definition.as_dict(),
            "arguments": args,
            "actions": actions,
            "estimated_ms": estimated_ms,
            "execution_path": "visible_ui",
        }

    def _plan_select_research(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        value = self._required(args, "research_id")
        return [_action("select", "research-select", value=value), _action("wait", ms=200), _action("assert", "research-select", equals=value)]

    def _plan_set_filters(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        if "research_id" in args:
            actions.extend(self._plan_select_research(args))
        mapping = {"year_from": "year-from-filter", "year_to": "year-to-filter"}
        for key, target in mapping.items():
            if key in args:
                actions.append(_action("select", target, value="" if args[key] is None else str(args[key])))
        if "query" in args:
            query = str(args["query"] or "")
            actions.append(_action("input", "patent-search", value=query))
            actions.append(_action("assert", "patent-search", equals=query))
        statuses = args.get("statuses")
        if statuses is not None:
            if not isinstance(statuses, list):
                raise CollaborationError("INVALID_ARGUMENT", "statusesは配列で指定してください")
            selected = set(map(str, statuses))
            for status in ("rights_acquired", "under_examination", "published"):
                actions.append(_action("check", f"status-{status.replace('_', '-')}", value=status in selected))
        if not actions:
            raise CollaborationError("INVALID_ARGUMENT", "設定するフィルタがありません")
        actions.append(_action("wait", ms=150))
        return actions

    def _plan_reset_filters(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return [_action("click", "filters-reset"), _action("wait", ms=150), _action("assert", "patent-search", equals="")]

    def _plan_set_view(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        view = str(args.get("view", ""))
        if view not in {"threat", "technology"}:
            raise CollaborationError("INVALID_ARGUMENT", "viewはthreatまたはtechnologyです")
        return [_action("click", f"view-{view}"), _action("assert", f"view-{view}", attribute="aria-pressed", equals="true")]

    def _plan_select_threat_cell(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        x, y = int(args.get("similarity", 0)), int(args.get("concept_level", 0))
        if x not in range(1, 6) or y not in range(1, 6):
            raise CollaborationError("INVALID_ARGUMENT", "similarityとconcept_levelは1〜5です")
        return [_action("click", f"threat-cell-{x}-{y}")]

    def _plan_clear_cell(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return [_action("click", "cell-filter-clear")]

    def _plan_set_cluster_min_size(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        value = int(args.get("minimum", 1))
        if value not in {1, 2, 3, 5, 10}:
            raise CollaborationError("INVALID_ARGUMENT", "minimumは1、2、3、5、10です")
        return [_action("select", "technology-cluster-min-size", value=str(value))]

    def _plan_open_patent(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        patent_id = self._required(args, "patent_id")
        return [_action("click", f"patent-open-{patent_id}"), _action("wait", ms=100)]

    def _plan_preview_pdf(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        actions = self._plan_open_patent(args) if args.get("patent_id") else []
        return actions + [_action("click", "selected-pdf-preview")]

    def _plan_close_pdf(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return [_action("click", "pdf-close")]

    def _plan_save_interpretation(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        actions = self._plan_open_patent(args) if args.get("patent_id") else []
        return actions + [
            _action("input", "interpretation-note", value=self._required(args, "note")),
            _action("click", "interpretation-save"),
        ]

    def _plan_open_preflight(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        actions = self._plan_select_research(args) if args.get("research_id") else []
        return actions + [_action("click", "llm-preflight")]

    def _plan_open_research_manager(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return [_action("click", "research-manage-open")]

    def _plan_open_legal_status_settings(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return [_action("click", "legal-status-settings")]

    def _plan_open_organization_groups(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return [_action("click", "organization-groups-manage")]

    def _plan_open_pipeline(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        actions = [_action("click", "pipeline-open"), _action("wait", ms=200)]
        if args.get("research_id"):
            actions.append(_action("select", "pipeline-research-select", value=str(args["research_id"])))
        return actions

    def _plan_prepare_pipeline(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return self._plan_open_pipeline(args) + [_action("click", "pipeline-prepare")]

    def _plan_execute_pipeline(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        if args.get("confirmation") != "RUN_LOCAL_LLM":
            raise CollaborationError("HUMAN_CONFIRMATION_REQUIRED", "confirmation=RUN_LOCAL_LLMが必要です")
        actions = self._plan_open_pipeline(args)
        actions.extend([
            _action("input", "pipeline-cooldown-seconds", value=str(max(0, min(180, int(args.get("cooldown_seconds", 0)))))),
            _action("check", "pipeline-overwrite", value=bool(args.get("overwrite", False))),
            _action("check", "pipeline-execute-confirm", value=True),
            _action("click", "pipeline-execute"),
        ])
        return actions

    def _plan_pipeline_control(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        control = str(args.get("control", ""))
        target = {"pause": "pipeline-job-pause", "run": "pipeline-job-resume", "cancel": "pipeline-job-cancel"}.get(control)
        if not target:
            raise CollaborationError("INVALID_ARGUMENT", "controlはpause、run、cancelです")
        return [_action("click", target)]

    @staticmethod
    def _required(args: dict[str, Any], key: str) -> str:
        value = str(args.get(key, "")).strip()
        if not value:
            raise CollaborationError("INVALID_ARGUMENT", f"{key}が必要です")
        return value


class AuditStore:
    """Append-only collaboration audit with a small idempotency index."""

    def __init__(self, runtime_root: Path):
        self.root = runtime_root / "collaboration"
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "audit.jsonl"
        self.lock = threading.RLock()
        self.idempotency: dict[str, dict[str, Any]] = {}
        self._load_index()

    def _load_index(self) -> None:
        if not self.path.exists():
            return
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines()[-2000:]:
                item = json.loads(line)
                key = item.get("idempotency_key")
                if key and item.get("event") == "command_created":
                    self.idempotency[key] = item
        except (OSError, json.JSONDecodeError):
            return

    def append(self, event: str, **detail: Any) -> dict[str, Any]:
        item = {"at": time.time(), "event": event, **detail}
        encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        with self.lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded + "\n")
            key = item.get("idempotency_key")
            if key and event == "command_created":
                self.idempotency[key] = item
        return item

    def find_idempotent(self, key: str) -> dict[str, Any] | None:
        with self.lock:
            return self.idempotency.get(key)

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        result: list[dict[str, Any]] = []
        for line in reversed(lines[-max(1, min(limit, 1000)):]):
            try:
                result.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return result


def target_snapshot_hash(targets: list[dict[str, Any]]) -> str:
    stable = json.dumps(targets, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:16]


def estimate_workload(operation: str, count: int = 0, cached: bool = False) -> dict[str, Any]:
    if cached:
        route, weight = "cache", "light"
    elif operation in {"status", "search", "filter", "dashboard", "preflight"}:
        route, weight = "rule_api", "light"
    elif operation in {"prepare_pipeline"}:
        route, weight = "rule_pipeline", "medium"
    elif operation in {"execute_pipeline"}:
        route, weight = "local_llm_pipeline", "heavy"
    else:
        route, weight = "visible_ui", "light"
    return {"operation": operation, "document_count": max(0, count), "route": route, "weight": weight}
