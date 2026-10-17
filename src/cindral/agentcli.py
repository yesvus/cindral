"""Agent-facing subcommands: status, run, explain, and the MCP server that wraps them.

These exist so a caller asks the broker instead of reading the target repository
and inferring what Cindral would do. Placement stays the broker's decision, so
nothing here accepts a lane, a target, or a runner name.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Callable, Mapping

from .agentapi import AgentConfig, explain_report, run_report, status_report
from .repositorystate import ADAPTER_WORKFLOW

MCP_HELP = "serve the same operations to an agent over MCP on stdio"


def _emit(payload: Mapping[str, Any], as_json: bool, stream: Any = None) -> None:
    # resolved per call rather than as a default argument, so a caller that
    # redirects sys.stdout after import still captures the output
    out = sys.stdout if stream is None else stream
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True), file=out)
        return
    for key, value in payload.items():
        if isinstance(value, (dict, list)):
            value = json.dumps(value, sort_keys=True)
        print(f"{key}: {value}", file=out)


def command_status(
    config: AgentConfig,
    repository: str | None = None,
    ref: str = "main",
    as_json: bool = False,
) -> int:
    """Report the broker and pool, and one repository's readiness when named."""
    from .agentapi import broker_overview

    payload: dict[str, Any] = broker_overview(config)
    policy_path = config.policy_path
    if repository is None:
        if not payload.get("reachable", False):
            # a status check that cannot reach the broker is a failure, not a
            # neutral report, so a caller scripting this does not read it as healthy
            _emit(payload, as_json, sys.stderr)
            return 1
        _emit(payload, as_json)
        return 0

    report = status_report(config, repository, ref)
    payload.update({key: value for key, value in report.items() if key not in payload})
    _emit(payload, as_json)
    state = report["repository"]
    return 0 if state["ready"] else 1


def command_run(
    config: AgentConfig,
    repository: str,
    ref: str = "main",
    workflow: str = ADAPTER_WORKFLOW,
    visibility: str = "private",
    as_json: bool = False,
) -> int:
    """Ask the broker to start a run, and report where it decided to put it."""
    report = run_report(config, repository, ref, workflow, visibility)
    dispatched = bool(report.get("dispatched"))
    _emit(report, as_json, sys.stdout if dispatched else sys.stderr)
    if dispatched:
        print(
            "the broker reports no run identity, read the cindral-dispatch check run on this ref",
            file=sys.stderr,
        )
    return 0 if dispatched else 1


def command_explain(
    config: AgentConfig,
    repository: str,
    ref: str = "main",
    visibility: str = "private",
    quota_status: str = "unknown",
    estimated_minutes: float = 10.0,
    as_json: bool = False,
) -> int:
    """Report the lane the policy would choose, without starting anything."""
    report = explain_report(config, repository, ref, visibility, quota_status, estimated_minutes)
    _emit(report, as_json)
    return 0 if report.get("decision") else 1


def add_agent_subcommands(subparsers: Any) -> None:
    """Register the agent-facing subcommands on the root parser."""

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--json", action="store_true", help="print one JSON object instead of lines")
        command.set_defaults(agent_command=True)
        return command

    status = add("status", "report broker, pool, and repository readiness")
    status.add_argument("--repo", help="repository in owner/name format")
    status.add_argument("--ref", default="main", help="ref to check the policy against, default main")

    run = add("run", "ask the broker to run a ref, and report the lane it chose")
    run.add_argument("repository", help="repository in owner/name format")
    run.add_argument("--ref", default="main", help="branch to run, default main")
    run.add_argument("--workflow", default=ADAPTER_WORKFLOW, help="workflow to dispatch, default the adapter")
    run.add_argument(
        "--visibility",
        default="private",
        choices=["private", "public"],
        help="repository visibility, which the policy reads, default private",
    )

    explain = add("explain", "report the lane the policy would choose, without running anything")
    explain.add_argument("repository", help="repository in owner/name format")
    explain.add_argument("--ref", default="main", help="branch to explain, default main")
    explain.add_argument(
        "--visibility",
        default="private",
        choices=["private", "public"],
        help="repository visibility, which the policy reads, default private",
    )
    explain.add_argument(
        "--quota",
        default="unknown",
        help="quota status the policy should assume, default unknown",
    )
    explain.add_argument(
        "--minutes",
        type=float,
        default=10.0,
        help="estimated run minutes, which the policy reads, default 10",
    )

    mcp = add("mcp", MCP_HELP)
    mcp.set_defaults(mcp=True)


def parse_cindral_args(parser: argparse.ArgumentParser, subparsers: Any) -> argparse.Namespace:
    """Register the agent subcommands, parse the root arguments, and run one if chosen.

    Registration and dispatch sit together here so the root parser holds a single
    call, and an agent subcommand cannot be registered without a way to run it.
    A non-agent invocation is returned for the root parser to handle as usual.
    """
    add_agent_subcommands(subparsers)
    args = parser.parse_args()
    if getattr(args, "agent_command", False):
        raise SystemExit(dispatch_agent_command(AgentConfig.from_env(), args))
    return args


def dispatch_agent_command(config: AgentConfig, args: argparse.Namespace) -> int:
    """Run the agent-facing subcommand the parser selected."""
    if getattr(args, "mcp", False):
        from .mcpserver import serve_stdio

        return serve_stdio(config)
    if args.command == "status":
        return command_status(config, args.repo, args.ref, args.json)
    if args.command == "run":
        return command_run(config, args.repository, args.ref, args.workflow, args.visibility, args.json)
    return command_explain(
        config,
        args.repository,
        args.ref,
        args.visibility,
        args.quota,
        args.minutes,
        args.json,
    )
