"""Shared harness for the agent-facing job endpoints.

The setUp/tearDown/request helpers live here so the endpoint tests can be split
across files without one of them sitting permanently at the line ceiling. This
is a mixin rather than a TestCase, so a file that only wants the harness does
not inherit every test in the file that owns it.
"""
import json
import tempfile
import threading
import time
from http.client import HTTPConnection
from pathlib import Path

from cindral.cache import CacheStore
from cindral.jobs import JobStore
from dataclasses import replace

from cindral.policy import Policy, load_dispatch_targets
from cindral.retry import MAX_ATTEMPTS, MAX_BACKOFF_SECONDS
from cindral.service import WEBHOOK_PATH, CindralHandler, CindralServer
from cindral.state import load_runners

AGENT_TOKEN = "agent-token"
POOL_TOKEN = "pool-token"
SECRET = "webhook-secret"
REPO = "example-org/example-app"


class JobEndpointHarness:
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.server = CindralServer(("127.0.0.1", 0), CindralHandler)
        # These tests exercise the device path, so the policy says the device
        # is the lane a push takes. A served repository prefers the hosted lane
        # by default, which is covered in tests/test_webhook.py.
        self.server.policy = replace(
            Policy.load("config/policy.toml"),
            dispatch_targets=load_dispatch_targets({"dispatch": {"repositories": [
                {"mode": "both", "repository": "example-org/example-app",
                 "workflows": ["cindral-dispatch.yml"], "refs": ["refs/heads/main"],
                 "push_lane": "device"}]}}),
        )
        self.server.runners = load_runners("examples/state.json")
        self.server.github = None
        self.server.webhook_secret = SECRET
        self.server.dispatch_token = "dispatch-token"
        self.server.workflow_file = "cindral-dispatch.yml"
        self.server.capacity_lock = threading.Lock()
        self.server.runner_reservations = {}
        self.server.reservation_seconds = 10
        self.server.jobs = JobStore(str(Path(self._tmp.name) / "jobs.db"))
        self.server.cache = CacheStore(Path(self._tmp.name) / "cache")
        self.server.agent_token = AGENT_TOKEN
        self.server.pool_token = POOL_TOKEN
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, body=None, headers=None, authorize=True, token=None):
        merged = {"Content-Type": "application/json"}
        if authorize:
            merged["Authorization"] = f"Bearer {token or AGENT_TOKEN}"
        merged.update(headers or {})
        payload = body if isinstance(body, (bytes, bytearray)) else (json.dumps(body).encode() if body is not None else None)
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(method, path, body=payload, headers=merged)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, (json.loads(raw) if raw else {})

    def fetch(self, method, path, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(method, path, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        content_type = response.getheader("Content-Type", "")
        cors = response.getheader("Access-Control-Allow-Origin")
        connection.close()
        return response.status, raw.decode(), content_type, cors

    def deliver(self, body, headers):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("POST", WEBHOOK_PATH, body=body, headers={"Content-Type": "application/json", **headers})
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, (json.loads(raw) if raw else {})

    def heartbeat(self, body=None, **kwargs):
        payload = {"device": "server", "labels": ["arm64"], "max_concurrency": 2}
        return self.call("POST", "/v1/agents/heartbeat", payload if body is None else body, **kwargs)

    def lose_attempts(self, job, count, error="docker is not running"):
        """Spend `count` attempts on a device that reports a device error each time."""
        base = time.time()
        for index in range(count):
            at = base - (count - index) * (MAX_BACKOFF_SECONDS + MAX_ATTEMPTS)
            self.server.jobs.claim("burst", [], lease_seconds=MAX_BACKOFF_SECONDS, now=at)
            self.server.jobs.report(job.id, "burst", None, now=at + 1, error=error)

