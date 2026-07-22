from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .domain import DataError, Repository
from .ollama_runtime import ManagedOllama, adaptive_runtime_config, detect_nvidia_gpu, detect_system_memory_mib
from .pipeline import ResearchPipeline, atomic_json


PROTOCOL_ACTIONS = ("click", "input", "check", "keydown", "select", "wait", "assert")
TERMINAL = {"completed", "failed", "cancelled"}


class AppState:
    def __init__(self, root: Path, idle_timeout: float = 1800, control_token: str = "", manage_ollama: bool = False, generation_workers: int | None = None, embedding_batch_size: int | None = None):
        self.root = root.resolve()
        self.repo = Repository(self.root)
        self.environment = "normal"
        self.idle_timeout = max(0.2, float(idle_timeout))
        self.control_token = control_token
        self.last_activity = time.time()
        self.last_user_event_at = 0.0
        self.clients: dict[str, dict] = {}
        self.commands: dict[str, dict] = {}
        self.pipeline_jobs: dict[str, dict] = {}
        self.activity: list[dict] = []
        self.lock = threading.RLock()
        self.server = None
        self.runtime_config = adaptive_runtime_config(
            detect_nvidia_gpu(), generation_workers=generation_workers,
            embedding_batch_size=embedding_batch_size, system_memory_mib=detect_system_memory_mib(),
        )
        self.ollama = ManagedOllama(self.root / "runtime" / "managed_ollama", self.runtime_config) if manage_ollama else None
        if self.ollama is not None:
            self.ollama.start()

    @property
    def ollama_url(self) -> str:
        if self.ollama is not None:
            return self.ollama.url
        return "http://127.0.0.1:11434"

    def preflight(self, environment: str, research_id: str) -> dict:
        result = self.repo.preflight(environment, research_id, self.ollama_url)
        result["ollama_runtime"] = self.ollama.status() if self.ollama else {
            "managed": False, "running": False, "url": self.ollama_url,
            "config": self.runtime_config.as_dict(),
        }
        return result

    def start_pipeline_job(self, environment: str, research_id: str, mode: str, overwrite: bool = False, cooldown_seconds: int = 0) -> dict:
        if mode not in {"prepare", "execute"}:
            raise DataError("pipeline modeはprepareまたはexecuteです")
        ResearchPipeline(self.root, environment, research_id)
        if not 0 <= cooldown_seconds <= 180:
            raise DataError("cooldown_secondsは0〜180秒で指定してください")
        with self.lock:
            for existing in self.pipeline_jobs.values():
                active = self.pipeline_job(existing["id"])["status"] in {"running", "paused", "cancelling"}
                if mode == "execute" and existing["mode"] == "execute" and active:
                    raise DataError("another GPU pipeline job is already running")
                if existing["environment"] == environment and existing["research_id"] == research_id and self.pipeline_job(existing["id"])["status"] in {"running", "paused", "cancelling"}:
                    raise DataError("このリサーチのpipeline jobは既に実行中です")
            job_id = uuid.uuid4().hex
            job_dir = self.root / "runtime" / environment / "pipeline_jobs" / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            control_file = job_dir / "control.json"
            events_file = job_dir / "progress.json"
            stdout_path = job_dir / "stdout.json"
            stderr_path = job_dir / "stderr.log"
            atomic_json(control_file, {"control": "run"})
            command = [
                sys.executable, "-X", "utf8", str(self.root / "tools/run_research_pipeline.py"), research_id,
                "--environment", environment, "--stage", mode,
                "--events-file", str(events_file), "--control-file", str(control_file),
                "--ollama-url", self.ollama_url,
                "--generation-workers", str(self.runtime_config.generation_workers),
                "--embedding-batch-size", str(self.runtime_config.embedding_batch_size),
                "--shard-size", str(self.runtime_config.shard_size),
                "--cooldown-every-documents", str(self.runtime_config.cooldown_every_documents),
                "--keep-alive", self.runtime_config.keep_alive,
            ]
            if self.runtime_config.durable_shards:
                command.append("--durable-shards")
            if overwrite:
                command.append("--overwrite")
            if mode == "execute" and cooldown_seconds:
                command.extend(["--cooldown-seconds", str(cooldown_seconds)])
            stdout_handle = stdout_path.open("wb")
            stderr_handle = stderr_path.open("wb")
            process = subprocess.Popen(command, cwd=self.root, stdout=stdout_handle, stderr=stderr_handle, shell=False)
            job = {
                "id": job_id, "environment": environment, "research_id": research_id, "mode": mode,
                "overwrite": overwrite,
                "cooldown_seconds": cooldown_seconds,
                "runtime_config": self.runtime_config.as_dict(),
                "ollama_runtime": self.ollama.status() if self.ollama else {"managed": False},
                "status": "running", "control": "run", "created_at": time.time(), "process": process,
                "control_file": control_file, "events_file": events_file, "stdout_path": stdout_path,
                "stderr_path": stderr_path, "stdout_handle": stdout_handle, "stderr_handle": stderr_handle,
            }
            self.pipeline_jobs[job_id] = job
            self.log("pipeline_job_started", job_id=job_id, research_id=research_id, mode=mode, environment=environment)
            return self.pipeline_job(job_id)

    def pipeline_job(self, job_id: str) -> dict:
        job = self.pipeline_jobs.get(job_id)
        if not job:
            raise DataError("pipeline jobが見つかりません")
        process = job["process"]
        returncode = process.poll()
        if returncode is not None and job["status"] not in {"completed", "failed", "cancelled"}:
            job["stdout_handle"].close(); job["stderr_handle"].close()
            job["status"] = "cancelled" if job["control"] == "cancel" else ("completed" if returncode == 0 else "failed")
            job["returncode"] = returncode
            job["completed_at"] = time.time()
            self.log("pipeline_job_finished", job_id=job_id, status=job["status"], returncode=returncode)
        progress = {}
        if job["events_file"].exists():
            try: progress = json.loads(job["events_file"].read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError): pass
        error = ""
        if job["status"] == "failed" and job["stderr_path"].exists():
            error = job["stderr_path"].read_text(encoding="utf-8", errors="replace")[-2000:]
            if not error and job["stdout_path"].exists(): error = job["stdout_path"].read_text(encoding="utf-8", errors="replace")[-2000:]
        return {key: value for key, value in job.items() if key not in {"process", "control_file", "events_file", "stdout_path", "stderr_path", "stdout_handle", "stderr_handle"}} | {"progress": progress, "error": error}

    def control_pipeline_job(self, job_id: str, control: str) -> dict:
        if control not in {"run", "pause", "cancel"}:
            raise DataError("pipeline controlが不正です")
        with self.lock:
            job = self.pipeline_jobs.get(job_id)
            if not job:
                raise DataError("pipeline jobが見つかりません")
            current = self.pipeline_job(job_id)
            if current["status"] in {"completed", "failed", "cancelled"}:
                raise DataError("終了したpipeline jobは制御できません")
            job["control"] = control
            atomic_json(job["control_file"], {"control": control})
            job["status"] = "paused" if control == "pause" else ("cancelling" if control == "cancel" else "running")
            self.log("pipeline_job_control", job_id=job_id, control=control)
            return self.pipeline_job(job_id)

    def stop_pipeline_jobs(self) -> None:
        with self.lock:
            for job in self.pipeline_jobs.values():
                if job["process"].poll() is None:
                    job["control"] = "cancel"
                    atomic_json(job["control_file"], {"control": "cancel"})
                    try:
                        job["process"].terminate()
                    except OSError:
                        pass

    def close(self) -> None:
        self.stop_pipeline_jobs()
        if self.ollama is not None:
            self.ollama.stop()

    def log(self, event: str, **detail):
        item = {"at": time.time(), "event": event, **detail}
        self.activity.append(item)
        self.activity[:] = self.activity[-300:]

    def touch(self, source: str) -> None:
        with self.lock:
            self.last_activity = time.time()
            self.log("activity", source=source)

    def idle_remaining(self) -> float:
        with self.lock:
            return max(0.0, self.idle_timeout - (time.time() - self.last_activity))

    def start_idle_monitor(self) -> None:
        def monitor():
            interval = min(5.0, max(0.1, self.idle_timeout / 5))
            while self.server is not None:
                time.sleep(interval)
                with self.lock:
                    pipeline_running = any(job["process"].poll() is None for job in self.pipeline_jobs.values())
                if pipeline_running:
                    continue
                if self.idle_remaining() <= 0:
                    self.log("idle_shutdown", idle_timeout=self.idle_timeout)
                    self.server.shutdown()
                    return

        threading.Thread(target=monitor, name="patent-viewer-idle-monitor", daemon=True).start()


class PatentViewerHandler(BaseHTTPRequestHandler):
    server_version = "PatentViewer/0.1"

    @property
    def state(self) -> AppState:
        return self.server.app_state  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        if getattr(self.server, "quiet", False):
            return
        super().log_message(fmt, *args)

    def _json(self, status: int, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            raise DataError("リクエストが大きすぎます")
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise DataError("JSONが不正です") from exc

    def _file(self, path: Path, content_type: str | None = None):
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc: Exception):
        status = HTTPStatus.NOT_FOUND if "見つかりません" in str(exc) else HTTPStatus.BAD_REQUEST
        self._json(status, {"error": str(exc)})

    def do_GET(self):
        try:
            self._get()
        except (DataError, OSError) as exc:
            self._error(exc)

    def _get(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)
        if path == "/api/health":
            return self._json(200, {
                "ok": True,
                "environment": self.state.environment,
                "version": "0.2.0",
                "idle_timeout_seconds": self.state.idle_timeout,
                "idle_remaining_seconds": round(self.state.idle_remaining(), 1),
                "ollama_runtime": self.state.ollama.status() if self.state.ollama else {"managed": False},
            })
        if path not in {"/api/ui/next"}:
            self.state.touch(f"GET {path}")
        if path == "/api/environment":
            return self._json(200, {"environment": self.state.environment})
        if path == "/api/researches":
            env = query.get("environment", [self.state.environment])[0]
            return self._json(200, {"environment": env, "items": self.state.repo.list_researches(env)})
        if path.startswith("/api/researches/") and path.endswith("/dashboard"):
            research_id = path.split("/")[3]
            env = query.get("environment", [self.state.environment])[0]
            return self._json(200, self.state.repo.dashboard(env, research_id))
        if path.startswith("/api/researches/") and path.endswith("/llm-preflight"):
            research_id = path.split("/")[3]
            env = query.get("environment", [self.state.environment])[0]
            return self._json(200, self.state.preflight(env, research_id))
        if path.startswith("/api/researches/") and path.endswith("/pipeline"):
            research_id = path.split("/")[3]
            env = query.get("environment", [self.state.environment])[0]
            overview = ResearchPipeline(self.state.root, env, research_id).overview()
            active = [self.state.pipeline_job(job_id) for job_id, job in self.state.pipeline_jobs.items() if job["environment"] == env and job["research_id"] == research_id]
            overview["jobs"] = sorted(active, key=lambda item: item["created_at"], reverse=True)[:10]
            return self._json(200, overview)
        if path.startswith("/api/pipeline/jobs/"):
            return self._json(200, self.state.pipeline_job(path.split("/")[4]))
        if path.startswith("/api/pdfs/"):
            pdf_name = path.removeprefix("/api/pdfs/")
            return self._file(self.state.repo.pdf_path(pdf_name), "application/pdf")
        if path == "/api/ui-protocol":
            return self._json(200, {"version": 2, "actions": PROTOCOL_ACTIONS, "max_actions": 100, "same_visible_dom": True, "capabilities": ["research-pipeline", "pipeline-control"], "pipeline_controls": ["run", "pause", "cancel"]})
        if path == "/api/ui/clients":
            now = time.time()
            with self.state.lock:
                items = [c for c in self.state.clients.values() if now - c["heartbeat_at"] < 20]
            return self._json(200, {"items": items})
        if path == "/api/ui/next":
            return self._claim_next(query.get("clientId", [""])[0])
        if path.startswith("/api/ui/commands/") and path.endswith("/control"):
            command = self._command(path.split("/")[4])
            return self._json(200, {"command_id": command["id"], "control": command["control"]})
        if path.startswith("/api/ui/commands/"):
            return self._json(200, self._command(path.split("/")[4]))
        if path == "/api/ui/activity":
            return self._json(200, {"items": list(reversed(self.state.activity[-100:]))})
        return self._static(path)

    def _static(self, path: str):
        relative = "index.html" if path == "/" else path.lstrip("/")
        public = (self.state.root / "public").resolve()
        target = (public / relative).resolve()
        if target != public and public not in target.parents:
            raise DataError("不正なパスです")
        if not target.is_file():
            raise DataError("ファイルが見つかりません")
        return self._file(target)

    def do_POST(self):
        try:
            self._post()
        except (DataError, OSError) as exc:
            self._error(exc)

    def _post(self):
        path = urlparse(self.path).path
        body = self._body()
        if path == "/api/admin/shutdown":
            return self._shutdown()
        if path != "/api/ui/clients/heartbeat":
            self.state.touch(f"POST {path}")
        if path == "/api/environment":
            env = body.get("environment")
            self.state.repo.paths(env)
            self.state.environment = env
            self.state.log("environment_changed", environment=env)
            return self._json(200, {"environment": env, "reload_required": True})
        if path == "/api/interpretations":
            env = body.get("environment", self.state.environment)
            if env != self.state.environment:
                return self._json(409, {"error": "画面とサーバーの環境が一致しません"})
            if body.get("source") == "codex" and not self._authorized_codex(env):
                return self._json(403, {"error": "Codexの保存は実行中の可視UIコマンドが必要です"})
            target = self.state.repo.save_interpretation(env, body)
            self.state.log("interpretation_saved", environment=env, patent_id=body.get("patent_id"))
            return self._json(201, {"ok": True, "path": str(target.relative_to(self.state.root))})
        if path.startswith("/api/researches/") and path.endswith("/pipeline/jobs"):
            env = body.get("environment", self.state.environment)
            if env != self.state.environment:
                return self._json(409, {"error": "画面とサーバーの環境が一致しません"})
            research_id = path.split("/")[3]
            mode = body.get("mode")
            if body.get("source") == "codex" and not self._authorized_codex(env):
                return self._json(403, {"error": "Codexのpipeline実行は実行中の可視UIコマンドが必要です"})
            if mode == "execute":
                if body.get("confirmation") != "RUN_LOCAL_LLM":
                    raise DataError("ローカルLLM実行の明示確認が必要です")
                preflight = self.state.preflight(env, research_id)
                if not preflight["ready"]:
                    raise DataError("LLM preflightがreadyではありません")
            try:
                cooldown_seconds = int(body.get("cooldown_seconds", 0)) if mode == "execute" else 0
            except (TypeError, ValueError) as exc:
                raise DataError("cooldown_secondsは整数で指定してください") from exc
            job = self.state.start_pipeline_job(env, research_id, mode, bool(body.get("overwrite")), cooldown_seconds)
            return self._json(202, job)
        if path.startswith("/api/pipeline/jobs/") and path.endswith("/control"):
            return self._json(200, self.state.control_pipeline_job(path.split("/")[4], str(body.get("control", ""))))
        if path == "/api/ui/clients/heartbeat":
            return self._heartbeat(body)
        if path == "/api/ui/commands":
            return self._create_command(body)
        if path.startswith("/api/ui/commands/") and path.endswith("/events"):
            return self._command_event(path.split("/")[4], body)
        if path.startswith("/api/ui/commands/") and path.endswith("/control"):
            return self._control(path.split("/")[4], body)
        raise DataError("APIが見つかりません")

    def _heartbeat(self, body):
        client_id = str(body.get("clientId", ""))
        if not client_id or len(client_id) > 100:
            raise DataError("clientIdが不正です")
        targets = body.get("targets", [])
        if not isinstance(targets, list) or len(targets) > 500:
            raise DataError("targetsが不正です")
        user_active_at = body.get("userActiveAt")
        if isinstance(user_active_at, (int, float)):
            user_active_at = float(user_active_at) / 1000.0
            now = time.time()
            if self.state.last_user_event_at < user_active_at <= now + 5:
                self.state.last_user_event_at = user_active_at
                self.state.touch("browser_user_event")
        client = {
            "clientId": client_id,
            "url": str(body.get("url", ""))[:500],
            "title": str(body.get("title", ""))[:200],
            "module": str(body.get("module", "patent-viewer"))[:100],
            "environment": self.state.environment,
            "capabilities": body.get("capabilities", []),
            "targets": targets,
            "currentCommandId": body.get("currentCommandId"),
            "heartbeat_at": time.time(),
        }
        with self.state.lock:
            self.state.clients[client_id] = client
        return self._json(200, {"ok": True, "environment": self.state.environment})

    def _shutdown(self):
        supplied = self.headers.get("X-PatentViewer-Control-Token", "")
        local = self.client_address[0] in {"127.0.0.1", "::1"}
        if not local or not self.state.control_token or not hmac.compare_digest(supplied, self.state.control_token):
            return self._json(403, {"error": "shutdown authorization failed"})
        self.state.log("manual_shutdown")
        self._json(202, {"ok": True, "status": "stopping"})
        threading.Thread(target=self.server.shutdown, name="patent-viewer-shutdown", daemon=True).start()

    def _create_command(self, body):
        actor = body.get("actor", "codex")
        client_id = body.get("targetClientId")
        actions = body.get("actions")
        if actor != "codex" or not str(body.get("intent", "")).strip():
            raise DataError("actor=codexとintentが必要です")
        if not isinstance(actions, list) or not 1 <= len(actions) <= 100:
            raise DataError("actionsは1〜100件です")
        if any(a.get("type") not in PROTOCOL_ACTIONS for a in actions):
            raise DataError("未対応のactionがあります")
        with self.state.lock:
            client = self.state.clients.get(client_id)
            if not client or time.time() - client["heartbeat_at"] >= 20:
                raise DataError("対象clientが見つかりません")
            command_id = uuid.uuid4().hex
            command = {
                "id": command_id,
                "actor": actor,
                "intent": str(body["intent"])[:500],
                "targetClientId": client_id,
                "environment": self.state.environment,
                "module": client["module"],
                "actions": actions,
                "status": "queued",
                "control": "run",
                "claimedBy": None,
                "created_at": time.time(),
                "events": [],
            }
            self.state.commands[command_id] = command
            self.state.log("command_queued", command_id=command_id, intent=command["intent"])
        return self._json(201, command)

    def _claim_next(self, client_id):
        with self.state.lock:
            client = self.state.clients.get(client_id)
            if not client or time.time() - client["heartbeat_at"] >= 20:
                raise DataError("clientが見つかりません")
            for command in self.state.commands.values():
                if command["status"] == "queued" and command["targetClientId"] == client_id:
                    if command["environment"] != self.state.environment:
                        command["status"] = "failed"
                        command["events"].append({"type": "failed", "error": "environment changed", "at": time.time()})
                        continue
                    command["status"] = "running"
                    command["claimedBy"] = client_id
                    self.state.log("command_claimed", command_id=command["id"], client_id=client_id)
                    return self._json(200, {"command": command})
        return self._json(200, {"command": None})

    def _command(self, command_id):
        with self.state.lock:
            command = self.state.commands.get(command_id)
            if not command:
                raise DataError("コマンドが見つかりません")
            return command

    def _command_event(self, command_id, body):
        with self.state.lock:
            command = self._command(command_id)
            if command["claimedBy"] != body.get("clientId"):
                return self._json(403, {"error": "claimしたclientではありません"})
            event_type = body.get("type")
            allowed = {"started", "step_started", "step_completed", "completed", "failed", "cancelled"}
            if event_type not in allowed:
                raise DataError("event typeが不正です")
            event = {k: v for k, v in body.items() if k != "clientId"}
            event["at"] = time.time()
            command["events"].append(event)
            if event_type in TERMINAL:
                command["status"] = event_type
                command["control"] = "run"
            self.state.log("command_event", command_id=command_id, type=event_type)
        return self._json(200, {"ok": True, "status": command["status"]})

    def _control(self, command_id, body):
        value = body.get("control")
        if value not in {"run", "pause", "cancel"}:
            raise DataError("controlが不正です")
        with self.state.lock:
            command = self._command(command_id)
            if command["status"] in TERMINAL:
                raise DataError("終端コマンドは制御できません")
            command["control"] = value
            self.state.log("command_control", command_id=command_id, control=value)
        return self._json(200, {"ok": True, "control": value})

    def _authorized_codex(self, environment):
        command_id = self.headers.get("X-PatentViewer-UI-Command", "")
        client_id = self.headers.get("X-PatentViewer-UI-Client", "")
        with self.state.lock:
            command = self.state.commands.get(command_id)
            client = self.state.clients.get(client_id)
            return bool(
                command and client
                and command["actor"] == "codex"
                and command["status"] == "running"
                and command["claimedBy"] == client_id
                and command["environment"] == environment == self.state.environment
                and command["module"] == client["module"] == "patent-viewer"
                and time.time() - client["heartbeat_at"] < 20
            )


def create_server(root: Path, host="127.0.0.1", port=8765, quiet=False, idle_timeout=1800, control_token="", manage_ollama=False, generation_workers=None, embedding_batch_size=None):
    server = ThreadingHTTPServer((host, port), PatentViewerHandler)
    server.app_state = AppState(
        root, idle_timeout=idle_timeout, control_token=control_token,
        manage_ollama=manage_ollama, generation_workers=generation_workers,
        embedding_batch_size=embedding_batch_size,
    )  # type: ignore[attr-defined]
    server.app_state.server = server  # type: ignore[attr-defined]
    server.quiet = quiet  # type: ignore[attr-defined]
    server.app_state.start_idle_monitor()  # type: ignore[attr-defined]
    return server


def main():
    parser = argparse.ArgumentParser(description="PatentViewer local server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--idle-timeout", type=float, default=1800, help="seconds without user/Codex activity before shutdown")
    parser.add_argument("--control-file", type=Path)
    parser.add_argument("--generation-workers", type=int, help="override automatic VRAM-derived generation concurrency")
    parser.add_argument("--embedding-batch-size", type=int, help="override automatic embedding input batch size")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    if args.generation_workers is not None and not 1 <= args.generation_workers <= 8:
        parser.error("--generation-workers must be between 1 and 8")
    if args.embedding_batch_size is not None and (
        not 2 <= args.embedding_batch_size <= 1024 or args.embedding_batch_size % 2
    ):
        parser.error("--embedding-batch-size must be an even number between 2 and 1024")
    control_token = uuid.uuid4().hex
    server = create_server(
        args.root, args.host, args.port, quiet=args.quiet, idle_timeout=args.idle_timeout,
        control_token=control_token, manage_ollama=True,
        generation_workers=args.generation_workers, embedding_batch_size=args.embedding_batch_size,
    )
    control_file = (args.control_file or (args.root / "runtime/server-control.json")).resolve()
    control_file.parent.mkdir(parents=True, exist_ok=True)
    control_payload = {
        "pid": os.getpid(), "host": args.host, "port": args.port,
        "url": f"http://{args.host}:{args.port}", "token": control_token,
        "idle_timeout_seconds": args.idle_timeout, "started_at": time.time(),
    }
    temp_control = control_file.with_suffix(".tmp")
    temp_control.write_text(json.dumps(control_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_control, control_file)
    print(f"PatentViewer: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.app_state.close()  # type: ignore[attr-defined]
        server.server_close()
        try:
            current = json.loads(control_file.read_text(encoding="utf-8"))
            if current.get("token") == control_token:
                control_file.unlink()
        except (OSError, json.JSONDecodeError):
            pass


if __name__ == "__main__":
    main()
