"""Test bulk webhook reconciliation against the policy's device entries."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cindral import cli
from cindral.github import GitHubAPIError
from cindral.policy import Policy
from cindral.reconcile import device_repositories, reconcile_webhooks


POLICY = """
[[dispatch.repositories]]
mode = "device"
repository = "org/device"

[[dispatch.repositories]]
mode = "both"
repository = "org/both"
workflows = ["ci.yml"]
refs = ["refs/heads/main"]

[[dispatch.repositories]]
mode = "dispatch"
repository = "org/hosted"
workflows = ["ci.yml"]
refs = ["refs/heads/main"]
"""


class ReconcileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "policy.toml"
        self.path.write_text(POLICY)

    def test_only_device_modes_receive_hooks(self) -> None:
        self.assertEqual(device_repositories(Policy.load(self.path)), ("org/device", "org/both"))

    def test_failure_does_not_hide_or_skip_other_repositories(self) -> None:
        def register(repository: str) -> str:
            if repository == "org/device":
                raise GitHubAPIError("permission denied")
            return "created"

        changed, failed = reconcile_webhooks(("org/device", "org/both"), register)
        self.assertEqual(changed, [("org/both", "created")])
        self.assertEqual(failed, [("org/device", "permission denied")])

    def run_cli(self, *args: str, **env: str) -> None:
        with mock.patch.dict(os.environ, env, clear=True), mock.patch(
            "sys.argv", ["cindral", "reconcile-webhooks", "--policy", str(self.path), *args]
        ):
            cli.main()

    def test_dry_run_does_not_need_or_use_credentials(self) -> None:
        with mock.patch.object(cli, "GitHubClient") as github:
            self.run_cli()
        github.assert_not_called()

    def test_apply_needs_both_credentials_before_any_request(self) -> None:
        with mock.patch.object(cli, "GitHubClient") as github, self.assertRaises(SystemExit):
            self.run_cli("--apply", GITHUB_TOKEN="token")
        github.assert_not_called()

    def test_invalid_url_is_refused_before_any_repository_is_updated(self) -> None:
        with mock.patch.object(cli, "GitHubClient") as github, self.assertRaises(SystemExit) as raised:
            self.run_cli("--apply", GITHUB_TOKEN="token", CINDRAL_WEBHOOK_SECRET="secret", CINDRAL_WEBHOOK_URL="http://unsafe.example/hook")
        self.assertEqual(raised.exception.code, 2)
        github.assert_not_called()

    def test_apply_registers_only_device_repositories(self) -> None:
        with mock.patch.object(cli, "GitHubClient") as github:
            github.return_value.ensure_push_webhook.return_value = "updated"
            self.run_cli("--apply", GITHUB_TOKEN="token", CINDRAL_WEBHOOK_SECRET="secret", CINDRAL_WEBHOOK_URL="https://hook.example.com/cindral/dispatch")
        self.assertEqual(
            [call.args[0] for call in github.return_value.ensure_push_webhook.call_args_list],
            ["org/device", "org/both"],
        )

    def test_apply_fails_if_one_repository_cannot_be_updated(self) -> None:
        with mock.patch.object(cli, "GitHubClient") as github:
            github.return_value.ensure_push_webhook.side_effect = [RuntimeError("denied"), "created"]
            with self.assertRaises(SystemExit) as raised:
                self.run_cli("--apply", GITHUB_TOKEN="token", CINDRAL_WEBHOOK_SECRET="secret", CINDRAL_WEBHOOK_URL="https://hook.example.com/cindral/dispatch")
        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(github.return_value.ensure_push_webhook.call_count, 2)
