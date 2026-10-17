"""The checkout ref a dispatch runs is the broker's decision, not the caller's.

Its own module because it is a trust rule rather than plumbing: everything else
`/v1/dispatch` receives is either validated against the target's allowlist or
overwritten with a value the broker computed, and this was the one input that was
neither. A caller holding the dispatch token could name a pull-request ref and a
self-hosted lane in the same request, and the adapter would check that ref out.
"""
from typing import Any

from .policy import canonical_ref

__all__ = ["CINDRAL_REF_INPUT", "checked_dispatch_ref"]

#: the input the adapter reads its checkout ref from, and the one this rule guards
CINDRAL_REF_INPUT = "cindral_ref"


def checked_dispatch_ref(
    policy: Any,
    repository: str,
    workflow: str,
    inputs: dict[str, str],
) -> None:
    """Refuse a caller-supplied checkout ref the policy does not name, in place.

    The same check as the request's own ref, because it is the same dimension: a
    branch the policy names is a normal thing to dispatch, and a pull-request, tag
    or sha ref is not one it can grant. Passing one alongside a self-hosted lane
    is the difference between a broker credential and a way to run unreviewed code
    on a device, which is the one thing a self-hosted lane exists not to do.

    It needs the dispatch token to reach, so it is not the unauthenticated path
    and not the same bug as a fork pull request arriving by webhook. The webhook
    route's trust decision does not apply here either, because no
    `PullRequestEvent` is built on this path at all.

    A permitted ref is written back in its canonical form, so the string that was
    checked is the string the adapter checks out. Policy reads a bare name as a
    branch and reads anything else as itself, so forwarding the caller's spelling
    would leave the validator and the consumer able to disagree about what a ref
    means, which is the class of gap rather than a detail.

    An absent or blank ref is left alone, because the adapter already falls back
    to the dispatch ref and a blank string must not become a refusal of an
    otherwise legitimate dispatch.
    """
    named = (inputs.get(CINDRAL_REF_INPUT) or "").strip()
    if not named:
        return
    policy.check_dispatch(repository, workflow, named)
    inputs[CINDRAL_REF_INPUT] = canonical_ref(named)
