import dataclasses
import hashlib
import json
import os
import tempfile
import threading
import unittest
from cindral.service import serve
from unittest import mock
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from cindral.app_auth import AppAuth
from cindral.github import RepositoryRunner
from cindral.cache import CacheStore
from cindral.cache_client import CacheClientError, CindralCacheClient
from cindral.deliveries import DeliveryStore
from cindral.jobs import JobStore
from cindral.budget import attach_budget
from cindral.metering import MeteredLedger
from cindral.observability import health_payload
from cindral.policy import Policy
from cindral.service import (
    WEBHOOK_PATH,
    CindralHandler,
    CindralServer,
)
from cindral.state import load_runners
from cindral.webhook import sign

SECRET = "webhook-secret"
AGENT_TOKEN = "agent-token"
POOL_TOKEN = "pool-token"
REPO = "example-org/example-app"
SHIPPED_POLICY = Path("config/policy.toml").read_text(encoding="utf-8")


def push_body(private=True):
    return json.dumps(
        {
            "ref": "refs/heads/main",
            "after": "abc123",
            "repository": {"full_name": REPO, "private": private, "default_branch": "main"},
        }
    ).encode()


class ServiceEndpointTest(unittest.TestCase):
    """The broker surfaces outside the job queue: routing, pool, metrics, cache.

    Claim, report, job status, job log, and agent presence live in
    ``test_jobapi.py``, which owns the endpoints a device agent drives.
    """

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
        self.server.deliveries = DeliveryStore(str(Path(self._tmp.name) / "jobs.db"))
        self.server.cache = CacheStore(Path(self._tmp.name) / "cache")
        self.server.agent_token = AGENT_TOKEN
        self.server.pool_token = POOL_TOKEN
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def load_policy(self, text: str) -> Policy:
        path = Path(self._tmp.name) / "policy.toml"
        path.write_text(text, encoding="utf-8")
        return Policy.load(path)

    def policy_for_mode(self, mode: str) -> Policy:
        """The shipped policy with one repository's mode changed.

        Replacing only the mode keeps every routing setting the shipped policy
        carries, so the test differs from its neighbours in the one way it
        names. A configuration that stopped listing REPO would break here rather
        than silently test nothing.
        """
        policy = Policy.load("config/policy.toml")
        self.assertEqual([t.repository for t in policy.dispatch_targets], [REPO])
        target = policy.dispatch_targets[0]
        return dataclasses.replace(
            policy,
            dispatch_targets=(dataclasses.replace(target, mode=mode),),
        )

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

    def deliver(self, body, headers):
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request("POST", WEBHOOK_PATH, body=body, headers={"Content-Type": "application/json", **headers})
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        return response.status, (json.loads(raw) if raw else {})

    def test_healthz_reports_the_build_without_a_credential(self) -> None:
        # the version is how an operator tells which deployment a page is
        # talking to, so it must not sit behind the pool or agent token
        with patch.dict("os.environ", {"CINDRAL_VERSION": "0.11.0", "CINDRAL_COMMIT": "b" * 40}):
            status, payload = self.call("GET", "/healthz", authorize=False)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["build"]["version"], "0.11.0")
        self.assertEqual(payload["build"]["commit"], "b" * 40)

    def test_healthz_reports_the_policy_digest_loaded_not_the_file(self) -> None:
        """A ConfigMap can change under a running broker, and nothing else says so.

        The digest answers "is the broker enforcing the policy I think it is", so it
        has to be the policy that was loaded. Re-reading the file would report the
        new file's digest while the old policy is still being enforced, which is the
        same silence with a fingerprint attached: an operator widens a policy,
        sees a fresh digest, and concludes the change took effect.
        """
        path = Path(self._tmp.name) / "policy.toml"
        path.write_text(SHIPPED_POLICY, encoding="utf-8")
        loaded = Policy.load(path)
        self.server.policy = loaded

        path.write_text(SHIPPED_POLICY + '\n[lanes.broken]\n', encoding="utf-8")

        status, payload = self.call("GET", "/healthz", authorize=False)
        self.assertEqual(status, 200)
        self.assertEqual(payload["policy"]["fingerprint"], loaded.fingerprint)
        self.assertEqual(len(payload["policy"]["fingerprint"]), 16)

        # the digest identifies a policy rather than merely being present, so
        # loading different bytes gives a different value and the same bytes give
        # the same one: an operator comparing two brokers' output learns something
        different = "one of them is running the policy you think it is"
        path.write_text(SHIPPED_POLICY, encoding="utf-8")
        self.assertEqual(Policy.load(path).fingerprint, loaded.fingerprint)
        path.write_text(SHIPPED_POLICY + "\n", encoding="utf-8")
        self.assertNotEqual(Policy.load(path).fingerprint, loaded.fingerprint)

    def test_healthz_omits_the_policy_when_no_digest_was_supplied(self) -> None:
        with patch.dict("os.environ", {"CINDRAL_VERSION": "0.11.0", "CINDRAL_COMMIT": "b" * 40}):
            body = health_payload(None)
        self.assertNotIn("policy", body)
        self.assertNotIn("metered", body)

    def test_healthz_reports_the_metered_month_when_a_ceiling_is_configured(self) -> None:
        """The month has to be readable on a probe, not only in a refusal.

        A refusal names the month and the minutes used, but a refusal means a
        dispatch was already turned away. Reading the ceiling only at that point
        is reading it only once it has cost something, so the same figures are on
        /healthz and /v1/pool whether or not anything is being refused.
        """
        path = Path(self._tmp.name) / "policy.toml"
        # the shipped policy already carries [quota], so the setting is added into
        # that table rather than as a second one, which is not valid TOML
        path.write_text(
            SHIPPED_POLICY.replace(
                "[quota]\n", "[quota]\nmonthly_minute_budget = 100.0\n", 1
            ),
            encoding="utf-8",
        )
        self.server.policy = Policy.load(path)
        self.server.metered = MeteredLedger(str(Path(self._tmp.name) / "metered.db"))
        attach_budget(self.server, self.server.metered)
        for _ in range(3):
            self.server.metered.reserve(REPO, "hosted", "cindral-dispatch.yml", "refs/heads/main")

        status, payload = self.call("GET", "/healthz", authorize=False)
        self.assertEqual(status, 200)
        self.assertEqual(payload["metered"]["used_minutes"], 30.0)
        self.assertEqual(payload["metered"]["remaining_minutes"], 70.0)
        self.assertTrue(payload["metered"]["next_dispatch_admitted"])

        status, pool = self.call("GET", "/v1/pool", token=POOL_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(pool["metered"], payload["metered"])

    def test_metrics_carry_the_build_as_labels(self) -> None:
        with patch.dict("os.environ", {"CINDRAL_VERSION": "0.11.0", "CINDRAL_COMMIT": "c" * 40}):
            connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
            connection.request("GET", "/metrics", headers={"Authorization": f"Bearer {POOL_TOKEN}"})
            response = connection.getresponse()
            text = response.read().decode()
        self.assertIn('cindral_build{version="0.11.0"', text)

    def test_push_to_a_dispatch_only_repository_still_dispatches(self) -> None:
        # mode = "dispatch" is the repository being served without being served
        # on a device, so the webhook path falls through to the hosted lane
        self.server.policy = self.policy_for_mode("dispatch")
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
            status, _ = self.deliver(
                body,
                {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"},
            )
        self.assertEqual(status, 200)
        github.dispatch.assert_called_once()
        self.assertEqual(self.server.jobs.list(), [])

    def test_signed_push_refuses_to_dispatch_without_a_delivery_database(self) -> None:
        self.server.policy = self.policy_for_mode("dispatch")
        self.server.deliveries = None
        body = push_body()
        with patch.object(self.server, "github") as github:
            status, payload = self.deliver(body, {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"})
        self.assertEqual(status, 503)
        self.assertIn("CINDRAL_JOBS_DB", payload["error"])
        github.dispatch.assert_not_called()

    def test_signed_push_deduplication_survives_store_restart(self) -> None:
        self.server.policy = self.policy_for_mode("dispatch")
        body = push_body()
        headers = {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push", "X-GitHub-Delivery": "stable-id"}
        with patch.object(self.server, "github") as github:
            github.list_runners.return_value = (RepositoryRunner("runner", "online", ("self-hosted", "Linux", "ARM64", "fallback")),)
            first, _ = self.deliver(body, headers)
            self.server.deliveries = DeliveryStore(str(Path(self._tmp.name) / "jobs.db"))
            second, payload = self.deliver(body, headers)
        self.assertEqual((first, second), (200, 202))
        self.assertEqual(payload["status"], "duplicate")
        github.dispatch.assert_called_once()

    def test_delivery_id_cannot_be_reused_for_a_different_signed_body(self) -> None:
        self.server.policy = self.policy_for_mode("dispatch")
        first_body = push_body()
        second_body = push_body(private=False)
        with patch.object(self.server, "github") as github:
            github.list_runners.return_value = (RepositoryRunner("runner", "online", ("self-hosted", "Linux", "ARM64", "fallback")),)
            first, _ = self.deliver(first_body, {"X-Hub-Signature-256": sign(SECRET, first_body), "X-GitHub-Event": "push", "X-GitHub-Delivery": "same-id"})
            second, payload = self.deliver(second_body, {"X-Hub-Signature-256": sign(SECRET, second_body), "X-GitHub-Event": "push", "X-GitHub-Delivery": "same-id"})
        self.assertEqual((first, second), (200, 409))
        self.assertIn("different payload", payload["error"])
        github.dispatch.assert_called_once()

    def test_ready_delivery_replays_with_persisted_lane(self) -> None:
        self.server.policy = self.policy_for_mode("dispatch")
        body = push_body()
        self.server.deliveries.register(REPO, "ready-id", hashlib.sha256(body).hexdigest(),
                                        "cindral-dispatch.yml", "main", {"cindral_lane": "fallback"})
        with patch.object(self.server, "github") as github:
            status, _ = self.deliver(body, {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push", "X-GitHub-Delivery": "ready-id"})
        self.assertEqual(status, 202)
        self.assertEqual(github.dispatch.call_args.args[3]["cindral_lane"], "fallback")
        github.list_runners.assert_not_called()

    def test_rejected_delivery_returns_422_without_a_second_dispatch(self) -> None:
        self.server.policy = self.policy_for_mode("dispatch")
        body = push_body()
        self.server.deliveries.register(REPO, "rejected-id", hashlib.sha256(body).hexdigest(),
                                        "cindral-dispatch.yml", "main", {"cindral_lane": "fallback"})
        self.assertTrue(self.server.deliveries.claim(REPO, "rejected-id"))
        self.server.deliveries.finish(REPO, "rejected-id", "rejected", "invalid workflow input")
        with patch.object(self.server, "github") as github:
            status, payload = self.deliver(body, {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push", "X-GitHub-Delivery": "rejected-id"})
        self.assertEqual((status, payload["status"]), (422, "rejected"))
        github.dispatch.assert_not_called()

    def test_missing_workflow_cannot_reject_a_claimed_delivery(self) -> None:
        self.server.policy = self.policy_for_mode("dispatch")
        body = push_body()
        store = self.server.deliveries
        store.register(REPO, "racing-id", hashlib.sha256(body).hexdigest(),
                       "cindral-dispatch.yml", "main", {"cindral_lane": "fallback"})

        def claimed_before_rejection(repository, delivery, _reason):
            self.assertTrue(store.claim(repository, delivery))
            return False

        with patch.object(self.server, "github") as github, patch.object(store, "reject_ready", side_effect=claimed_before_rejection):
            github.workflow_exists.return_value = False
            status, payload = self.deliver(body, {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push", "X-GitHub-Delivery": "racing-id"})
        self.assertEqual((status, payload["status"]), (503, "attempting"))
        github.dispatch.assert_not_called()

    def test_concurrent_registration_reports_the_recorded_lane(self) -> None:
        self.server.policy = self.policy_for_mode("dispatch")
        body = push_body(private=False)
        store = self.server.deliveries
        store.register(REPO, "lane-id", hashlib.sha256(body).hexdigest(),
                       "cindral-dispatch.yml", "main", {"cindral_lane": "fallback"})
        with patch.object(self.server, "github") as github, patch.object(store, "get", return_value=None):
            status, payload = self.deliver(body, {"X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push", "X-GitHub-Delivery": "lane-id"})
        self.assertEqual((status, payload["status"]), (202, "accepted"))
        self.assertNotIn("lane", payload)
        self.assertEqual(github.dispatch.call_args.args[3]["cindral_lane"], "fallback")

    def test_pool_snapshot_requires_authorization(self) -> None:
        status, _ = self.call("GET", "/v1/pool", authorize=False)
        self.assertEqual(status, 401)

    def test_pool_snapshot_accepts_the_read_only_token(self) -> None:
        status, payload = self.call("GET", "/v1/pool", token=POOL_TOKEN)
        self.assertEqual(status, 200)
        self.assertIn("queue_depth", payload)
        # the pool is where an operator looks when routing looks wrong, so the
        # policy digest belongs beside the queue rather than only on the probe
        self.assertEqual(payload["policy"]["fingerprint"], self.server.policy.fingerprint)

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
        self.assertIn("cindral_cache_hit_ratio", raw)

    def test_cache_api_round_trip_uses_agent_token_and_content_digest(self) -> None:
        client = CindralCacheClient(f"http://127.0.0.1:{self.port}", AGENT_TOKEN)
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "cache.tar.gz"
            archive.write_bytes(b"cache contents")
            uploaded = client.upload(REPO, "main", "arm64", "pnpm-arm64-lock", archive)
            self.assertTrue(uploaded["created"])
            entry = client.lookup(REPO, "main", "arm64", "pnpm-arm64-lock")
            self.assertEqual(entry["digest"], uploaded["entry"]["digest"])
            destination = Path(tmp) / "restored.tar.gz"
            client.download(entry["digest"], destination, REPO, "main", "arm64", "pnpm-arm64-lock")
            self.assertEqual(destination.read_bytes(), b"cache contents")

    def test_cache_lookup_requires_the_agent_token(self) -> None:
        status, _ = self.call(
            "POST",
            "/v1/cache/lookup",
            {"repository": REPO, "branch": "main", "architecture": "arm64", "key": "key"},
            authorize=False,
        )
        self.assertEqual(status, 401)

    def test_cache_api_refuses_the_read_only_pool_token(self) -> None:
        status, _ = self.call(
            "POST",
            "/v1/cache/lookup",
            {"repository": REPO, "branch": "main", "architecture": "arm64", "key": "key"},
            headers={"Authorization": f"Bearer {POOL_TOKEN}"},
            authorize=False,
        )
        self.assertEqual(status, 401)
        status, _ = self.call(
            "PUT",
            "/v1/cache/entries",
            headers={
                "Authorization": f"Bearer {POOL_TOKEN}",
                "Content-Type": "application/octet-stream",
                "X-Cindral-Repository": REPO,
                "X-Cindral-Branch": "main",
                "X-Cindral-Architecture": "arm64",
                "X-Cindral-Key": "key",
            },
            body=b"data",
            authorize=False,
        )
        self.assertEqual(status, 401)

    def test_cache_blob_requires_a_matching_scoped_entry(self) -> None:
        client = CindralCacheClient(f"http://127.0.0.1:{self.port}", AGENT_TOKEN)
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "cache.tar.gz"
            archive.write_bytes(b"cache contents")
            uploaded = client.upload(REPO, "main", "arm64", "key", archive)
            other_scope = Path(tmp) / "other.tar.gz"
            with self.assertRaises(CacheClientError):
                client.download(uploaded["entry"]["digest"], other_scope, "another/repo", "main", "arm64", "key")
            self.assertFalse(other_scope.exists())

    def test_download_digest_mismatch_preserves_preexisting_destination(self) -> None:
        client = CindralCacheClient(f"http://127.0.0.1:{self.port}", AGENT_TOKEN)
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "cache.tar.gz"
            archive.write_bytes(b"cache contents")
            uploaded = client.upload(REPO, "main", "arm64", "key", archive)
            digest = uploaded["entry"]["digest"]
            self.server.cache.blob_path(digest).write_bytes(b"corrupted contents")

            destination = Path(tmp) / "preexisting.tar.gz"
            destination.write_bytes(b"original data")
            with self.assertRaises(CacheClientError) as cm:
                client.download(digest, destination, REPO, "main", "arm64", "key")
            self.assertIn("content digest check", str(cm.exception))
            self.assertTrue(destination.exists())
            self.assertEqual(destination.read_bytes(), b"original data")
            self.assertFalse((destination.parent / f".tmp-{destination.name}-{os.getpid()}").exists())

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


class AppHealthTest(ServiceEndpointTest):
    """/healthz has to say the App is unusable, or it is not a health check.

    The App is how every push reaches the broker. A broker that is up, serving,
    and cannot authenticate delivers nothing, and a `status: ok` on /healthz is
    exactly what a probe or an operator reads to conclude the opposite.
    """

    def test_a_broker_with_no_app_configured_still_reports_ok(self) -> None:
        status, payload = self.call("GET", "/healthz", authorize=False)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertNotIn("app_auth", payload)

    def test_a_healthy_app_is_reported_alongside_the_build(self) -> None:
        self.server.app = {"ok": True, "installation": 42, "account": "example-org"}
        status, payload = self.call("GET", "/healthz", authorize=False)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["app_auth"]["ok"])
        self.assertEqual(payload["app_auth"]["installation"], 42)

    def test_the_probe_reports_how_much_the_installation_covers(self) -> None:
        """The count was computed at startup and printed, so a probe could not have it.

        Recomputing it per probe would mean an API call on a health check, so it is
        carried from the one the startup already paid for. A reader can then ask how
        much of GitHub this broker can reach without reading its logs.
        """
        self.server.app = {
            "ok": True, "installation": 42, "account": "example-org",
            "repository_selection": "selected", "repositories": 7,
        }
        status, payload = self.call("GET", "/healthz", authorize=False)
        self.assertEqual(status, 200)
        self.assertEqual(payload["app_auth"]["repositories"], 7)
        self.assertEqual(payload["app_auth"]["repository_selection"], "selected")

    def test_the_block_passes_through_exactly_what_the_app_reported(self) -> None:
        """The probe reports the App's own summary and filters nothing.

        A test asserting the token never reaches `/healthz` belongs to
        `AppAuth.health()`, which is what decides not to include it, and
        `test_app_auth.py` already asserts that. Filtering here as well would be a
        second implementation of the rule, and a pass-through that silently drops a
        field is its own kind of wrong.
        """
        self.server.app = {
            "ok": True, "installation": 42, "account": "example-org",
            "token_expires_in_seconds": 1800,
        }
        status, payload = self.call("GET", "/healthz", authorize=False)
        self.assertEqual(status, 200)
        self.assertEqual(payload["app_auth"], self.server.app)

    def test_an_expired_token_surfaces_as_not_ok_with_the_reason(self) -> None:
        # remaining life is zeroed rather than negative, and the reason names why
        self.server.app = {
            "ok": False, "error": "installation token expired",
            "token_expires_in_seconds": 0,
        }
        status, payload = self.call("GET", "/healthz", authorize=False)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "degraded")
        self.assertEqual(payload["app_auth"]["token_expires_in_seconds"], 0)
        self.assertIn("expired", payload["app_auth"]["error"])

    def test_a_broken_app_degrades_the_status(self) -> None:
        # the whole point: a 200 with status ok would pass every probe
        self.server.app = {"ok": False, "error": "installation is suspended"}
        status, payload = self.call("GET", "/healthz", authorize=False)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "degraded")
        self.assertIn("suspended", payload["app_auth"]["error"])

    def test_healthz_passes_through_exactly_what_health_reported(self) -> None:
        # /healthz echoes AppAuth.health() rather than assembling its own view,
        # so the guarantee that a token never appears belongs there. Asserting it
        # here against a hand-built dict would only prove the echo is an echo.
        with patch.object(AppAuth, "health", return_value={"ok": True, "installation": 7}):
            reported = AppAuth("client", b"key").health()
        self.assertNotIn("token", json.dumps(reported))
        self.server.app = reported
        _, payload = self.call("GET", "/healthz", authorize=False)
        self.assertNotIn("token", json.dumps(payload))


class DisconnectLoggingTest(unittest.TestCase):
    """A client that hangs up is routine and must not print a traceback.

    GitHub abandons deliveries it has already retried, a panel tab navigates
    away, a port scanner connects and leaves. The default handler prints a full
    traceback for each, so an ordinary day buries the real errors.
    """

    def test_a_broken_pipe_is_swallowed(self) -> None:
        server = CindralServer(("127.0.0.1", 0), CindralHandler)
        self.addCleanup(server.server_close)
        with patch.object(ThreadingHTTPServer, "handle_error") as parent:
            try:
                raise BrokenPipeError(32, "Broken pipe")
            except BrokenPipeError:
                server.handle_error(object(), ("127.0.0.1", 1))
        parent.assert_not_called()

    def test_a_connection_reset_is_swallowed(self) -> None:
        server = CindralServer(("127.0.0.1", 0), CindralHandler)
        self.addCleanup(server.server_close)
        with patch.object(ThreadingHTTPServer, "handle_error") as parent:
            try:
                raise ConnectionResetError(104, "Connection reset by peer")
            except ConnectionResetError:
                server.handle_error(object(), ("127.0.0.1", 1))
        parent.assert_not_called()

    def test_a_real_error_still_reaches_the_parent(self) -> None:
        # the point of the override is the two disconnects, not silence
        server = CindralServer(("127.0.0.1", 0), CindralHandler)
        self.addCleanup(server.server_close)
        with patch.object(ThreadingHTTPServer, "handle_error") as parent:
            try:
                raise ValueError("something actually broke")
            except ValueError:
                server.handle_error(object(), ("127.0.0.1", 1))
        parent.assert_called_once()


if __name__ == "__main__":
    unittest.main()
