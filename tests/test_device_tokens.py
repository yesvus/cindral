"""A device name is only worth what the credential behind it can prove.

One shared agent token made every device-facing request impersonable, because the
device name arrives in the request body or a header and was never checked against
anything. Naming the device that held a lease was enough to report its verdict and
write the commit status, enough to claim work and take it from the pool, and enough
to announce labels and concurrency the device does not have.

A per-device token makes the name something the credential has to match.
"""
import unittest

from cindral.jobs import SUCCESS
from job_endpoint_harness import REPO, JobEndpointHarness


class PerDeviceTokenTest(JobEndpointHarness, unittest.TestCase):
    """A device's name is only worth what the credential behind it can prove.

    One shared agent token made every device-facing request impersonable: the
    device name arrives in the request body or a header, so naming the device that
    held a lease was enough to report its verdict, and enough to take work by
    claiming as it. A per-device token makes the name something the credential has
    to match rather than something the request merely says.
    """

    def setUp(self) -> None:
        super().setUp()
        self.server.agent_token = None
        self.server.agent_tokens = {"worker-a": "token-a", "worker-b": "token-b"}

    def leased_to_a(self) -> str:
        """A job leased to worker-a, so worker-b is asking about someone else's run."""
        enqueued = self.server.jobs.enqueue(REPO, "abc123", "main", labels=("arm64",))
        self.server.jobs.claim("worker-a", ["arm64"], lease_seconds=600)
        return enqueued.id

    def test_a_device_may_speak_only_for_itself(self) -> None:
        status, _ = self.heartbeat(
            {"device": "worker-a", "labels": ["arm64"], "max_concurrency": 2}, token="token-a"
        )
        self.assertEqual(status, 200)

    def test_one_device_cannot_report_a_verdict_as_another(self) -> None:
        job_id = self.leased_to_a()
        status, _ = self.call(
            "POST", f"/v1/jobs/{job_id}/report",
            {"device": "worker-a", "exit_code": 0},
            token="token-b",
        )
        self.assertEqual(status, 401)
        self.assertNotEqual(
            self.server.jobs.get(job_id).status,
            SUCCESS,
            "a forged report wrote a verdict for a device that did not run the job",
        )

    def test_one_device_cannot_claim_as_another(self) -> None:
        self.server.jobs.enqueue(REPO, "abc123", "main", labels=("arm64",))
        status, _ = self.call("POST", "/v1/jobs/claim", {"device": "worker-a"}, token="token-b")
        self.assertEqual(status, 401)
        self.assertIsNotNone(
            self.server.jobs.claim("worker-a", ["arm64"], lease_seconds=60),
            "a forged claim took work away from the pool",
        )

    def test_one_device_cannot_renew_another_lease(self) -> None:
        job_id = self.leased_to_a()
        before = self.server.jobs.get(job_id).lease_expires
        status, _ = self.call(
            "POST", f"/v1/jobs/{job_id}/renew", {"device": "worker-a"}, token="token-b"
        )
        self.assertEqual(status, 401)
        self.assertEqual(self.server.jobs.get(job_id).lease_expires, before)

    def test_one_device_cannot_heartbeat_as_another(self) -> None:
        status, _ = self.heartbeat(
            {"device": "worker-a", "labels": ["arm64"], "max_concurrency": 2},
            token="token-b",
        )
        self.assertEqual(status, 401)

    def test_an_unknown_token_speaks_for_nobody(self) -> None:
        status, _ = self.heartbeat(
            {"device": "worker-a", "labels": ["arm64"], "max_concurrency": 2},
            token="token-nobody",
        )
        self.assertEqual(status, 401)
