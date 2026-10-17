"""Event parsing, signature verification, and the trust decision.

Split out of `test_webhook.py`, which sits exactly on its line limit, so the
trust decision and the delivery ledger's own rules have somewhere to be tested
directly rather than only through the endpoint.
"""
import hashlib
import hmac
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from unittest.mock import MagicMock, patch

from cindral.deliveries import DeliveryStore
from cindral.github import GitHubClient, GitHubDispatchError
from cindral.jobs import JobStore
from cindral.policy import Policy, load_dispatch_targets
from cindral.service import WEBHOOK_PATH, CindralHandler, CindralServer
from cindral.webhook import WebhookError, parse_pull_request_event, parse_push_event, sign, verify_signature
from cindral.webhook_dispatch import missing_workflow, should_fallback

SECRET = "shhh"
REPO = "example-org/example-app"


def push_body(repository: str = REPO, private: bool = True, ref: str = "refs/heads/main", deleted: bool = False) -> bytes:
    return json.dumps(
        {
            "ref": ref,
            "after": "0" * 40,
            "deleted": deleted,
            "repository": {"full_name": repository, "private": private, "default_branch": "main"},
        }
    ).encode()


def pull_request_body(
    repository: str = REPO,
    private: bool = True,
    action: str = "opened",
    number: int = 42,
    author_association: str = "OWNER",
    head_repo: str | None = None,
    head_sha: str = "head123",
    merge_sha: str = "merge123",
    draft: bool = False,
    author: str | None = "octocat",
    author_type: str = "User",
) -> bytes:
    return json.dumps(
        {
            "action": action,
            "number": number,
            "repository": {"full_name": repository, "private": private, "default_branch": "main"},
            "pull_request": {
                "author_association": author_association,
                "draft": draft,
                "head": {"sha": head_sha, "repo": {"full_name": head_repo or repository}},
                "merge_commit_sha": merge_sha,
                "user": {"login": author, "type": author_type},
            },
        }
    ).encode()


def bot_pull_request_body(
    head_repo: str | None = None, author: str = "dependabot[bot]", author_type: str = "Bot"
) -> bytes:
    return pull_request_body(
        author_association="NONE",
        head_repo=head_repo,
        author=author,
        author_type=author_type,
    )



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


class MissingAdapterTest(unittest.TestCase):
    """What a push learns when the repository has no adapter to dispatch.

    The answer used to be a 202 saying the repository had not opted in, which
    is both a conclusion the broker cannot draw from a 404 on one file and a
    success code, so nothing alarmed. These run through the endpoint because
    the two visible signals, the reason and the commit status, are the answer.
    """

    def setUp(self) -> None:
        self.db = tempfile.TemporaryDirectory()
        self.addCleanup(self.db.cleanup)
        self.server = CindralServer(("127.0.0.1", 0), CindralHandler)
        self.server.deliveries = DeliveryStore(str(Path(self.db.name) / "jobs.db"))
        self.server.policy = Policy.load("config/policy.toml")
        self.server.runners = ()
        self.server.github = None
        self.server.webhook_secret = SECRET
        self.server.workflow_file = "cindral-dispatch.yml"
        self.server.capacity_lock = threading.Lock()
        self.server.runner_reservations = {}
        self.server.reservation_seconds = 10
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def push(self) -> tuple[int, dict, MagicMock]:
        body = push_body()
        with patch.object(self.server, "github") as github:
            github.workflow_exists.return_value = False
            conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request(
                "POST", WEBHOOK_PATH, body=body,
                headers={"Content-Type": "application/json",
                         "X-Hub-Signature-256": sign(SECRET, body), "X-GitHub-Event": "push"},
            )
            response = conn.getresponse()
            payload = json.loads(response.read() or b"{}")
            conn.close()
        return response.status, payload, github

    def test_a_device_repository_runs_the_push_instead_of_naming_the_file(self) -> None:
        # what is absent is the adapter and not the pool, and this push would
        # have run on a device before hosted-first routing asked GitHub first
        self.server.jobs = JobStore(str(Path(self.db.name) / "queue.db"))
        self.server.agent_token = "agent-token"
        status, payload, github = self.push()
        self.assertEqual((status, payload["queued"]), (202, True))
        self.assertEqual(len(self.server.jobs.list()), 1)
        github.dispatch.assert_not_called()

    def test_a_dispatch_only_repository_is_named_on_the_commit(self) -> None:
        # A 202 is a success code, so an ignored push that leaves no check
        # behind it is indistinguishable from one that has not run. With no
        # device to queue, the commit is where the refusal has to appear.
        entry = {"mode": "dispatch", "repository": REPO, "workflows": ["cindral-dispatch.yml"],
                 "refs": ["refs/heads/main"]}
        self.server.policy = replace(
            self.server.policy,
            dispatch_targets=load_dispatch_targets({"dispatch": {"repositories": [entry]}}),
        )
        status, payload, github = self.push()
        self.assertEqual((status, payload["status"], payload["workflow"]), (202, "ignored", "cindral-dispatch.yml"))
        self.assertIn("cindral-dispatch.yml", payload["reason"])
        self.assertNotIn("opted in", payload["reason"])
        repository, sha, state, description = github.post_status.call_args[0][:4]
        self.assertEqual((repository, sha, state, description.split()[0]), (REPO, "0" * 40, "error", REPO))
        self.assertIn("cindral-dispatch.yml", description)
        github.dispatch.assert_not_called()


class PushLaneTest(unittest.TestCase):
    def test_a_dispatch_only_repository_offers_no_device_lane(self) -> None:
        # mode = "dispatch" names no device, so preferring one would fall back to
        # a pool the policy has not agreed can serve this repository
        policy = Policy.load("config/policy.toml")
        dispatch_only = replace(
            policy,
            dispatch_targets=load_dispatch_targets({"dispatch": {"repositories": [
                {"mode": "dispatch", "repository": REPO,
                 "workflows": ["cindral-dispatch.yml"]}]}}),
        )
        self.assertIsNone(dispatch_only.push_lane(REPO))

    def test_a_push_lane_outside_the_two_known_lanes_is_refused_at_load(self) -> None:
        # a typo in the policy would otherwise name a lane nothing routes to, and
        # the push would take whichever branch the comparison happened to miss
        with self.assertRaises(ValueError) as caught:
            load_dispatch_targets({"dispatch": {"repositories": [
                {"mode": "both", "repository": REPO,
                 "workflows": ["cindral-dispatch.yml"], "push_lane": "selfhosted"}]}})
        self.assertIn("push_lane", str(caught.exception))

    def test_the_default_lane_for_a_served_repository_is_hosted(self) -> None:
        # a repository the policy serves with no push_lane named goes to GitHub's
        # runners, because that costs a user account nothing until its included
        # minutes run out
        policy = load_dispatch_targets(
            {"dispatch": {"repositories": [
                {"mode": "both", "repository": REPO, "workflows": ["cindral-dispatch.yml"]}]}}
        )[0]
        self.assertEqual(policy.push_lane, "hosted")


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


class FallbackRuleTest(unittest.TestCase):
    """Which delivery states may be handed to the other lane.

    One predicate serves the live push and both replay paths, so a commit that
    was queued on a device when it arrived cannot be dropped when it is
    retried. Pinning the boundary here is what keeps the three paths equal.
    """

    def test_only_a_refusal_may_reach_the_other_lane(self) -> None:
        # REJECTED is GitHub saying no, so no run exists and the device is free
        self.assertTrue(should_fallback("rejected"))
        # UNCERTAIN is a 5xx where a run may already exist, so the device must
        # not run the same commit a second time
        for state in ("uncertain", "accepted", "attempting", "ready", "duplicate", ""):
            with self.subTest(state=state):
                self.assertFalse(should_fallback(state))

    def test_a_missing_adapter_is_named_rather_than_called_an_opt_out(self) -> None:
        # "has not opted in" is a conclusion the broker cannot draw: it asked
        # for one file and GitHub answered 404, which is usually an adapter
        # installed under a different name
        reason = missing_workflow("example-org/example-app", "cindral-dispatch.yml")
        self.assertIn("cindral-dispatch.yml", reason)
        self.assertIn("example-org/example-app", reason)
        self.assertNotIn("opted in", reason)


class DispatchFailureTest(unittest.TestCase):
    """Which dispatch failures leave the push with no run at all.

    A `REJECTED` state means GitHub refused, so the device pool can take the
    push. An `UNCERTAIN` one means a run may already exist, so falling back
    would run the same commit twice. Getting that boundary wrong is silent
    either way, so each case is pinned by the state it produces.
    """

    def _state_for(self, exc: Exception) -> str:
        from cindral.webhook_dispatch import dispatch_record
        with tempfile.TemporaryDirectory() as tmp:
            store = DeliveryStore(str(Path(tmp) / "jobs.db"))
            record = store.register("example-org/example-app", "delivery-1", "fp",
                                    "cindral-dispatch.yml", "refs/heads/main", {})
            client = GitHubClient("token")
            with patch.object(client, "dispatch", side_effect=exc):
                state, _ = dispatch_record(store, client, record)
            return state

    def test_a_transport_failure_leaves_no_run_so_the_device_takes_it(self) -> None:
        # URLError carries no status code, so the request never reached GitHub.
        # Reading that as "the answer was lost" would leave the push with no
        # check at all, on the default configuration.
        self.assertEqual(
            self._state_for(GitHubDispatchError("GitHub dispatch failed: [Errno -2] Name or service not known")),
            "rejected",
        )

    def test_a_server_error_may_already_have_a_run(self) -> None:
        # 5xx is the one answer where a workflow run may exist, so the device
        # pool must not also run the commit
        self.assertEqual(self._state_for(GitHubDispatchError("HTTP 500", 500)), "uncertain")

    def test_a_definitive_refusal_is_not_a_lost_answer(self) -> None:
        # 400, 404 and 422 are GitHub saying no, and no run exists
        for code in (400, 404, 422):
            with self.subTest(code=code):
                self.assertEqual(self._state_for(GitHubDispatchError(f"HTTP {code}", code)), "rejected")

    def test_a_refusal_that_may_clear_keeps_the_delivery_retryable(self) -> None:
        # 403 and 429 are GitHub refusing for now, and a rate limit clears, so
        # these stay terminal-free rather than becoming a permanent rejection
        for code in (403, 429):
            with self.subTest(code=code):
                self.assertEqual(self._state_for(GitHubDispatchError(f"HTTP {code}", code)), "uncertain")


class PullRequestEventTest(unittest.TestCase):
    def test_trusted_pull_request_uses_merge_ref(self) -> None:
        event = parse_pull_request_event(pull_request_body())
        self.assertTrue(event.trusted)
        self.assertEqual(event.ref, "refs/pull/42/merge")
        self.assertTrue(event.should_route)
        self.assertTrue(event.same_repo)
        self.assertTrue(event.direct_eligible)
        self.assertEqual(event.run_sha, "merge123")
        self.assertEqual(event.status_sha, "head123")

    def test_fork_pull_request_is_not_direct_eligible(self) -> None:
        event = parse_pull_request_event(pull_request_body(head_repo="someone/example-app"))
        self.assertFalse(event.same_repo)
        self.assertFalse(event.direct_eligible)

    def test_draft_pull_request_is_not_direct_eligible(self) -> None:
        event = parse_pull_request_event(pull_request_body(draft=True))
        self.assertFalse(event.direct_eligible)

    def test_closed_pull_request_is_not_direct_eligible(self) -> None:
        event = parse_pull_request_event(pull_request_body(action="closed"))
        self.assertFalse(event.direct_eligible)

    def test_forty_zeros_is_not_a_merge_commit(self) -> None:
        """GitHub's "no merge commit" answer is a string, and it is truthy.

        Forty zeros is what `merge_commit_sha` carries when a pull request has no
        merge commit yet, or when it conflicts and there never will be. A
        truthiness test walks straight past it, so the run is enqueued against a
        commit that cannot be checked out while the verdict is attached to the real
        head. The check then reports on code nothing ever built, which is a failure
        with no error anywhere in it.
        """
        zeros = "0" * 40
        event = parse_pull_request_event(pull_request_body(merge_sha=zeros))
        self.assertEqual(event.merge_sha, "")
        self.assertEqual(event.run_sha, "head123")
        self.assertTrue(event.should_route, "the event was dropped instead of routed")
        self.assertTrue(event.direct_eligible, "the run was not eligible at all")

    def test_run_sha_falls_back_to_head_without_a_merge_commit(self) -> None:
        event = parse_pull_request_event(pull_request_body(merge_sha=""))
        self.assertEqual(event.run_sha, "head123")

    def test_outside_contributor_is_untrusted(self) -> None:
        event = parse_pull_request_event(pull_request_body(author_association="CONTRIBUTOR"))
        self.assertFalse(event.trusted)

    def test_dependency_bot_pull_request_is_trusted_in_the_same_repository(self) -> None:
        event = parse_pull_request_event(bot_pull_request_body())
        self.assertTrue(event.bot_authored)
        self.assertTrue(event.trusted)
        self.assertTrue(event.direct_eligible)
        self.assertEqual(event.status_sha, "head123")

    def test_bot_pull_request_from_a_fork_is_untrusted(self) -> None:
        event = parse_pull_request_event(bot_pull_request_body(head_repo="someone/example-app"))
        self.assertTrue(event.bot_authored)
        self.assertFalse(event.trusted)
        self.assertFalse(event.direct_eligible)

    def test_reserved_bot_login_suffix_marks_the_author_without_a_type(self) -> None:
        event = parse_pull_request_event(bot_pull_request_body(author="renovate[bot]", author_type=""))
        self.assertTrue(event.bot_authored)
        self.assertTrue(event.trusted)

    def test_draft_bot_pull_request_still_runs_on_the_hosted_lane(self) -> None:
        event = parse_pull_request_event(
            pull_request_body(author_association="NONE", draft=True, author="dependabot[bot]", author_type="Bot")
        )
        self.assertTrue(event.trusted)
        self.assertFalse(event.direct_eligible)

    def test_missing_author_is_not_treated_as_a_bot(self) -> None:
        event = parse_pull_request_event(pull_request_body(author_association="NONE", author=None, author_type=""))
        self.assertEqual(event.author, "")
        self.assertFalse(event.bot_authored)
        self.assertFalse(event.trusted)

    def test_non_routing_action_is_ignored(self) -> None:
        event = parse_pull_request_event(pull_request_body(action="closed"))
        self.assertFalse(event.should_route)

    def test_invalid_pull_request_number_is_refused(self) -> None:
        with self.assertRaises(WebhookError):
            parse_pull_request_event(pull_request_body(number=0))

    def test_a_fork_pull_request_is_never_trusted(self) -> None:
        # `author_association` describes the author's relationship to the BASE
        # repository, so a collaborator who opens a pull request from a fork
        # arrives as COLLABORATOR. Reading that as trusted put a self-hosted
        # lane on code that did not come from the repository, and the fallback
        # runners execute as a user that can read the App private key.
        for association in ("NONE", "CONTRIBUTOR", "COLLABORATOR", "MEMBER", "OWNER"):
            with self.subTest(association=association):
                event = parse_pull_request_event(
                    pull_request_body(
                        author_association=association, head_repo="attacker/example-app"
                    )
                )
                self.assertFalse(event.trusted)

    def test_the_hosted_reason_names_the_reason_that_applies(self) -> None:
        # a same-repository pull request from an outside contributor has
        # nothing odd about its head repository, so telling an operator the
        # head differs would be simply false
        fork = parse_pull_request_event(
            pull_request_body(author_association="COLLABORATOR", head_repo="attacker/example-app")
        )
        self.assertIn("head repository is not this repository", fork.hosted_reason)
        same_repo = parse_pull_request_event(
            pull_request_body(author_association="CONTRIBUTOR")
        )
        self.assertIn("author may not run code", same_repo.hosted_reason)

    def test_a_same_repository_collaborator_is_still_trusted(self) -> None:
        # the other direction: ordinary same-repository pull requests are the
        # ones the device pool exists for, and the fix must not untrust them
        event = parse_pull_request_event(pull_request_body(author_association="COLLABORATOR"))
        self.assertTrue(event.trusted)
