import io
import shutil
import json
import pathlib
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from cindral.build_info import BuildInfo
from cindral.agent import (
    MAX_CONCURRENCY,
    MAX_ERROR_CHARS,
    MAX_HEARTBEAT_SECONDS,
    MIN_IDLE_SECONDS,
    ONLINE_WINDOW_SECONDS,
    Agent,
    CindralClient,
    CindralError,
    JobSpec,
)

# a stalled reply has to outlast the client timeout, and the client timeout has
# to stay short enough that a test which waits on it stays quick
STALL_SECONDS = 1.0
CLIENT_TIMEOUT = 0.4
JOB_BODY = {
    "job": {
        "id": "job-1",
        "repository": "example-org/example-app",
        "sha": "abc123",
        "ref": "main",
        "command": ["pnpm", "ci"],
        "timeout": 3600,
    }
}


class Response:
    def __init__(self, body=b"", status=200):
        self.body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


def job(job_id="job-1", command=("pnpm", "ci")):
    return JobSpec(
        id=job_id,
        repository="example-org/example-app",
        sha="abc123",
        ref="main",
        command=command,
        timeout=3600,
    )


class StalledHandler(BaseHTTPRequestHandler):
    """A real broker socket that can stop or hang up part way through a reply.

    Each path answers from the list the test scripted: a dict is sent as JSON,
    while "stall" sends only the first bytes of a body and then goes quiet, and
    "cut" sends those bytes and closes the connection.
    """

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with self.server.lock:
            self.server.paths.append(self.path)
            scripted = self.server.script.get(self.path, [])
            reply = scripted.pop(0) if scripted else {}
        if reply in ("stall", "cut"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "64")
            self.end_headers()
            self.wfile.write(b'{"job')
            self.wfile.flush()
            if reply == "cut":
                self.connection.close()
            else:
                time.sleep(STALL_SECONDS)
            return
        body = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args) -> None:
        pass


class FakeClient:
    def __init__(self, jobs=(), fail_renew=False, fail_heartbeat=False):
        self._jobs = list(jobs)
        self._fail_renew = fail_renew
        self._fail_heartbeat = fail_heartbeat
        self.claims = []
        self.renewals = []
        self.reports = []
        self.losses = []
        self.logs = []
        self.reported_steps = []
        self.heartbeats = []
        #: what the broker said it is running. Empty means it has not said, which
        #: the agent must treat as unknown rather than as up to date.
        self.broker_build: dict[str, str] = {}
        self._lock = threading.Lock()

    def claim(self, device, labels=(), lease_seconds=300):
        self.claims.append((device, tuple(labels), lease_seconds))
        with self._lock:
            return self._jobs.pop(0) if self._jobs else None

    def renew(self, job_id, device, lease_seconds=300):
        if self._fail_renew:
            raise CindralError("lease lost")
        self.renewals.append((job_id, device))
        return True

    def heartbeat(self, device, labels=(), max_concurrency=1):
        if self._fail_heartbeat:
            raise CindralError("relay request failed with HTTP 404: not found")
        self.heartbeats.append((device, tuple(labels), max_concurrency))
        return {"status": "ok"}

    def report(self, job_id, device, exit_code, log="", steps=None):
        self.reports.append((job_id, device, exit_code))
        self.logs.append(log)
        self.reported_steps.append(list(steps or []))
        return {"status": "success" if exit_code == 0 else "failure"}

    def report_lost(self, job_id, device, error, log="", steps=None):
        self.losses.append((job_id, device, error))
        self.reported_steps.append(list(steps or []))
        return {"status": "pending"}


class CindralClientTest(unittest.TestCase):
    @patch("cindral.agent.urllib.request.urlopen")
    def test_claim_parses_the_job(self, urlopen) -> None:
        urlopen.return_value = Response(
            json.dumps(
                {
                    "job": {
                        "id": "job-1",
                        "repository": "example-org/example-app",
                        "sha": "abc123",
                        "ref": "main",
                        "command": ["pnpm", "ci"],
                        "timeout": 3600,
                    }
                }
            ).encode()
        )
        claimed = CindralClient("https://relay.example/", "token").claim("server", ["arm64"])
        self.assertEqual(claimed.sha, "abc123")
        self.assertEqual(claimed.command, ("pnpm", "ci"))
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://relay.example/v1/jobs/claim")
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("Bearer token", request.headers["Authorization"])

    @patch("cindral.agent.urllib.request.urlopen")
    def test_claim_returns_none_when_the_queue_is_empty(self, urlopen) -> None:
        urlopen.return_value = Response(b"", 204)
        self.assertIsNone(CindralClient("https://relay.example", "token").claim("server"))

    @patch("cindral.agent.urllib.request.urlopen")
    def test_http_errors_become_cindral_errors(self, urlopen) -> None:
        from urllib.error import HTTPError

        urlopen.side_effect = HTTPError("url", 401, "unauthorized", {}, io.BytesIO(b"nope"))
        with self.assertRaises(CindralError):
            CindralClient("https://relay.example", "token").claim("server")

    @patch("cindral.agent.urllib.request.urlopen")
    def test_malformed_json_becomes_a_cindral_error(self, urlopen) -> None:
        urlopen.return_value = Response(b"not json")
        with self.assertRaises(CindralError):
            CindralClient("https://relay.example", "token").claim("server")

    @patch("cindral.agent.urllib.request.urlopen")
    def test_report_sends_the_exit_code(self, urlopen) -> None:
        urlopen.return_value = Response(json.dumps({"status": "failure"}).encode())
        payload = CindralClient("https://relay.example", "token").report("job-1", "server", 7)
        self.assertEqual(payload["status"], "failure")
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(sent["exit_code"], 7)

    @patch("cindral.agent.urllib.request.urlopen")
    def test_report_lost_sends_the_reason_and_no_exit_code(self, urlopen) -> None:
        urlopen.return_value = Response(json.dumps({"status": "pending"}).encode())
        CindralClient("https://relay.example", "token").report_lost("job-1", "server", "disk full")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://relay.example/v1/jobs/job-1/report")
        sent = json.loads(request.data)
        self.assertEqual(sent["error"], "disk full")
        self.assertNotIn("exit_code", sent)

    @patch("cindral.agent.urllib.request.urlopen")
    def test_heartbeat_announces_the_device_and_its_slots(self, urlopen) -> None:
        urlopen.return_value = Response(json.dumps({"status": "ok"}).encode())
        CindralClient("https://relay.example", "token").heartbeat("server", ["arm64"], 4)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://relay.example/v1/agents/heartbeat")
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("Bearer token", request.headers["Authorization"])
        sent = json.loads(request.data)
        self.assertEqual(
            {k: sent[k] for k in ("device", "labels", "max_concurrency")},
            {"device": "server", "labels": ["arm64"], "max_concurrency": 4},
        )
        # the build travels with the heartbeat, because a checkout-based agent
        # has no image for the broker to ask and no way to say it is behind
        self.assertEqual(set(sent["build"]), {"version", "commit", "built_at"})

    @patch("cindral.agent.urllib.request.urlopen")
    def test_the_heartbeat_carries_the_build_it_was_given(self, urlopen) -> None:
        # a device agent is constructed with its own build, since the
        # environment it runs in has no image metadata to read
        urlopen.return_value = Response(json.dumps({"status": "ok"}).encode())
        build = BuildInfo(version="0.12.0", commit="c" * 40, built_at="2026-09-29T16:13:04Z")
        CindralClient("https://relay.example", "token", build=build).heartbeat("server", [], 1)
        sent = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(sent["build"], build.as_dict())

    @patch("cindral.agent.urllib.request.urlopen")
    def test_heartbeat_accepts_a_reply_it_needs_nothing_from(self, urlopen) -> None:
        """The broker may answer 204, an empty body, or a JSON object."""
        for response in (Response(b"", 204), Response(b"", 200), Response(b'{"status": "ok"}', 200)):
            urlopen.return_value = response
            CindralClient("https://relay.example", "token").heartbeat("server")


class AgentBehindBrokerTest(unittest.TestCase):
    """A device behind the broker must leave the queue alone.

    The comparison itself is tested in `test_freshness`. These two are here
    because the defect they catch is not in the comparison: a correct guard with
    no caller reads as a device that is merely idle, and the tests for the
    comparison all pass anyway.
    """

    def _checkout(self) -> tuple[str, str, str]:
        root = tempfile.mkdtemp(prefix="cindral-behind-")
        self.addCleanup(shutil.rmtree, root, True)
        run = lambda *args: subprocess.run(  # noqa: E731
            ["git", *args], cwd=root, capture_output=True, text=True, check=True
        )
        run("init", "-q", "-b", "main")
        run("config", "user.email", "device@example.invalid")
        run("config", "user.name", "device")
        (pathlib.Path(root) / "VERSION").write_text("0.12.0\n", encoding="utf-8")
        run("add", "-A")
        run("commit", "-qm", "old")
        old = run("rev-parse", "HEAD").stdout.strip()
        (pathlib.Path(root) / "VERSION").write_text("0.13.0\n", encoding="utf-8")
        run("commit", "-qam", "new")
        return root, old, run("rev-parse", "HEAD").stdout.strip()

    def _agent(self, root, own, broker, jobs=(), notes=None):
        client = FakeClient(list(jobs))
        client.broker_build = {"version": "0.13.0", "commit": broker, "built_at": "2026-09-29T00:00:00Z"}
        client.build = BuildInfo(version="0.12.0", commit=own, built_at="2026-09-29T00:00:00Z")
        return Agent(
            client, "server", executor=lambda spec, cancel: 0, source_root=root,
            log=(notes.append if notes is not None else None),
        ), client

    def test_a_device_behind_the_broker_claims_nothing(self) -> None:
        root, old, new = self._checkout()
        notes: list[str] = []
        agent, client = self._agent(root, old, new, jobs=[job()], notes=notes)
        self.assertFalse(agent.run_once())
        # the claim is the thing that must not happen. Asserting only that the
        # job did not run would pass against a device that claimed and skipped
        self.assertEqual(client.claims, [], "a device behind the broker claimed work")
        self.assertEqual(client.reports, [])
        self.assertTrue(
            any("behind" in note and old[:8] in note and new[:8] in note for note in notes),
            f"the log did not say which commits differ, notes were {notes}",
        )

    def test_a_device_at_the_brokers_commit_claims_normally(self) -> None:
        root, _old, new = self._checkout()
        agent, client = self._agent(root, new, new, jobs=[job()])
        self.assertIsNone(agent.behind_broker())
        self.assertTrue(agent.run_once())
        self.assertEqual(len(client.claims), 1)
        self.assertEqual(client.reports, [("job-1", "server", 0)])

    def test_the_note_is_said_once_per_streak_not_once_per_poll(self) -> None:
        # a poll every few seconds would otherwise fill the log with the same
        # line, which buries the one line that matters when it appears
        root, old, new = self._checkout()
        notes: list[str] = []
        agent, _client = self._agent(root, old, new, notes=notes)
        for _ in range(3):
            agent.run_once()
        self.assertEqual(len([n for n in notes if "not claiming work" in n]), 1, notes)
        agent.client.broker_build = {"commit": old}
        agent.run_once()
        agent.client.broker_build = {"commit": new}
        agent.run_once()
        self.assertEqual(len([n for n in notes if "not claiming work" in n]), 2, notes)


class AgentTest(unittest.TestCase):
    def test_run_once_returns_false_without_a_job(self) -> None:
        client = FakeClient()
        executed = []
        agent = Agent(client, "server", executor=lambda spec, cancel: executed.append(spec) or 0)
        self.assertFalse(agent.run_once())
        self.assertEqual(executed, [])
        self.assertEqual(client.reports, [])

    def test_run_once_executes_and_reports(self) -> None:
        client = FakeClient([job()])
        seen = []

        def executor(spec, cancel):
            seen.append(spec)
            return 0

        agent = Agent(client, "server", executor=executor, labels=("arm64",))
        self.assertTrue(agent.run_once())
        self.assertEqual(seen[0].id, "job-1")
        self.assertEqual(client.claims[0][1], ("arm64",))
        self.assertEqual(client.reports, [("job-1", "server", 0)])

    def test_an_executor_failure_reports_a_device_error_not_a_failed_test(self) -> None:
        # a device that dies mid-job produces no verdict, and reporting exit 1
        # for it is what made a killed run indistinguishable from a red test
        client = FakeClient([job()])
        notes = []

        def executor(spec, cancel):
            raise RuntimeError("docker is not running")

        agent = Agent(client, "server", executor=executor, log=notes.append)
        self.assertTrue(agent.run_once())
        self.assertEqual(client.reports, [])
        self.assertEqual(client.losses, [("job-1", "server", "RuntimeError: docker is not running")])
        self.assertTrue(any("docker is not running" in note for note in notes), notes)

    def test_a_nonzero_step_exit_is_still_a_failed_test(self) -> None:
        client = FakeClient([job()])
        agent = Agent(client, "server", executor=lambda spec, cancel: 3)
        self.assertTrue(agent.run_once())
        self.assertEqual(client.reports, [("job-1", "server", 3)])
        self.assertEqual(client.losses, [])

    def test_a_device_error_is_flattened_and_bounded(self) -> None:
        client = FakeClient([job()])

        def executor(spec, cancel):
            raise RuntimeError("docker\n  " + "x" * 500)

        Agent(client, "server", executor=executor).run_once()
        error = client.losses[0][2]
        self.assertNotIn("\n", error)
        self.assertLessEqual(len(error), MAX_ERROR_CHARS)

    def test_execution_result_log_is_reported(self) -> None:
        from cindral.models import ExecutionResult

        client = FakeClient([job()])
        agent = Agent(client, "server", executor=lambda spec, cancel: ExecutionResult(2, "boom"))
        agent.run_once()
        self.assertEqual(client.reports, [("job-1", "server", 2)])
        self.assertEqual(client.logs, ["boom"])

    def test_lease_is_renewed_while_the_job_runs(self) -> None:
        client = FakeClient([job()])

        def executor(spec, cancel):
            deadline = time.time() + 4
            while not client.renewals and time.time() < deadline:
                time.sleep(0.02)
            return 0

        Agent(client, "server", executor=executor, lease_seconds=3).run_once()
        self.assertTrue(client.renewals)
        self.assertEqual(client.renewals[0], ("job-1", "server"))

    def test_a_lost_lease_cancels_the_executor_and_skips_the_report(self) -> None:
        client = FakeClient([job()], fail_renew=True)
        cancelled = []

        def executor(spec, cancel):
            cancelled.append(cancel.wait(4))
            return 0

        self.assertTrue(Agent(client, "server", executor=executor, lease_seconds=3).run_once())
        self.assertEqual(cancelled, [True])
        self.assertEqual(client.reports, [])

    def test_no_job_is_claimed_while_the_capability_is_absent(self) -> None:
        """A claim the device cannot run costs a lease, a commit status and a retry."""
        client = FakeClient([job()])
        capable = [False]
        agent = Agent(client, "server", executor=lambda spec, cancel: 0, preflight=lambda: capable[0])
        self.assertFalse(agent.run_once())
        self.assertEqual(client.claims, [])
        self.assertEqual(client.reports, [])
        capable[0] = True
        self.assertTrue(agent.run_once())
        self.assertEqual(client.reports, [("job-1", "server", 0)])
