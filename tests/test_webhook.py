"""Tests for webhook signature verification and the gated dispatch paths."""
import hashlib
import hmac
import json
import threading
import unittest
from http.client import HTTPConnection
from unittest.mock import MagicMock, patch

from runner_relay.github import GitHubAPIError, RepositoryRunner
from runner_relay.service import WEBHOOK_PATH, RelayHandler, RelayServer
from runner_relay.state import load_runners
from runner_relay.policy import Policy
from runner_relay.webhook import parse_push_event, sign, verify_signature, WebhookError

SECRET = "shhh"
REPO = "yesvus/leotron-yesvus"


def push_body(repository: str = REPO, private: bool = True, ref: str = "refs/heads/main", deleted: bool = False) -> bytes:
    return json.dumps(
        {
            "ref": ref,
            "after": "0" * 40,
            "deleted": deleted,
            "repository": {"full_name": repository, "private": private, "default_branch": "main"},
        }
    ).encode()


class SignatureTest(unittest.TestCase):
    def test_valid_signature_is_accepted(self) -> None:
        body = push_body()
        self.assertTrue(verify_signature(SECRET, body, sign(SECRET, body)))

    def test_wrong_secret_is_rejected(self) -> None:
        body = push_body()
        self.assertFalse(verify_signature("other", body, sign(SECRET, body)))

    def test_tampered_body_is_rejected(self) -> None:
        body = push_body()
        header = sign(SECRET, body)
        self.assertFalse(verify_signature(SECRET, body + b" ", header))

    def test_missing_header_is_rejected(self) -> None:
        self.assertFalse(verify_signature(SECRET, push_body(), None))

    def test_garbage_header_is_rejected(self) -> None:
        self.assertFalse(verify_signature(SECRET, push_body(), "sha256=nothex"))

    def test_signature_matches_github_algorithm(self) -> None:
        body = b'{"zen":"x"}'
        expected = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        self.assertEqual(sign(SECRET, body), expected)


class PushEventTest(unittest.TestCase):
    def test_branch_is_extracted_from_ref(self) -> None:
        event = parse_push_event(push_body(ref="refs/heads/staging"))
        self.assertEqual(event.branch, "staging")
        self.assertEqual(event.repository, REPO)
        self.assertTrue(event.private)

    def test_public_repository_is_read_from_the_payload(self) -> None:
        self.assertFalse(parse_push_event(push_body(private=False)).private)

    def test_tag_refs_have_no_branch(self) -> None:
        self.assertEqual(parse_push_event(push_body(ref="refs/tags/v1")).branch, "")

    def test_deleted_ref_is_refused(self) -> None:
        with self.assertRaises(WebhookError):
            parse_push_event(push_body(deleted=True))

    def test_missing_repository_is_refused(self) -> None:
        with self.assertRaises(WebhookError):
            parse_push_event(b'{"ref":"refs/heads/main"}')

    def test_non_json_is_refused(self) -> None:
        with self.assertRaises(WebhookError):
            parse_push_event(b"not json")


class WebhookEndpointTest(unittest.TestCase):
    server: RelayServer
    thread: threading.Thread

    def setUp(self) -> None:
        self.server = RelayServer(("127.0.0.1", 0), RelayHandler)
        self.server.policy = Policy.load("config/policy.toml")
        self.server.runners = load_runners("examples/state.json")
        self.server.github = None
        self.server.webhook_secret = SECRET
        self.server.dispatch_token = "dispatch-token"
        self.server.workflow_file = "relay-dispatch.yml"
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
                    RepositoryRunner("papyrus-helmdeck-web", "online", ("self-hosted", "Linux", "ARM64")),
                )
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json", **headers})
        response = conn.getresponse()
        payload = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, payload

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
            status, payload = self.post(
                WEBHOOK_PATH,
                body,
                {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"},
            )
        self.assertEqual(status, 200)
        self.assertTrue(payload["dispatched"])
        self.assertEqual(payload["branch"], "main")
        # private repository with unknown quota must take the local lane
        self.assertEqual(payload["lane"], "fallback")
        self.assertEqual(payload["capacity"]["source"], "github_repository_runners")
        self.assertEqual(payload["capacity"]["registered"], 1)
        self.assertEqual(payload["capacity"]["available"], 1)
        self.assertEqual(payload["capacity"]["runner_candidate"], "papyrus-helmdeck-web")
        repository, workflow, ref, inputs = github.dispatch.call_args[0]
        self.assertEqual(repository, REPO)
        self.assertEqual(workflow, "relay-dispatch.yml")
        self.assertEqual(ref, "main")
        self.assertEqual(inputs["relay_lane"], "fallback")

    def test_busy_repository_runner_prevents_local_dispatch(self) -> None:
        body = push_body()
        with patch.object(self.server, "github") as github:
            github.list_runners.return_value = (
                RepositoryRunner(
                    "papyrus-helmdeck-web",
                    "online",
                    ("self-hosted", "Linux", "ARM64"),
                    busy=True,
                ),
            )
            status, payload = self.post(
                WEBHOOK_PATH,
                body,
                {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"},
            )
        self.assertEqual(status, 409)
        self.assertIn("no local runner is eligible", payload["error"])
        github.dispatch.assert_not_called()

    def test_offline_repository_runner_is_ineligible(self) -> None:
        body = push_body()
        with patch.object(self.server, "github") as github:
            github.list_runners.return_value = (
                RepositoryRunner("papyrus-helmdeck-web", "offline", ("self-hosted", "Linux", "ARM64")),
            )
            status, payload = self.post(
                WEBHOOK_PATH,
                body,
                {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"},
            )
        self.assertEqual(status, 409)
        self.assertIn("no local runner is eligible", payload["error"])
        github.dispatch.assert_not_called()

    def test_reservations_are_shared_across_overlapping_lanes(self) -> None:
        body = push_body()
        runner = RepositoryRunner(
            "papyrus-helmdeck-web",
            "online",
            ("self-hosted", "Linux", "ARM64", "burst"),
        )
        burst_payload = json.dumps(
            {
                "repository": REPO,
                "workflow": "relay-dispatch.yml",
                "ref": "main",
                "requested_lane": "burst",
            }
        ).encode()
        with patch.object(self.server, "github") as github:
            github.list_runners.return_value = (runner,)
            push_status, _ = self.post(
                WEBHOOK_PATH,
                body,
                {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"},
            )
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
            RepositoryRunner("papyrus-helmdeck-web", "online", ("self-hosted", "Linux", "ARM64", "papyrus")),
            RepositoryRunner("rover-helmdeck-web", "online", ("self-hosted", "Linux", "ARM64", "rover")),
        )
        with patch.object(self.server, "github") as github:
            github.list_runners.return_value = runners
            statuses = []
            for target in ("papyrus", "rover"):
                payload = json.dumps(
                    {
                        "repository": REPO,
                        "workflow": "relay-dispatch.yml",
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
            status, payload = self.post(
                WEBHOOK_PATH,
                body,
                {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"},
            )
        self.assertEqual(status, 502)
        self.assertIn("runner lookup unavailable", payload["error"])
        github.dispatch.assert_not_called()

    def test_failed_dispatch_releases_capacity_reservation(self) -> None:
        body = push_body()
        with patch.object(self.server, "github") as github:
            github.dispatch.side_effect = RuntimeError("unexpected transport failure")
            status, payload = self.post(
                WEBHOOK_PATH,
                body,
                {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"},
            )
        self.assertEqual(status, 502)
        self.assertEqual(payload["error"], "GitHub dispatch failed")
        self.assertFalse(self.server.runner_reservations.get(REPO))

    def test_pending_dispatch_reserves_the_available_runner(self) -> None:
        body = push_body()
        headers = {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"}
        statuses: list[int] = []
        start = threading.Barrier(3)

        def dispatch() -> None:
            start.wait()
            status, _ = self.post(WEBHOOK_PATH, body, headers)
            statuses.append(status)

        with patch.object(self.server, "github") as github:
            github.list_runners.return_value = (
                RepositoryRunner("papyrus-helmdeck-web", "online", ("self-hosted", "Linux", "ARM64")),
            )
            requests = [threading.Thread(target=dispatch) for _ in range(2)]
            for request in requests:
                request.start()
            start.wait()
            for request in requests:
                request.join()

        self.assertCountEqual(statuses, [200, 409])
        self.assertEqual(github.dispatch.call_count, 1)

    def test_dispatch_uses_current_pool_capacity(self) -> None:
        body = push_body()
        headers = {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"}
        with patch.object(self.server, "github") as github:
            github.list_runners.return_value = (
                RepositoryRunner("papyrus-helmdeck-web", "online", ("self-hosted", "Linux", "ARM64")),
                RepositoryRunner("rover-helmdeck-web", "online", ("self-hosted", "Linux", "ARM64")),
            )
            status_a, payload_a = self.post(WEBHOOK_PATH, body, headers)
            status_b, payload_b = self.post(WEBHOOK_PATH, body, headers)
        self.assertEqual((status_a, status_b), (200, 200))
        self.assertEqual(payload_a["capacity"]["available"], 2)
        self.assertEqual(payload_b["capacity"]["reserved"], 1)
        self.assertEqual(payload_b["capacity"]["available"], 1)

    def test_public_repository_routes_to_hosted(self) -> None:
        body = push_body(private=False)
        with patch.object(self.server, "github") as github:
            status, payload = self.post(
                WEBHOOK_PATH,
                body,
                {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["lane"], "hosted")
        self.assertEqual(github.dispatch.call_args[0][3]["relay_lane"], "hosted")

    def test_repository_without_the_relay_workflow_is_ignored(self) -> None:
        body = push_body(repository="yesvus/other")
        with patch.object(self.server, "github") as github:
            github.workflow_exists.return_value = False
            status, payload = self.post(
                WEBHOOK_PATH, body, {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"}
            )
        self.assertEqual(status, 202)
        self.assertIn("opted in", payload["reason"])
        github.dispatch.assert_not_called()

    def test_non_default_branch_is_ignored_without_dispatch(self) -> None:
        body = push_body(ref="refs/heads/some-feature")
        with patch.object(self.server, "github") as github:
            status, payload = self.post(
                WEBHOOK_PATH, body, {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"}
            )
        self.assertEqual(status, 202)
        self.assertEqual(payload["status"], "ignored")
        github.dispatch.assert_not_called()

    def test_default_branch_is_read_from_the_repository_payload(self) -> None:
        body = json.dumps(
            {
                "ref": "refs/heads/master",
                "after": "0" * 40,
                "repository": {"full_name": "yesvus/birtedcom", "private": True, "default_branch": "master"},
            }
        ).encode()
        with patch.object(self.server, "github") as github:
            status, payload = self.post(
                WEBHOOK_PATH, body, {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"}
            )
        self.assertEqual(status, 200)
        self.assertEqual(payload["branch"], "master")
        github.dispatch.assert_called_once()

    def test_non_push_event_is_ignored(self) -> None:
        body = push_body()
        with patch.object(self.server, "github") as github:
            status, _ = self.post(
                WEBHOOK_PATH, body, {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "ping"}
            )
        self.assertEqual(status, 202)
        github.dispatch.assert_not_called()

    def test_unset_secret_refuses_rather_than_serving(self) -> None:
        self.server.webhook_secret = None
        status, _ = self.post(WEBHOOK_PATH, push_body(), {})
        self.assertEqual(status, 503)

    def test_internal_dispatch_requires_a_bearer_token(self) -> None:
        payload = json.dumps(
            {"repository": REPO, "workflow": "relay-dispatch.yml", "ref": "main", "private": True, "quota_status": "unknown"}
        ).encode()
        status, _ = self.post("/v1/dispatch", payload, {})
        self.assertEqual(status, 401)

    def test_internal_dispatch_with_wrong_token_is_refused(self) -> None:
        payload = json.dumps(
            {"repository": REPO, "workflow": "relay-dispatch.yml", "ref": "main", "private": True, "quota_status": "unknown"}
        ).encode()
        status, _ = self.post("/v1/dispatch", payload, {"Authorization": "Bearer nope"})
        self.assertEqual(status, 401)

    def test_internal_dispatch_with_token_is_allowed(self) -> None:
        payload = json.dumps(
            {"repository": REPO, "workflow": "relay-dispatch.yml", "ref": "main", "private": True, "quota_status": "unknown"}
        ).encode()
        with patch.object(self.server, "github"):
            status, body = self.post("/v1/dispatch", payload, {"Authorization": "Bearer dispatch-token"})
        self.assertEqual(status, 200)
        self.assertTrue(body["dispatched"])

    def test_internal_dispatch_rejects_invalid_inputs_before_capacity_check(self) -> None:
        payload = json.dumps(
            {
                "repository": REPO,
                "workflow": "relay-dispatch.yml",
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
            {"repository": REPO, "workflow": "relay-dispatch.yml", "ref": "main", "private": True, "quota_status": "unknown"}
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
