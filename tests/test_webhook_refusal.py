"""What a refused push does, and where a delivery the broker lost ends up.

Split from the endpoint tests because these are about the decision rather than
the request: a refusal that may be covered, a repository with no device to cover
it on, and a delivery that GitHub no longer has an adapter for. All of it is
"the push has to run somewhere", which is a different question from "what does
the endpoint answer".
"""
import io
import unittest.mock
from unittest.mock import patch

from cindral.github import GitHubDispatchError

from test_webhook import (
    REPO,
    SECRET,
    WEBHOOK_PATH,
    WebhookEndpointTest,
    push_body,
)


class MissingAdapterTest(WebhookEndpointTest):
    """A repository with no adapter still has to run somewhere, if it may.

    A 404 from GitHub means the adapter file is not there. That is a missing
    file and not a decision, so the push is not ignored, and for a repository the
    policy serves on a device it is queued there: what is missing is the adapter
    and not the pool. A repository the policy serves on GitHub alone has no
    device to take it, and the commit says so rather than running it on hardware
    the policy never offered.
    """

    def _both_mode(self) -> None:
        from dataclasses import replace as dataclass_replace

        from cindral.policy import load_dispatch_targets

        self.server.policy = dataclass_replace(
            self.server.policy,
            dispatch_targets=load_dispatch_targets({"dispatch": {"repositories": [
                {"mode": "both", "repository": REPO, "workflows": ["cindral-dispatch.yml"],
                 "refs": ["refs/heads/main"]}]}}),
        )

    def _dispatch_only(self) -> None:
        from dataclasses import replace as dataclass_replace

        from cindral.policy import load_dispatch_targets

        self.server.policy = dataclass_replace(
            self.server.policy,
            dispatch_targets=load_dispatch_targets({"dispatch": {"repositories": [
                {"mode": "dispatch", "repository": REPO, "workflows": ["cindral-dispatch.yml"],
                 "refs": ["refs/heads/main"]}]}}),
        )

    def test_a_device_repository_whose_adapter_is_missing_is_queued(self) -> None:
        self._with_jobs()
        self._both_mode()
        body = push_body()
        with patch.object(self.server, "github") as github:
            github.workflow_exists.return_value = False
            github.post_status.return_value = {}
            github.list_runners.return_value = ()
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertEqual(status, 202, payload)
        self.assertEqual(len(self.server.jobs.list()), 1, "the push was not queued")
        github.dispatch.assert_not_called()

    def test_a_redelivery_of_a_push_with_no_adapter_queues_one_run(self) -> None:
        # The path returns before the delivery is registered, so nothing in the
        # ledger stops a redelivery. What stops it is the enqueue being idempotent
        # on the delivery ID, which is worth a test of its own because the reason
        # is in a different module from the code that relies on it.
        self._with_jobs()
        self._both_mode()
        body = push_body()
        headers = dict(self.signed(body, "push"), **{"X-GitHub-Delivery": "delivery-nf"})
        with patch.object(self.server, "github") as github:
            github.workflow_exists.return_value = False
            github.post_status.return_value = {}
            github.list_runners.return_value = ()
            first, _ = self.post(WEBHOOK_PATH, body, headers)
            second, _ = self.post(WEBHOOK_PATH, body, headers)
        self.assertEqual(first, 202)
        self.assertEqual(second, 202)
        jobs = self.server.jobs.list()
        self.assertEqual(len(jobs), 1, f"a redelivery queued a second run: {len(jobs)}")
        self.assertEqual(jobs[0].status, "pending")

    def test_the_guard_is_in_the_function_and_not_a_property_of_its_caller(self) -> None:
        """The mutation that finds this.

        On the live path the caller only builds a fallback for a repository the
        policy serves on a device, so removing the check in `_adapter_absent`
        changes nothing there. The check has to be where the decision is taken,
        because the thing that could stop being careful is the caller, and a
        caller that stopped would queue a commit on hardware the device path
        would have refused.
        """
        from cindral.webhook import PushEvent

        self._with_jobs()
        self._dispatch_only()
        event = PushEvent(
            repository=REPO, private=True, ref="refs/heads/main",
            after="a" * 40, default_branch="main",
        )
        queued: list[str] = []
        with patch.object(self.server, "github") as github:
            github.post_status.return_value = {}
            handler = self._handler()
            handler._adapter_absent(
                event, "cindral-dispatch.yml", "no adapter", lambda: queued.append("queued")
            )
        self.assertEqual(queued, [], "a dispatch-only repository reached the device")
        self.assertEqual(self.server.jobs.list(), [])

    def test_a_dispatch_only_repository_whose_adapter_is_missing_runs_nowhere(self) -> None:
        # the guard is the policy, not the caller having built no fallback: a
        # caller that stopped consulting push_lane would otherwise queue a commit
        # on local hardware the device path would have refused
        self._with_jobs()
        self._dispatch_only()
        body = push_body()
        with patch.object(self.server, "github") as github:
            github.workflow_exists.return_value = False
            github.post_status.return_value = {}
            github.list_runners.return_value = ()
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertEqual(status, 202, payload)
        self.assertEqual(self.server.jobs.list(), [], "a dispatch-only repository ran on a device")
        self.assertIn("does not serve this repository on a device", payload["reason"])
        # and the commit says so, because a 202 with nothing behind it reads as a
        # push that has not run yet
        github.post_status.assert_called()
        self.assertIn("ran nowhere", github.post_status.call_args.args[3])


    def _handler(self):
        """The live handler, bound to the harness's server.

        `_adapter_absent` is private because no other caller exists. The defect
        this guards is precisely at that boundary, so the test calls it directly
        rather than routing around the caller whose care it is trying not to
        depend on.
        """
        from cindral.service import CindralHandler

        handler = CindralHandler.__new__(CindralHandler)
        handler.server = self.server
        handler.path = "/"
        handler.request = None
        handler.client_address = None
        handler.wfile = io.BytesIO()
        handler.rfile = io.BytesIO()
        handler.requestline = ""
        handler.request_version = "HTTP/1.1"
        handler.command = "POST"
        handler.headers = {}
        return handler


class RefusedPushTest(WebhookEndpointTest):
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

