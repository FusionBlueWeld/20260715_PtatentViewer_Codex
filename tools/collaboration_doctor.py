from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from patent_viewer.collaboration import PROTOCOL_VERSION
from patent_viewer.collaboration_client import PatentViewerClient


def main() -> int:
    parser = argparse.ArgumentParser(description="PatentViewer Codex collaboration diagnostics")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    checks = [
        {"id": "python", "ok": sys.version_info >= (3, 10), "detail": platform.python_version()},
        {"id": "repo", "ok": (ROOT / "src/patent_viewer").is_dir(), "detail": str(ROOT)},
        {"id": "mcp", "ok": (ROOT / "tools/collaboration_mcp.py").is_file(), "detail": "stdio"},
    ]
    status = {}
    try:
        status = PatentViewerClient(ROOT).status()
        checks.extend([
            {"id": "server", "ok": bool(status.get("ok")), "detail": "localhost"},
            {"id": "protocol", "ok": status.get("protocol_version") == PROTOCOL_VERSION, "detail": str(status.get("protocol_version"))},
            {"id": "browser", "ok": bool(status.get("clients")), "detail": f"{len(status.get('clients', []))} client(s)"},
            {"id": "targets", "ok": any(c.get("target_count", 0) > 0 for c in status.get("clients", [])), "detail": str(sum(c.get("target_count", 0) for c in status.get("clients", [])))},
        ])
    except Exception as exc:
        checks.append({"id": "server", "ok": False, "detail": str(exc)})
    result = {"ready": all(item["ok"] for item in checks), "checks": checks, "status": status}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for item in checks:
            print(f"[{'OK' if item['ok'] else 'NG'}] {item['id']}: {item['detail']}")
        print("READY" if result["ready"] else "BLOCKED")
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
