"""Why a delivery is refused, and whether a refusal may go to the other lane.

Split out because this is a decision about trust and it is the same decision on
every path that can make it: the live push, a redelivery, and a replay after a
restart. One predicate, so a refusal that is safe to cover when it arrives cannot
become unsafe when the push is retried, and a refusal found on a replay is not
dropped for a reason the live path would have accepted.

The reasons themselves live here too, for the same reason: a refusal a sender
cannot act on and one it can look identical on the wire and differ only in prose.
"""
from collections.abc import Callable

from .deliveries import REJECTED, Delivery
from .policy import Policy



def should_fallback(state: str) -> bool:
    """Whether a refused dispatch may be handed to the other lane.

    Only `REJECTED` may. Every `UNCERTAIN` here is a 5xx, where a workflow run
    may already exist, so the same commit must not also be queued on a device.
    One predicate for every caller, so the live path and both replay paths
    cannot drift apart on which refusals are safe to cover.
    """
    return state == REJECTED


def missing_workflow(repository: str, workflow: str) -> str:
    """Name the adapter a lookup did not find, rather than drawing a conclusion.

    "Has not opted in" reads as a decision the broker made. It did not: it asked
    GitHub for one workflow file and the answer was 404, and the usual cause of
    that is an adapter installed under a different name or not yet synced, which
    is a mistake an operator can fix and silence cannot.
    """
    return f"{repository} has no workflow file named {workflow}"


def tag_push(ref: str) -> dict[str, str]:
    """The body and status for a tag push, which is 422 rather than 202.

    A tag is a release someone asked for, and dropping it as 202 leaves no symptom
    at all: GitHub's delivery log shows a healthy hook, the repository is wired,
    onboard reports it served, and `git push --tags` produces nothing. The absence
    is the only evidence, and nobody learns of it from the outside.

    422 is deliberate. A non-2xx is what makes the delivery show as failed in the
    delivery log, which is the one place the sender is looking. The reason says
    what happened rather than what did not, because "this is not a branch" is not
    something a maintainer can act on and "a tag is not a device-lane ref" is.
    """
    return {
        "status": "refused",
        "error": f"a tag is not a device-lane ref: {ref}",
        "ref": ref,
        "hint": "release from a tag push needs a dispatch workflow,"
        " or a repository in policy that names this tag ref",
    }


def non_default_branch(branch: str | None, ref: str) -> dict[str, str]:
    """The body for a branch that is not the default one, which is 202.

    Not a failure, on purpose: a push to a branch that is not the default one is
    the expected outcome, not a refused request. Every contributor's feature branch
    would otherwise show a red delivery for something the broker did correctly. A
    tag push is the different case, because there a release was asked for and
    discarded.
    """
    return {"status": "ignored", "reason": f"branch is not the default branch: {branch or ref}"}


def hand_off(
    record: Delivery, reason: str, policy: Policy,
    fallback: Callable[[Delivery], None] | None,
) -> bool:
    """Queue a refused delivery on the device pool, when the policy allows it.

    The rule the live push path follows, applied to a delivery it never saw: a
    `READY` row exists because the broker died before it could dispatch, so a
    refusal found on replay is the one case the device lane exists for. It is
    also the only case where the push would otherwise be lost with no run
    anywhere, because nothing re-sends a delivery the broker has already
    answered. A repository the policy serves on GitHub alone has no device to
    take it, and says so, rather than reporting a push that ran nowhere.
    """
    if not policy.permits_device(record.repository):
        print(
            f"warning: delivery {record.delivery} for {record.repository} ran nowhere: {reason}",
            flush=True,
        )
        return False
    if fallback is None:
        print(
            f"warning: delivery {record.delivery} for {record.repository} ran nowhere:"
            f" the replay has no device lane: {reason}",
            flush=True,
        )
        return False
    if not record.sha:
        # a row written before the ledger carried the commit, and a device
        # checks a commit out rather than a payload
        print(
            f"warning: delivery {record.delivery} for {record.repository} ran nowhere:"
            f" no commit was recorded for it: {reason}",
            flush=True,
        )
        return False
    fallback(record)
    return True


