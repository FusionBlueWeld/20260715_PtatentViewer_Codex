import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request, urlopen

from tests.support import build_fixture

import sys
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from patent_viewer.server import create_server


class LifecycleTests(unittest.TestCase):
    def make_server(self, idle_timeout=60, token="test-control-token"):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name); build_fixture(root)
        server = create_server(root, port=0, quiet=True, idle_timeout=idle_timeout, control_token=token)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        return temp, server, thread

    def test_idle_timeout_ignores_health_checks(self):
        temp, server, thread = self.make_server(idle_timeout=0.5)
        try:
            port = server.server_address[1]
            with urlopen(f"http://127.0.0.1:{port}/api/health") as response:
                health = json.loads(response.read())
            self.assertLessEqual(health["idle_timeout_seconds"], 0.5)
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), "health polling must not prevent idle shutdown")
        finally:
            server.server_close(); temp.cleanup()

    def test_token_required_for_graceful_shutdown(self):
        temp, server, thread = self.make_server()
        try:
            port = server.server_address[1]
            bad = Request(f"http://127.0.0.1:{port}/api/admin/shutdown", data=b"{}", method="POST")
            with self.assertRaises(Exception): urlopen(bad)
            good = Request(
                f"http://127.0.0.1:{port}/api/admin/shutdown", data=b"{}", method="POST",
                headers={"X-PatentViewer-Control-Token": "test-control-token", "Content-Type": "application/json"},
            )
            with urlopen(good) as response: self.assertEqual(response.status, 202)
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
        finally:
            server.server_close(); temp.cleanup()


if __name__ == "__main__": unittest.main()
