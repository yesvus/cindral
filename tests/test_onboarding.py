import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from runner_relay.cli import main
from runner_relay.github import RepositoryRunner
from runner_relay.onboarding import check_lane_readiness, validate_adapter
from runner_relay.policy import Policy


ROOT = Path(__file__).resolve().parents[1]


class OnboardingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = Policy.load(ROOT / "config/policy.toml")

    def test_repository_adapter_matches_policy(self) -> None:
        self.assertEqual(
            validate_adapter(ROOT / "templates/personal-dispatch.yml", self.policy),
            (),
        )

    def test_adapter_reports_missing_lane_inputs(self) -> None:
        content = (ROOT / "templates/personal-dispatch.yml").read_text()
        content = content.replace("      relay_reason:\n", "      reason:\n", 1)
        with tempfile.NamedTemporaryFile(mode="w+") as adapter:
            adapter.write(content)
            adapter.flush()
            errors = validate_adapter(adapter.name, self.policy)
        self.assertIn("workflow_dispatch is missing the relay_reason input", errors)

    def test_lane_readiness_reports_registered_online_and_missing(self) -> None:
        runners = (
            RepositoryRunner("fallback-box", "offline", self.policy.lane_labels["fallback"]),
            RepositoryRunner(
                "papyrus",
                "online",
                self.policy.lane_labels["device"] + ("papyrus",),
            ),
        )
        readiness = {lane.lane: lane for lane in check_lane_readiness(self.policy, runners)}
        self.assertTrue(readiness["hosted"].ready)
        self.assertFalse(readiness["fallback"].ready)
        self.assertEqual(readiness["fallback"].registered, ("fallback-box",))
        self.assertTrue(readiness["device:papyrus"].ready)
        self.assertFalse(readiness["burst"].ready)

    @patch("runner_relay.cli.GitHubClient")
    def test_onboard_command_reports_ready_lanes(self, github_client) -> None:
        runners = [
            RepositoryRunner("fallback", "online", self.policy.lane_labels["fallback"]),
            RepositoryRunner("burst", "online", self.policy.lane_labels["burst"]),
        ]
        runners.extend(
            RepositoryRunner(target, "online", self.policy.lane_labels["device"] + (target,))
            for target in self.policy.device_priority
        )
        github_client.return_value.list_runners.return_value = tuple(runners)
        output = io.StringIO()
        with (
            patch.object(sys, "argv", [
                "runner-relay",
                "onboard",
                "yesvus/runner-relay",
                "--adapter",
                str(ROOT / "templates/personal-dispatch.yml"),
                "--github-token-env",
                "ONBOARD_TEST_TOKEN",
            ]),
            patch.dict(os.environ, {"ONBOARD_TEST_TOKEN": "token"}),
            patch("sys.stdout", output),
        ):
            main()
        self.assertIn("Adapter: valid", output.getvalue())
        self.assertIn("device:papyrus: ready", output.getvalue())
        self.assertIn("Overall: ready", output.getvalue())


if __name__ == "__main__":
    unittest.main()
