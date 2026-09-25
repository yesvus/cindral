"""HTTP service for routing decisions."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any

from .models import RouteRequest
from .policy import Policy, RouteUnavailable
from .state import load_runners


class RelayServer(ThreadingHTTPServer):
    policy: Policy
    runners: tuple


class RelayHandler(BaseHTTPRequestHandler):
    server: RelayServer

    def do_GET(self) -> None:
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/route":
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
        self._send(200, decision.as_dict())

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(policy_path: str | Path, state_path: str | Path, host: str, port: int) -> None:
    server = RelayServer((host, port), RelayHandler)
    server.policy = Policy.load(policy_path)
    server.runners = load_runners(state_path)
    try:
        server.serve_forever()
    finally:
        server.server_close()
