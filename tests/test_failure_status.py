"""The commit status a failed run publishes must name the step it stopped in.

The markers a device writes carry a per-run token, and the log the broker
receives is a bounded tail that may no longer contain the marker at all. These
cover both the structured path an agent uses today and the marker fallback an
older agent's log still needs.
"""
import unittest
from unittest.mock import patch

from job_endpoint_harness import REPO, JobEndpointHarness


class FailureStatusStepTest(JobEndpointHarness, unittest.TestCase):
    def _report(self, body: dict):
        job = self.server.jobs.enqueue(REPO, "abc123", "main")
        self.server.jobs.claim("server", [])
        with patch.object(self.server, "github") as github:
            self.call("POST", f"/v1/jobs/{job.id}/report", {"device": "server", **body})
        return github.post_status.call_args[0][3]

    def test_the_reported_records_name_the_step(self) -> None:
        steps = [
            {"index": 1, "name": "pnpm install", "output": "added 412 packages", "completed": True},
            {"index": 2, "name": "pnpm run ci", "output": "FAILED (failures=1)", "completed": False},
        ]
        description = self._report({"exit_code": 1, "log": "tail without markers\n", "steps": steps})
        self.assertEqual(description, "device pool run failed (exit 1): step 2: pnpm run ci")
        # command output never reaches a commit status
        self.assertNotIn("failures", description)

    def test_a_marker_with_a_run_token_is_read_when_no_records_arrived(self) -> None:
        # an agent that predates the step records still reports a log, and the
        # status must not fall back to a broker diagnostic
        nonce = "c" * 32
        log = f"cindral: step 1 {nonce}: pnpm install\ncindral: step 2 {nonce}: pnpm run ci\n"
        description = self._report({"exit_code": 1, "log": log})
        self.assertIn("step 2: pnpm run ci", description)

    def test_a_legacy_marker_without_a_token_is_still_read(self) -> None:
        description = self._report(
            {"exit_code": 1, "log": "cindral: step 1: pnpm install\ncindral: step 2: pnpm run ci\n"}
        )
        self.assertIn("step 2: pnpm run ci", description)

    def test_a_run_that_died_before_any_step_names_the_broker_diagnostic(self) -> None:
        description = self._report({"exit_code": 1, "log": "cindral: image pull failed\n"})
        self.assertIn("image pull failed", description)


if __name__ == "__main__":
    unittest.main()
