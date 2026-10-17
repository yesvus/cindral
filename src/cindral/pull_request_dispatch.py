"""The pull request event, answered on whichever lane the policy picks.

Apart from the push webhook, because the two differ in what a reviewer sees: a
pull request run reports against the default branch, so it needs an explicit head
status or the review shows no check at all.
"""
from typing import Any

from .budget import release_reservation
from .merge_ref import run_sha_for_pull_request
from .github import GitHubAPIError, GitHubDispatchError, is_repository_slug
from .models import RouteRequest
from .policy import RouteUnavailable
from .refusal import missing_workflow
from .webhook import (
    DELIVERY_HEADER,
    PullRequestEvent,
    WebhookError,
    parse_pull_request_event,
)


class PullRequestDispatchMixin:
    """Answer a pull request event, and post a status the reviewer can see."""

    server: Any

    def _post_pull_request_status(self, event: PullRequestEvent, state: str, description: str) -> bool:
        # a dispatched run reports against the default branch, so without an
        # explicit head status the reviewer sees no check on the pull request
        if self.server.github is None or not event.status_sha:
            return False
        try:
            self.server.github.post_status(
                event.repository,
                event.status_sha,
                state,
                description,
                context=self.server.status_context,
            )
            return True
        except GitHubAPIError:
            return False

    def _run_sha(self, event: PullRequestEvent) -> str:
        """The commit to build for this pull request, resolved now rather than read.

        The event's `merge_commit_sha` cannot be trusted for this. GitHub computes
        a test merge asynchronously, so a `synchronize` event can be delivered while
        the merge ref still points at the previous head, and the value in the payload
        is then a real sha for a commit that does not contain the code under review.
        A run built from it produces a verdict about something else, and the check
        is attached to a different commit from the one it describes, which is worse
        than no check because it is trusted.

        So the ref is resolved here, and used only when its parents include the head
        this event arrived with. Anything else falls back to the head: the code as
        written rather than as merged, which is the honest thing to build when
        there is no current merge to build.
        """
        if self.server.github is None:
            return event.run_sha
        try:
            resolved = self.server.github.pull_request_merge_sha(event.repository, event.number)
        except GitHubAPIError as exc:
            # a lookup that failed is not a reason to refuse the run, and the head
            # is always a commit that exists, so the fallback is safe
            self.log_error("merge ref lookup failed for %s#%d: %s", event.repository, event.number, exc)
            return event.head_sha
        return run_sha_for_pull_request(event.head_sha, resolved)

    def _pull_request_webhook(self, body: bytes) -> None:
        try:
            event = parse_pull_request_event(body)
        except WebhookError as exc:
            self._send(400, {"error": str(exc)})
            return
        if not is_repository_slug(event.repository):
            self._send(400, {"error": "invalid repository name"})
            return
        if not event.should_route:
            self._send(202, {"status": "ignored", "reason": f"pull request action is not routed: {event.action}"})
            return
        if (
            self.server.jobs is not None
            and event.direct_eligible
            and self.server.policy.permits_device(event.repository)
        ):
            self._enqueue_direct(
                event.repository,
                self._run_sha(event),
                event.ref,
                status_sha=event.status_sha,
                extra={"pull_request": event.number, "trusted": True},
            )
            return
        if self.server.github is None:
            self._send(503, {"error": "GitHub dispatch token is not configured"})
            return
        try:
            if not self.server.github.workflow_exists(event.repository, self.server.workflow_file):
                # the same refusal the push path already writes, for the same
                # reason: "has not opted in" reads as a decision the broker made,
                # when what happened is that one workflow file was not found and
                # the usual cause is an adapter installed under another name or
                # not yet synced. A 202 with nothing behind it reads as a pull
                # request that has not run yet.
                reason = missing_workflow(event.repository, self.server.workflow_file)
                self._post_pull_request_status(event, "error", f"{reason}; this pull request ran nowhere")
                self._send(202, {
                    "status": "ignored",
                    "reason": reason,
                    "workflow": self.server.workflow_file,
                })
                return
        except GitHubAPIError as exc:
            self._send(502, {"error": str(exc)})
            return
        request = RouteRequest(
            requested_lane="auto" if event.trusted else "hosted",
            repository_visibility="private" if event.private else "public",
            quota_status="unknown",
        )
        try:
            decision = self.server.policy.choose(request, self.server.runners)
        except RouteUnavailable as exc:
            self._send(409, {"error": str(exc)})
            return
        inputs = {
            "cindral_lane": decision.lane,
            "cindral_target": request.target or "",
            "cindral_reason": decision.reason if event.trusted else event.hosted_reason,
            "cindral_ref": event.ref,
        }
        status_posted = self._post_pull_request_status(
            event, "pending", f"dispatched on the {decision.lane} lane"
        )
        try:
            self._send_dispatch(
                event.repository, self.server.workflow_file, event.default_branch, inputs
            )
        except GitHubDispatchError as exc:
            # nothing ran, so nothing was charged, and the hold taken when the
            # lane was chosen goes back rather than tightening the month
            release_reservation(self.server, decision.reservation)
            self._post_pull_request_status(event, "error", "the dispatch was rejected by GitHub")
            self._send(502, {"error": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - the caller must get an answer
            # The hold stays. A rejection from GitHub is a definite answer that
            # the run did not start; a connection that failed part way through is
            # not, and it may have started one. An unreleased hold makes the
            # ceiling one dispatch tighter than the money spent, and an erased one
            # books a run nobody paid for, so the error goes the other way.
            self.log_error("unexpected dispatch failure for a pull request: %s", exc)
            self._post_pull_request_status(event, "error", "the dispatch could not be sent")
            self._send(502, {"error": "the dispatch could not be sent"})
            return
        self._name_hold(
            decision, event.repository, self.server.workflow_file, event.default_branch
        )
        self._send(
            200,
            {
                **decision.as_dict(),
                "reason": inputs["cindral_reason"],
                "dispatched": True,
                "repository": event.repository,
                "pull_request": event.number,
                "ref": event.ref,
                "trusted": event.trusted,
                "status_posted": status_posted,
                "delivery": self.headers.get(DELIVERY_HEADER, ""),
            },
        )
