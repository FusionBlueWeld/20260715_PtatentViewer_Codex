from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).parents[1].resolve()
sys.path.insert(0, str(ROOT / "src"))

from patent_viewer.collaboration_client import PatentViewerClient


BASELINES = ROOT / "tests" / "visual_baselines"
CURRENT = ROOT / "runtime" / "visual-regression"
VIEWPORTS = {
    "wide": {"width": 1440, "height": 900},
    "narrow": {"width": 700, "height": 900},
}


def browser_executable() -> Path | None:
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
    ]
    return next((path for path in candidates if path.is_file()), None)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def healthy(url: str) -> bool:
    try:
        with urlopen(url.rstrip("/") + "/api/health", timeout=1) as response:
            return bool(json.loads(response.read()).get("ok"))
    except (OSError, URLError, json.JSONDecodeError):
        return False


def start_server() -> tuple[str, str, subprocess.Popen | None]:
    port = free_port()
    local_control = ROOT / "runtime/visual-server-control.json"
    local_control.parent.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([
        sys.executable, "-X", "utf8", str(ROOT / "run.py"), "--root", str(ROOT),
        "--host", "127.0.0.1", "--port", str(port), "--idle-timeout", "180",
        "--control-file", str(local_control), "--quiet", "--no-managed-ollama",
    ], cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        if process.poll() is not None:
            error = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
            raise RuntimeError(f"visual test server failed: {error}")
        if healthy(url) and local_control.exists():
            control = json.loads(local_control.read_text(encoding="utf-8-sig"))
            return url, str(control["token"]), process
        time.sleep(0.1)
    process.terminate()
    raise RuntimeError("visual test server timeout")


def compare_images(baseline: Path, current: Path, threshold: int, max_fraction: float) -> dict:
    from PIL import Image, ImageChops

    with Image.open(baseline).convert("RGB") as expected, Image.open(current).convert("RGB") as actual:
        if expected.size != actual.size:
            return {"ok": False, "reason": "size", "expected": expected.size, "actual": actual.size}
        difference = ImageChops.difference(expected, actual)
        pixels = difference.load()
        changed = 0
        for y in range(difference.height):
            for x in range(difference.width):
                if max(pixels[x, y]) > threshold:
                    changed += 1
        fraction = changed / max(1, difference.width * difference.height)
        return {"ok": fraction <= max_fraction, "changed_fraction": fraction, "threshold": threshold, "max_fraction": max_fraction}


def wait_client(client: PatentViewerClient, client_id: str, environment: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        client._cache.clear()
        match = next((item for item in client.status().get("clients", []) if item["clientId"] == client_id), None)
        if match and match["environment"] == environment and match["target_count"]:
            return
        time.sleep(0.2)
    raise RuntimeError(f"browser client did not reach {environment}")


def wait_registered(client: PatentViewerClient, client_id: str, timeout: float = 15) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        client._cache.clear()
        match = next((item for item in client.status().get("clients", []) if item["clientId"] == client_id), None)
        if match and match["target_count"]:
            return match
        time.sleep(0.2)
    raise RuntimeError("headless browser did not register with UI Bridge")


def main() -> int:
    parser = argparse.ArgumentParser(description="PatentViewer responsive screenshot and image regression check")
    parser.add_argument("--url")
    parser.add_argument("--token")
    parser.add_argument("--update-baselines", action="store_true")
    parser.add_argument("--allow-skip", action="store_true")
    parser.add_argument("--pixel-threshold", type=int, default=32)
    parser.add_argument("--max-changed-fraction", type=float, default=0.025)
    args = parser.parse_args()

    executable = browser_executable()
    if not executable:
        message = "EdgeまたはChromeが見つかりません"
        if args.allow_skip:
            print(f"SKIPPED: {message}")
            return 0
        print(f"BLOCKED: {message}", file=sys.stderr)
        return 2
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        message = "playwrightが未導入です: python -m pip install -r requirements.txt"
        if args.allow_skip:
            print(f"SKIPPED: {message}")
            return 0
        print(f"BLOCKED: {message}", file=sys.stderr)
        return 2

    process = None
    original_environment = "normal"
    client_id = ""
    results = {}
    try:
        if args.url:
            url, token = args.url, args.token or ""
        else:
            url, token, process = start_server()
        client = PatentViewerClient(ROOT, url, token)
        CURRENT.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=str(executable), headless=True)
            page = browser.new_page(viewport=VIEWPORTS["wide"])
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_function("document.querySelector('#metric-total')?.textContent.includes('total')")
                client_id = page.evaluate("sessionStorage.getItem('patentViewerClientId')")
                registered = wait_registered(client, client_id)
                original_environment = registered["environment"]
                if original_environment != "debug":
                    client.switch_environment(client_id, "debug")
                    page.wait_for_function("document.body.dataset.environment === 'debug'", timeout=15_000)
                    page.wait_for_function("document.querySelector('#metric-total')?.textContent.includes('total')")
                    wait_client(client, client_id, "debug")

                for name, viewport in VIEWPORTS.items():
                    page.set_viewport_size(viewport)
                    page.wait_for_timeout(150)
                    current = CURRENT / f"patent-viewer-{name}.png"
                    baseline = BASELINES / current.name
                    page.screenshot(path=str(current), full_page=False, animations="disabled")
                    if args.update_baselines:
                        BASELINES.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(current, baseline)
                        results[name] = {"ok": True, "updated": True}
                    elif not baseline.exists():
                        results[name] = {"ok": False, "reason": "baseline_missing", "path": str(baseline)}
                    else:
                        results[name] = compare_images(
                            baseline, current, max(0, min(args.pixel_threshold, 255)),
                            max(0.0, min(args.max_changed_fraction, 1.0)),
                        )
                if not all(item["ok"] for item in results.values()):
                    print(json.dumps({"ok": False, "results": results}, ensure_ascii=False, indent=2), file=sys.stderr)
                    return 1
            finally:
                try:
                    if client_id and original_environment != "debug":
                        client.switch_environment(client_id, original_environment)
                        page.wait_for_function(
                            f"document.body.dataset.environment === {json.dumps(original_environment)}",
                            timeout=15_000,
                        )
                        wait_client(client, client_id, original_environment)
                finally:
                    browser.close()
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
    print(json.dumps({"ok": True, "results": results, "restored_environment": original_environment}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
