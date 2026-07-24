from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from patent_viewer.collaboration_client import PatentViewerClient


TERMINAL = {"completed", "failed", "cancelled"}


def wait_command(client: PatentViewerClient, command_id: str, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        client._cache.clear()
        command = client.command(command_id)
        if command["status"] in TERMINAL:
            if command["status"] != "completed":
                raise RuntimeError(json.dumps(command, ensure_ascii=False))
            return command
        time.sleep(0.1)
    raise RuntimeError(f"UI command timeout: {command_id}")


def wait_environment(client: PatentViewerClient, client_id: str, environment: str, timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        client._cache.clear()
        status = client.status()
        match = next((item for item in status.get("clients", []) if item["clientId"] == client_id), None)
        if match and match["environment"] == environment and match["target_count"] > 0:
            return match
        time.sleep(0.2)
    raise RuntimeError(f"browser did not enter {environment}: {client_id}")


def select_client(status: dict, requested: str | None) -> dict:
    clients = status.get("clients", [])
    if requested:
        match = next((item for item in clients if item["clientId"] == requested), None)
        if not match:
            raise RuntimeError(f"指定clientが見つかりません: {requested}")
        return match
    if len(clients) != 1:
        raise RuntimeError(f"browser clientを一意に選べません。--client-idを指定してください: {[item['clientId'] for item in clients]}")
    return clients[0]


def execute(client: PatentViewerClient, client_id: str, block: str, arguments: dict, intent: str) -> dict:
    created = client.execute(
        block, arguments, target_client_id=client_id,
        idempotency_key=f"browser-smoke-{block}-{time.time_ns()}", intent=intent,
    )
    return wait_command(client, created["id"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Real-browser DEBUG PatentViewer Bridge smoke test")
    parser.add_argument("--url")
    parser.add_argument("--token")
    parser.add_argument("--client-id")
    parser.add_argument("--allow-skip", action="store_true", help="return success when no visible browser is connected")
    args = parser.parse_args()
    client = PatentViewerClient(ROOT, args.url, args.token)
    try:
        browser = select_client(client.status(), args.client_id)
    except Exception as exc:
        if args.allow_skip:
            print(f"SKIPPED: {exc}")
            return 0
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2

    client_id = browser["clientId"]
    original_environment = browser["environment"]
    completed: list[dict] = []
    restored = False
    try:
        if original_environment != "debug":
            client.switch_environment(client_id, "debug")
            wait_environment(client, client_id, "debug")

        researches = client.researches("debug")["items"]
        if not researches:
            raise RuntimeError("DEBUG research fixtureがありません")
        research_id = researches[0]["id"]
        dashboard = client.dashboard(research_id, "debug")
        patents = dashboard.get("patents", [])
        if not patents:
            raise RuntimeError("DEBUG文献がありません")

        marker = "__codex_bridge_smoke__"
        completed.append(execute(client, client_id, "set_filters", {"query": marker}, "Browser smoke: filter"))
        completed.append(execute(client, client_id, "reset_filters", {}, "Browser smoke: restore filters"))

        ready = next((item for item in patents if item.get("analysis_state") == "ready"), None)
        if ready:
            completed.append(execute(client, client_id, "select_threat_cell", {
                "similarity": int(ready["similarity"]), "concept_level": int(ready["concept_level"]),
            }, "Browser smoke: threat cell"))
            completed.append(execute(client, client_id, "open_patent", {
                "patent_id": ready["id"],
            }, "Browser smoke: patent detail"))
            if ready.get("pdf_available"):
                completed.append(execute(client, client_id, "preview_pdf", {}, "Browser smoke: PDF preview"))
                completed.append(execute(client, client_id, "close_pdf", {}, "Browser smoke: close PDF"))

        completed.append(execute(client, client_id, "open_preflight", {}, "Browser smoke: preflight dialog"))
    finally:
        try:
            execute(client, client_id, "reset_filters", {}, "Browser smoke: final filter restore")
        except Exception:
            pass
        if original_environment != "debug":
            client.switch_environment(client_id, original_environment)
            wait_environment(client, client_id, original_environment)
        restored = True

    print(json.dumps({
        "ok": True,
        "client_id": client_id,
        "tested_environment": "debug",
        "restored_environment": original_environment,
        "restored": restored,
        "commands": [{"id": item["id"], "block": item.get("block")} for item in completed],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
