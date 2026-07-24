from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from patent_viewer.collaboration_client import PatentViewerClient


TOOLS = [
    ("viewer_status", "PatentViewerと可視ブラウザの接続状態を取得", {}),
    ("list_researches", "リサーチ一覧を取得", {"environment": {"type": "string"}, "status": {"type": "string"}}),
    ("get_dashboard", "リサーチのダッシュボードデータを取得", {"research_id": {"type": "string"}, "environment": {"type": "string"}}),
    ("search_documents", "LLMを使わず規則ベースで文献を検索", {
        "research_id": {"type": "string"}, "query": {"type": "string"},
        "states": {"type": "array", "items": {"type": "string"}},
        "analysis_states": {"type": "array", "items": {"type": "string"}},
        "environment": {"type": "string"},
    }),
    ("list_blocks", "利用可能な高水準UI操作ブロックを取得", {}),
    ("plan_block", "UIを変更せずブロックの操作計画と負荷を取得", {
        "block": {"type": "string"}, "arguments": {"type": "object"},
    }),
    ("execute_ui_block", "可視UIで高水準ブロックを実行", {
        "block": {"type": "string"}, "arguments": {"type": "object"},
        "dry_run": {"type": "boolean"}, "target_client_id": {"type": "string"},
        "idempotency_key": {"type": "string"}, "intent": {"type": "string"},
    }),
    ("get_command_status", "UIコマンドの状態とイベントを取得", {"command_id": {"type": "string"}}),
    ("control_command", "UIコマンドをpause、run、cancel", {
        "command_id": {"type": "string"}, "control": {"type": "string", "enum": ["pause", "run", "cancel"]},
    }),
    ("get_activity_log", "永続協働監査ログを取得", {"limit": {"type": "integer"}}),
    ("get_pipeline_overview", "分析パイプラインの状態を取得", {
        "research_id": {"type": "string"}, "environment": {"type": "string"},
    }),
]


def schemas() -> list[dict[str, Any]]:
    required = {
        "get_dashboard": ["research_id"], "search_documents": ["research_id"],
        "plan_block": ["block"], "execute_ui_block": ["block"],
        "get_command_status": ["command_id"], "control_command": ["command_id", "control"],
        "get_pipeline_overview": ["research_id"],
    }
    return [
        {
            "name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": required.get(name, []), "additionalProperties": False},
        }
        for name, description, properties in TOOLS
    ]


def call_tool(client: PatentViewerClient, name: str, args: dict[str, Any]) -> Any:
    if name == "viewer_status":
        return client.status()
    if name == "list_researches":
        return client.researches(args.get("environment"), args.get("status", "active"))
    if name == "get_dashboard":
        return client.dashboard(args["research_id"], args.get("environment"))
    if name == "search_documents":
        return client.search_documents(
            args["research_id"], query=args.get("query", ""), states=args.get("states"),
            analysis_states=args.get("analysis_states"), environment=args.get("environment"),
        )
    if name == "list_blocks":
        return client.blocks()
    if name == "plan_block":
        return client.plan(args["block"], args.get("arguments"))
    if name == "execute_ui_block":
        return client.execute(
            args["block"], args.get("arguments"), dry_run=bool(args.get("dry_run", False)),
            target_client_id=args.get("target_client_id"), idempotency_key=args.get("idempotency_key"),
            intent=args.get("intent"),
        )
    if name == "get_command_status":
        return client.command(args["command_id"])
    if name == "control_command":
        return client.control(args["command_id"], args["control"])
    if name == "get_activity_log":
        return client.audit(int(args.get("limit", 100)))
    if name == "get_pipeline_overview":
        return client.pipeline(args["research_id"], args.get("environment"))
    raise ValueError(f"unknown tool: {name}")


def response(identifier: Any, result: Any = None, error: dict[str, Any] | None = None) -> None:
    payload = {"jsonrpc": "2.0", "id": identifier}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    client = PatentViewerClient(ROOT)
    for line in sys.stdin:
        try:
            message = json.loads(line)
            identifier, method = message.get("id"), message.get("method")
            if method == "initialize":
                response(identifier, {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "patent-viewer", "version": "1.0.0"},
                })
            elif method == "notifications/initialized":
                continue
            elif method == "ping":
                response(identifier, {})
            elif method == "tools/list":
                response(identifier, {"tools": schemas()})
            elif method == "tools/call":
                params = message.get("params", {})
                result = call_tool(client, str(params.get("name", "")), params.get("arguments", {}))
                response(identifier, {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False})
            else:
                response(identifier, error={"code": -32601, "message": f"method not found: {method}"})
        except Exception as exc:
            response(message.get("id") if "message" in locals() else None, error={"code": -32000, "message": str(exc)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
