import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from patent_viewer.ollama_runtime import (
    GpuInfo,
    ManagedOllama,
    adaptive_runtime_config,
    detect_nvidia_gpu,
    detect_system_memory_mib,
)


class OllamaRuntimeTests(unittest.TestCase):
    def test_runtime_capacity_is_derived_from_available_vram(self):
        constrained = adaptive_runtime_config(GpuInfo("test", "gpu-a", 16376, 15300), system_memory_mib=16 * 1024)
        roomy = adaptive_runtime_config(GpuInfo("production", "gpu-b", 24564, 23500), system_memory_mib=128 * 1024)
        self.assertEqual(constrained.generation_workers, 1)
        self.assertEqual(roomy.generation_workers, 2)
        self.assertGreater(roomy.embedding_batch_size, constrained.embedding_batch_size)
        self.assertGreater(roomy.shard_size, constrained.shard_size)
        self.assertEqual(roomy.mode, "auto")
        self.assertFalse(constrained.durable_shards)
        self.assertTrue(roomy.durable_shards)
        self.assertEqual(roomy.system_memory_mib, 128 * 1024)

    def test_system_memory_detection_returns_a_nonnegative_value(self):
        self.assertGreaterEqual(detect_system_memory_mib(), 0)

    def test_manual_values_override_capacity_estimate(self):
        config = adaptive_runtime_config(
            GpuInfo("gpu", "gpu-c", 16376, 15000), generation_workers=2, embedding_batch_size=48,
            system_memory_mib=16 * 1024,
        )
        self.assertEqual(config.generation_workers, 2)
        self.assertEqual(config.embedding_batch_size, 48)
        self.assertEqual(config.mode, "manual")

    @patch("patent_viewer.ollama_runtime._run_text")
    @patch("patent_viewer.ollama_runtime.shutil.which", return_value="nvidia-smi")
    def test_gpu_detection_selects_most_available_memory(self, _which, run_text):
        run_text.return_value = "GPU A, GPU-a, 16384, 12000\nGPU B, GPU-b, 24576, 22000\n"
        detected = detect_nvidia_gpu()
        self.assertEqual(detected.uuid, "GPU-b")
        self.assertEqual(detected.free_mib, 22000)

    def test_managed_server_uses_isolated_port_and_adaptive_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "ollama.exe"
            executable.write_bytes(b"fixture")
            config = adaptive_runtime_config(GpuInfo("gpu", "gpu-d", 24564, 23000))
            process = MagicMock()
            process.poll.return_value = None
            process.pid = 123
            response = MagicMock()
            response.__enter__.return_value.read.return_value = b'{"models": []}'
            with patch("patent_viewer.ollama_runtime.subprocess.Popen", return_value=process) as popen, patch(
                "patent_viewer.ollama_runtime.urllib.request.urlopen", return_value=response
            ):
                manager = ManagedOllama(Path(temporary) / "runtime", config, executable)
                self.assertTrue(manager.start(timeout=1))
                command = popen.call_args.args[0]
                environment = popen.call_args.kwargs["env"]
                self.assertEqual(command, [str(executable.resolve()), "serve"])
                self.assertEqual(environment["OLLAMA_NUM_PARALLEL"], "2")
                self.assertEqual(environment["OLLAMA_MAX_LOADED_MODELS"], "1")
                self.assertIn(str(manager.port), environment["OLLAMA_HOST"])
                self.assertTrue(manager.status()["running"])
                manager.stop()
                process.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
