"""HTTP service for routing decisions."""
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from .github import GitHubAPIError, GitHubClient, GitHubDispatchError, RepositoryRunner, is_repository_slug
from .models import RouteDecision, RouteRequest, Runner
from .policy import Policy, RouteUnavailable
from .state import load_runners
from .webhook import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    WebhookError,
    parse_pull_request_event,
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
    workflow_file: str
    capacity_lock: threading.Lock
    runner_reservations: dict[str, dict[int, tuple[float, tuple[str, ...]]]]
    reservation_seconds: int


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
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self._send(400, {"error": str(exc)})
            return
        inputs = payload.get("inputs", {})
        if not isinstance(inputs, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in inputs.items()
        ):
            self._send(400, {"error": "inputs must be a string-to-string object"})
            return
        if self.path == "/v1/route":
            try:
                decision = self.server.policy.choose(request, self.server.runners)
            except RouteUnavailable as exc:
                self._send(409, {"error": str(exc)})
                return
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
        if not is_repository_slug(repository):
            self._send(400, {"error": "invalid repository name"})
            return
        try:
            decision, reservation = self._dispatch_decision(request, repository)
        except GitHubAPIError as exc:
            self._send(502, {"error": str(exc)})
            return
        except RouteUnavailable as exc:
            self._send(409, {"error": str(exc)})
            return
        self._dispatch(repository, workflow, ref, inputs, request, decision, reservation)

    def _dispatch(
        self,
        repository: str,
        workflow: str,
        ref: str,
        inputs: dict[str, str],
        request: RouteRequest,
        decision: RouteDecision,
        reservation: tuple[str, int] | None,
    ) -> None:
        inputs = dict(inputs)
        inputs.update(
            {
                "relay_lane": decision.lane,
                "relay_target": request.target or "",
                "relay_reason": decision.reason,
            }
        )
        if not self._dispatch_to_github(repository, workflow, ref, inputs, reservation):
            return
        self._send(200, {**decision.as_dict(), "dispatched": True})

    def _dispatch_to_github(
        self,
        repository: str,
        workflow: str,
        ref: str,
        inputs: dict[str, str],
        reservation: tuple[str, int] | None,
    ) -> bool:
        dispatched = False
        try:
            self.server.github.dispatch(repository, workflow, ref, inputs)
            dispatched = True
        except GitHubDispatchError as exc:
            self._send(502, {"error": str(exc)})
            return False
        except Exception as exc:
            self.log_error("unexpected GitHub dispatch failure: %s", exc)
            self._send(502, {"error": "GitHub dispatch failed"})
            return False
        finally:
            if not dispatched:
                self._release_reservation(reservation)
        return True

    def _dispatch_decision(
        self,
        request: RouteRequest,
        repository: str,
    ) -> tuple[RouteDecision, tuple[str, int] | None]:
        hosted_decision: RouteDecision | None = None
        try:
            hosted_decision = self.server.policy.choose(request, ())
        except RouteUnavailable:
            pass
        if hosted_decision is not None:
            return hosted_decision, None
        if self.server.github is None:
            raise GitHubAPIError("GitHub runner lookup is not configured")

        repository_runners = self.server.github.list_runners(repository)
        now = time.monotonic()
        with self.server.capacity_lock:
            self._expire_reservations(repository, now)

            local_runners = tuple(
                Runner(
                    name=runner.name,
                    status=runner.status,
                    busy=runner.busy,
                    labels=runner.labels,
                    healthy=runner.status == "online",
                )
                for runner in repository_runners
            )
            decision = self.server.policy.choose(request, local_runners)
            online_idle = sum(runner.status == "online" and not runner.busy for runner in repository_runners)
            label_key = tuple(sorted(decision.runs_on))
            required_labels = set(label_key)
            lane_idle = sum(
                runner.status == "online"
                and not runner.busy
                and required_labels.issubset(set(runner.labels))
                for runner in repository_runners
            )
            reservations = self.server.runner_reservations.get(repository, {})
            reserved = self._overlapping_reservations(
                repository_runners,
                required_labels,
                reservations.values(),
            )
            available = lane_idle - reserved
            if decision.lane != "hosted" and available <= 0:
                raise RouteUnavailable("all eligible repository runners are busy or reserved")
            snapshot = {
                "source": "github_repository_runners",
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "registered": len(repository_runners),
                "online_idle": online_idle,
                "lane_idle": lane_idle,
                "reserved": reserved,
                "available": max(available, 0),
                "runner_candidate": decision.runner,
            }
            decision = replace(decision, capacity=snapshot)
            reservation = None
            if decision.lane != "hosted" and decision.runner:
                reservation_id = time.monotonic_ns()
                reservation = (repository, reservation_id)
                self.server.runner_reservations.setdefault(repository, {})[reservation_id] = (
                    now + self.server.reservation_seconds,
                    label_key,
                )
            return decision, reservation

    def _expire_reservations(self, repository: str, now: float) -> None:
        reservations = self.server.runner_reservations.get(repository, {})
        active = {
            reservation_id: reservation
            for reservation_id, reservation in reservations.items()
            if reservation[0] > now
        }
        if active:
            self.server.runner_reservations[repository] = active
        else:
            self.server.runner_reservations.pop(repository, None)

    def _overlapping_reservations(
        self,
        repository_runners: tuple[RepositoryRunner, ...],
        required_labels: set[str],
        reservations: Iterable[tuple[float, tuple[str, ...]]],
    ) -> int:
        idle_labels = [
            set(runner.labels)
            for runner in repository_runners
            if runner.status == "online" and not runner.busy
        ]
        return sum(
            any(set(reservation_labels).union(required_labels).issubset(labels) for labels in idle_labels)
            for _, reservation_labels in reservations
        )

    def _release_reservation(self, reservation: tuple[str, int] | None) -> None:
        if reservation is None:
            return
        with self.server.capacity_lock:
            repository, reservation_id = reservation
            reservations = self.server.runner_reservations.get(repository, {})
            reservations.pop(reservation_id, None)
            if not reservations:
                self.server.runner_reservations.pop(repository, None)

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
        event_name = self.headers.get(EVENT_HEADER)
        if event_name not in {"push", "pull_request"}:
            self._send(202, {"status": "ignored", "reason": "event is not supported"})
            return
        if event_name == "pull_request":
            self._pull_request_webhook(body)
            return
        try:
            event = parse_push_event(body)
        except WebhookError as exc:
            self._send(400, {"error": str(exc)})
            return
        if not is_repository_slug(event.repository):
            self._send(400, {"error": "invalid repository name"})
            return
        branch = event.branch
        if not branch or branch != event.default_branch:
            self._send(202, {"status": "ignored", "reason": f"branch is not the default branch: {branch or event.ref}"})
            return
        if self.server.github is None:
            self._send(503, {"error": "GitHub dispatch token is not configured"})
            return
        try:
            if not self.server.github.workflow_exists(event.repository, self.server.workflow_file):
                self._send(202, {"status": "ignored", "reason": "repository has not opted in with the relay workflow"})
                return
        except GitHubAPIError as exc:
            self._send(502, {"error": str(exc)})
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
            decision, reservation = self._dispatch_decision(request, event.repository)
        except GitHubAPIError as exc:
            self._send(502, {"error": str(exc)})
            return
        except RouteUnavailable as exc:
            self._send(409, {"error": str(exc)})
            return
        inputs = {
            "relay_lane": decision.lane,
            "relay_target": request.target or "",
            "relay_reason": decision.reason,
        }
        if not self._dispatch_to_github(
            event.repository,
            self.server.workflow_file,
            branch,
            inputs,
            reservation,
        ):
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
        if self.server.github is None:
            self._send(503, {"error": "GitHub dispatch token is not configured"})
            return
        try:
            if not self.server.github.workflow_exists(event.repository, self.server.workflow_file):
                self._send(202, {"status": "ignored", "reason": "repository has not opted in with the relay workflow"})
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
            "relay_lane": decision.lane,
            "relay_target": request.target or "",
            "relay_reason": (
                decision.reason if event.trusted else "untrusted pull request requires hosted execution"
            ),
            "relay_ref": event.ref,
        }
        try:
            self.server.github.dispatch(event.repository, self.server.workflow_file, event.default_branch, inputs)
        except GitHubDispatchError as exc:
            self._send(502, {"error": str(exc)})
            return
        self._send(
            200,
            {
                **decision.as_dict(),
                "dispatched": True,
                "repository": event.repository,
                "pull_request": event.number,
                "ref": event.ref,
                "trusted": event.trusted,
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
    server.workflow_file = os.environ.get("RELAY_WORKFLOW_FILE") or "relay-dispatch.yml"
    server.capacity_lock = threading.Lock()
    server.runner_reservations = {}
    server.reservation_seconds = int(os.environ.get("RELAY_RUNNER_RESERVATION_SECONDS", "10"))
    if server.reservation_seconds < 1:
        raise ValueError("RELAY_RUNNER_RESERVATION_SECONDS must be positive")
    if not server.webhook_secret:
        # refuse loudly at startup rather than serving a path that 503s later
        print("warning: RELAY_WEBHOOK_SECRET is unset, /relay/dispatch will refuse every delivery")
    if not server.dispatch_token:
        print("warning: RELAY_DISPATCH_TOKEN is unset, /v1/dispatch will refuse every request")
    try:
        server.serve_forever()
    finally:
        server.server_close()
