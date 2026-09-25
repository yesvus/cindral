import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from runner_relay.jobs import JobStore
from runner_relay.policy import Policy
from runner_relay.service import WEBHOOK_PATH, RelayHandler, RelayServer
from runner_relay.state import load_runners
from runner_relay.webhook import sign

SECRET = "webhook-secret"
AGENT_TOKEN = "agent-token"
REPO = "yesvus/leotron-yesvus"


def push_body(private=True):
    return json.dumps(
        {
            "ref": "refs/heads/main",
            "after": "abc123",
            "repository": {"full_name": REPO, "private": private, "default_branch": "main"},
        }
    ).encode()


class JobEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.server = RelayServer(("127.0.0.1", 0), RelayHandler)
        self.server.policy = Policy.load("config/policy.toml")
        self.server.runners = load_runners("examples/state.json")
        self.server.github = None
        self.server.webhook_secret = SECRET
        self.server.dispatch_token = "dispatch-token"
        self.server.workflow_file = "relay-dispatch.yml"
        self.server.jobs = JobStore(str(Path(self._tmp.name) / "jobs.db"))
        self.server.agent_token = AGENT_TOKEN
        self.server.direct_repositories = (REPO,)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def call(self, method, path, body=None, headers=None, authorize=True):
        merged = {"Content-Type": "application/json"}
        if authorize:
            merged["Authorization"] = f"Bearer {AGENT_TOKEN}"
        merged.update(headers or {})
        payload = json.dumps(body).encode() if body is not None else None
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(method, path, body=payload, headers=merged)
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, (json.loads(raw) if raw else {})

    def deliver(self, body, headers):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("POST", WEBHOOK_PATH, body=body, headers={"Content-Type": "application/json", **headers})
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, (json.loads(raw) if raw else {})

    def test_claim_requires_a_bearer_token(self) -> None:
        status, _ = self.call("POST", "/v1/jobs/claim", {"device": "gurbet"}, authorize=False)
        self.assertEqual(status, 401)

    def test_claim_returns_no_content_when_queue_is_empty(self) -> None:
        status, payload = self.call("POST", "/v1/jobs/claim", {"device": "gurbet"})
        self.assertEqual(status, 204)
        self.assertEqual(payload, {})

    def test_claim_and_report_round_trip(self) -> None:
        enqueued = self.server.jobs.enqueue(REPO, "abc123", "main", labels=("arm64",))
        status, payload = self.call("POST", "/v1/jobs/claim", {"device": "gurbet", "labels": ["arm64"]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["job"]["id"], enqueued.id)
        self.assertEqual(payload["job"]["sha"], "abc123")
        status, payload = self.call("POST", f"/v1/jobs/{enqueued.id}/report", {"device": "gurbet", "exit_code": 0})
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "success")
        self.assertFalse(payload["status_posted"])

    def test_claim_skips_jobs_the_device_cannot_match(self) -> None:
        self.server.jobs.enqueue(REPO, "abc123", "main", labels=("arm64",))
        status, _ = self.call("POST", "/v1/jobs/claim", {"device": "gurbet", "labels": ["x64"]})
        self.assertEqual(status, 204)

    def test_renew_rejects_the_wrong_device(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("gurbet", [])
        status, _ = self.call("POST", f"/v1/jobs/{job.id}/renew", {"device": "zombie"})
        self.assertEqual(status, 409)
        status, payload = self.call("POST", f"/v1/jobs/{job.id}/renew", {"device": "gurbet"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["renewed"])

    def test_report_rejects_a_job_leased_to_another_device(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("gurbet", [])
        status, _ = self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "zombie", "exit_code": 0})
        self.assertEqual(status, 409)

    def test_report_posts_a_failure_commit_status(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("gurbet", [])
        with patch.object(self.server, "github") as github:
            status, payload = self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "gurbet", "exit_code": 7})
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "failure")
        self.assertTrue(payload["status_posted"])
        repository, sha, state = github.post_status.call_args[0][:3]
        self.assertEqual((repository, sha, state), (REPO, "abc123", "failure"))

    def test_successful_report_posts_a_success_commit_status(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("gurbet", [])
        with patch.object(self.server, "github") as github:
            _, payload = self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "gurbet", "exit_code": 0})
        self.assertTrue(payload["status_posted"])
        self.assertEqual(github.post_status.call_args[0][2], "success")

    def test_unknown_job_status_is_not_found(self) -> None:
        status, _ = self.call("GET", "/v1/jobs/missing")
        self.assertEqual(status, 404)

    def test_known_job_status_is_returned(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        status, payload = self.call("GET", f"/v1/jobs/{job.id}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "pending")

    def test_push_to_a_direct_repository_queues_instead_of_dispatching(self) -> None:
        body = push_body()
        with patch.object(self.server, "github") as github:
            status, payload = self.deliver(
                body,
                {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"},
            )
        self.assertEqual(status, 202)
        self.assertTrue(payload["queued"])
        self.assertTrue(payload["status_posted"])
        github.dispatch.assert_not_called()
        self.assertEqual(github.post_status.call_args[0][2], "pending")
        queued = self.server.jobs.list()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].sha, "abc123")
        self.assertEqual(queued[0].repository, REPO)

    def test_push_to_a_non_direct_repository_still_dispatches(self) -> None:
        self.server.direct_repositories = ()
        body = push_body()
        with patch.object(self.server, "github") as github:
            github.workflow_exists.return_value = True
            status, payload = self.deliver(
                body,
                {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"},
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["dispatched"])
        github.dispatch.assert_called_once()
        self.assertEqual(self.server.jobs.list(), [])


if __name__ == "__main__":
    unittest.main()
