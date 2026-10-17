"""MCP stdio server exposing the agent-facing operations as tools.

Written against the stdlib rather than an SDK because the surface is three tools
over HTTP the broker already serves. The tools take a repository and a ref, never
a lane or a target, so an agent can learn where Cindral runs something and still
cannot tell it where to run it.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Callable, Mapping, TextIO

from .agentapi import AgentConfig, explain_report, run_report, status_report
from .brokerclient import BrokerError
from .build_info import current_build
from .repositorystate import ADAPTER_WORKFLOW

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "cindral"
INSTRUCTIONS = (
    "Cindral routes CI across GitHub-hosted runners and a device pool. Call "
    "cindral_status to see whether a repository is wired and what the pool is "
    "carrying, cindral_explain to learn which lane a ref would take and why, and "
    "cindral_run to start it. Placement is Cindral's decision and none of these "
    "tools accepts a lane, a target, or a runner, so read cindral_explain rather "
    "than trying to choose. cindral_run and cindral_explain do take the "
    "repository's visibility, which steers the lane rather than naming one: a "
    "public repository is hosted before the quota rules are consulted, so "
    "stating it overrides the local fallback. A dispatch reports the lane it "
    "chose and returns no "
    "run identity, so the outcome is the cindral-dispatch check run GitHub "
    "creates for the workflow it started, on the ref you asked for."
)

TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "cindral_status",
        "description": (
            "Report Cindral's state for a repository: whether the dispatch adapter and the push "
            "webhook are in place, whether the policy permits a dispatch on the ref, the live "
            "queue depth and device slots, and the single next action when something is missing. "
            "Read-only. Call this before cindral_run."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repository": {"type": "string", "description": "repository in owner/name format"},
                "ref": {"type": "string", "description": "branch to report on, default main"},
            },
            "required": ["repository"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "cindral_explain",
        "description": (
            "Report which lane a ref would run on and why, without starting anything. This is how "
            "to answer where Cindral places work. Read-only."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repository": {"type": "string", "description": "repository in owner/name format"},
                "ref": {"type": "string", "description": "branch to explain, default main"},
                "visibility": {
                    "type": "string",
                    "enum": ["private", "public"],
                    "description": "repository visibility, which the policy reads, default private",
                },
                "quota_status": {
                    "type": "string",
                    "description": "quota status the policy should assume, default unknown",
                },
                "estimated_minutes": {
                    "type": "number",
                    "description": "estimated run minutes, which the policy reads, default 10",
                },
            },
            "required": ["repository"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    {
        "name": "cindral_run",
        "description": (
            "Ask the broker to run a ref now, and report the lane, runner labels, reason and "
            "capacity it decided on. Takes no lane or target, because placement is the broker's "
            "decision. Returns no run identity: with the default workflow the outcome is the "
            "cindral-dispatch check run on the ref, and if you pass another workflow it is that "
            "workflow's own check run instead. Either way the check run is created a moment after "
            "this call returns, because workflow_dispatch answers immediately without one, so an "
            "immediate poll finds nothing and a refused dispatch never gets one at all."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repository": {"type": "string", "description": "repository in owner/name format"},
                "ref": {"type": "string", "description": "branch to run, default main"},
                "workflow": {
                    "type": "string",
                    "description": f"workflow to dispatch, default {ADAPTER_WORKFLOW}",
                },
                "visibility": {
                    "type": "string",
                    "enum": ["private", "public"],
                    "description": "repository visibility, which the policy reads, default private",
                },
            },
            "required": ["repository"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": False, "openWorldHint": False},
    },
)


def _minutes(args: Mapping[str, Any]) -> float:
    """The caller's estimate as a float, refusing what is not a number.

    The schema says number, and every other shape that reaches here is a caller
    mistake rather than a number to be charitable about: JSON null, a numeric
    string, a list. A TypeError or a float() message from the bottom of the stack
    would end the session rather than answer the call, so the refusal is raised as
    the ValueError that `call` already turns into a JSON-RPC error naming the
    argument, for that one call.
    """
    value = args.get("estimated_minutes", 10.0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("estimated_minutes must be a number")
    return float(value)


HANDLERS: Mapping[str, Callable[[AgentConfig, Mapping[str, Any]], dict[str, Any]]] = {
    "cindral_status": lambda config, args: status_report(
        config, str(args["repository"]), str(args.get("ref", "main"))
    ),
    "cindral_explain": lambda config, args: explain_report(
        config,
        str(args["repository"]),
        str(args.get("ref", "main")),
        str(args.get("visibility", "private")),
        str(args.get("quota_status", "unknown")),
        _minutes(args),
    ),
    "cindral_run": lambda config, args: run_report(
        config,
        str(args["repository"]),
        str(args.get("ref", "main")),
        str(args.get("workflow", ADAPTER_WORKFLOW)),
        str(args.get("visibility", "private")),
    ),
}


class McpSession:
    """One stdio JSON-RPC conversation with an agent host."""

    def __init__(self, config: AgentConfig, stdin: TextIO, stdout: TextIO) -> None:
        self.config = config
        self.stdin = stdin
        self.stdout = stdout

    def serve(self) -> int:
        for line in self.stdin:
            if not line.strip():
                continue
            # an agent host that loses the process mid-conversation cannot retry,
            # and -32603 is the code the protocol has for a fault in the server
            try:
                self.handle(line)
            except Exception as exc:  # noqa: BLE001 - one bad call must not end the session
                self.fail(None, -32603, f"internal error handling the request: {exc}")
        return 0

    def handle(self, line: str) -> None:
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            self.fail(None, -32700, "the request is not JSON")
            return
        if not isinstance(request, dict):
            self.fail(None, -32600, "the request is not an object")
            return
        method = request.get("method")
        request_id = request.get("id")
        if "id" not in request:
            # A request without an id is a notification, and JSON-RPC says a
            # notification gets no response. Answering one anyway desynchronises
            # a host that matches replies by id: our unsolicited reply is read as
            # the answer to whatever it sent next.
            return
        if method == "initialize":
            self.reply(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": current_build().version},
                    "instructions": INSTRUCTIONS,
                },
            )
        elif method in {"notifications/initialized", "notifications/cancelled"}:
            return
        elif method == "tools/list":
            self.reply(request_id, {"tools": [dict(tool) for tool in TOOLS]})
        elif method == "tools/call":
            self.call(request_id, request.get("params") or {})
        else:
            self.fail(request_id, -32601, f"unknown method: {method}")

    def call(self, request_id: Any, params: Mapping[str, Any]) -> None:
        handler = HANDLERS.get(str(params.get("name")))
        if handler is None:
            self.fail(request_id, -32602, f"unknown tool: {params.get('name')}")
            return
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            self.fail(request_id, -32602, "arguments must be an object")
            return
        try:
            report = handler(self.config, arguments)
        except KeyError as exc:
            self.fail(request_id, -32602, f"missing argument: {exc.args[0]}")
            return
        except (ValueError, BrokerError) as exc:
            report = {"error": str(exc)}
        self.reply(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(report, indent=2, sort_keys=True)}],
                # a refused run is a result the agent should read, not a broken call,
                # so isError marks the failure without hiding the payload
                "isError": bool(report.get("error")),
            },
        )

    def reply(self, request_id: Any, result: Mapping[str, Any]) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "result": dict(result)})

    def fail(self, request_id: Any, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    def send(self, payload: Mapping[str, Any]) -> None:
        self.stdout.write(json.dumps(payload) + "\n")
        self.stdout.flush()


def serve_stdio(config: AgentConfig, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    return McpSession(config, stdin or sys.stdin, stdout or sys.stdout).serve()
