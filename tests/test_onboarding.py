import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from cindral.cli import main
from cindral.github import RepositoryRunner
from cindral.onboarding import (
    check_lane_readiness,
    describe_policy,
    resolve_policy_path,
    validate_adapter,
)
from cindral.policy import Policy


ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT_TARGETS = ("device-gamma", "device-delta", "device-epsilon")


def _deployment_policy(targets: tuple[str, ...]) -> str:
    """A policy that differs from the repository default, as a mounted ConfigMap does."""
    device_priority = ", ".join(f'"{target}"' for target in targets)
    return f"""[hosted]
runner = "ubuntu-24.04"

[local]
priority = ["fallback"]
device_priority = [{device_priority}]

[lanes.fallback]
labels = ["self-hosted", "Linux", "ARM64", "fallback"]

[lanes.device]
labels = ["self-hosted", "Linux", "ARM64"]

[lanes.burst]
labels = ["self-hosted", "Linux", "ARM64", "burst"]
"""


def _adapter_for(targets: tuple[str, ...]) -> str:
    """The repository template, with the device job naming the given targets.

    Built by construction rather than by replacing text the shipped template
    happens to contain. The template no longer enumerates targets, because the
    runner label already selects the device, so a helper that replaced the old
    hardcoded names would quietly become a no-op and every test using it would keep
    passing while testing nothing. The anchor is therefore required, not assumed.
    """
    template = (ROOT / "templates/personal-dispatch.yml").read_text()
    anchor = "    if: inputs.cindral_lane == 'device'"
    if anchor not in template:
        raise AssertionError(f"the device job condition moved: {anchor!r} is not in the template")
    guard = " || ".join(f"inputs.cindral_target == '{target}'" for target in targets)
    return template.replace(anchor, f"{anchor} && ({guard})", 1)


class OnboardingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = Policy.load(ROOT / "config/policy.toml")

    def _run_onboard(self, github_client, *arguments, environment, runners=()):
        """Run the onboard command in an isolated environment, returning its exit code and output."""
        github_client.return_value.list_runners.return_value = tuple(runners)
        output, errors = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, "argv", ["cindral", "onboard", "example-org/example-app", *arguments]),
            patch.dict(os.environ, {"GITHUB_TOKEN": "token", **environment}, clear=True),
            patch("sys.stdout", output),
            patch("sys.stderr", errors),
        ):
            try:
                main()
            except SystemExit as exit_request:
                return exit_request.code, output.getvalue(), errors.getvalue()
        return 0, output.getvalue(), errors.getvalue()

    def test_repository_adapter_matches_policy(self) -> None:
        self.assertEqual(
            validate_adapter(ROOT / "templates/personal-dispatch.yml", self.policy),
            (),
        )

    def test_adapter_can_delegate_to_a_local_reusable_workflow(self) -> None:
        adapter = """on:
  workflow_dispatch:
    inputs:
      cindral_lane:
        required: true
        type: choice
        options: [hosted, fallback, device, burst]
      cindral_target:
        type: string
      cindral_reason:
        type: string
      cindral_ref:
        required: false
        type: string
        default: ""
permissions:
  contents: read
concurrency:
  group: cindral-${{ github.ref }}
  cancel-in-progress: false
jobs:
  hosted:
    if: inputs.cindral_lane == 'hosted'
    uses: ./.github/workflows/cindral-ci.yml
    with:
      runner: '[\"ubuntu-24.04\"]'
      lane: hosted
  fallback:
    if: inputs.cindral_lane == 'fallback'
    uses: ./.github/workflows/cindral-ci.yml
    with:
      runner: '[\"self-hosted\",\"Linux\",\"ARM64\",\"fallback\"]'
      lane: fallback
  device:
    if: inputs.cindral_lane == 'device' && (inputs.cindral_target == 'desktop' || inputs.cindral_target == 'laptop' || inputs.cindral_target == 'phone')
    uses: ./.github/workflows/cindral-ci.yml
    with:
      runner: ${{ inputs.cindral_target == 'desktop' && '[\"self-hosted\",\"Linux\",\"ARM64\",\"desktop\"]' || inputs.cindral_target == 'laptop' && '[\"self-hosted\",\"Linux\",\"ARM64\",\"laptop\"]' || '[\"self-hosted\",\"Linux\",\"ARM64\",\"phone\"]' }}
      lane: device
  burst:
    if: inputs.cindral_lane == 'burst'
    uses: ./.github/workflows/cindral-ci.yml
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
            adapter_path = workflows / "cindral-dispatch.yml"
            adapter_path.write_text(adapter)
            (workflows / "cindral-ci.yml").write_text(reusable)
            self.assertEqual(validate_adapter(adapter_path, self.policy), ())

    def test_adapter_reports_missing_lane_inputs(self) -> None:
        content = (ROOT / "templates/personal-dispatch.yml").read_text()
        content = content.replace("      cindral_reason:\n", "      reason:\n", 1)
        with tempfile.NamedTemporaryFile(mode="w+") as adapter:
            adapter.write(content)
            adapter.flush()
            errors = validate_adapter(adapter.name, self.policy)
        self.assertIn("workflow_dispatch is missing the cindral_reason input", errors)

    def test_adapter_requires_cindral_ref_input(self) -> None:
        content = (ROOT / "templates/personal-dispatch.yml").read_text()
        content = content.replace("      cindral_ref:\n", "      pr_ref:\n", 1)
        with tempfile.NamedTemporaryFile(mode="w+") as adapter:
            adapter.write(content)
            adapter.flush()
            errors = validate_adapter(adapter.name, self.policy)
        self.assertIn("workflow_dispatch is missing the cindral_ref input", errors)

    def test_lane_readiness_requires_an_online_matching_runner(self) -> None:
        runners = (
            RepositoryRunner("fallback-box", "offline", self.policy.lane_labels["fallback"]),
            RepositoryRunner("spare-box", "online", ("self-hosted", "Linux", "X64")),
        )
        readiness = {lane.lane: lane for lane in check_lane_readiness(self.policy, runners)}
        self.assertTrue(readiness["hosted"].ready)
        self.assertFalse(readiness["fallback"].ready)
        self.assertEqual(readiness["fallback"].registered, ("fallback-box",))

    def test_device_runner_does_not_make_fallback_lane_ready(self) -> None:
        runners = (
            RepositoryRunner(
                "desktop",
                "online",
                self.policy.lane_labels["device"] + ("desktop",),
            ),
        )
        readiness = {lane.lane: lane for lane in check_lane_readiness(self.policy, runners)}
        self.assertFalse(readiness["fallback"].ready)
        self.assertTrue(readiness["device:desktop"].ready)
        self.assertFalse(readiness["device:laptop"].ready)
        self.assertFalse(readiness["burst"].ready)

    @patch("cindral.cli.GitHubClient")
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
                "cindral",
                "onboard",
                "example-org/cindral",
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
        self.assertIn("device:desktop: ready", output.getvalue())
        self.assertIn("Overall: ready", output.getvalue())

    @patch("cindral.cli.GitHubClient")
    def test_onboard_registers_webhook_after_remote_adapter_is_confirmed(self, github_client) -> None:
        runners = (RepositoryRunner("fallback", "online", self.policy.lane_labels["fallback"]),)
        github_client.return_value.list_runners.return_value = runners
        github_client.return_value.workflow_exists.return_value = True
        github_client.return_value.ensure_push_webhook.return_value = "created"
        output = io.StringIO()
        with (
            patch.object(sys, "argv", [
                "cindral",
                "onboard",
                "example-org/example-app",
                "--adapter",
                str(ROOT / "templates/personal-dispatch.yml"),
                "--register-webhook",
            ]),
            patch.dict(os.environ, {"GITHUB_TOKEN": "token", "CINDRAL_WEBHOOK_SECRET": "secret"}),
            patch("sys.stdout", output),
        ):
            main()
        github_client.return_value.workflow_exists.assert_called_once_with("example-org/example-app", "cindral-dispatch.yml")
        github_client.return_value.ensure_push_webhook.assert_called_once_with(
            "example-org/example-app", "https://cindral.example.com/cindral/dispatch", "secret"
        )
        self.assertIn("Webhook: created", output.getvalue())

    @patch("cindral.cli.GitHubClient")
    def test_onboard_refuses_webhook_before_adapter_is_on_default_branch(self, github_client) -> None:
        runners = (RepositoryRunner("fallback", "online", self.policy.lane_labels["fallback"]),)
        github_client.return_value.list_runners.return_value = runners
        github_client.return_value.workflow_exists.return_value = False
        with (
            patch.object(sys, "argv", [
                "cindral",
                "onboard",
                "example-org/example-app",
                "--adapter",
                str(ROOT / "templates/personal-dispatch.yml"),
                "--register-webhook",
            ]),
            patch.dict(os.environ, {"GITHUB_TOKEN": "token", "CINDRAL_WEBHOOK_SECRET": "secret"}),
            patch("sys.stdout", io.StringIO()),
            patch("sys.stderr", io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            main()
        github_client.return_value.ensure_push_webhook.assert_not_called()

    def test_the_policy_flag_wins_over_the_variable_then_the_default(self) -> None:
        with patch.dict(os.environ, {"CINDRAL_POLICY": "/policy/policy.toml"}, clear=True):
            self.assertEqual(
                resolve_policy_path("/etc/cindral/policy.toml"),
                Path("/etc/cindral/policy.toml").resolve(),
            )
            self.assertEqual(resolve_policy_path(), Path("/policy/policy.toml").resolve())
            self.assertEqual(resolve_policy_path("   "), Path("/policy/policy.toml").resolve())
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(resolve_policy_path(), (ROOT / "config/policy.toml").resolve())
        with patch.dict(os.environ, {"CINDRAL_POLICY": "\t"}, clear=True):
            self.assertEqual(resolve_policy_path(), (ROOT / "config/policy.toml").resolve())

    def test_the_policy_report_names_the_file_and_its_device_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.toml"
            path.write_text(_deployment_policy(DEPLOYMENT_TARGETS))
            policy = Policy.load(path)
            with patch.dict(os.environ, {}, clear=True):
                report = describe_policy(None, path, policy)
                chosen = describe_policy(str(path), path, policy)
            with patch.dict(os.environ, {"CINDRAL_POLICY": str(path)}, clear=True):
                named = describe_policy(None, path, policy)
        self.assertIn(f"Policy: {path}", report)
        self.assertIn(f"device targets: {', '.join(DEPLOYMENT_TARGETS)}", report)
        self.assertIn("this is the repository default", report)
        self.assertNotIn("repository default", named)
        self.assertNotIn("repository default", chosen)

    def test_a_policy_without_device_targets_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.toml"
            path.write_text(_deployment_policy(()))
            report = describe_policy(str(path), path, Policy.load(path))
        self.assertIn("device targets: none", report)

    @patch("cindral.cli.GitHubClient")
    def test_onboard_validates_against_the_policy_the_broker_reads(self, github_client) -> None:
        """An adapter written for the deployment's device targets is the correct one."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = root / "policy.toml"
            policy_path.write_text(_deployment_policy(DEPLOYMENT_TARGETS))
            adapter_path = root / "cindral-dispatch.yml"
            adapter_path.write_text(_adapter_for(DEPLOYMENT_TARGETS))
            labels = Policy.load(policy_path).lane_labels["fallback"]
            resolved = policy_path.resolve()
            code, output, _ = self._run_onboard(
                github_client,
                "--adapter",
                str(adapter_path),
                environment={"CINDRAL_POLICY": str(policy_path)},
                runners=(RepositoryRunner("device-alpha", "online", labels),),
            )
        self.assertEqual(code, 0)
        self.assertIn(f"Policy: {resolved}", output)
        self.assertIn(f"device targets: {', '.join(DEPLOYMENT_TARGETS)}", output)
        self.assertIn("Adapter: valid", output)
        self.assertIn("Overall: ready", output)
        self.assertIn("device:device-gamma", output)
        self.assertNotIn("device:desktop", output)

    @patch("cindral.cli.GitHubClient")
    def test_the_shipped_template_onboards_against_a_policy_it_cannot_name(
        self, github_client
    ) -> None:
        """The bug this issue is about, as a test.

        The shipped adapter gated its device job on three hardcoded target names
        while a real policy configures its own, so `onboard` failed closed against
        every real deployment and the template could not be adopted without hand
        editing. The template now names no target and lets the runner label select
        the device, so it onboards whatever the policy happens to configure.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = root / "policy.toml"
            policy_path.write_text(_deployment_policy(DEPLOYMENT_TARGETS))
            labels = Policy.load(policy_path).lane_labels["fallback"]
            code, output, _ = self._run_onboard(
                github_client,
                "--adapter",
                str(ROOT / "templates/personal-dispatch.yml"),
                environment={"CINDRAL_POLICY": str(policy_path)},
                runners=(RepositoryRunner("device-alpha", "online", labels),),
            )
        self.assertEqual(code, 0, output)
        self.assertIn("Adapter: valid", output)
        self.assertIn("Overall: ready", output)

    @patch("cindral.cli.GitHubClient")
    def test_a_policy_mismatch_reports_the_policy_and_its_device_targets(self, github_client) -> None:
        """The complaint has to name the policy, or it reads as a defect in the adapter.

        The adapter names targets the policy does not configure, which is the case
        fail-closed exists for. It used to rely on the shipped template happening to
        hardcode three unrelated names; now that the template defers to the runner
        label, the mismatching adapter is written out so the guarantee is tested
        rather than inherited.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy_path = root / "policy.toml"
            policy_path.write_text(_deployment_policy(DEPLOYMENT_TARGETS))
            adapter_path = root / "cindral-dispatch.yml"
            adapter_path.write_text(_adapter_for(("desktop", "laptop")))
            resolved = policy_path.resolve()
            code, output, _ = self._run_onboard(
                github_client,
                "--adapter",
                str(adapter_path),
                environment={"CINDRAL_POLICY": str(policy_path)},
            )
        self.assertEqual(code, 1)
        self.assertIn(f"Policy: {resolved}", output)
        self.assertIn("device job must allow exactly the configured device targets", output)
        self.assertIn("device:device-delta", output)
        self.assertNotIn("device:desktop", output)
        self.assertNotIn("this is the repository default", output)

    @patch("cindral.cli.GitHubClient")
    def test_an_explicit_policy_overrides_the_broker_policy_variable(self, github_client) -> None:
        code, output, _ = self._run_onboard(
            github_client,
            "--adapter",
            str(ROOT / "templates/personal-dispatch.yml"),
            "--policy",
            str(ROOT / "config/policy.toml"),
            environment={"CINDRAL_POLICY": "/policy/policy.toml"},
            runners=(RepositoryRunner("fallback", "online", self.policy.lane_labels["fallback"]),),
        )
        self.assertEqual(code, 0)
        self.assertIn(f"Policy: {(ROOT / 'config/policy.toml').resolve()}", output)
        self.assertIn("device targets: desktop, laptop, phone", output)
        self.assertIn("Adapter: valid", output)

    @patch("cindral.cli.GitHubClient")
    def test_the_repository_default_policy_says_it_is_the_repository_default(self, github_client) -> None:
        code, output, _ = self._run_onboard(
            github_client,
            "--adapter",
            str(ROOT / "templates/personal-dispatch.yml"),
            environment={},
            runners=(RepositoryRunner("fallback", "online", self.policy.lane_labels["fallback"]),),
        )
        self.assertEqual(code, 0)
        self.assertIn(f"Policy: {(ROOT / 'config/policy.toml').resolve()}", output)
        self.assertIn("this is the repository default", output)

    @patch("cindral.cli.GitHubClient")
    def test_onboard_refuses_a_webhook_secret_that_was_never_read(self, github_client) -> None:
        """`cat` of a file the deployment never created hands onboard an empty secret."""
        github_client.return_value.workflow_exists.return_value = True
        for value in ("", "   ", "\t\n"):
            with self.subTest(value=value):
                code, _, errors = self._run_onboard(
                    github_client,
                    "--adapter",
                    str(ROOT / "templates/personal-dispatch.yml"),
                    "--register-webhook",
                    environment={"CINDRAL_WEBHOOK_SECRET": value},
                    runners=(RepositoryRunner("fallback", "online", self.policy.lane_labels["fallback"]),),
                )
                self.assertEqual(code, 1)
                self.assertIn("CINDRAL_WEBHOOK_SECRET is unset or empty", errors)
                github_client.return_value.ensure_push_webhook.assert_not_called()

    @patch("cindral.cli.GitHubClient")
    def test_onboard_trims_the_webhook_secret_it_registers(self, github_client) -> None:
        github_client.return_value.workflow_exists.return_value = True
        github_client.return_value.ensure_push_webhook.return_value = "created"
        self._run_onboard(
            github_client,
            "--adapter",
            str(ROOT / "templates/personal-dispatch.yml"),
            "--register-webhook",
            environment={"CINDRAL_WEBHOOK_SECRET": "  secret\n"},
            runners=(RepositoryRunner("fallback", "online", self.policy.lane_labels["fallback"]),),
        )
        github_client.return_value.ensure_push_webhook.assert_called_once_with(
            "example-org/example-app", "https://cindral.example.com/cindral/dispatch", "secret"
        )


if __name__ == "__main__":
    unittest.main()
