from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class PatentViewerClient:
    def __init__(self, root: Path, url: str | None = None, token: str | None = None):
        self.root = root.resolve()
        control = self._control()
        self.url = (url or control.get("url") or "http://127.0.0.1:8765").rstrip("/")
        self.token = token if token is not None else str(control.get("token", ""))
        self._cache: dict[str, tuple[float, Any]] = {}

    def _control(self) -> dict[str, Any]:
        path = self.root / "runtime" / "server-control.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}

    def request(
        self, path: str, body: dict[str, Any] | None = None, *, authorized: bool = False,
        headers: dict[str, str] | None = None,
    ) -> Any:
        if body is None:
            cached = self._cache.get(path)
            if cached and time.monotonic() - cached[0] < 1.0:
                return cached[1]
        request_headers = {"Accept": "application/json", **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        if authorized and self.token:
            request_headers["X-PatentViewer-Collaboration-Token"] = self.token
        request = Request(self.url + path, data=data, headers=request_headers, method="POST" if body is not None else "GET")
        try:
            with urlopen(request, timeout=10) as response:
                result = json.loads(response.read().decode("utf-8"))
                if body is None:
                    self._cache[path] = (time.monotonic(), result)
                else:
                    self._cache.clear()
                return result
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {"error": str(exc), "code": "HTTP_ERROR"}
            raise RuntimeError(json.dumps(payload, ensure_ascii=False)) from exc
        except URLError as exc:
            raise RuntimeError(json.dumps({
                "error": f"PatentViewerへ接続できません: {self.url}",
                "code": "SERVER_NOT_RUNNING", "retryable": True,
            }, ensure_ascii=False)) from exc

    def status(self) -> dict[str, Any]:
        return self.request("/api/collaboration/status")

    def environment(self, client_id: str) -> str:
        return str(self.request("/api/environment", headers={"X-PatentViewer-UI-Client": client_id})["environment"])

    def switch_environment(self, client_id: str, environment: str) -> dict[str, Any]:
        if environment not in {"normal", "debug"}:
            raise ValueError("environment must be normal or debug")
        return self.request(
            "/api/environment", {"environment": environment},
            headers={"X-PatentViewer-UI-Client": client_id},
        )

    def researches(self, environment: str | None = None, status: str = "active") -> dict[str, Any]:
        query = urlencode({"status": status, **({"environment": environment} if environment else {})})
        return self.request(f"/api/researches?{query}")

    def dashboard(self, research_id: str, environment: str | None = None) -> dict[str, Any]:
        query = urlencode({"environment": environment}) if environment else ""
        return self.request(f"/api/researches/{quote(research_id)}/dashboard" + (f"?{query}" if query else ""))

    def search_documents(
        self, research_id: str, *, query: str = "", states: list[str] | None = None,
        analysis_states: list[str] | None = None, environment: str | None = None,
    ) -> dict[str, Any]:
        dashboard = self.dashboard(research_id, environment)
        needle = query.casefold().strip()
        legal = set(states or [])
        analysis = set(analysis_states or [])
        items = []
        for patent in dashboard.get("patents", []):
            haystack = " ".join(str(patent.get(key, "")) for key in ("id", "publication_number", "title", "applicant")).casefold()
            if needle and needle not in haystack:
                continue
            if legal and patent.get("legal_status_category") not in legal:
                continue
            if analysis and patent.get("analysis_state") not in analysis:
                continue
            items.append({
                key: patent.get(key) for key in (
                    "id", "publication_number", "title", "applicant", "year",
                    "legal_status_category", "analysis_state", "pdf_available", "skip_reason",
                )
            })
        return {"research_id": research_id, "count": len(items), "items": items, "execution_path": "rule_api"}

    def blocks(self) -> dict[str, Any]:
        return self.request("/api/collaboration/blocks")

    def plan(self, block: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.request("/api/collaboration/plan", {"block": block, "arguments": arguments or {}})

    def execute(
        self, block: str, arguments: dict[str, Any] | None = None, *, dry_run: bool = False,
        target_client_id: str | None = None, idempotency_key: str | None = None, intent: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "block": block, "arguments": arguments or {}, "dry_run": dry_run,
            "targetClientId": target_client_id, "idempotency_key": idempotency_key, "intent": intent,
        }
        return self.request("/api/collaboration/execute", body, authorized=not dry_run)

    def command(self, command_id: str) -> dict[str, Any]:
        return self.request(f"/api/ui/commands/{quote(command_id)}")

    def control(self, command_id: str, control: str) -> dict[str, Any]:
        return self.request(f"/api/ui/commands/{quote(command_id)}/control", {"control": control})

    def audit(self, limit: int = 100) -> dict[str, Any]:
        return self.request(f"/api/collaboration/audit?limit={max(1, min(limit, 1000))}")

    def pipeline(self, research_id: str, environment: str | None = None) -> dict[str, Any]:
        query = urlencode({"environment": environment}) if environment else ""
        return self.request(f"/api/researches/{quote(research_id)}/pipeline" + (f"?{query}" if query else ""))
