from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).parents[1].resolve()
RUNTIME = ROOT / "runtime"
CONTROL = RUNTIME / "server-control.json"


def read_control() -> dict:
    try:
        return json.loads(CONTROL.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def health(url: str) -> dict:
    try:
        with urlopen(url.rstrip("/") + "/api/health", timeout=1) as response:
            return json.loads(response.read())
    except (OSError, URLError, json.JSONDecodeError):
        return {}


def start(args) -> int:
    control = read_control()
    if control and health(str(control.get("url", ""))).get("ok"):
        url = control["url"]
        print(f"PatentViewer is already running: {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0
    RUNTIME.mkdir(parents=True, exist_ok=True)
    stdout = (RUNTIME / "server.stdout.log").open("ab")
    stderr = (RUNTIME / "server.stderr.log").open("ab")
    command = [
        sys.executable, "-X", "utf8", str(ROOT / "run.py"), "--root", str(ROOT),
        "--host", "127.0.0.1", "--port", str(args.port),
        "--idle-timeout", str(max(1, round(args.idle_timeout_minutes * 60))),
        "--control-file", str(CONTROL), "--quiet",
    ]
    if args.no_managed_ollama:
        command.append("--no-managed-ollama")
    process = subprocess.Popen(
        command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    url = f"http://127.0.0.1:{args.port}"
    for _ in range(150):
        if process.poll() is not None:
            break
        if health(url).get("ok"):
            print(f"PatentViewer started: {url}")
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        time.sleep(0.1)
    print(f"PatentViewer startup failed. See {RUNTIME / 'server.stderr.log'}", file=sys.stderr)
    return 1


def stop(_args) -> int:
    control = read_control()
    if not control:
        print("PatentViewer is not running.")
        return 0
    request = Request(
        str(control["url"]).rstrip("/") + "/api/admin/shutdown", data=b"{}",
        headers={"Content-Type": "application/json", "X-PatentViewer-Control-Token": str(control.get("token", ""))},
        method="POST",
    )
    try:
        with urlopen(request, timeout=3):
            pass
    except URLError:
        if CONTROL.exists():
            CONTROL.unlink()
        print("Removed stale server control data.")
        return 0
    print("PatentViewer stopping.")
    return 0


def doctor(args) -> int:
    command = [sys.executable, "-X", "utf8", str(ROOT / "tools/collaboration_doctor.py")]
    if args.json:
        command.append("--json")
    return subprocess.call(command, cwd=ROOT)


def mcp_config(args) -> int:
    payload = {
        "mcpServers": {
            "patent-viewer": {
                "command": sys.executable,
                "args": ["-X", "utf8", str(ROOT / "tools/collaboration_mcp.py")],
                "cwd": str(ROOT),
            }
        }
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.write:
        target = ROOT / ".codex" / "mcp.local.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered + "\n", encoding="utf-8")
        print(target)
    else:
        print(rendered)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Portable PatentViewer launcher and Codex collaboration utility")
    commands = parser.add_subparsers(dest="command", required=True)
    start_parser = commands.add_parser("start")
    start_parser.add_argument("--port", type=int, default=8765)
    start_parser.add_argument("--idle-timeout-minutes", type=float, default=30)
    start_parser.add_argument("--no-browser", action="store_true")
    start_parser.add_argument("--no-managed-ollama", action="store_true")
    start_parser.set_defaults(handler=start)
    stop_parser = commands.add_parser("stop")
    stop_parser.set_defaults(handler=stop)
    doctor_parser = commands.add_parser("doctor")
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.set_defaults(handler=doctor)
    config_parser = commands.add_parser("mcp-config")
    config_parser.add_argument("--write", action="store_true")
    config_parser.set_defaults(handler=mcp_config)
    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
