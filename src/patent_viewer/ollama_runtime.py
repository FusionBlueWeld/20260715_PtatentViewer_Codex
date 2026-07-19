from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


MIB = 1024 * 1024


@dataclass(frozen=True)
class GpuInfo:
    name: str
    uuid: str
    total_mib: int
    free_mib: int


@dataclass(frozen=True)
class AdaptiveRuntimeConfig:
    mode: str
    gpu: GpuInfo | None
    reserved_mib: int
    generation_workers: int
    embedding_batch_size: int
    shard_size: int
    cooldown_every_documents: int
    max_loaded_models: int = 1
    keep_alive: str = "1h"

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["gpu"] = asdict(self.gpu) if self.gpu else None
        return result


def _run_text(command: list[str], timeout: float = 5) -> str:
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, check=True, shell=False,
    )
    return completed.stdout


def detect_nvidia_gpu() -> GpuInfo | None:
    """Return the NVIDIA GPU with the largest amount of free VRAM."""
    executable = shutil.which("nvidia-smi")
    if not executable:
        return None
    try:
        output = _run_text([
            executable,
            "--query-gpu=name,uuid,memory.total,memory.free",
            "--format=csv,noheader,nounits",
        ])
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    candidates: list[GpuInfo] = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 4:
            continue
        try:
            candidates.append(GpuInfo(parts[0], parts[1], int(parts[2]), int(parts[3])))
        except ValueError:
            continue
    return max(candidates, key=lambda item: item.free_mib, default=None)


def adaptive_runtime_config(
    gpu: GpuInfo | None,
    generation_workers: int | None = None,
    embedding_batch_size: int | None = None,
) -> AdaptiveRuntimeConfig:
    """Derive safe throughput settings from memory capacity, not GPU labels.

    The estimates deliberately include headroom. A later real-model benchmark may
    override these values without changing the pipeline implementation.
    """
    if gpu is None:
        workers = generation_workers or 1
        return AdaptiveRuntimeConfig(
            mode="fallback", gpu=None, reserved_mib=0,
            generation_workers=max(1, workers),
            embedding_batch_size=embedding_batch_size or 16,
            shard_size=250, cooldown_every_documents=50,
        )
    reserved = max(2048, round(gpu.total_mib * 0.15))
    usable = max(0, min(gpu.total_mib - reserved, gpu.free_mib - 512))
    # gemma4:e4b is currently about 9.6 GB. Include runner/framing space and
    # estimate one 16K KV slot conservatively. The formula naturally selects
    # one worker on the test GPU and two only when there is real headroom.
    generation_base_mib = 12_000
    kv_slot_mib = 3_000
    automatic_workers = max(1, min(2, (usable - generation_base_mib) // kv_slot_mib))
    workers = max(1, generation_workers or automatic_workers)
    embed_batch = embedding_batch_size or 32 * workers
    return AdaptiveRuntimeConfig(
        mode="manual" if generation_workers or embedding_batch_size else "auto",
        gpu=gpu, reserved_mib=reserved, generation_workers=workers,
        embedding_batch_size=max(2, embed_batch), shard_size=250 * workers,
        cooldown_every_documents=50 * workers,
    )


def find_ollama_executable() -> Path | None:
    configured = os.environ.get("PATENT_VIEWER_OLLAMA_EXE")
    candidates = [Path(configured)] if configured else []
    discovered = shutil.which("ollama")
    if discovered:
        candidates.append(Path(discovered))
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(Path(local_app_data) / "Programs" / "Ollama" / "ollama.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class ManagedOllama:
    """Own one isolated Ollama server for the lifetime of the application."""

    def __init__(self, runtime_dir: Path, config: AdaptiveRuntimeConfig, executable: Path | None = None):
        self.runtime_dir = runtime_dir
        self.config = config
        self.executable = executable or find_ollama_executable()
        self.port = free_local_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen | None = None
        self.stdout_handle = None
        self.stderr_handle = None
        self.error = ""

    def start(self, timeout: float = 20) -> bool:
        if not self.executable:
            self.error = "Ollama executable was not found"
            return False
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.stdout_handle = (self.runtime_dir / "ollama.stdout.log").open("ab")
        self.stderr_handle = (self.runtime_dir / "ollama.stderr.log").open("ab")
        environment = os.environ.copy()
        environment.update({
            "OLLAMA_HOST": f"127.0.0.1:{self.port}",
            "OLLAMA_NUM_PARALLEL": str(self.config.generation_workers),
            "OLLAMA_MAX_LOADED_MODELS": str(self.config.max_loaded_models),
            "OLLAMA_KEEP_ALIVE": self.config.keep_alive,
            "OLLAMA_FLASH_ATTENTION": "1",
        })
        kwargs: dict[str, Any] = {
            "cwd": self.runtime_dir,
            "env": environment,
            "stdout": self.stdout_handle,
            "stderr": self.stderr_handle,
            "shell": False,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self.process = subprocess.Popen([str(self.executable), "serve"], **kwargs)
        except OSError as exc:
            self.error = str(exc)
            self.stop()
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.error = f"Ollama exited with code {self.process.returncode}"
                self.stop()
                return False
            try:
                with urllib.request.urlopen(self.url + "/api/tags", timeout=1) as response:
                    json.loads(response.read().decode("utf-8"))
                return True
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
                time.sleep(0.2)
        self.error = "Timed out waiting for the managed Ollama server"
        self.stop()
        return False

    def status(self) -> dict[str, Any]:
        running = self.process is not None and self.process.poll() is None
        return {
            "managed": True, "running": running, "url": self.url if running else None,
            "pid": self.process.pid if running else None, "error": self.error,
            "config": self.config.as_dict(),
        }

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        for handle in (self.stdout_handle, self.stderr_handle):
            if handle and not handle.closed:
                handle.close()
        self.stdout_handle = None
        self.stderr_handle = None
