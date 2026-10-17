"""HTTP client for the broker's operator surfaces, for callers outside the cluster.

The broker answers `/healthz`, `/v1/route`, `/v1/dispatch` and `/v1/pool`, but
reaching them means hand-writing a bearer-authenticated request. This wraps them
so a CLI or an MCP tool can ask the broker a question instead of reading the
repository to infer the answer.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

DEFAULT_BROKER_URL = "http://127.0.0.1:8095"
REQUEST_TIMEOUT_SECONDS = 15


def _is_loopback(host: str) -> bool:
    """Whether a URL's host is this machine, where plaintext does not leave it.

    Loopback is not a network: the bytes never touch a wire, so refusing it would
    forbid the default configuration for no gain.
    """
    name = urlsplit(host).hostname or ""
    return name in {"localhost", "127.0.0.1", "::1"} or name.startswith("127.")


class BrokerError(RuntimeError):
    """A broker request that did not produce a decision, with the reason attached.

    The broker's own error text is the useful part: it names the refused
    dimension, a missing token, or an exhausted pool. Replacing it with a generic
    message would send the caller back to reading the source.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class BrokerClient:
    """One broker, and the tokens this caller is allowed to present.

    ``dispatch_token`` and ``pool_token`` are separate credentials the broker
    refuses to accept interchangeably, so they are separate fields rather than one
    ``token``. A caller holding only the pool token must not be able to dispatch by
    falling back to it.
    """

    url: str = DEFAULT_BROKER_URL
    dispatch_token: str | None = None
    pool_token: str | None = None

    def _call(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        token: str | None = None,
    ) -> Any:
        if token and not self.url.startswith("https://") and not _is_loopback(self.url):
            # The dispatch token authorises a dispatch to any repository in the
            # allowlist, so sending it in cleartext hands that to whoever is on
            # the path. Loopback is exempt because the bytes never leave the
            # machine, which is the default configuration.
            raise BrokerError(
                f"refusing to send a broker token to {self.url} in cleartext:"
                " use https, or point CINDRAL_BROKER_URL at loopback"
            )
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(f"{self.url.rstrip('/')}{path}", data=body, method=method)
        request.add_header("Accept", "application/json")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                raw = response.read()
        except HTTPError as exc:
            raise BrokerError(self._reason(exc), status=exc.code) from exc
        except URLError as exc:
            raise BrokerError(f"cannot reach the broker at {self.url}: {exc.reason}") from exc
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BrokerError("the broker returned a response that is not JSON") from exc

    @staticmethod
    def _reason(exc: HTTPError) -> str:
        try:
            payload = json.loads(exc.read())
        except (ValueError, OSError):
            return f"the broker refused the request with {exc.code}"
        detail = payload.get("error") if isinstance(payload, dict) else None
        return str(detail) if detail else f"the broker refused the request with {exc.code}"

    def health(self) -> dict[str, Any]:
        return self._call("GET", "/healthz") or {}

    def pool(self) -> dict[str, Any]:
        """The queue and device snapshot, which needs the read-only pool token."""
        if not self.pool_token:
            raise BrokerError("CINDRAL_POOL_TOKEN is unset, so the pool snapshot cannot be read")
        return self._call("GET", "/v1/pool", token=self.pool_token) or {}

    def route(self, request_body: Mapping[str, Any]) -> dict[str, Any]:
        """Ask what the policy would choose, without starting anything."""
        return self._call("POST", "/v1/route", request_body) or {}

    def dispatch(
        self,
        repository: str,
        workflow: str,
        ref: str,
        request_body: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Start a run through the broker, which chooses the lane.

        The broker answers with its routing decision and no run identity, so the
        caller learns where the work went but not which run to poll.
        """
        if not self.dispatch_token:
            raise BrokerError("CINDRAL_DISPATCH_TOKEN is unset, so nothing can be dispatched")
        return (
            self._call(
                "POST",
                "/v1/dispatch",
                {**request_body, "repository": repository, "workflow": workflow, "ref": ref},
                token=self.dispatch_token,
            )
            or {}
        )


def auto_route_request(
    repository_visibility: str = "private",
    quota_status: str = "unknown",
    estimated_minutes: float = 10.0,
) -> dict[str, Any]:
    """The request body that asks the broker to decide, with no placement given.

    ``requested_lane`` stays ``auto`` and ``target`` stays unset on purpose. A
    client that lets the caller set either is re-implementing the policy engine in
    a place that cannot see live capacity.
    """
    return {
        "requested_lane": "auto",
        "target": None,
        "repository_visibility": repository_visibility,
        "quota_status": quota_status,
        "estimated_minutes": estimated_minutes,
    }
