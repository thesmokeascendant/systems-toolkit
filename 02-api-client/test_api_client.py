"""
Unit/integration tests for api_client.py

These spin up a real local HTTP server (127.0.0.1, ephemeral port) so the
retry/backoff/error-handling logic is exercised against actual HTTP
responses rather than mocked internals — while staying fully offline.

Run with:
    python3 -m unittest discover -s tests -v
"""

import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api_client import (  # noqa: E402
    APIClient,
    APIConnectionError,
    APIDecodeError,
    ClientError,
    RateLimitExceeded,
    ServerError,
)


class ScriptedHandler(BaseHTTPRequestHandler):
    """
    Handler whose behavior per path is controlled by a class-level script,
    so each test can program exactly the failure sequence it wants to
    exercise (e.g. "500, 500, then 200").
    """

    script: dict = {}
    call_counts: dict = {}

    def log_message(self, format, *args):
        pass  # silence default request logging

    def do_GET(self):
        path = self.path
        ScriptedHandler.call_counts[path] = ScriptedHandler.call_counts.get(path, 0) + 1
        attempt = ScriptedHandler.call_counts[path] - 1

        steps = ScriptedHandler.script.get(path, [])
        step = steps[min(attempt, len(steps) - 1)] if steps else {"status": 200, "body": {}}

        if step.get("hang"):
            time.sleep(step["hang"])

        status = step.get("status", 200)
        self.send_response(status)
        if "retry_after" in step:
            self.send_header("Retry-After", str(step["retry_after"]))
        self.send_header("Content-Type", "application/json")
        self.end_headers()

        body = step.get("raw_body")
        if body is None:
            body = json.dumps(step.get("body", {}))
        try:
            self.wfile.write(body.encode("utf-8"))
        except BrokenPipeError:
            # Expected when a client times out and disconnects before we
            # finish writing (see test_timeout_raises_after_retries) — the
            # client's timeout firing is the whole point of that test.
            pass


class TestAPIClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), ScriptedHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        ScriptedHandler.script = {}
        ScriptedHandler.call_counts = {}

    def _client(self, max_retries=3, timeout=10.0):
        # sleep_fn=lambda: None keeps retry tests fast — we're testing the
        # retry *logic*, not real wall-clock backoff.
        return APIClient(
            self.base_url, timeout=timeout, max_retries=max_retries, sleep_fn=lambda s: None
        )

    def test_successful_get_returns_json(self):
        ScriptedHandler.script["/ok"] = [{"status": 200, "body": {"hello": "world"}}]
        client = self._client()
        self.assertEqual(client.get("/ok"), {"hello": "world"})

    def test_retries_on_500_then_succeeds(self):
        ScriptedHandler.script["/flaky"] = [
            {"status": 500, "body": {}},
            {"status": 500, "body": {}},
            {"status": 200, "body": {"recovered": True}},
        ]
        client = self._client(max_retries=3)
        self.assertEqual(client.get("/flaky"), {"recovered": True})

    def test_server_error_after_exhausted_retries_raises(self):
        ScriptedHandler.script["/always-down"] = [{"status": 503, "body": {}}]
        client = self._client(max_retries=2)
        with self.assertRaises(ServerError) as ctx:
            client.get("/always-down")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_client_error_does_not_retry(self):
        ScriptedHandler.script["/not-found"] = [{"status": 404, "body": {}}]
        client = self._client(max_retries=5)
        with self.assertRaises(ClientError):
            client.get("/not-found")
        # A 404 shouldn't be retried at all — exactly one call.
        self.assertEqual(ScriptedHandler.call_counts["/not-found"], 1)

    def test_rate_limit_respects_retry_after_then_succeeds(self):
        ScriptedHandler.script["/limited"] = [
            {"status": 429, "retry_after": 1},
            {"status": 200, "body": {"ok": True}},
        ]
        client = self._client(max_retries=2)
        self.assertEqual(client.get("/limited"), {"ok": True})

    def test_rate_limit_exhausted_raises_with_retry_after(self):
        ScriptedHandler.script["/always-limited"] = [{"status": 429, "retry_after": 2}]
        client = self._client(max_retries=1)
        with self.assertRaises(RateLimitExceeded) as ctx:
            client.get("/always-limited")
        self.assertEqual(ctx.exception.retry_after, 2.0)

    def test_malformed_json_raises_decode_error_not_crash(self):
        ScriptedHandler.script["/bad-json"] = [
            {"status": 200, "raw_body": "{not: valid json,,,"}
        ]
        client = self._client()
        with self.assertRaises(APIDecodeError) as ctx:
            client.get("/bad-json")
        self.assertIn("not: valid json", ctx.exception.raw_body)

    def test_empty_body_returns_none(self):
        ScriptedHandler.script["/empty"] = [{"status": 200, "raw_body": ""}]
        client = self._client()
        self.assertIsNone(client.get("/empty"))

    def test_connection_error_when_server_unreachable(self):
        # Port 1 is a privileged, almost-certainly-closed port — connecting
        # should fail fast with a connection error, not hang or crash.
        client = APIClient("http://127.0.0.1:1", timeout=2.0, max_retries=0, sleep_fn=lambda s: None)
        with self.assertRaises(APIConnectionError):
            client.get("/anything")

    def test_timeout_raises_after_retries(self):
        ScriptedHandler.script["/slow"] = [{"status": 200, "body": {}, "hang": 0.5}]
        client = self._client(max_retries=0, timeout=0.1)
        with self.assertRaises(Exception):
            # Either APITimeoutError directly, or wrapped — either way it
            # must not hang forever or return silently wrong data.
            client.get("/slow")


if __name__ == "__main__":
    unittest.main()
