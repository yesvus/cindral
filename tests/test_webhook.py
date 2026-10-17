"""The gated dispatch paths, reached through the HTTP endpoint.

Signature verification is in `test_webhook_signature.py`; the refusals and the
hand-offs are in `test_webhook_refusal.py`.
"""
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from dataclasses import replace
from http.client import HTTPConnection
from unittest.mock import MagicMock, patch

from cindral.deliveries import DeliveryStore
from cindral.github import GitHubAPIError, GitHubDispatchError, RepositoryRunner
from cindral.jobs import JobStore
from cindral.service import WEBHOOK_PATH, CindralHandler, CindralServer
from cindral.state import load_runners
from cindral.policy import Policy, load_dispatch_targets
from cindral.webhook import sign

SECRET = "shhh"
REPO = "example-org/example-app"

def busy_fallback_runner() -> RepositoryRunner:
    return RepositoryRunner("desktop-example-web", "online", ("self-hosted", "Linux", "ARM64", "fallback"), busy=True)


def push_body(repository: str = REPO, private: bool = True, ref: str = "refs/heads/main", deleted: bool = False) -> bytes:
    return json.dumps(
        {
            "ref": ref,
            "after": "0" * 40,
            "deleted": deleted,
            "repository": {"full_name": repository, "private": private, "default_branch": "main"},
        }
    ).encode()


def pull_request_body(
    repository: str = REPO,
    private: bool = True,
    action: str = "opened",
    number: int = 42,
    author_association: str = "OWNER",
    head_repo: str | None = None,
    head_sha: str = "head123",
    merge_sha: str = "merge123",
    draft: bool = False,
    author: str | None = "octocat",
    author_type: str = "User",
) -> bytes:
    return json.dumps(
        {
            "action": action,
            "number": number,
            "repository": {"full_name": repository, "private": private, "default_branch": "main"},
            "pull_request": {
                "author_association": author_association,
                "draft": draft,
                "head": {"sha": head_sha, "repo": {"full_name": head_repo or repository}},
                "merge_commit_sha": merge_sha,
                "user": {"login": author, "type": author_type},
            },
        }
    ).encode()


def bot_pull_request_body(
    head_repo: str | None = None, author: str = "dependabot[bot]", author_type: str = "Bot"
) -> bytes:
    return pull_request_body(
        author_association="NONE",
        head_repo=head_repo,
        author=author,
        author_type=author_type,
    )


class WebhookEndpointTest(unittest.TestCase):
    server: CindralServer
    thread: threading.Thread

    def setUp(self) -> None:
        self.db = tempfile.TemporaryDirectory()
        self.addCleanup(self.db.cleanup)
        self.server = CindralServer(("127.0.0.1", 0), CindralHandler)
        self.server.deliveries = DeliveryStore(str(Path(self.db.name) / "jobs.db"))
        self.server.policy = Policy.load("config/policy.toml")
        self.server.runners = load_runners("examples/state.json")
        self.server.github = None
        self.server.webhook_secret = SECRET
        self.server.dispatch_token = "dispatch-token"
        self.server.workflow_file = "cindral-dispatch.yml"
        self.server.capacity_lock = threading.Lock()
        self.server.runner_reservations = {}
        self.server.reservation_seconds = 10
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def post(self, path: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
        if self.server.github is not None:
            list_runners = self.server.github.list_runners
            if isinstance(list_runners.return_value, MagicMock) and list_runners.side_effect is None:
                list_runners.return_value = (
                    RepositoryRunner("desktop-example-web", "online",
                                     ("self-hosted", "Linux", "ARM64", "fallback")),
                )
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json", **headers})
        response = conn.getresponse()
        payload = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, payload

    def signed(self, body: bytes, event: str) -> dict[str, str]:
        return {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": event}

    def test_unsigned_delivery_is_refused(self) -> None:
        status, payload = self.post(WEBHOOK_PATH, push_body(), {})
        self.assertEqual(status, 401)
        self.assertIn("signature", payload["error"])

    def test_bad_signature_is_refused(self) -> None:
        body = push_body()
        status, _ = self.post(WEBHOOK_PATH, body, {"X-Hub-Signature-256": sign("wrong", body)})
        self.assertEqual(status, 401)

    def test_signed_delivery_for_allowed_repo_reaches_dispatch(self) -> None:
        body = push_body()
        self.server.runners = ()
        with patch.object(self.server, "github") as github:
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertEqual(status, 200)
        self.assertTrue(payload["dispatched"])
        self.assertEqual(payload["branch"], "main")
        # private repository with unknown quota must take the local lane
        self.assertEqual(payload["lane"], "fallback")
        self.assertEqual(payload["capacity"]["source"], "github_repository_runners")
        self.assertEqual(payload["capacity"]["registered"], 1)
        self.assertEqual(payload["capacity"]["available"], 1)
        self.assertEqual(payload["capacity"]["runner_candidate"], "desktop-example-web")
        repository, workflow, ref, inputs = github.dispatch.call_args[0]
        self.assertEqual(repository, REPO)
        self.assertEqual(workflow, "cindral-dispatch.yml")
        self.assertEqual(ref, "main")
        self.assertEqual(inputs["cindral_lane"], "fallback")

    def test_busy_repository_runner_queues_with_github(self) -> None:
        body = push_body()
        with patch.object(self.server, "github") as github:
            github.list_runners.return_value = (busy_fallback_runner(),)
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertEqual(status, 200)
        self.assertEqual(payload["lane"], "fallback")
        self.assertEqual(payload["capacity"]["available"], 0)
        self.assertTrue(payload["capacity"]["queued_by_github"])
        self.assertIn("GitHub queues", payload["reason"])
        self.assertEqual(github.dispatch.call_count, 1)

    def test_busy_runner_suppresses_duplicate_delivery_but_queues_distinct_pushes(self) -> None:
        body = push_body()
        with patch.object(self.server, "github") as github:
            github.list_runners.return_value = (busy_fallback_runner(),)
            headers = {**self.signed(body, "push"), "X-GitHub-Delivery": "delivery-1"}
            first, _ = self.post(WEBHOOK_PATH, body, headers)
            duplicate, payload = self.post(WEBHOOK_PATH, body, headers)
            other, _ = self.post(WEBHOOK_PATH, body, {**headers, "X-GitHub-Delivery": "delivery-2"})
        self.assertEqual((first, duplicate, other), (200, 202, 200))
        self.assertEqual((payload["status"], github.dispatch.call_count), ("duplicate", 2))

    def test_unknown_dispatch_result_requires_reconciliation(self) -> None:
        body = push_body()
        with patch.object(self.server, "github") as github:
            github.list_runners.return_value = (busy_fallback_runner(),)
            github.dispatch.side_effect = [RuntimeError("transport failure"), None]
            headers = {**self.signed(body, "push"), "X-GitHub-Delivery": "delivery-retry"}
            failed, _ = self.post(WEBHOOK_PATH, body, headers)
            succeeded, _ = self.post(WEBHOOK_PATH, body, headers)
        self.assertEqual((failed, succeeded), (503, 503))
        self.assertEqual(self.server.deliveries.get(REPO, "delivery-retry").state, "uncertain")

    def test_offline_repository_runner_is_ineligible(self) -> None:
        body = push_body()
        with patch.object(self.server, "github") as github:
            github.list_runners.return_value = (
                RepositoryRunner("desktop-example-web", "offline", ("self-hosted", "Linux", "ARM64")),
            )
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertEqual(status, 409)
        self.assertIn("no local runner is eligible", payload["error"])
        github.dispatch.assert_not_called()

    def test_reservations_are_shared_across_overlapping_lanes(self) -> None:
        body = push_body()
        runner = RepositoryRunner(
            "desktop-example-web",
            "online",
            ("self-hosted", "Linux", "ARM64", "fallback", "burst"),
        )
        burst_payload = json.dumps(
            {
                "repository": REPO,
                "workflow": "cindral-dispatch.yml",
                "ref": "main",
                "requested_lane": "burst",
            }
        ).encode()
        with patch.object(self.server, "github") as github:
            github.list_runners.return_value = (runner,)
            push_status, _ = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
            burst_status, payload = self.post(
                "/v1/dispatch",
                burst_payload,
                {"Authorization": "Bearer dispatch-token"},
            )
        self.assertEqual(push_status, 200)
        self.assertEqual(burst_status, 409)
        self.assertIn("reserved", payload["error"])
        self.assertEqual(github.dispatch.call_count, 1)

    def test_disjoint_device_reservations_do_not_block_each_other(self) -> None:
        runners = (
            RepositoryRunner("desktop-example-web", "online", ("self-hosted", "Linux", "ARM64", "desktop")),
            RepositoryRunner("laptop-example-web", "online", ("self-hosted", "Linux", "ARM64", "laptop")),
        )
        with patch.object(self.server, "github") as github:
            github.list_runners.return_value = runners
            statuses = []
            for target in ("desktop", "laptop"):
                payload = json.dumps(
                    {
                        "repository": REPO,
                        "workflow": "cindral-dispatch.yml",
                        "ref": "main",
                        "requested_lane": "device",
                        "target": target,
                    }
                ).encode()
                status, _ = self.post(
                    "/v1/dispatch",
                    payload,
                    {"Authorization": "Bearer dispatch-token"},
                )
                statuses.append(status)
        self.assertEqual(statuses, [200, 200])
        self.assertEqual(github.dispatch.call_count, 2)

    def test_runner_lookup_failure_fails_closed(self) -> None:
        body = push_body()
        with patch.object(self.server, "github") as github:
            github.list_runners.side_effect = GitHubAPIError("runner lookup unavailable")
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertEqual(status, 502)
        self.assertIn("runner lookup unavailable", payload["error"])
        github.dispatch.assert_not_called()

    def _with_jobs(self) -> None:
        """A job store, so the device path is open and the lane is consulted."""
        self.server.jobs = JobStore(str(Path(self.db.name) / "queue.db"))
        self.server.agent_token = "agent-token"

    def test_an_uncertain_dispatch_does_not_also_run_on_the_device(self) -> None:
        self._with_jobs()
        # UNCERTAIN means the request reached GitHub and the answer was lost, so
        # a run may already exist. Falling back would report the same commit
        # twice and burn a runner twice.
        body = push_body()
        with patch.object(self.server, "github") as github:
            github.dispatch.side_effect = GitHubDispatchError("GitHub had a bad day", 502)
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertEqual(status, 503)
        self.assertEqual(payload["status"], "uncertain")
        self.assertEqual(self.server.jobs.list(), [])

    def _policy_for(self, mode: str, push_lane: str | None = None) -> Policy:
        """A policy whose only entry is this repository, in the named mode.

        Every entry carries refs, so a dispatch is attempted at all: without
        them the request is refused earlier and the test passes against the
        bug it was written for.
        """
        entry = {"mode": mode, "repository": REPO, "workflows": ["cindral-dispatch.yml"],
                 "refs": ["refs/heads/main"]}
        if push_lane is not None:
            entry["push_lane"] = push_lane
        return replace(
            self.server.policy,
            dispatch_targets=load_dispatch_targets({"dispatch": {"repositories": [entry]}}),
        )

    def test_a_dispatch_only_repository_is_never_queued_after_a_refusal(self) -> None:
        self._with_jobs()
        body = push_body()
        with patch.object(self.server, "github") as github:
            github.dispatch.side_effect = GitHubDispatchError("HTTP 422", 422)
            with patch.object(self.server, "policy", self._policy_for("dispatch")):
                status, _ = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        github.dispatch.assert_called_once()
        self.assertEqual(status, 422)
        self.assertEqual(self.server.jobs.list(), [])

    def test_a_private_push_with_unknown_quota_still_prefers_the_hosted_lane(self) -> None:
        # "auto" with unknown quota resolves to the local pool, so a push asking
        # for "auto" ran on a self-hosted runner whatever push_lane said
        body = push_body(private=True)
        with patch.object(self.server, "github") as github:
            with patch.object(self.server, "policy", self._policy_for("both")):
                status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertEqual(payload["lane"], "hosted")
        self.assertEqual(payload["runs_on"], ["ubuntu-24.04"])

    def test_a_rejected_dispatch_falls_back_to_the_device(self) -> None:
        # 422 is GitHub saying no, so nothing was dispatched and the other lane
        # is free to take it
        self._with_jobs()
        body = push_body()
        with patch.object(self.server, "github") as github:
            github.dispatch.side_effect = GitHubDispatchError("workflow does not have inputs", 422)
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertEqual(status, 202)
        self.assertTrue(payload["queued"])

    def test_a_successful_dispatch_does_not_also_enqueue_on_the_device(self) -> None:
        # the fallback is for a dispatch that did not happen, not a second run
        body = push_body()
        headers = {**self.signed(body, "push"), "X-GitHub-Delivery": "live-1"}
        with patch.object(self.server, "github") as github:
            status, payload = self.post(WEBHOOK_PATH, body, headers)
        self.assertEqual(status, 200)
        self.assertTrue(payload["dispatched"])
        self.assertNotIn("queued", payload)
        # the ledger carries the commit, so a replay of this row can be queued
        self.assertEqual(self.server.deliveries.get(REPO, "live-1").sha, "0" * 40)

    def test_a_dispatch_only_repository_is_never_queued_after_a_refusal(self) -> None:
        self._with_jobs()
        body = push_body()
        with patch.object(self.server, "github") as github:
            github.dispatch.side_effect = GitHubDispatchError("HTTP 422", 422)
            with patch.object(self.server, "policy", self._policy_for("dispatch")):
                status, _ = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        github.dispatch.assert_called_once()
        self.assertEqual(status, 422)
        self.assertEqual(self.server.jobs.list(), [])

    def test_a_private_push_with_unknown_quota_still_prefers_the_hosted_lane(self) -> None:
        # "auto" with unknown quota resolves to the local pool, so a push asking
        # for "auto" ran on a self-hosted runner whatever push_lane said
        body = push_body(private=True)
        with patch.object(self.server, "github") as github:
            with patch.object(self.server, "policy", self._policy_for("both")):
                status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertEqual(payload["lane"], "hosted")
        self.assertEqual(payload["runs_on"], ["ubuntu-24.04"])

    def test_a_rejected_dispatch_falls_back_to_the_device(self) -> None:
        # 422 is GitHub saying no, so nothing was dispatched and the other
        # lane is free to take it
        self._with_jobs()
        body = push_body()
        with patch.object(self.server, "github") as github:
            github.dispatch.side_effect = GitHubDispatchError("workflow does not have inputs", 422)
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertEqual(status, 202)
        self.assertTrue(payload["queued"])

    def test_a_successful_dispatch_does_not_also_enqueue_on_the_device(self) -> None:
        # the fallback is for a dispatch that did not happen, not a second run
        body = push_body()
        headers = {**self.signed(body, "push"), "X-GitHub-Delivery": "live-1"}
        with patch.object(self.server, "github") as github:
            status, payload = self.post(WEBHOOK_PATH, body, headers)
        self.assertEqual(status, 200)
        self.assertTrue(payload["dispatched"])
        self.assertNotIn("queued", payload)
        # the ledger carries the commit, so a replay of this row can be queued
        self.assertEqual(self.server.deliveries.get(REPO, "live-1").sha, "0" * 40)

    def test_a_replayed_delivery_lands_on_the_lane_its_refusal_allows(self) -> None:
        # A READY row is a delivery whose first attempt never finished, and
        # nothing re-sends a delivery the broker already answered, so a refusal
        # found here lands where the live path would have put the push.
        cases = {
            "refused by GitHub": (GitHubDispatchError("HTTP 422", 422), 202, "both"),
            "adapter removed": (None, 202, "both"),
            # a 5xx is the one answer where a run may already exist, so the
            # retry must not also run the commit and report it twice
            "answer lost": (GitHubDispatchError("GitHub had a bad day", 502), 503, "both"),
            # a dispatch-only repository names no device, so there is no lane
            # to fall back to and the refusal stands
            "no device offered": (GitHubDispatchError("HTTP 422", 422), 422, "dispatch"),
        }
        self._with_jobs()
        body = push_body()
        for index, (label, (error, expected, mode)) in enumerate(cases.items()):
            with self.subTest(case=label):
                delivery = f"replayed-{index}"
                self.server.deliveries.register(REPO, delivery, hashlib.sha256(body).hexdigest(),
                                                "cindral-dispatch.yml", "main", {}, "0" * 40)
                with patch.object(self.server, "github") as github:
                    if error is None:
                        github.workflow_exists.return_value = False
                    else:
                        github.dispatch.side_effect = error
                    with patch.object(self.server, "policy", self._policy_for(mode)):
                        status, _ = self.post(
                            WEBHOOK_PATH, body, {**self.signed(body, "push"), "X-GitHub-Delivery": delivery}
                        )
                self.assertEqual(status, expected)
                # the job carries the commit and the delivery it came from, so
                # a redelivery joins this run rather than starting a second
                jobs = [(j.sha, j.delivery) for j in self.server.jobs.list() if j.delivery == delivery]
                self.assertEqual(jobs, [("0" * 40, delivery)] if expected == 202 else [])

    def test_a_device_preference_never_dispatches(self) -> None:
        self._with_jobs()
        # the opt-in for local hardware, and it must not also try GitHub
        body = push_body()
        with patch.object(self.server, "github") as github:
            with patch.object(self.server, "policy", self._policy_for("both", "device")):
                status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertEqual(status, 202)
        self.assertTrue(payload["queued"])
        github.dispatch.assert_not_called()

    def test_failed_dispatch_releases_capacity_reservation(self) -> None:
        body = push_body()
        with patch.object(self.server, "github") as github:
            github.dispatch.side_effect = RuntimeError("unexpected transport failure")
            self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertFalse(self.server.runner_reservations.get(REPO))

    def test_pending_dispatch_reserves_the_available_runner(self) -> None:
        # One runner and two pushes at once: the second must find the runner
        # reserved. Each thread carries its own delivery ID, or whichever lost
        # the race could be answered as a duplicate and the test would be
        # asserting on which of two correct answers arrived.
        body = push_body()
        statuses: list[int] = []
        start = threading.Barrier(3)

        def dispatch(delivery: str) -> None:
            headers = dict(self.signed(body, "push"), **{"X-GitHub-Delivery": delivery})
            start.wait()
            status, _ = self.post(WEBHOOK_PATH, body, headers)
            statuses.append(status)

        with patch.object(self.server, "github") as github:
            github.list_runners.return_value = (
                RepositoryRunner(
                    "desktop-example-web",
                    "online",
                    ("self-hosted", "Linux", "ARM64", "fallback"),
                ),
            )
            requests = [
                threading.Thread(target=dispatch, args=(f"delivery-{index}",))
                for index in range(2)
            ]
            for request in requests:
                request.start()
            start.wait()
            for request in requests:
                request.join()

        self.assertCountEqual(statuses, [200, 409])
        self.assertEqual(github.dispatch.call_count, 1)

    def test_dispatch_uses_current_pool_capacity(self) -> None:
        body = push_body()
        headers = self.signed(body, "push")
        with patch.object(self.server, "github") as github:
            github.list_runners.return_value = (
                RepositoryRunner(
                    "desktop-example-web",
                    "online",
                    ("self-hosted", "Linux", "ARM64", "fallback"),
                ),
                RepositoryRunner(
                    "laptop-example-web",
                    "online",
                    ("self-hosted", "Linux", "ARM64", "fallback"),
                ),
            )
            status_a, payload_a = self.post(WEBHOOK_PATH, body, {**headers, "X-GitHub-Delivery": "first"})
            status_b, payload_b = self.post(WEBHOOK_PATH, body, {**headers, "X-GitHub-Delivery": "second"})
        self.assertEqual((status_a, status_b), (200, 200))
        self.assertEqual(payload_a["capacity"]["available"], 2)
        self.assertEqual(payload_b["capacity"]["reserved"], 1)
        self.assertEqual(payload_b["capacity"]["available"], 1)

    def test_public_repository_routes_to_hosted(self) -> None:
        body = push_body(private=False)
        with patch.object(self.server, "github") as github:
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertEqual(status, 200)
        self.assertEqual(payload["lane"], "hosted")
        self.assertEqual(github.dispatch.call_args[0][3]["cindral_lane"], "hosted")

    def test_repository_outside_dispatch_policy_is_refused(self) -> None:
        body = push_body(repository="example-org/other")
        with patch.object(self.server, "github") as github:
            github.workflow_exists.return_value = False
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertEqual(status, 403)
        self.assertIn("not permitted", payload["error"])
        github.dispatch.assert_not_called()

    def test_non_default_branch_is_ignored_without_dispatch(self) -> None:
        body = push_body(ref="refs/heads/some-feature")
        with patch.object(self.server, "github") as github:
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertEqual(status, 202)
        self.assertEqual(payload["status"], "ignored")
        github.dispatch.assert_not_called()

    def test_unlisted_default_branch_is_refused(self) -> None:
        body = json.dumps(
            {
                "ref": "refs/heads/master",
                "after": "0" * 40,
                "repository": {"full_name": "example-org/example-store", "private": True, "default_branch": "master"},
            }
        ).encode()
        with patch.object(self.server, "github") as github:
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertEqual(status, 403)
        self.assertIn("not permitted", payload["error"])
        github.dispatch.assert_not_called()

    def test_non_push_event_is_ignored(self) -> None:
        body = push_body()
        with patch.object(self.server, "github") as github:
            status, _ = self.post(WEBHOOK_PATH, body, self.signed(body, "ping"))
        self.assertEqual(status, 202)
        github.dispatch.assert_not_called()


    def test_unset_secret_refuses_rather_than_serving(self) -> None:
        self.server.webhook_secret = None
        status, _ = self.post(WEBHOOK_PATH, push_body(), {})
        self.assertEqual(status, 503)

    def test_internal_dispatch_requires_a_bearer_token(self) -> None:
        payload = json.dumps(
            {"repository": REPO, "workflow": "cindral-dispatch.yml", "ref": "main", "private": True, "quota_status": "unknown"}
        ).encode()
        status, _ = self.post("/v1/dispatch", payload, {})
        self.assertEqual(status, 401)

    def test_internal_dispatch_with_wrong_token_is_refused(self) -> None:
        payload = json.dumps(
            {"repository": REPO, "workflow": "cindral-dispatch.yml", "ref": "main", "private": True, "quota_status": "unknown"}
        ).encode()
        status, _ = self.post("/v1/dispatch", payload, {"Authorization": "Bearer nope"})
        self.assertEqual(status, 401)

    def test_internal_dispatch_with_token_is_allowed(self) -> None:
        payload = json.dumps(
            {"repository": REPO, "workflow": "cindral-dispatch.yml", "ref": "main", "private": True, "quota_status": "unknown"}
        ).encode()
        with patch.object(self.server, "github"):
            status, body = self.post("/v1/dispatch", payload, {"Authorization": "Bearer dispatch-token"})
        self.assertEqual(status, 200)
        self.assertTrue(body["dispatched"])

    def test_internal_dispatch_rejects_invalid_inputs_before_capacity_check(self) -> None:
        payload = json.dumps(
            {
                "repository": REPO,
                "workflow": "cindral-dispatch.yml",
                "ref": "main",
                "inputs": ["invalid"],
            }
        ).encode()
        with patch.object(self.server, "github") as github:
            status, body = self.post(
                "/v1/dispatch",
                payload,
                {"Authorization": "Bearer dispatch-token"},
            )
        self.assertEqual(status, 400)
        self.assertIn("string-to-string", body["error"])
        github.list_runners.assert_not_called()

    def test_unset_dispatch_token_refuses_rather_than_serving(self) -> None:
        self.server.dispatch_token = None
        payload = json.dumps(
            {"repository": REPO, "workflow": "cindral-dispatch.yml", "ref": "main", "private": True, "quota_status": "unknown"}
        ).encode()
        status, _ = self.post("/v1/dispatch", payload, {})
        self.assertEqual(status, 401)

    def test_route_stays_open_for_internal_use(self) -> None:
        payload = json.dumps(
            {"repository": REPO, "private": True, "quota_status": "unknown"}
        ).encode()
        status, body = self.post("/v1/route", payload, {})
        self.assertEqual(status, 200)
        self.assertEqual(body["lane"], "fallback")

    def test_oversized_body_is_refused(self) -> None:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", WEBHOOK_PATH)
        conn.putheader("Content-Length", str(2 << 20))
        conn.putheader("Content-Type", "application/json")
        conn.endheaders()
        response = conn.getresponse()
        response.read()
        conn.close()
        self.assertEqual(response.status, 413)
