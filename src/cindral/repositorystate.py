"""What a caller needs to know about one repository before asking the broker to run it.

Onboarding state lives in three places that do not talk to each other: GitHub
holds the adapter and the webhook, the policy file holds the allowlist, and the
broker holds the tokens. A caller that checks only one of them concludes the
repository is wired, so this reads all three and names what is missing.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .github import GitHubAPIError, GitHubClient, is_repository_slug
from .policy import DispatchNotPermitted, Policy

ADAPTER_WORKFLOW = "cindral-dispatch.yml"


@dataclass(frozen=True)
class RepositoryState:
    """One repository's readiness, and the next action when it is not ready.

    ``ready`` means nothing this report can see would refuse a dispatch. It does
    not promise capacity: the broker decides that at dispatch time from live
    runner state, which is deliberately not read here.
    """

    repository: str
    ref: str = "main"
    adapter_present: bool | None = None
    webhook_urls: tuple[str, ...] = ()
    github_error: str | None = None
    policy_path: str | None = None
    policy_error: str | None = None
    permitted: bool | None = None
    refusal: str | None = None
    target: str | None = None
    blockers: tuple[str, ...] = field(default_factory=tuple)
    next_step: str | None = None

    @property
    def ready(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "ref": self.ref,
            "ready": self.ready,
            "adapter_present": self.adapter_present,
            "webhook_urls": list(self.webhook_urls),
            "github_error": self.github_error,
            "policy_path": self.policy_path,
            "policy_error": self.policy_error,
            "permitted": self.permitted,
            "refusal": self.refusal,
            "target": self.target,
            "blockers": list(self.blockers),
            "next_step": self.next_step,
        }


def load_policy(policy_path: str | None) -> tuple[Policy | None, str, str | None]:
    """Read a policy file, reporting the path and any failure instead of raising.

    The broker's own copy is authoritative, so a missing or unreadable file here
    only means this report cannot answer the allowlist question. Raising would
    turn a partial answer into none, which is the opposite of what a caller
    needs when it is deciding whether to dispatch.
    """
    if not policy_path:
        return None, "", None
    path = Path(policy_path)
    if not path.exists():
        return None, str(path), f"no policy file at {path}"
    try:
        return Policy.load(path), str(path), None
    except (OSError, ValueError) as exc:
        return None, str(path), f"cannot read the policy at {path}: {exc}"


def _describe_target(policy: Policy, repository: str) -> str | None:
    entry = policy.target_for(repository)
    if entry is None:
        return None
    return (
        f"mode={entry.mode} workflows={','.join(entry.workflows) or '-'} "
        f"refs={','.join(entry.refs) or '-'}"
    )


def _check(policy: Policy, repository: str, ref: str) -> tuple[bool, str | None]:
    try:
        policy.check_dispatch(repository, ADAPTER_WORKFLOW, ref)
    except DispatchNotPermitted as exc:
        return False, str(exc)
    return True, None


def read_repository_state(
    repository: str,
    github: GitHubClient | None = None,
    policy: Policy | None = None,
    policy_path: str = "",
    policy_error: str | None = None,
    ref: str = "main",
    webhook_url: str | None = None,
) -> RepositoryState:
    """Assemble the readiness answer from GitHub and the policy, best effort.

    A GitHub failure becomes an unknown rather than a blocker: a token without
    hook-read access is not evidence that a repository is unwired, and calling it
    one would send an operator to fix something that already works.
    """
    if not is_repository_slug(repository):
        raise ValueError("repository must use the owner/name format")

    state = RepositoryState(
        repository=repository,
        ref=ref,
        policy_path=policy_path or None,
        policy_error=policy_error,
    )
    if policy is not None:
        permitted, refusal = _check(policy, repository, ref)
        state = replace(
            state,
            permitted=permitted,
            refusal=refusal,
            target=_describe_target(policy, repository),
        )
        if permitted is False:
            # before the GitHub block, because GitHub is optional: a caller with
            # no token would otherwise be handed a ready verdict on a ref the
            # broker refuses with a 403
            state = replace(
                state,
                blockers=state.blockers + (f"the policy refuses a dispatch: {refusal}",),
            )

    if github is not None:
        try:
            state = _with_github(state, github, webhook_url)
        except GitHubAPIError as exc:
            state = replace(state, github_error=str(exc))

    return _with_verdict(state)


def _with_github(
    state: RepositoryState,
    github: GitHubClient,
    webhook_url: str | None,
) -> RepositoryState:
    blockers = list(state.blockers)
    adapter_present = github.workflow_exists(state.repository, ADAPTER_WORKFLOW)
    if not adapter_present:
        blockers.append(f"{ADAPTER_WORKFLOW} is not on the default branch")

    urls = github.push_webhook_urls(state.repository)
    if not urls:
        blockers.append("no push webhook is registered, so no event reaches the broker")
    elif webhook_url and webhook_url not in urls:
        blockers.append(f"the push webhook points elsewhere: {', '.join(urls)}")

    return replace(
        state,
        adapter_present=adapter_present,
        webhook_urls=urls,
        blockers=tuple(blockers),
    )


def _with_verdict(state: RepositoryState) -> RepositoryState:
    """Name one next action, because a list of blockers is not a task."""
    repository, ref = state.repository, state.ref
    if not state.blockers:
        return replace(
            state,
            next_step=f"cindral run {repository} --ref {ref}",
        )
    first = state.blockers[0]
    if ADAPTER_WORKFLOW in first:
        step = (
            f"add templates/personal-dispatch.yml to {repository}, then "
            f"cindral onboard {repository}"
        )
    elif "webhook" in first:
        step = (
            f"cindral onboard {repository} --register-webhook, or "
            "cindral reconcile-webhooks --apply for every repository at once"
        )
    else:
        step = (
            f"add a [dispatch] entry for {repository} to the policy the broker "
            f"loads, then re-check ref {ref}"
        )
    return replace(state, next_step=step)
