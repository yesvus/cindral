"""The pull request cases of the webhook endpoint, and the bodies they send.

Split out because a pull request is a different trust question from a push: who
opened it, whose branch it is on, and what a bot's branch is allowed to do. The
bodies live here with the cases, so a change to either cannot leave the other
describing an event that no longer exists. The harness is the endpoint one the
push cases use, because it is the same endpoint.
"""
import json
import tempfile
import unittest
from unittest.mock import patch

from cindral.github import GitHubDispatchError
from cindral.jobs import JobStore
from pathlib import Path
from cindral.service import WEBHOOK_PATH
from cindral.webhook import sign

from test_webhook import REPO, SECRET, RepositoryRunner, WebhookEndpointTest, push_body

#: a merge sha of the shape git produces, so a test that means to exercise the
#: parent check cannot be satisfied by the shape check rejecting it first
MERGE_SHA = "b" * 40
BASE_SHA = "a" * 40


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


class PullRequestEndpointTest(WebhookEndpointTest):
    def test_trusted_private_pull_request_uses_policy_fallback(self) -> None:
        body = pull_request_body()
        with patch.object(self.server, "github") as github:
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "pull_request"))
        self.assertEqual(status, 200)
        self.assertTrue(payload["trusted"])
        self.assertEqual(payload["lane"], "fallback")
        repository, workflow, ref, inputs = github.dispatch.call_args[0]
        self.assertEqual(repository, REPO)
        self.assertEqual(workflow, "cindral-dispatch.yml")
        self.assertEqual(ref, "main")
        self.assertEqual(inputs["cindral_lane"], "fallback")
        self.assertEqual(inputs["cindral_ref"], "refs/pull/42/merge")

    def test_untrusted_private_pull_request_is_forced_to_hosted(self) -> None:
        body = pull_request_body(author_association="CONTRIBUTOR")
        with patch.object(self.server, "github") as github:
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "pull_request"))
        self.assertEqual(status, 200)
        self.assertFalse(payload["trusted"])
        self.assertEqual(payload["lane"], "hosted")
        # same repository, untrusted by association rather than by provenance
        self.assertEqual(
            payload["reason"],
            "the author may not run code on local hardware, so hosted execution is required",
        )
        self.assertEqual(github.dispatch.call_args[0][3]["cindral_lane"], "hosted")

    def test_a_fork_pull_request_never_reaches_a_local_lane(self) -> None:
        """Provenance decides the lane, and nothing else does.

        `author_association` is GitHub's statement about the author and the *base*
        repository, so someone with write access who opens a pull request from
        their own fork arrives as COLLABORATOR. Asking for `auto` on the strength
        of that, with the pull-request path's quota hardcoded to unknown and the
        policy mapping unknown to local, resolved to the fallback lane, which is a
        self-hosted runner executing as a user that can read the App private key.

        The tests above vary the *association* on a same-repository head, which is
        the other dimension and the one that already worked. This one varies the
        *provenance* through the real endpoint and asserts the lane that comes out,
        because `trusted` being false is a predicate and the lane is the thing a
        reader of a run needs to be true.
        """
        for association in ("NONE", "CONTRIBUTOR", "COLLABORATOR", "MEMBER", "OWNER"):
            with self.subTest(association=association):
                body = pull_request_body(
                    author_association=association, head_repo="attacker/example-app"
                )
                with patch.object(self.server, "github") as github:
                    status, payload = self.post(
                        WEBHOOK_PATH, body, self.signed(body, "pull_request")
                    )
                self.assertEqual(status, 200)
                self.assertFalse(payload["trusted"])
                self.assertEqual(payload["lane"], "hosted")
                inputs = github.dispatch.call_args[0][3]
                self.assertEqual(inputs["cindral_lane"], "hosted")
                self.assertIn("head repository", inputs["cindral_reason"])

    def test_public_pull_request_uses_hosted_lane(self) -> None:
        body = pull_request_body(private=False)
        with patch.object(self.server, "github") as github:
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "pull_request"))
        self.assertEqual(status, 200)
        self.assertEqual(payload["lane"], "hosted")
        self.assertEqual(github.dispatch.call_args[0][3]["cindral_lane"], "hosted")

    def _queue_for(self, body, resolved):
        """Post a pull request with the merge ref the API will report."""
        with tempfile.TemporaryDirectory() as tmp, patch.object(self.server, "github") as github:
            github.pull_request_merge_sha.return_value = resolved
            self.server.jobs = JobStore(str(Path(tmp) / "jobs.db"))
            self.server.agent_token = "agent-token"
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "pull_request"))
            return status, payload, github, self.server.jobs.list()

    def test_a_bot_pull_request_queues_the_merge_the_api_confirms(self) -> None:
        """A merge is only built when the ref is confirmed to be this head's.

        The event's `merge_commit_sha` cannot be taken at face value. GitHub computes
        a test merge asynchronously, so a `synchronize` event can arrive while the
        merge ref still points at the previous head, and the payload then names a
        real commit that does not contain the code under review. A run built from it
        reports a verdict about something else while the check is attached to a
        different commit, which is worse than no check because it is trusted.

        So the ref is resolved at dispatch and used only when its parents include the
        head this event arrived with.
        """
        body = bot_pull_request_body()
        status, payload, github, queued = self._queue_for(body, (MERGE_SHA, (BASE_SHA, "head123")))
        self.assertEqual(status, 202)
        self.assertTrue(payload["status_posted"])
        # the status still lands on the head, so it surfaces on the pull request
        self.assertEqual(github.post_status.call_args[0][:2], (REPO, "head123"))
        self.assertEqual([(job.sha, job.status_sha) for job in queued], [(MERGE_SHA, "head123")])
        github.dispatch.assert_not_called()

    def test_a_stale_merge_ref_is_not_built(self) -> None:
        """The demonstrated failure: the ref is real, and it is the previous push's.

        Its parents do not include this head, so it is not this pull request's test
        merge. Building it would produce a verdict about a tree that is not under
        review, recorded against the commit that is.
        """
        body = bot_pull_request_body()
        _, _, github, queued = self._queue_for(body, (MERGE_SHA, (BASE_SHA, "older999")))
        self.assertEqual([job.sha for job in queued], ["head123"])

    def test_no_merge_ref_queues_the_head_rather_than_nothing(self) -> None:
        # a pull request that conflicts, or a fork whose base moved, has no merge
        # to test; the head is the code as written, and is always buildable
        body = bot_pull_request_body()
        _, _, _, queued = self._queue_for(body, None)
        self.assertEqual([job.sha for job in queued], ["head123"])

    def test_a_merge_answer_of_the_wrong_shape_is_ignored(self) -> None:
        # a client that cannot answer has not earned the right to stop a run, and
        # the decision is the last thing between a lookup and the built commit
        for answer in ((), MERGE_SHA, (None, ()), (MERGE_SHA, None), object()):
            with self.subTest(answer=type(answer).__name__):
                _, _, _, queued = self._queue_for(bot_pull_request_body(), answer)
                self.assertEqual([job.sha for job in queued], ["head123"])

    def test_dispatched_pull_request_reports_on_the_head_commit(self) -> None:
        body = bot_pull_request_body()
        with patch.object(self.server, "github") as github:
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "pull_request"))
            repository, sha, state, description = github.post_status.call_args[0][:4]
        self.assertEqual(status, 200)
        self.assertTrue(payload["status_posted"])
        self.assertEqual((repository, sha, state), (REPO, "head123", "pending"))
        self.assertEqual(github.post_status.call_args[1]["context"], "cindral/ci")
        self.assertIn("fallback", description)

    def test_a_pull_request_missing_the_adapter_is_named_and_marked(self) -> None:
        """A missing adapter must not leave a pull request looking unrun.

        202 is a success code, so an `ignored` pull request that posts no status
        is indistinguishable from one that has not been looked at yet. The push
        path already answers this by naming the workflow and writing the refusal
        on the commit; the pull request path answered with silence and a
        conclusion ("has not opted in") the broker cannot draw from a 404.

        Both are what an operator needs: which file was looked for, and the fact
        that this pull request ran nowhere.
        """
        body = bot_pull_request_body()
        with patch.object(self.server, "github") as github:
            github.workflow_exists.return_value = False
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "pull_request"))

        self.assertEqual(status, 202)
        self.assertEqual(payload["workflow"], self.server.workflow_file)
        self.assertIn(self.server.workflow_file, payload["reason"])
        self.assertNotIn("opted in", payload["reason"])
        github.dispatch.assert_not_called()
        self.assertEqual(
            github.post_status.call_args[0][1:3],
            ("head123", "error"),
            "a pull request that ran nowhere must be visible on the head commit",
        )
        self.assertIn("ran nowhere", github.post_status.call_args[0][3])

    def test_rejected_dispatch_resolves_the_head_commit_status(self) -> None:
        body = bot_pull_request_body()
        with patch.object(self.server, "github") as github:
            github.dispatch.side_effect = GitHubDispatchError("no runner is available")
            status, _ = self.post(WEBHOOK_PATH, body, self.signed(body, "pull_request"))
        self.assertEqual(status, 502)
        self.assertEqual(github.post_status.call_args[0][1:3], ("head123", "error"))

    def test_bad_signature_on_pull_request_is_refused(self) -> None:
        body = pull_request_body()
        with patch.object(self.server, "github") as github:
            status, _ = self.post(
                WEBHOOK_PATH,
                body,
                {"X-Hub-Signature-256": sign("wrong", body), "X-GitHub-Event": "pull_request"},
            )
        self.assertEqual(status, 401)
        github.dispatch.assert_not_called()

    def test_closed_pull_request_is_ignored(self) -> None:
        body = pull_request_body(action="closed")
        with patch.object(self.server, "github") as github:
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "pull_request"))
        self.assertEqual(status, 202)
        github.dispatch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
