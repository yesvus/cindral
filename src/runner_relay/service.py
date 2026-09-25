"""HTTP service for routing decisions."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
from typing import Any

from .github import GitHubClient, GitHubDispatchError
from .models import RouteRequest
from .policy import Policy, RouteUnavailable
from .state import load_runners
from .webhook import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    WebhookError,
    parse_push_event,
    verify_signature,
)

WEBHOOK_PATH = "/relay/dispatch"
MAX_BODY = 1 << 20


class RelayServer(ThreadingHTTPServer):
    policy: Policy
    runners: tuple
    github: GitHubClient | None
    webhook_secret: str | None
    dispatch_token: str | None
    allowed_repositories: frozenset[str]
    allowed_branches: frozenset[str]
    workflow_file: str


class RelayHandler(BaseHTTPRequestHandler):
    server: RelayServer

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path == WEBHOOK_PATH:
            self._webhook()
            return
        if self.path not in {"/v1/route", "/v1/dispatch"}:
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_BODY:
            self._send(413, {"error": "payload too large"})
            return
        body = self.rfile.read(length)
        if self.path == "/v1/dispatch" and not self._dispatch_authorized():
            self._send(401, {"error": "dispatch requires a bearer token"})
            return
        try:
            payload: dict[str, Any] = json.loads(body)
            request = RouteRequest.from_dict(payload)
            decision = self.server.policy.choose(request, self.server.runners)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})
            return
        except RouteUnavailable as exc:
            self._send(409, {"error": str(exc)})
            return
        if self.path == "/v1/route":
            self._send(200, decision.as_dict())
            return
        self._dispatch(payload, request, decision)

    def _dispatch(self, payload: dict[str, Any], request: RouteRequest, decision: Any) -> None:
        if self.server.github is None:
            self._send(503, {"error": "GitHub dispatch token is not configured"})
            return
        try:
            repository = str(payload["repository"])
            workflow = str(payload["workflow"])
            ref = str(payload["ref"])
        except KeyError as exc:
            self._send(400, {"error": f"missing dispatch field: {exc.args[0]}"})
            return
        inputs = dict(payload.get("inputs", {}))
        inputs.update(
            {
                "relay_lane": decision.lane,
                "relay_target": request.target or "",
                "relay_reason": decision.reason,
            }
        )
        try:
            self.server.github.dispatch(repository, workflow, ref, inputs)
        except GitHubDispatchError as exc:
            self._send(502, {"error": str(exc)})
            return
        self._send(200, {**decision.as_dict(), "dispatched": True})

    def _dispatch_authorized(self) -> bool:
        expected = self.server.dispatch_token
        if not expected:
            # no token configured means dispatch is refused rather than open,
            # so an unset secret cannot silently expose the endpoint
            return False
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix):].strip(), expected)

    def _webhook(self) -> None:
        secret = self.server.webhook_secret
        if not secret:
            self._send(503, {"error": "webhook secret is not configured"})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_BODY:
            self._send(413, {"error": "payload too large"})
            return
        body = self.rfile.read(length)
        if not verify_signature(secret, body, self.headers.get(SIGNATURE_HEADER)):
            self._send(401, {"error": "signature verification failed"})
            return
        if self.headers.get(EVENT_HEADER) != "push":
            self._send(202, {"status": "ignored", "reason": "event is not push"})
            return
        try:
            event = parse_push_event(body)
        except WebhookError as exc:
            self._send(400, {"error": str(exc)})
            return
        if event.repository not in self.server.allowed_repositories:
            self._send(403, {"error": f"repository is not allowed: {event.repository}"})
            return
        branch = event.branch
        if not branch or branch not in self.server.allowed_branches:
            self._send(202, {"status": "ignored", "reason": f"branch not dispatched: {branch or event.ref}"})
            return
        if self.server.github is None:
            self._send(503, {"error": "GitHub dispatch token is not configured"})
            return
        # quota is deliberately unknown: only the caller knows the remaining
        # minutes, and the policy default for unknown quota is the local lane.
        # visibility comes from the payload, so public repositories still route
        # to hosted runners.
        request = RouteRequest(
            repository_visibility="private" if event.private else "public",
            quota_status="unknown",
        )
        try:
            decision = self.server.policy.choose(request, self.server.runners)
        except RouteUnavailable as exc:
            self._send(409, {"error": str(exc)})
            return
        inputs = {
            "relay_lane": decision.lane,
            "relay_target": request.target or "",
            "relay_reason": decision.reason,
        }
        try:
            self.server.github.dispatch(event.repository, self.server.workflow_file, branch, inputs)
        except GitHubDispatchError as exc:
            self._send(502, {"error": str(exc)})
            return
        self._send(
            200,
            {
                **decision.as_dict(),
                "dispatched": True,
                "repository": event.repository,
                "branch": branch,
                "delivery": self.headers.get(DELIVERY_HEADER, ""),
            },
        )

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(policy_path: str | Path, state_path: str | Path, host: str, port: int, github_token: str | None = None) -> None:
    server = RelayServer((host, port), RelayHandler)
    server.policy = Policy.load(policy_path)
    server.runners = load_runners(state_path)
    server.github = GitHubClient(github_token) if github_token else None
    server.webhook_secret = os.environ.get("RELAY_WEBHOOK_SECRET") or None
    server.dispatch_token = os.environ.get("RELAY_DISPATCH_TOKEN") or None
    server.allowed_repositories = _split_env("RELAY_ALLOWED_REPOSITORIES")
    server.allowed_branches = _split_env("RELAY_ALLOWED_BRANCHES") or frozenset({"main"})
    server.workflow_file = os.environ.get("RELAY_WORKFLOW_FILE") or "relay-dispatch.yml"
    if not server.webhook_secret:
        # refuse loudly at startup rather than serving a path that 503s later
        print("warning: RELAY_WEBHOOK_SECRET is unset, /relay/dispatch will refuse every delivery")
    if not server.dispatch_token:
        print("warning: RELAY_DISPATCH_TOKEN is unset, /v1/dispatch will refuse every request")
    if not server.allowed_repositories:
        print("warning: RELAY_ALLOWED_REPOSITORIES is unset, /relay/dispatch will refuse every repository")
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _split_env(name: str) -> frozenset[str]:
    return frozenset(item.strip() for item in os.environ.get(name, "").split(",") if item.strip())
