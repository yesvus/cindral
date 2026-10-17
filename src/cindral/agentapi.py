"""The three agent-facing operations, as functions that return data.

Kept apart from the CLI and the MCP server so both surfaces answer from the same
code. An agent reaching Cindral through either one gets the same report, and
there is no second implementation to drift.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Mapping

from .brokerclient import DEFAULT_BROKER_URL, BrokerClient, BrokerError, auto_route_request
from .github import GitHubClient
from .onboarding import resolve_policy_path
from .repositorystate import ADAPTER_WORKFLOW, load_policy, read_repository_state

DISPATCH_TOKEN_ENV = "CINDRAL_DISPATCH_TOKEN"
POOL_TOKEN_ENV = "CINDRAL_POOL_TOKEN"
GITHUB_TOKEN_ENV = "GITHUB_TOKEN"

# What `cindral run` assumes about hosted quota when the caller states none.
# `unknown` reads as the local pool in the policy, so assuming it put every
# default run on a maintainer's device. GitHub's runners cost the account
# nothing, so being wrong here surfaces as a job that does not start, which the
# caller can see, rather than as code run on hardware the operator did not offer.
HOSTED_QUOTA_ASSUMPTION = "available"


@dataclass(frozen=True)
class AgentConfig:
    """Where the broker is, and which credentials this caller may present.

    The three tokens are distinct broker capabilities that refuse to stand in for
    each other, so they stay distinct here rather than collapsing into one
    ``token`` that would silently pick the wrong one.
    """

    broker_url: str = DEFAULT_BROKER_URL
    dispatch_token: str | None = None
    pool_token: str | None = None
    github_token: str | None = None
    webhook_url: str | None = None
    policy_path: str = ""

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "AgentConfig":
        env = os.environ if environ is None else environ
        explicit = env.get("CINDRAL_POLICY", "").strip()
        return cls(
            broker_url=env.get("CINDRAL_BROKER_URL", "").strip() or DEFAULT_BROKER_URL,
            dispatch_token=env.get(DISPATCH_TOKEN_ENV, "").strip() or None,
            pool_token=env.get(POOL_TOKEN_ENV, "").strip() or None,
            github_token=env.get(GITHUB_TOKEN_ENV, "").strip() or None,
            webhook_url=env.get("CINDRAL_WEBHOOK_URL", "").strip() or None,
            policy_path=explicit or resolve_policy_path(None),
        )

    def broker(self) -> BrokerClient:
        return BrokerClient(
            url=self.broker_url,
            dispatch_token=self.dispatch_token,
            pool_token=self.pool_token,
        )

    def github(self) -> GitHubClient | None:
        return GitHubClient(self.github_token) if self.github_token else None


def broker_overview(config: AgentConfig) -> dict[str, Any]:
    """What the broker is, and what it is currently carrying.

    A broker that cannot be reached is reported rather than raised, so one
    unreachable service does not hide the repository state, which is answered
    without the broker.
    """
    overview: dict[str, Any] = {"broker": config.broker_url}
    try:
        health = config.broker().health()
    except BrokerError as exc:
        overview["reachable"] = False
        overview["error"] = str(exc)
        return overview

    overview["reachable"] = True
    build = health.get("build") if isinstance(health, dict) else None
    overview["build"] = build if isinstance(build, dict) else health

    if not config.pool_token:
        overview["pool"] = f"{POOL_TOKEN_ENV} is unset, so the queue cannot be read"
        return overview
    try:
        pool = config.broker().pool()
    except BrokerError as exc:
        overview["pool"] = f"unavailable: {exc}"
        return overview
    overview["queue_depth"] = pool.get("queue_depth")
    overview["devices"] = [
        {
            "name": device.get("name"),
            "status": device.get("status"),
            "available_slots": device.get("available_slots"),
            "agent_online": device.get("agent_online"),
        }
        for device in pool.get("devices", [])
        if isinstance(device, dict)
    ]
    overview["stalled_reason"] = pool.get("stalled_reason")
    return overview


def status_report(
    config: AgentConfig,
    repository: str,
    ref: str = "main",
) -> dict[str, Any]:
    """The broker, the pool, and one repository's readiness with its next action."""
    policy, policy_path, policy_error = load_policy(config.policy_path)
    report = broker_overview(config)
    if policy_error:
        report["policy_error"] = policy_error
    report["repository"] = read_repository_state(
        repository,
        github=config.github(),
        policy=policy,
        policy_path=policy_path,
        policy_error=policy_error,
        ref=ref,
        webhook_url=config.webhook_url,
    ).as_dict()
    return report


def explain_report(
    config: AgentConfig,
    repository: str,
    ref: str = "main",
    visibility: str = "private",
    quota_status: str = "unknown",
    estimated_minutes: float = 10.0,
) -> dict[str, Any]:
    """What the policy would choose for a ref, without starting anything.

    This is the read-only half of the surface: a caller learns where work would go
    and why, and still has no way to say where it should go.
    """
    policy, policy_path, policy_error = load_policy(config.policy_path)
    state = read_repository_state(
        repository,
        github=None,
        policy=policy,
        policy_path=policy_path,
        policy_error=policy_error,
        ref=ref,
    )
    report: dict[str, Any] = {
        "repository": repository,
        "ref": ref,
        "state": state.as_dict(),
    }
    if not state.ready:
        # asking the broker to route a repository the policy would refuse answers a
        # question about the policy, not about placement, so the refusal stands alone
        report["decision"] = None
        return report
    try:
        decision = config.broker().route(
            auto_route_request(
                repository_visibility=visibility,
                quota_status=quota_status,
                estimated_minutes=estimated_minutes,
            )
        )
        report["decision"] = {**decision, "lane_risk": _lane_risk(decision)}
    except BrokerError as exc:
        report["decision"] = None
        report["error"] = str(exc)
    return report


def _lane_risk(decision: Mapping[str, Any]) -> str:
    """What the resolved lane means for the code the run will execute.

    A ``self-hosted`` label is the whole difference between a run on GitHub's
    runner and a run on hardware a person owns, and a caller that asked for
    neither cannot tell the two apart from the lane name alone.
    """
    runs_on = decision.get("runs_on") or ()
    if "self-hosted" in runs_on:
        return (
            "self-hosted: the run executes on a maintainer's own device, with that "
            "device's network and credentials"
        )
    return "hosted: the run executes on a GitHub runner, not on Cindral hardware"


def run_report(
    config: AgentConfig,
    repository: str,
    ref: str = "main",
    workflow: str = ADAPTER_WORKFLOW,
    visibility: str = "private",
) -> dict[str, Any]:
    """Start a run through the broker and report where it decided to put it.

    The response carries no run identity, because GitHub's dispatch answers 204
    and the broker does not read the run back. A caller that needs the outcome
    reads the check run for the workflow it dispatched, which is the
    ``cindral-dispatch`` check run for the default adapter, on the ref. That run
    lands a moment after the dispatch returns rather than with it, and it is the
    only signal a dispatch produces: the device pool posts a commit status of
    its own on a separate path, which a dispatch never triggers.
    """
    report: dict[str, Any] = {
        "repository": repository,
        "ref": ref,
        "workflow": workflow,
        # named rather than implied, so a reader can tell which assumption
        # produced the lane below without inferring it from the decision
        "quota_assumption": HOSTED_QUOTA_ASSUMPTION,
    }
    try:
        decision = config.broker().dispatch(
            repository,
            workflow,
            ref,
            auto_route_request(
                repository_visibility=visibility,
                quota_status=HOSTED_QUOTA_ASSUMPTION,
            ),
        )
    except BrokerError as exc:
        report["dispatched"] = False
        report["error"] = str(exc)
        return report
    return {**report, **decision, "lane_risk": _lane_risk(decision)}
