"""Tests for account-wide adoption: discovery, selection, and the adopt command."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock
from urllib.error import HTTPError

from cindral import cli
from cindral.github import DiscoveredRepository, GitHubAPIError, GitHubClient
from cindral.reconcile import (
    DEFAULT_OPS_REPOSITORY,
    definition_note,
    select_adoption,
    slug_set,
    trusted_default_branch,
)


class Response:
    def __init__(self, body=b""):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


def repository(
    name,
    *,
    private=True,
    archived=False,
    default_branch="main",
    has_contract=False,
    has_workflows=False,
):
    return DiscoveredRepository(name, private, archived, default_branch, has_contract, has_workflows)


def inventory(*items):
    return json.dumps(list(items)).encode()


def entry(name, **overrides):
    item = {"full_name": name, "private": True, "archived": False, "default_branch": "main"}
    item.update(overrides)
    return item


def full_page(prefix):
    return [entry(f"{prefix}/{index}") for index in range(100)]


def contract_probe():
    return Response(b'{"name": "ci.toml", "type": "file"}')


def workflows_probe():
    return Response(json.dumps([{"name": "ci.yml"}]).encode())


NOT_FOUND = HTTPError("url", 404, "not found", {}, None)


DISCOVERED = (
    repository("example-org/app", has_contract=True),
    repository("example-org/lib", has_workflows=True),
    repository("example-org/public-docs", private=False),
    repository("example-org/old", archived=True),
    repository("example-org/empty", default_branch=""),
    repository("example-org/operations"),
    repository("example-org/quiet"),
)

HOOK_URL = "https://hook.example.com/cindral/dispatch"


class ListRepositoriesTest(unittest.TestCase):
    @mock.patch("cindral.github.urlopen")
    def test_lists_the_owned_inventory_and_probes_each_repository(self, urlopen) -> None:
        urlopen.side_effect = [
            Response(inventory(entry("example-org/app"), entry("example-org/lib"))),
            contract_probe(),
            workflows_probe(),
            NOT_FOUND,
            NOT_FOUND,
        ]
        discovered = GitHubClient("token").list_repositories()
        listing, app_contract, app_workflows, lib_contract, lib_workflows = [
            call.args[0].full_url for call in urlopen.call_args_list
        ]
        self.assertEqual(
            listing,
            "https://api.github.com/user/repos?affiliation=owner&sort=full_name&per_page=100&page=1",
        )
        self.assertEqual(app_contract, "https://api.github.com/repos/example-org/app/contents/.cindral/ci.toml")
        self.assertEqual(app_workflows, "https://api.github.com/repos/example-org/app/contents/.github/workflows")
        self.assertEqual(lib_contract, "https://api.github.com/repos/example-org/lib/contents/.cindral/ci.toml")
        self.assertEqual(lib_workflows, "https://api.github.com/repos/example-org/lib/contents/.github/workflows")
        self.assertEqual(
            discovered,
            (
                repository("example-org/app", has_contract=True, has_workflows=True),
                repository("example-org/lib"),
            ),
        )
        for call in urlopen.call_args_list:
            self.assertIn("Bearer token", call.args[0].headers["Authorization"])
            self.assertEqual(call.args[0].get_method(), "GET")

    def test_a_collaborator_or_membership_relationship_is_never_consented_to(self) -> None:
        """The listing must ask for owned repositories only, so a repository the
        credential can reach as a collaborator or organization member never
        enters the inventory adoption reads."""
        with mock.patch("cindral.github.urlopen") as urlopen:
            urlopen.side_effect = [
                Response(inventory(entry("example-org/app"))),
                contract_probe(),
                workflows_probe(),
            ]
            GitHubClient("token").list_repositories()
        listing = urlopen.call_args_list[0].args[0].full_url
        self.assertIn("affiliation=owner", listing)
        self.assertNotIn("collaborator", listing)
        self.assertNotIn("organization_member", listing)

    @mock.patch("cindral.github.urlopen")
    def test_inventory_flags_map_onto_the_discovery_record(self, urlopen) -> None:
        urlopen.side_effect = [
            Response(inventory(entry("example-org/old", archived=True, private=False, default_branch="trunk"))),
            NOT_FOUND,
            NOT_FOUND,
        ]
        discovered = GitHubClient("token").list_repositories()
        self.assertEqual(
            discovered,
            (repository("example-org/old", private=False, archived=True, default_branch="trunk"),),
        )

    @mock.patch("cindral.github.urlopen")
    def test_pages_until_a_partial_page(self, urlopen) -> None:
        probes = [contract_probe(), workflows_probe()] * 100
        urlopen.side_effect = [
            Response(inventory(*full_page("example-org"))),
            *probes,
            Response(inventory(entry("example-org/last"))),
            contract_probe(),
            workflows_probe(),
        ]
        discovered = GitHubClient("token").list_repositories()
        self.assertEqual(len(discovered), 101)
        self.assertEqual(discovered[-1], repository("example-org/last", has_contract=True, has_workflows=True))
        self.assertIn("page=2", urlopen.call_args_list[201].args[0].full_url)

    @mock.patch("cindral.github.urlopen")
    def test_a_cap_reached_with_more_pages_coming_is_refused(self, urlopen) -> None:
        probes = [contract_probe(), workflows_probe()] * 100
        urlopen.side_effect = [
            Response(inventory(*full_page("example-org"))),
            *probes,
            Response(inventory(entry("example-org/more"))),
        ]
        with self.assertRaises(GitHubAPIError) as raised:
            GitHubClient("token").list_repositories(max_pages=1)
        self.assertIn("more than 100 repositories", str(raised.exception))

    @mock.patch("cindral.github.urlopen")
    def test_a_cap_reached_with_an_empty_following_page_is_accepted(self, urlopen) -> None:
        probes = [contract_probe(), workflows_probe()] * 100
        urlopen.side_effect = [Response(inventory(*full_page("example-org"))), *probes, Response(b"[]")]
        discovered = GitHubClient("token").list_repositories(max_pages=1)
        self.assertEqual(len(discovered), 100)

    @mock.patch("cindral.github.urlopen")
    def test_a_probe_that_cannot_be_answered_stops_the_run(self, urlopen) -> None:
        urlopen.side_effect = [
            Response(inventory(entry("example-org/app"))),
            contract_probe(),
            HTTPError("url", 403, "forbidden", {}, None),
        ]
        with self.assertRaises(GitHubAPIError):
            GitHubClient("token").list_repositories()

    @mock.patch("cindral.github.urlopen")
    def test_an_empty_repository_is_one_ineligible_repository_not_a_broken_inventory(self, urlopen) -> None:
        # one empty repository used to abort the whole account, so on a real one
        # the command never ran at all; the caller skips it with a reason
        for payload in (
            inventory(entry("example-org/app", default_branch=None)),
            inventory({k: v for k, v in entry("example-org/app").items() if k != "default_branch"}),
        ):
            with self.subTest(payload=payload):
                urlopen.side_effect = [Response(payload), contract_probe(), workflows_probe()]
                discovered = GitHubClient("token").list_repositories()
                self.assertEqual(len(discovered), 1)
                self.assertEqual(discovered[0].repository, "example-org/app")
                self.assertEqual(discovered[0].default_branch, "")

    @mock.patch("cindral.github.urlopen")
    def test_a_named_repository_is_adopted_despite_an_empty_repository_elsewhere(self, urlopen) -> None:
        urlopen.side_effect = [
            Response(inventory(entry("example-org/empty", default_branch=None), entry("example-org/app"))),
            *[probe for _ in range(2) for probe in (contract_probe(), workflows_probe())],
        ]
        discovered = GitHubClient("token").list_repositories()
        eligible, _skips, unselected = select_adoption(discovered, frozenset({"example-org/app"}), False, frozenset(), DEFAULT_OPS_REPOSITORY)
        self.assertEqual([item.repository for item in eligible], ["example-org/app"])
        self.assertEqual(unselected, 1)

    @mock.patch("cindral.github.urlopen")
    def test_a_pass_that_does_not_probe_makes_one_request(self, urlopen) -> None:
        # the call-site kwarg only pins run_adoption; without this, deleting the
        # guards inside the client passes every test and an applying pass goes
        # back to two requests a repository, and to aborting on one it cannot
        # answer
        urlopen.side_effect = [Response(inventory(entry("example-org/a"), entry("example-org/b")))]
        discovered = GitHubClient("token").list_repositories(probe=False)
        self.assertEqual(urlopen.call_count, 1)
        self.assertFalse(any(item.has_contract or item.has_workflows for item in discovered))

    @mock.patch("cindral.github.urlopen")
    def test_a_malformed_inventory_is_refused(self, urlopen) -> None:
        for payload in (
            b'[{"private": true}]',
            b'[{"full_name": "not a slug", "private": true, "archived": false, "default_branch": "main"}]',
            b'[{"full_name": "example-org/app", "private": "yes", "archived": false, "default_branch": "main"}]',
            b'[{"full_name": "example-org/app", "private": true, "archived": false, "default_branch": 42}]',
            b'[{"full_name": "example-org/app"}]',
            b'"repos"',
        ):
            with self.subTest(payload=payload):
                urlopen.side_effect = None
                urlopen.return_value = Response(payload)
                with self.assertRaises(GitHubAPIError):
                    GitHubClient("token").list_repositories()

    def test_the_page_cap_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            GitHubClient("token").list_repositories(max_pages=0)


class SelectionTest(unittest.TestCase):
    def test_slug_set_parses_slugs_case_insensitively(self) -> None:
        self.assertEqual(slug_set("EXAMPLE-ORG/app, example-org/lib ,, "), frozenset({"example-org/app", "example-org/lib"}))
        self.assertEqual(slug_set(["Example-Org/App"]), frozenset({"example-org/app"}))
        self.assertEqual(slug_set(""), frozenset())

    def test_a_default_branch_must_be_a_plain_trusted_branch_name(self) -> None:
        self.assertTrue(trusted_default_branch("main"))
        self.assertFalse(trusted_default_branch(""))
        self.assertFalse(trusted_default_branch("feature..x"))
        self.assertFalse(trusted_default_branch("has space"))

    def test_without_a_selection_nothing_is_adopted(self) -> None:
        """No allowlist and no opt-in means the pass adopts nothing at all."""
        eligible, skips, unselected = select_adoption(DISCOVERED)
        self.assertEqual((eligible, skips), ((), ()))
        self.assertEqual(unselected, len(DISCOVERED))

    def test_an_allowlist_adopts_only_named_repositories(self) -> None:
        eligible, skips, unselected = select_adoption(DISCOVERED, frozenset({"example-org/app", "example-org/quiet"}))
        self.assertEqual([item.repository for item in eligible], ["example-org/app", "example-org/quiet"])
        self.assertEqual(skips, ())
        self.assertEqual(unselected, len(DISCOVERED) - 2)

    def test_a_named_repository_the_credential_does_not_own_is_reported(self) -> None:
        eligible, skips, unselected = select_adoption(DISCOVERED, frozenset({"another-owner/private-repo"}))
        self.assertEqual(eligible, ())
        self.assertEqual(skips, (("another-owner/private-repo", "credential does not own this repository"),))
        self.assertEqual(unselected, len(DISCOVERED))

    def test_a_named_repository_failing_eligibility_is_reported(self) -> None:
        eligible, skips, _ = select_adoption(DISCOVERED, frozenset({"example-org/old", "example-org/empty"}))
        self.assertEqual(eligible, ())
        self.assertEqual(
            skips,
            (
                ("example-org/empty", "repository has no default branch"),
                ("example-org/old", "archived repositories accept no pushes"),
            ),
        )

    def test_an_exclusion_refuses_even_a_named_repository(self) -> None:
        eligible, skips, _ = select_adoption(
            DISCOVERED, frozenset({"example-org/app"}), exclusions=frozenset({"example-org/app"})
        )
        self.assertEqual(eligible, ())
        self.assertEqual(skips, (("example-org/app", "excluded by CINDRAL_ADOPT_EXCLUDE"),))

    def test_all_owned_adopts_every_eligible_owned_repository(self) -> None:
        eligible, skips, unselected = select_adoption(DISCOVERED, all_owned=True, ops_repository="example-org/operations")
        self.assertEqual([item.repository for item in eligible], ["example-org/app", "example-org/lib", "example-org/quiet"])
        self.assertEqual(
            skips,
            (
                ("example-org/empty", "repository has no default branch"),
                ("example-org/old", "archived repositories accept no pushes"),
                ("example-org/operations", "repository owns the deployment policy"),
                ("example-org/public-docs", "public repositories route to hosted runners"),
            ),
        )
        self.assertEqual(unselected, 0)

    def test_the_ops_repository_protection_follows_its_configuration(self) -> None:
        eligible, skips, _ = select_adoption(
            (repository("example-org/operations"), repository("me/depot")), all_owned=True, ops_repository="me/depot"
        )
        self.assertEqual([item.repository for item in eligible], ["example-org/operations"])
        self.assertEqual(skips, (("me/depot", "repository owns the deployment policy"),))

    def test_the_deployment_policy_repository_has_no_account_specific_default(self) -> None:
        self.assertIsNone(DEFAULT_OPS_REPOSITORY)

    def test_the_report_describes_what_the_default_branch_offers(self) -> None:
        self.assertEqual(
            definition_note(repository("r", has_contract=True, has_workflows=True)),
            "committed contract and workflow files",
        )
        self.assertEqual(definition_note(repository("r", has_contract=True)), "committed contract")
        self.assertEqual(definition_note(repository("r", has_workflows=True)), "workflow files")
        self.assertEqual(
            definition_note(repository("r")),
            "no committed CI definition; the device infers one from the tree",
        )


class AdoptCommandTest(unittest.TestCase):
    def run_cli(self, *args, ensure_side_effects=None, discovery=None, **env):
        env.setdefault("CINDRAL_OPS_REPOSITORY", "example-org/operations")
        output = io.StringIO()
        errors = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), mock.patch.dict(
            os.environ, env, clear=True
        ), mock.patch("sys.argv", ["cindral", "adopt", *args]), mock.patch.object(cli, "GitHubClient") as github:
            github.return_value.list_repositories.return_value = DISCOVERED
            if discovery is not None:
                github.return_value.list_repositories.side_effect = discovery
            if ensure_side_effects is not None:
                github.return_value.ensure_push_webhook.side_effect = ensure_side_effects
            raised = None
            try:
                cli.main()
            except SystemExit as exc:
                raised = exc
        return github, output.getvalue(), errors.getvalue(), raised

    def test_adoption_requires_the_deployment_policy_repository(self) -> None:
        self.assertEqual(self.run_cli(GITHUB_TOKEN="token", CINDRAL_OPS_REPOSITORY="")[3].code, 2)

    def test_a_dry_run_without_a_selection_prints_the_landscape_and_adopts_nothing(self) -> None:
        github, output, _, raised = self.run_cli(GITHUB_TOKEN="token")
        self.assertIsNone(raised)
        github.return_value.ensure_push_webhook.assert_not_called()
        self.assertIn("Credential owns 7 repositories: 3 adoptable, 4 outside adoption", output)
        self.assertIn(
            "Nothing is selected for adoption; pass --repository OWNER/NAME for each repository, "
            "set CINDRAL_ADOPT_REPOSITORIES, or pass --all-owned",
            output,
        )
        self.assertIn("example-org/app: not selected (committed contract)", output)
        self.assertIn(
            "example-org/quiet: not selected (no committed CI definition; the device infers one from the tree)",
            output,
        )
        self.assertIn("example-org/operations: skipped: repository owns the deployment policy", output)
        self.assertIn("example-org/public-docs: skipped: public repositories route to hosted runners", output)

    def test_apply_without_a_selection_is_refused_before_any_github_request(self) -> None:
        github, _, _, raised = self.run_cli(
            "--apply", GITHUB_TOKEN="token", CINDRAL_WEBHOOK_SECRET="secret", CINDRAL_WEBHOOK_URL=HOOK_URL
        )
        self.assertEqual(raised.code, 2)
        github.return_value.list_repositories.assert_not_called()
        github.return_value.ensure_push_webhook.assert_not_called()

    def test_a_dry_run_with_an_allowlist_reports_exactly_what_would_change(self) -> None:
        github, output, _, raised = self.run_cli(
            "--repository", "example-org/app", "--repository", "example-org/lib", "--repository", "example-org/quiet",
            GITHUB_TOKEN="token",
        )
        self.assertIsNone(raised)
        github.return_value.ensure_push_webhook.assert_not_called()
        self.assertIn("Selection: 3 named with --repository", output)
        self.assertIn(f"example-org/app: would reconcile (set CINDRAL_WEBHOOK_URL) (committed contract)", output)
        self.assertIn(f"example-org/lib: would reconcile (set CINDRAL_WEBHOOK_URL) (workflow files)", output)
        self.assertIn("4 owned repositories outside the selection were not touched", output)

    def test_apply_with_an_allowlist_registers_only_the_named_repositories(self) -> None:
        github, output, _, raised = self.run_cli(
            "--repository", "example-org/app", "--repository", "example-org/quiet", "--apply",
            GITHUB_TOKEN="token",
            CINDRAL_WEBHOOK_SECRET="secret",
            CINDRAL_WEBHOOK_URL=HOOK_URL,
            ensure_side_effects=["created", "created"],
        )
        self.assertIsNone(raised)
        calls = github.return_value.ensure_push_webhook.call_args_list
        self.assertEqual([call.args[0] for call in calls], ["example-org/app", "example-org/quiet"])
        self.assertTrue(all(call.args[1] == HOOK_URL and call.args[2] == "secret" for call in calls))
        self.assertIn("example-org/app: created webhook", output)
        self.assertIn("5 owned repositories outside the selection were not touched", output)

    def test_an_ambient_allowlist_cannot_widen_a_typed_selection(self) -> None:
        # unioning them let a variable set in a service unit adopt repositories
        # the operator never typed, which is why they are separate opt-ins
        github, _, errors, raised = self.run_cli(
            "--repository", "example-org/app", "--apply",
            GITHUB_TOKEN="token",
            CINDRAL_WEBHOOK_SECRET="secret",
            CINDRAL_WEBHOOK_URL=HOOK_URL,
            CINDRAL_ADOPT_REPOSITORIES="example-org/secret-scratch",
        )
        self.assertEqual(raised.code, 2)
        self.assertIn("separate opt-ins", errors)
        # refused before any request, so the ambient value never reaches GitHub
        github.return_value.list_repositories.assert_not_called()
        github.return_value.ensure_push_webhook.assert_not_called()

    def test_an_applying_pass_does_not_probe_the_tree(self) -> None:
        # two requests a repository whose results only feed the definition note
        # that no applying pass prints, and a probe that cannot be answered
        # stops the run, so --apply must not make them
        github, _, _, raised = self.run_cli(
            "--apply", "--repository", "example-org/app",
            GITHUB_TOKEN="token",
            CINDRAL_WEBHOOK_SECRET="secret",
            CINDRAL_WEBHOOK_URL=HOOK_URL,
        )
        self.assertIsNone(raised)
        self.assertIs(github.return_value.list_repositories.call_args.kwargs.get("probe"), False)

    def test_a_dry_run_still_probes_and_prints_the_definition_note(self) -> None:
        github, output, _, raised = self.run_cli("--repository", "example-org/app", GITHUB_TOKEN="token")
        self.assertIsNone(raised)
        self.assertTrue(github.return_value.list_repositories.call_args.kwargs.get("probe", True))
        self.assertIn("would reconcile", output)
        self.assertIn("committed contract", output)

    def test_the_selection_line_names_the_opt_in_that_ran(self) -> None:
        # this exists because the ambient variable and the typed flag are not
        # interchangeable, so the report must say which one ran
        typed = self.run_cli("--repository", "example-org/app", GITHUB_TOKEN="token")[1]
        ambient = self.run_cli(GITHUB_TOKEN="token", CINDRAL_ADOPT_REPOSITORIES="example-org/app")[1]
        self.assertIn("named with --repository", typed)
        self.assertIn("named with CINDRAL_ADOPT_REPOSITORIES", ambient)

    def test_all_owned_refuses_the_ambient_opt_in(self) -> None:
        # the combination most likely to arrive from a service unit
        github, _, errors, raised = self.run_cli(
            "--all-owned", "--apply",
            GITHUB_TOKEN="token",
            CINDRAL_WEBHOOK_SECRET="secret",
            CINDRAL_WEBHOOK_URL=HOOK_URL,
            CINDRAL_ADOPT_REPOSITORIES="example-org/app",
        )
        self.assertEqual(raised.code, 2)
        self.assertIn("separate opt-ins", errors)
        github.return_value.ensure_push_webhook.assert_not_called()

    def test_an_environment_allowlist_counts_as_a_selection(self) -> None:
        github, _, _, raised = self.run_cli(
            "--apply",
            GITHUB_TOKEN="token",
            CINDRAL_WEBHOOK_SECRET="secret",
            CINDRAL_WEBHOOK_URL=HOOK_URL,
            CINDRAL_ADOPT_REPOSITORIES="example-org/app, example-org/lib",
            ensure_side_effects=["created", "created"],
        )
        self.assertIsNone(raised)
        self.assertEqual(
            [call.args[0] for call in github.return_value.ensure_push_webhook.call_args_list],
            ["example-org/app", "example-org/lib"],
        )

    def test_a_named_repository_outside_the_owned_inventory_fails_the_apply(self) -> None:
        github, output, _, raised = self.run_cli(
            "--repository", "another-owner/private-repo", "--repository", "example-org/app", "--apply",
            GITHUB_TOKEN="token",
            CINDRAL_WEBHOOK_SECRET="secret",
            CINDRAL_WEBHOOK_URL=HOOK_URL,
            ensure_side_effects=["created"],
        )
        self.assertEqual(raised.code, 1)
        self.assertEqual(
            [call.args[0] for call in github.return_value.ensure_push_webhook.call_args_list],
            ["example-org/app"],
        )
        self.assertIn("another-owner/private-repo: skipped: credential does not own this repository", output)

    def test_a_named_ineligible_repository_fails_the_apply(self) -> None:
        github, output, _, raised = self.run_cli(
            "--repository", "example-org/old", "--repository", "example-org/app", "--apply",
            GITHUB_TOKEN="token",
            CINDRAL_WEBHOOK_SECRET="secret",
            CINDRAL_WEBHOOK_URL=HOOK_URL,
            ensure_side_effects=["created"],
        )
        self.assertEqual(raised.code, 1)
        self.assertEqual(
            [call.args[0] for call in github.return_value.ensure_push_webhook.call_args_list],
            ["example-org/app"],
        )
        self.assertIn("example-org/old: skipped: archived repositories accept no pushes", output)

    def test_the_all_owned_opt_in_adopts_every_eligible_owned_repository(self) -> None:
        github, output, _, raised = self.run_cli(
            "--all-owned", "--apply",
            GITHUB_TOKEN="token",
            CINDRAL_WEBHOOK_SECRET="secret",
            CINDRAL_WEBHOOK_URL=HOOK_URL,
            ensure_side_effects=["created", "created", "created"],
        )
        self.assertIsNone(raised)
        self.assertEqual(
            [call.args[0] for call in github.return_value.ensure_push_webhook.call_args_list],
            ["example-org/app", "example-org/lib", "example-org/quiet"],
        )
        self.assertIn("Selection: every owned private repository, opted in with --all-owned", output)
        self.assertIn("example-org/operations: skipped: repository owns the deployment policy", output)

    def test_all_owned_and_an_allowlist_are_refused_together(self) -> None:
        github, _, _, raised = self.run_cli(
            "--all-owned", "--repository", "example-org/app", "--apply",
            GITHUB_TOKEN="token",
            CINDRAL_WEBHOOK_SECRET="secret",
            CINDRAL_WEBHOOK_URL=HOOK_URL,
        )
        self.assertEqual(raised.code, 2)
        github.return_value.list_repositories.assert_not_called()

    def test_an_exclusion_removes_one_repository_from_the_all_owned_opt_in(self) -> None:
        github, output, _, raised = self.run_cli(
            "--all-owned", "--apply",
            GITHUB_TOKEN="token",
            CINDRAL_WEBHOOK_SECRET="secret",
            CINDRAL_WEBHOOK_URL=HOOK_URL,
            CINDRAL_ADOPT_EXCLUDE="EXAMPLE-ORG/quiet",
            ensure_side_effects=["created", "created"],
        )
        self.assertIsNone(raised)
        self.assertEqual(
            [call.args[0] for call in github.return_value.ensure_push_webhook.call_args_list],
            ["example-org/app", "example-org/lib"],
        )
        self.assertIn("example-org/quiet: skipped: excluded by CINDRAL_ADOPT_EXCLUDE", output)

    def test_an_exclusion_beats_an_explicit_name_and_fails_the_apply(self) -> None:
        github, output, _, raised = self.run_cli(
            "--repository", "example-org/app", "--apply",
            GITHUB_TOKEN="token",
            CINDRAL_WEBHOOK_SECRET="secret",
            CINDRAL_WEBHOOK_URL=HOOK_URL,
            CINDRAL_ADOPT_EXCLUDE="example-org/app",
        )
        self.assertEqual(raised.code, 1)
        github.return_value.ensure_push_webhook.assert_not_called()
        self.assertIn("example-org/app: skipped: excluded by CINDRAL_ADOPT_EXCLUDE", output)

    def test_the_ops_repository_protection_follows_its_configuration(self) -> None:
        github, output, _, raised = self.run_cli(
            "--all-owned", "--apply",
            GITHUB_TOKEN="token",
            CINDRAL_WEBHOOK_SECRET="secret",
            CINDRAL_WEBHOOK_URL=HOOK_URL,
            CINDRAL_OPS_REPOSITORY="example-org/lib",
            ensure_side_effects=["created", "created", "created"],
        )
        self.assertIsNone(raised)
        self.assertIn("example-org/lib: skipped: repository owns the deployment policy", output)
        self.assertIn("example-org/operations: created webhook", output)

    def test_rerunning_apply_updates_in_place_without_failing(self) -> None:
        self.run_cli(
            "--repository", "example-org/app", "--apply",
            GITHUB_TOKEN="token",
            CINDRAL_WEBHOOK_SECRET="secret",
            CINDRAL_WEBHOOK_URL=HOOK_URL,
            ensure_side_effects=["created"],
        )
        github, output, _, raised = self.run_cli(
            "--repository", "example-org/app", "--apply",
            GITHUB_TOKEN="token",
            CINDRAL_WEBHOOK_SECRET="secret",
            CINDRAL_WEBHOOK_URL=HOOK_URL,
            ensure_side_effects=["updated"],
        )
        self.assertIsNone(raised)
        self.assertEqual(len(github.return_value.ensure_push_webhook.call_args_list), 1)
        self.assertIn("example-org/app: updated webhook", output)

    def test_one_failed_repository_does_not_block_the_rest(self) -> None:
        github, output, errors, raised = self.run_cli(
            "--repository", "example-org/app", "--repository", "example-org/lib", "--apply",
            GITHUB_TOKEN="token",
            CINDRAL_WEBHOOK_SECRET="secret",
            CINDRAL_WEBHOOK_URL=HOOK_URL,
            ensure_side_effects=[GitHubAPIError("permission denied"), "created"],
        )
        self.assertEqual(raised.code, 1)
        self.assertEqual(github.return_value.ensure_push_webhook.call_count, 2)
        self.assertIn("example-org/lib: created webhook", output)
        self.assertIn("example-org/app: permission denied", errors)

    def test_apply_requires_the_url_and_the_secret_before_any_github_request(self) -> None:
        github, _, _, raised = self.run_cli("--repository", "example-org/app", "--apply", GITHUB_TOKEN="token")
        self.assertEqual(raised.code, 2)
        github.return_value.list_repositories.assert_not_called()

        github, _, _, raised = self.run_cli(
            "--repository", "example-org/app", "--apply", GITHUB_TOKEN="token", CINDRAL_WEBHOOK_SECRET="secret"
        )
        self.assertEqual(raised.code, 2)
        github.return_value.list_repositories.assert_not_called()

    def test_an_unsafe_webhook_url_is_refused_before_discovery(self) -> None:
        github, _, _, raised = self.run_cli(
            "--repository", "example-org/app", "--apply",
            GITHUB_TOKEN="token",
            CINDRAL_WEBHOOK_SECRET="secret",
            CINDRAL_WEBHOOK_URL="http://unsafe.example/hook",
        )
        self.assertEqual(raised.code, 2)
        github.return_value.list_repositories.assert_not_called()
        github.return_value.ensure_push_webhook.assert_not_called()

    def test_a_missing_token_is_refused_before_any_github_request(self) -> None:
        github, _, _, raised = self.run_cli("--repository", "example-org/app")
        self.assertEqual(raised.code, 2)
        github.assert_not_called()

    def test_a_discovery_failure_exits_nonzero_without_writing(self) -> None:
        github, _, errors, raised = self.run_cli(
            "--repository", "example-org/app", GITHUB_TOKEN="token", discovery=GitHubAPIError("rate limited")
        )
        self.assertEqual(raised.code, 1)
        self.assertIn("Repository discovery failed: rate limited", errors)
        github.return_value.ensure_push_webhook.assert_not_called()


if __name__ == "__main__":
    unittest.main()
