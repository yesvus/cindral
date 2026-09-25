"""HTTP service for routing decisions."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any

from .github import GitHubClient, GitHubDispatchError
from .models import RouteRequest
from .policy import Policy, RouteUnavailable
from .state import load_runners


class RelayServer(ThreadingHTTPServer):
    policy: Policy
    runners: tuple
    github: GitHubClient | None


class RelayHandler(BaseHTTPRequestHandler):
    server: RelayServer

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path not in {"/v1/route", "/v1/dispatch"}:
            self._send(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload: dict[str, Any] = json.loads(self.rfile.read(length))
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
    try:
        server.serve_forever()
    finally:
        server.server_close()
