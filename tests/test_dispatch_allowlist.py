"""What POST /v1/dispatch may reach when the caller holds a valid token.

The allowlist is the limit that still holds when the dispatch token is broader
than the broker needs, so these tests assert both what it permits and that a
refused target never reaches the GitHub API.
"""
import json
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import patch

from cindral.policy import DispatchNotPermitted, Policy
from cindral.service import CindralHandler, CindralServer

REPO = "example-org/example-app"
OTHER = "example-org/other-app"
WORKFLOW = "cindral-dispatch.yml"
TOKEN = "dispatch-token"

POLICY = f"""
[hosted]
runner = "ubuntu-24.04"

[[dispatch.repositories]]
mode = "both"
repository = "{REPO}"
workflows = ["{WORKFLOW}"]
refs = ["refs/heads/main", "refs/heads/feature/*"]
"""

WITHOUT_DISPATCH = """
[hosted]
runner = "ubuntu-24.04"
"""


class PolicyTargetTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "policy.toml"
        self.write(POLICY)
        self.policy = Policy.load(self.path)

    def write(self, text: str) -> None:
        self.path.write_text(text, encoding="utf-8")

    def check(self, **overrides: str) -> None:
        target = {"repository": REPO, "workflow": WORKFLOW, "ref": "main"}
        target.update(overrides)
        self.policy.check_dispatch(**target)

    def load(self, text: str) -> Policy:
        self.write(text)
        return Policy.load(self.path)

    def test_configured_target_is_permitted(self) -> None:
        self.check()
        self.check(ref="refs/heads/main")
        self.check(ref="feature/login")

    def test_repository_outside_the_allowlist_is_refused(self) -> None:
        with self.assertRaisesRegex(DispatchNotPermitted, "repository is not permitted"):
            self.check(repository=OTHER)

    def test_repository_matching_ignores_case(self) -> None:
        # GitHub resolves a repository name case-insensitively, so refusing a
        # differently-cased but valid caller would report a permissions problem
        # where the only difference is casing
        for spelling in (REPO.upper(), REPO.capitalize()):
            self.check(repository=spelling)

    def test_workflow_outside_the_allowlist_is_refused(self) -> None:
        with self.assertRaisesRegex(DispatchNotPermitted, "workflow is not permitted"):
            self.check(workflow="release.yml")

    def test_ref_outside_the_policy_is_refused(self) -> None:
        with self.assertRaisesRegex(DispatchNotPermitted, "ref is not permitted"):
            self.check(ref="refs/heads/attacker")

    def test_tag_ref_is_refused(self) -> None:
        with self.assertRaises(DispatchNotPermitted):
            self.check(ref="refs/tags/v1.0.0")

    def test_pull_request_ref_is_refused(self) -> None:
        with self.assertRaises(DispatchNotPermitted):
            self.check(ref="refs/pull/7/merge")

    def test_commit_sha_is_refused(self) -> None:
        with self.assertRaises(DispatchNotPermitted):
            self.check(ref="a" * 40)

    def test_empty_ref_is_refused(self) -> None:
        with self.assertRaises(DispatchNotPermitted):
            self.check(ref="")

    def test_prefix_pattern_does_not_match_a_sibling_prefix(self) -> None:
        with self.assertRaises(DispatchNotPermitted):
            self.check(ref="refs/heads/featureless")

    def test_prefix_pattern_cannot_be_walked_past(self) -> None:
        with self.assertRaises(DispatchNotPermitted):
            self.check(ref="refs/heads/feature/login/../../main")

    def test_malformed_repository_is_refused(self) -> None:
        with self.assertRaisesRegex(DispatchNotPermitted, "repository is not permitted"):
            self.check(repository="../../etc/passwd")

    def test_refusal_does_not_reveal_the_allowlist(self) -> None:
        with self.assertRaises(DispatchNotPermitted) as caught:
            self.check(repository=OTHER)
        self.assertNotIn(REPO, str(caught.exception))
        self.assertNotIn(WORKFLOW, str(caught.exception))

    def test_policy_without_a_dispatch_section_refuses_every_target(self) -> None:
        policy = self.load(WITHOUT_DISPATCH)
        self.assertEqual(policy.dispatch_targets, ())
        with self.assertRaisesRegex(DispatchNotPermitted, "repository is not permitted"):
            policy.check_dispatch(REPO, WORKFLOW, "main")

    def test_empty_dispatch_section_refuses_every_target(self) -> None:
        policy = self.load('[dispatch]\nrepositories = []\n')
        self.assertEqual(policy.dispatch_targets, ())

    def test_repository_without_a_workflow_list_fails_to_load(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one dispatch workflow"):
            self.load(f'[[dispatch.repositories]]\nmode = "both"\nrepository = "{REPO}"\nrefs = ["refs/heads/main"]\n')

    def test_repository_outside_owner_name_fails_to_load(self) -> None:
        with self.assertRaisesRegex(ValueError, "owner/name"):
            self.load('[[dispatch.repositories]]\nmode = "both"\nrepository = "example-org"\nworkflows = ["a.yml"]\n')

    def test_workflow_path_fails_to_load(self) -> None:
        with self.assertRaisesRegex(ValueError, "workflow file name"):
            self.load(
                f'[[dispatch.repositories]]\nmode = "both"\nrepository = "{REPO}"\nworkflows = ["../a.yml"]\n'
                'refs = ["refs/heads/main"]\n'
            )

    def test_tag_ref_pattern_fails_to_load(self) -> None:
        with self.assertRaisesRegex(ValueError, "branches under refs/heads/"):
            self.load(
                f'[[dispatch.repositories]]\nmode = "both"\nrepository = "{REPO}"\nworkflows = ["{WORKFLOW}"]\n'
                'refs = ["refs/tags/*"]\n'
            )

    def test_mid_ref_wildcard_fails_to_load(self) -> None:
        with self.assertRaisesRegex(ValueError, "one trailing"):
            self.load(
                f'[[dispatch.repositories]]\nmode = "both"\nrepository = "{REPO}"\nworkflows = ["{WORKFLOW}"]\n'
                'refs = ["refs/heads/re*lease"]\n'
            )

    def test_wildcard_without_a_slash_fails_to_load(self) -> None:
        # refs/heads/feature* would match refs/heads/featureXYZ as well, which is
        # not a branch under feature/, so a wildcard only ever follows a slash
        with self.assertRaisesRegex(ValueError, "must follow a slash"):
            self.load(
                f'[[dispatch.repositories]]\nmode = "both"\nrepository = "{REPO}"\nworkflows = ["{WORKFLOW}"]\n'
                'refs = ["refs/heads/feature*"]\n'
            )

    def test_duplicate_repository_fails_to_load(self) -> None:
        entry = f'[[dispatch.repositories]]\nmode = "both"\nrepository = "{REPO}"\nworkflows = ["{WORKFLOW}"]\n'
        with self.assertRaisesRegex(ValueError, "more than once"):
            self.load(entry * 2)

    def test_repository_duplicated_across_casing_fails_to_load(self) -> None:
        # matching is case-insensitive, so two entries differing only in case
        # would leave one of them permanently unmatchable
        lower = f'[[dispatch.repositories]]\nmode = "both"\nrepository = "{REPO}"\nworkflows = ["{WORKFLOW}"]\n'
        upper = lower.replace(REPO, REPO.upper())
        with self.assertRaisesRegex(ValueError, "more than once"):
            self.load(lower + upper)


class RepositoryModeTest(unittest.TestCase):
    """One list naming both paths a repository may be served on.

    The two lists this replaced could disagree, and in production they did. The
    mode is required rather than defaulted so a repository's reach is a line a
    reader can see, and each path still runs its own check: the mode selects
    which check applies, it does not replace one with a weaker one.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "policy.toml"

    def load(self, text: str) -> Policy:
        self.path.write_text(text, encoding="utf-8")
        return Policy.load(self.path)

    def check_dispatch(self, policy: Policy, repository: str) -> None:
        policy.check_dispatch(repository=repository, workflow=WORKFLOW, ref="main")

    def load_mode(self, mode: str) -> Policy:
        return self.load(
            f'[[dispatch.repositories]]\nmode = "{mode}"\nrepository = "{REPO}"\n'
            f'workflows = ["{WORKFLOW}"]\nrefs = ["refs/heads/main"]\n'
        )

    def test_an_owner_default_loads_and_covers_its_repositories(self) -> None:
        # the owner/* form is the only branch of the parser that reaches a
        # regular expression, and an untested branch here is how a missing import
        # ships as a policy that will not load at all
        policy = self.load(
            f'[[dispatch.repositories]]\nmode = "dispatch"\nrepository = "example-org/*"\n'
            f'workflows = ["{WORKFLOW}"]\nrefs = ["refs/heads/main"]\n'
        )
        self.check_dispatch(policy, REPO)
        with self.assertRaises(DispatchNotPermitted):
            policy.check_dispatch(repository="other-org/example-app",
                                  workflow=WORKFLOW, ref="main")

    def test_an_owner_default_without_an_owner_fails_to_load(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be owner/\\*"):
            self.load(
                f'[[dispatch.repositories]]\nmode = "dispatch"\nrepository = "/*"\n'
                f'workflows = ["{WORKFLOW}"]\nrefs = ["refs/heads/main"]\n'
            )

    def test_repository_without_a_mode_fails_to_load(self) -> None:
        # guessing here would grant both paths to a repository whose reach was
        # never decided
        with self.assertRaisesRegex(ValueError, "must set mode"):
            self.load(
                f'[[dispatch.repositories]]\nrepository = "{REPO}"\n'
                f'workflows = ["{WORKFLOW}"]\nrefs = ["refs/heads/main"]\n'
            )

    def test_unknown_mode_fails_to_load(self) -> None:
        with self.assertRaisesRegex(ValueError, "must set mode"):
            self.load_mode("trusted")

    def test_device_mode_needs_no_workflow_list(self) -> None:
        # the repository cannot dispatch, so requiring it to name dispatchable
        # workflows would be a line of configuration that does nothing
        policy = self.load(
            f'[[dispatch.repositories]]\nmode = "device"\nrepository = "{REPO}"\n'
        )
        self.assertEqual(policy.dispatch_targets[0].workflows, ())
        self.assertTrue(policy.permits_device(REPO))

    def test_dispatch_mode_still_needs_a_workflow_list(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least one dispatch workflow"):
            self.load(
                f'[[dispatch.repositories]]\nmode = "dispatch"\nrepository = "{REPO}"\n'
            )

    def test_dispatch_mode_refuses_the_device_path(self) -> None:
        policy = self.load_mode("dispatch")
        self.assertFalse(policy.permits_device(REPO))
        policy.check_dispatch(REPO, WORKFLOW, "main")

    def test_device_mode_refuses_the_dispatch_path(self) -> None:
        policy = self.load_mode("device")
        self.assertTrue(policy.permits_device(REPO))
        with self.assertRaisesRegex(DispatchNotPermitted, "repository is not permitted"):
            policy.check_dispatch(REPO, WORKFLOW, "main")

    def test_both_mode_permits_both_paths(self) -> None:
        policy = self.load_mode("both")
        self.assertTrue(policy.permits_device(REPO))
        policy.check_dispatch(REPO, WORKFLOW, "main")

    def test_device_path_ignores_case(self) -> None:
        # a delivery refused for its casing reads as a permissions problem, the
        # same reason check_dispatch matches the way it does
        policy = self.load_mode("device")
        for spelling in (REPO.upper(), REPO.capitalize()):
            self.assertTrue(policy.permits_device(spelling))

    def test_repository_outside_the_list_is_served_on_neither_path(self) -> None:
        policy = self.load_mode("both")
        self.assertFalse(policy.permits_device(OTHER))
        with self.assertRaises(DispatchNotPermitted):
            policy.check_dispatch(OTHER, WORKFLOW, "main")

    def test_policy_without_a_dispatch_section_serves_no_device(self) -> None:
        policy = self.load('[hosted]\nrunner = "ubuntu-24.04"\n')
        self.assertEqual(policy.dispatch_targets, ())
        self.assertFalse(policy.permits_device(REPO))

    def test_a_dispatch_only_list_never_enables_the_device_path(self) -> None:
        # the half-state the two lists allowed: a repository reachable by
        # dispatch while the device path quietly never sees it
        policy = self.load_mode("dispatch")
        self.assertFalse(policy.permits_device(REPO))


class DispatchEndpointTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "policy.toml"
        self.server = CindralServer(("127.0.0.1", 0), CindralHandler)
        self.server.policy = Policy.load(self.write(POLICY))
        self.server.runners = ()
        self.server.github = None
        self.server.webhook_secret = "webhook-secret"
        self.server.dispatch_token = TOKEN
        self.server.workflow_file = WORKFLOW
        self.server.capacity_lock = threading.Lock()
        self.server.runner_reservations = {}
        self.server.reservation_seconds = 10
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def write(self, text: str) -> Path:
        self.path.write_text(text, encoding="utf-8")
        return self.path

    def post(self, payload: dict) -> tuple[int, dict]:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(
            "POST",
            "/v1/dispatch",
            body=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {TOKEN}",
            },
        )
        response = connection.getresponse()
        result = (response.status, json.loads(response.read() or b"{}"))
        connection.close()
        return result

    def dispatch(self, **overrides: str) -> tuple[int, dict, object]:
        payload = {
            "repository": REPO,
            "workflow": WORKFLOW,
            "ref": "main",
            "quota_status": "available",
        }
        payload.update(overrides)
        with patch.object(self.server, "github") as github:
            status, body = self.post(payload)
        return status, body, github

    def assert_no_github_call(self, github: object) -> None:
        github.dispatch.assert_not_called()
        github.list_runners.assert_not_called()
        github.workflow_exists.assert_not_called()

    def test_a_pull_request_ref_in_the_inputs_is_refused(self) -> None:
        """The checkout ref is a server decision, not a caller input.

        `_dispatch` overwrites the lane, the target and the reason with values it
        computed, and passed the ref through untouched. The adapter checks that ref
        out, so a request naming `refs/pull/42/merge` alongside a self-hosted lane
        ran a fork's merge commit on local hardware. No `PullRequestEvent` is
        built on this path, so the webhook route's trust decision said nothing
        about it.

        It needs the dispatch token, so it is not the unauthenticated path. It is
        still the difference between a broker credential and a way to run
        unreviewed code on a device.
        """
        status, body, github = self.dispatch(
            inputs={"cindral_lane": "fallback", "cindral_ref": "refs/pull/42/merge"}
        )
        self.assertEqual(status, 403, body)
        self.assertIn("ref is not permitted", body["error"])
        self.assert_no_github_call(github)

    def test_a_tag_or_sha_ref_in_the_inputs_is_refused(self) -> None:
        # same dimension as a pull request: a ref the policy does not name, and
        # a tag is how a release is run on unreviewed input
        for named in ("refs/tags/v1.0.0", "a" * 40, "refs/heads/attacker"):
            with self.subTest(ref=named):
                status, body, github = self.dispatch(inputs={"cindral_ref": named})
                self.assertEqual(status, 403, body)
                self.assert_no_github_call(github)

    def test_a_named_branch_ref_in_the_inputs_is_dispatched(self) -> None:
        """A branch the policy names is permitted, and it is dispatched canonical.

        The ref is written back in the form the policy read, so the string that
        was checked is the string the adapter checks out. Forwarding the caller's
        spelling would leave the validator and the consumer disagreeing about
        what a ref means, which is the gap this closes rather than a detail.
        """
        status, body, github = self.dispatch(inputs={"cindral_ref": "feature/login"})
        self.assertEqual(status, 200, body)
        self.assertEqual(
            github.dispatch.call_args[0][3]["cindral_ref"], "refs/heads/feature/login"
        )

    def test_an_absent_or_blank_ref_input_is_left_to_the_adapter(self) -> None:
        # the adapter already falls back to the dispatch ref, and a blank string
        # must not become a ref check that refuses a legitimate dispatch
        for inputs in ({}, {"cindral_ref": ""}, {"cindral_ref": "   "}):
            with self.subTest(inputs=inputs):
                status, body, _ = self.dispatch(inputs=inputs)
                self.assertEqual(status, 200, body)

    def test_allowed_target_reaches_github(self) -> None:
        status, body, github = self.dispatch()
        self.assertEqual(status, 200)
        self.assertTrue(body["dispatched"])
        self.assertEqual(github.dispatch.call_args[0][:3], (REPO, WORKFLOW, "main"))

    def test_repository_outside_the_allowlist_makes_no_github_call(self) -> None:
        status, body, github = self.dispatch(repository=OTHER)
        self.assertEqual(status, 403)
        self.assertIn("repository is not permitted", body["error"])
        self.assert_no_github_call(github)

    def test_workflow_outside_the_allowlist_makes_no_github_call(self) -> None:
        status, body, github = self.dispatch(workflow="release.yml")
        self.assertEqual(status, 403)
        self.assertIn("workflow is not permitted", body["error"])
        self.assert_no_github_call(github)

    def test_ref_outside_the_policy_makes_no_github_call(self) -> None:
        status, body, github = self.dispatch(ref="refs/heads/attacker")
        self.assertEqual(status, 403)
        self.assertIn("ref is not permitted", body["error"])
        self.assert_no_github_call(github)

    def test_unconfigured_allowlist_refuses_every_dispatch(self) -> None:
        self.server.policy = Policy.load(self.write(WITHOUT_DISPATCH))
        status, body, github = self.dispatch()
        self.assertEqual(status, 403)
        self.assertIn("repository is not permitted", body["error"])
        self.assert_no_github_call(github)

    def test_refusal_does_not_reveal_the_allowlist(self) -> None:
        _, body, _ = self.dispatch(repository=OTHER)
        self.assertNotIn(REPO, json.dumps(body))
        self.assertNotIn(WORKFLOW, json.dumps(body))

    def test_malformed_repository_is_refused_without_a_github_call(self) -> None:
        status, body, github = self.dispatch(repository="../other")
        self.assertEqual(status, 403)
        self.assertIn("repository is not permitted", body["error"])
        self.assert_no_github_call(github)

    def test_unauthorized_caller_is_refused_before_the_allowlist(self) -> None:
        connection = HTTPConnection("127.0.0.1", self.port, timeout=10)
        connection.request(
            "POST",
            "/v1/dispatch",
            body=json.dumps({"repository": REPO, "workflow": WORKFLOW, "ref": "main"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 401)


if __name__ == "__main__":
    unittest.main()
