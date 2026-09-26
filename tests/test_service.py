import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from cindral import jobs
from cindral.github import RepositoryRunner
from cindral.jobs import JobStore
from cindral.policy import Policy
from cindral.service import WEBHOOK_PATH, CindralHandler, CindralServer
from cindral.state import load_runners
from cindral.webhook import sign

SECRET = "webhook-secret"
AGENT_TOKEN = "agent-token"
POOL_TOKEN = "pool-token"
REPO = "example-org/example-app"


def push_body(private=True):
    return json.dumps(
        {
            "ref": "refs/heads/main",
            "after": "abc123",
            "repository": {"full_name": REPO, "private": private, "default_branch": "main"},
        }
    ).encode()


def pull_request_body(head_repo=REPO, action="opened", number=42, author_association="OWNER"):
    return json.dumps(
        {
            "action": action,
            "number": number,
            "repository": {"full_name": REPO, "private": True, "default_branch": "main"},
            "pull_request": {
                "author_association": author_association,
                "draft": False,
                "head": {"sha": "head123", "repo": {"full_name": head_repo}},
                "merge_commit_sha": "merge123",
            },
        }
    ).encode()


class JobEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.server = CindralServer(("127.0.0.1", 0), CindralHandler)
        self.server.policy = Policy.load("config/policy.toml")
        self.server.runners = load_runners("examples/state.json")
        self.server.github = None
        self.server.webhook_secret = SECRET
        self.server.dispatch_token = "dispatch-token"
        self.server.workflow_file = "cindral-dispatch.yml"
        self.server.capacity_lock = threading.Lock()
        self.server.runner_reservations = {}
        self.server.reservation_seconds = 10
        self.server.jobs = JobStore(str(Path(self._tmp.name) / "jobs.db"))
        self.server.agent_token = AGENT_TOKEN
        self.server.pool_token = POOL_TOKEN
        self.server.direct_repositories = (REPO,)
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
        status, _ = self.call("POST", "/v1/jobs/claim", {"device": "server"}, authorize=False)
        self.assertEqual(status, 401)

    def test_claim_returns_no_content_when_queue_is_empty(self) -> None:
        status, payload = self.call("POST", "/v1/jobs/claim", {"device": "server"})
        self.assertEqual(status, 204)
        self.assertEqual(payload, {})

    def test_claim_and_report_round_trip(self) -> None:
        enqueued = self.server.jobs.enqueue(REPO, "abc123", "main", labels=("arm64",))
        status, payload = self.call("POST", "/v1/jobs/claim", {"device": "server", "labels": ["arm64"]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["job"]["id"], enqueued.id)
        self.assertEqual(payload["job"]["sha"], "abc123")
        status, payload = self.call("POST", f"/v1/jobs/{enqueued.id}/report", {"device": "server", "exit_code": 0})
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "success")
        self.assertFalse(payload["status_posted"])

    def test_claim_skips_jobs_the_device_cannot_match(self) -> None:
        self.server.jobs.enqueue(REPO, "abc123", "main", labels=("arm64",))
        status, _ = self.call("POST", "/v1/jobs/claim", {"device": "server", "labels": ["x64"]})
        self.assertEqual(status, 204)

    def test_renew_rejects_the_wrong_device(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        status, _ = self.call("POST", f"/v1/jobs/{job.id}/renew", {"device": "burst"})
        self.assertEqual(status, 409)
        status, payload = self.call("POST", f"/v1/jobs/{job.id}/renew", {"device": "server"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["renewed"])

    def test_report_rejects_a_job_leased_to_another_device(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        status, _ = self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "burst", "exit_code": 0})
        self.assertEqual(status, 409)

    def test_report_posts_a_failure_commit_status(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        with patch.object(self.server, "github") as github:
            status, payload = self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "server", "exit_code": 7})
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "failure")
        self.assertTrue(payload["status_posted"])
        repository, sha, state = github.post_status.call_args[0][:3]
        self.assertEqual((repository, sha, state), (REPO, "abc123", "failure"))

    def test_successful_report_posts_a_success_commit_status(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        with patch.object(self.server, "github") as github:
            _, payload = self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "server", "exit_code": 0})
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

    def test_trusted_pull_request_on_a_direct_repository_queues(self) -> None:
        body = pull_request_body()
        with patch.object(self.server, "github") as github:
            status, payload = self.deliver(
                body,
                {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "pull_request"},
            )
        self.assertEqual(status, 202)
        self.assertTrue(payload["queued"])
        self.assertEqual(payload["pull_request"], 42)
        github.dispatch.assert_not_called()
        self.assertEqual(github.post_status.call_args[0][1], "head123")
        queued = self.server.jobs.list()
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].sha, "merge123")
        self.assertEqual(queued[0].ref, "refs/pull/42/merge")
        self.assertEqual(queued[0].status_sha, "head123")

    def test_fork_pull_request_on_a_direct_repository_stays_on_actions(self) -> None:
        body = pull_request_body(head_repo="someone/example-app")
        with patch.object(self.server, "github") as github:
            github.workflow_exists.return_value = True
            status, payload = self.deliver(
                body,
                {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "pull_request"},
            )
        self.assertEqual(status, 200)
        github.dispatch.assert_called_once()
        self.assertEqual(self.server.jobs.list(), [])

    def test_push_to_a_non_direct_repository_still_dispatches(self) -> None:
        self.server.direct_repositories = ()
        body = push_body()
        with patch.object(self.server, "github") as github:
            github.workflow_exists.return_value = True
            github.list_runners.return_value = (
                RepositoryRunner(
                    "desktop-example-web",
                    "online",
                    ("self-hosted", "Linux", "ARM64", "fallback"),
                ),
            )
            status, payload = self.deliver(
                body,
                {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"},
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["dispatched"])
        github.dispatch.assert_called_once()
        self.assertEqual(self.server.jobs.list(), [])


    def test_claim_rejects_get(self) -> None:
        status, _ = self.call("GET", "/v1/jobs/claim")
        self.assertEqual(status, 405)

    def test_status_rejects_post(self) -> None:
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        status, _ = self.call("POST", f"/v1/jobs/{job.id}")
        self.assertEqual(status, 405)

    def test_report_retries_after_a_failed_status_update(self) -> None:
        from cindral.github import GitHubAPIError

        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        with patch.object(self.server, "github") as github:
            github.post_status.side_effect = GitHubAPIError("boom")
            status, _ = self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "server", "exit_code": 0})
        self.assertEqual(status, 502)
        self.assertEqual(self.server.jobs.get(job.id).status, "running")
        with patch.object(self.server, "github") as github:
            status, payload = self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "server", "exit_code": 0})
        self.assertEqual(status, 200)
        self.assertTrue(payload["status_posted"])

    def test_direct_push_without_agent_token_is_refused(self) -> None:
        self.server.agent_token = None
        body = push_body()
        with patch.object(self.server, "github") as github:
            status, _ = self.deliver(
                body,
                {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"},
            )
        self.assertEqual(status, 503)
        github.dispatch.assert_not_called()
        self.assertEqual(self.server.jobs.list(), [])

    def test_duplicate_delivery_is_idempotent(self) -> None:
        body = push_body()
        headers = {
            "X-Hub-Signature-256": sign(SECRET, body),
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-1",
        }
        with patch.object(self.server, "github"):
            self.deliver(body, headers)
            status, payload = self.deliver(body, headers)
        self.assertEqual(status, 202)
        self.assertTrue(payload["queued"])
        self.assertEqual(len(self.server.jobs.list()), 1)

    def test_pool_snapshot_requires_authorization(self) -> None:
        status, _ = self.call("GET", "/v1/pool", authorize=False)
        self.assertEqual(status, 401)

    def test_pool_snapshot_accepts_the_read_only_token(self) -> None:
        status, payload = self.call("GET", "/v1/pool", token=POOL_TOKEN)
        self.assertEqual(status, 200)
        self.assertIn("queue_depth", payload)

    def test_pool_snapshot_refuses_the_agent_token(self) -> None:
        # the agent token can claim and report jobs, so it must not be the
        # credential a read-only consumer holds
        status, _ = self.call("GET", "/v1/pool", token=AGENT_TOKEN)
        self.assertEqual(status, 401)

    def test_pool_snapshot_refuses_the_dispatch_token(self) -> None:
        self.server.dispatch_token = "dispatch-token"
        status, _ = self.call("GET", "/v1/pool", token="dispatch-token")
        self.assertEqual(status, 401)

    def test_pool_snapshot_is_refused_without_a_configured_token(self) -> None:
        # an unset secret must not silently expose the snapshot
        self.server.pool_token = None
        status, _ = self.call("GET", "/v1/pool", token=POOL_TOKEN)
        self.assertEqual(status, 401)

    def test_job_status_accepts_the_read_only_token(self) -> None:
        # the panel holds only the pool token, so it must be able to read a run
        job = self.server.jobs.enqueue("example-org/app", "sha1", "main", command=("ci",))
        status, payload = self.call("GET", f"/v1/jobs/{job.id}", token=POOL_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(payload["id"], job.id)

    def test_claim_refuses_the_read_only_token(self) -> None:
        self.server.jobs.enqueue("example-org/app", "sha1", "main", command=("ci",))
        status, _ = self.call(
            "POST", "/v1/jobs/claim", {"device": "server"}, token=POOL_TOKEN
        )
        self.assertEqual(status, 401)
        self.assertEqual(self.server.jobs.list(jobs.RUNNING), [])

    def test_report_refuses_the_read_only_token(self) -> None:
        job = self.server.jobs.enqueue("example-org/app", "sha1", "main", command=("ci",))
        self.server.jobs.claim("server", [])
        status, _ = self.call(
            "POST",
            f"/v1/jobs/{job.id}/report",
            {"device": "server", "exit_code": 0},
            token=POOL_TOKEN,
        )
        self.assertEqual(status, 401)
        self.assertEqual(self.server.jobs.get(job.id).status, "running")

    def test_renew_refuses_the_read_only_token(self) -> None:
        job = self.server.jobs.enqueue("example-org/app", "sha1", "main", command=("ci",))
        self.server.jobs.claim("server", [])
        status, _ = self.call(
            "POST", f"/v1/jobs/{job.id}/renew", {"device": "server"}, token=POOL_TOKEN
        )
        self.assertEqual(status, 401)

    def test_metrics_is_not_readable_cross_origin(self) -> None:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("GET", "/metrics")
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))

    def test_metrics_preflight_is_refused(self) -> None:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("OPTIONS", "/metrics")
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 405)

    def test_metrics_reports_an_empty_queue(self) -> None:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("GET", "/metrics")
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        connection.close()
        self.assertIn('cindral_queue_depth{state="running"} 0', raw)

    def test_pool_snapshot_returns_pool_state(self) -> None:
        job = self.server.jobs.enqueue("example-org/app", "sha1", "main", command=("ci",))
        self.server.jobs.claim("server", ["self-hosted", "Linux", "ARM64", "server", "fallback"])
        status, payload = self.call("GET", "/v1/pool", token=POOL_TOKEN)
        self.assertEqual(status, 200)
        self.assertIn("devices", payload)
        self.assertIn("queue_depth", payload)
        self.assertEqual(payload["queue_depth"]["running"], 1)
        self.assertEqual(payload["queue_depth"]["pending"], 0)
        server_dev = next(d for d in payload["devices"] if d["name"] == "server")
        self.assertTrue(server_dev["busy"])
        self.assertEqual(server_dev["current_lease"]["job_id"], job.id)

    def test_metrics_endpoint_returns_prometheus_format(self) -> None:
        self.server.jobs.enqueue("example-org/app", "sha1", "main", command=("ci",))
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("GET", "/metrics")
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        connection.close()

        self.assertEqual(response.status, 200)
        self.assertIn("text/plain", response.getheader("Content-Type", ""))
        self.assertIn('cindral_queue_depth{state="pending"} 1', raw)
        self.assertIn('cindral_devices{status="online"}', raw)
        self.assertIn("cindral_oldest_pending_seconds", raw)

    def test_pool_preflight_allows_the_bearer_header(self) -> None:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("OPTIONS", "/v1/pool")
        response = connection.getresponse()
        response.read()
        connection.close()

        self.assertEqual(response.status, 204)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "*")
        self.assertIn("GET", response.getheader("Access-Control-Allow-Methods", ""))
        # the panel must be able to present Authorization on the pool request
        self.assertIn("Authorization", response.getheader("Access-Control-Allow-Headers", ""))


if __name__ == "__main__":
    unittest.main()
