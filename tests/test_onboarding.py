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

    def test_adapter_can_delegate_to_a_local_reusable_workflow(self) -> None:
        adapter = """on:
  workflow_dispatch:
    inputs:
      relay_lane:
        required: true
        type: choice
        options: [hosted, fallback, device, burst]
      relay_target:
        type: string
      relay_reason:
        type: string
permissions:
  contents: read
concurrency:
  group: relay-${{ github.ref }}
  cancel-in-progress: false
jobs:
  hosted:
    if: inputs.relay_lane == 'hosted'
    uses: ./.github/workflows/relay-ci.yml
    with:
      runner: '[\"ubuntu-24.04\"]'
      lane: hosted
  fallback:
    if: inputs.relay_lane == 'fallback'
    uses: ./.github/workflows/relay-ci.yml
    with:
      runner: '[\"self-hosted\",\"Linux\",\"ARM64\"]'
      lane: fallback
  device:
    if: inputs.relay_lane == 'device' && (inputs.relay_target == 'papyrus' || inputs.relay_target == 'rover' || inputs.relay_target == 'colak')
    uses: ./.github/workflows/relay-ci.yml
    with:
      runner: ${{ inputs.relay_target == 'papyrus' && '[\"self-hosted\",\"Linux\",\"ARM64\",\"papyrus\"]' || inputs.relay_target == 'rover' && '[\"self-hosted\",\"Linux\",\"ARM64\",\"rover\"]' || '[\"self-hosted\",\"Linux\",\"ARM64\",\"colak\"]' }}
      lane: device
  burst:
    if: inputs.relay_lane == 'burst'
    uses: ./.github/workflows/relay-ci.yml
    with:
      runner: '[\"self-hosted\",\"Linux\",\"ARM64\",\"burst\"]'
      lane: burst
"""
        reusable = """on:
  workflow_call:
    inputs:
      runner:
        required: true
        type: string
      lane:
        required: true
        type: string
permissions:
  contents: read
jobs:
  ci:
    runs-on: ${{ fromJSON(inputs.runner) }}
    timeout-minutes: 60
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workflows = root / ".github/workflows"
            workflows.mkdir(parents=True)
            adapter_path = workflows / "relay-dispatch.yml"
            adapter_path.write_text(adapter)
            (workflows / "relay-ci.yml").write_text(reusable)
            self.assertEqual(validate_adapter(adapter_path, self.policy), ())

    def test_adapter_reports_missing_lane_inputs(self) -> None:
        content = (ROOT / "templates/personal-dispatch.yml").read_text()
        content = content.replace("      relay_reason:\n", "      reason:\n", 1)
        with tempfile.NamedTemporaryFile(mode="w+") as adapter:
            adapter.write(content)
            adapter.flush()
            errors = validate_adapter(adapter.name, self.policy)
        self.assertIn("workflow_dispatch is missing the relay_reason input", errors)

    def test_lane_readiness_requires_an_online_matching_runner(self) -> None:
        # fallback and device deliberately share the base label set, because
        # that is what every self-hosted runner on this account actually
        # carries. An offline runner must not make a lane ready.
        runners = (
            RepositoryRunner("fallback-box", "offline", self.policy.lane_labels["fallback"]),
            RepositoryRunner("spare-box", "online", ("self-hosted", "Linux", "X64")),
        )
        readiness = {lane.lane: lane for lane in check_lane_readiness(self.policy, runners)}
        self.assertTrue(readiness["hosted"].ready)
        self.assertFalse(readiness["fallback"].ready)
        self.assertEqual(readiness["fallback"].registered, ("fallback-box",))

    def test_online_runner_matching_base_labels_serves_fallback_and_its_device_lane(self) -> None:
        # An online ARM64 self-hosted runner satisfies the fallback lane as well
        # as its own device lane, because both are selected by the same three
        # labels and the device lane adds only the runner name. Documented
        # consequence of dropping the class labels: fallback is "any local
        # runner", not "a runner that calls itself fallback".
        runners = (
            RepositoryRunner(
                "papyrus",
                "online",
                self.policy.lane_labels["device"] + ("papyrus",),
            ),
        )
        readiness = {lane.lane: lane for lane in check_lane_readiness(self.policy, runners)}
        self.assertTrue(readiness["fallback"].ready)
        self.assertTrue(readiness["device:papyrus"].ready)
        self.assertFalse(readiness["device:rover"].ready)
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

    @patch("runner_relay.cli.GitHubClient")
    def test_onboard_registers_webhook_after_remote_adapter_is_confirmed(self, github_client) -> None:
        runners = (RepositoryRunner("fallback", "online", self.policy.lane_labels["fallback"]),)
        github_client.return_value.list_runners.return_value = runners
        github_client.return_value.workflow_exists.return_value = True
        github_client.return_value.ensure_push_webhook.return_value = "created"
        output = io.StringIO()
        with (
            patch.object(sys, "argv", [
                "runner-relay",
                "onboard",
                "yesvus/waymux",
                "--adapter",
                str(ROOT / "templates/personal-dispatch.yml"),
                "--register-webhook",
            ]),
            patch.dict(os.environ, {"GITHUB_TOKEN": "token", "RELAY_WEBHOOK_SECRET": "secret"}),
            patch("sys.stdout", output),
        ):
            main()
        github_client.return_value.workflow_exists.assert_called_once_with("yesvus/waymux", "relay-dispatch.yml")
        github_client.return_value.ensure_push_webhook.assert_called_once_with(
            "yesvus/waymux", "https://hook.yesvus.com/relay/dispatch", "secret"
        )
        self.assertIn("Webhook: created", output.getvalue())

    @patch("runner_relay.cli.GitHubClient")
    def test_onboard_refuses_webhook_before_adapter_is_on_default_branch(self, github_client) -> None:
        runners = (RepositoryRunner("fallback", "online", self.policy.lane_labels["fallback"]),)
        github_client.return_value.list_runners.return_value = runners
        github_client.return_value.workflow_exists.return_value = False
        with (
            patch.object(sys, "argv", [
                "runner-relay",
                "onboard",
                "yesvus/waymux",
                "--adapter",
                str(ROOT / "templates/personal-dispatch.yml"),
                "--register-webhook",
            ]),
            patch.dict(os.environ, {"GITHUB_TOKEN": "token", "RELAY_WEBHOOK_SECRET": "secret"}),
            patch("sys.stdout", io.StringIO()),
            patch("sys.stderr", io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            main()
        github_client.return_value.ensure_push_webhook.assert_not_called()


if __name__ == "__main__":
    unittest.main()
