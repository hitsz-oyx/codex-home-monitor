import json
import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import AppServerClient, AppServerError


FAKE_SERVER = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, sys
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if method == "initialize":
            print(json.dumps({"id": request["id"], "result": {"codexHome": "fake"}}), flush=True)
        elif method == "thread/resume":
            print(json.dumps({"id": request["id"], "result": {"thread": {"id": request["params"]["threadId"]}}}), flush=True)
        elif method == "thread/start":
            print(json.dumps({"id": request["id"], "result": {"thread": {"id": "thread-new"}}}), flush=True)
        elif method == "turn/start":
            print(json.dumps({"id": request["id"], "result": {"turn": {"id": "turn-1"}}}), flush=True)
            print(json.dumps({"method": "item/agentMessage/delta", "params": {"delta": "hello"}}), flush=True)
            print(json.dumps({"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "completed"}}}), flush=True)
''')


class AppServerTests(unittest.TestCase):
    def test_stdio_turn_streams_delta_and_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "codex"
            fake.write_text(FAKE_SERVER)
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            with patch("server.find_app_server_binary", return_value=fake):
                client = AppServerClient(Path(directory))
                events = list(client.run_turn("thread-1", "hello"))
            self.assertEqual(events[0]["type"], "started")
            self.assertEqual(events[1]["params"]["delta"], "hello")
            self.assertEqual(events[-1]["type"], "completed")

    def test_missing_binary_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(AppServerError):
                list(AppServerClient(Path(directory)).run_turn("thread-1", "hello"))

    def test_new_thread_streams_created_event(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "codex"
            fake.write_text(FAKE_SERVER)
            fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
            with patch("server.find_app_server_binary", return_value=fake):
                events = list(AppServerClient(Path(directory)).run_new_thread("hello"))
            self.assertEqual(events[0]["type"], "created")
            self.assertEqual(events[0]["thread_id"], "thread-new")
            self.assertEqual(events[-1]["type"], "completed")


if __name__ == "__main__":
    unittest.main()
