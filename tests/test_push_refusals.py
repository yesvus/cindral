"""Which pushes the broker acts on, and how loudly it says no to the rest.

Split out because the question here is one question. A push delivery is either
something the broker runs or something it declines, and the declines are the part
that was wrong: a tag push came back `202`, which is a success, and was then
discarded, so a repository could be fully wired, report itself served, and never
release.

A non-default branch is the other decline and it stays `202` on purpose, because
that is the expected outcome rather than a refused request. The two sit in one `if`
and mean opposite things, which is why they are now named separately.
"""
import unittest
from unittest.mock import patch

from cindral.service import WEBHOOK_PATH
from cindral.webhook import sign

from test_webhook import SECRET, WebhookEndpointTest, push_body


class PushRefusalTest(WebhookEndpointTest):
    """A declined push, and what the sender is told about it."""

    def test_a_tag_push_is_refused_loudly_rather_than_accepted_and_dropped(self) -> None:
        """A release was asked for, and 202 left the absence as the only symptom.

        A tag took the non-default-branch path, because a tag ref carries no branch,
        so it was accepted and discarded. GitHub's delivery log showed a healthy
        hook, the repository was wired, onboard reported it served, and
        `git push --tags` produced nothing at all.

        A non-2xx is what makes the delivery show as failed, which is the one place
        the sender is looking. The reason names what is wrong rather than what is
        not: "this is not a branch" is not actionable and "a tag is not a
        device-lane ref" is.
        """
        body = push_body(ref="refs/tags/v1.2.3")
        with patch.object(self.server, "github") as github:
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertEqual(status, 422)
        self.assertEqual(payload["status"], "refused")
        self.assertEqual(payload["ref"], "refs/tags/v1.2.3")
        self.assertIn("tag", payload["error"])
        self.assertIn("dispatch", payload["hint"])
        github.dispatch.assert_not_called()

    def test_a_non_default_branch_is_still_ignored_and_still_a_success(self) -> None:
        """The case that looks the same in the code and is not the same at all.

        A push to a branch that is not the default one is the expected outcome, not
        a refused request. Marking it failed would put a red delivery in every
        contributor's log for something the broker did correctly, and a delivery
        log full of red is a thing people learn to ignore, which costs more than
        the one real case it would catch.

        Asserted so that making the tag case loud does not quietly make this one
        loud too.
        """
        body = push_body(ref="refs/heads/some-feature")
        with patch.object(self.server, "github") as github:
            status, payload = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertEqual(status, 202)
        self.assertEqual(payload["status"], "ignored")
        github.dispatch.assert_not_called()

    def test_a_deleted_ref_was_already_refused_before_this(self) -> None:
        """A deleted ref never reaches the tag branch, and never needed to.

        Checked because it looks like the same case and is not: a deleted ref is
        refused with a 400 further up, so it was already a visible failure and the
        silence this file is about never applied to it. Asserted so that moving the
        branch check does not quietly turn a deletion into a 202.
        """
        body = push_body(ref="refs/tags/v1.2.3", deleted=True)
        with patch.object(self.server, "github") as github:
            status, _ = self.post(WEBHOOK_PATH, body, self.signed(body, "push"))
        self.assertEqual(status, 400)
        github.dispatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
