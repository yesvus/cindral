"""Pool snapshot and metrics endpoints for the control panel and scrapers."""
from typing import Any

from .build_info import current_build
from .budget import budget_snapshot
from .pool import render_metrics, store_metrics, store_snapshot

POOL_PATH = "/v1/pool"
METRICS_PATH = "/metrics"


def health_payload(
    app_auth: dict[str, Any] | None,
    fingerprint: str = "",
    policy: Any = None,
) -> dict[str, Any]:
    """What /healthz reports: the build, and whether the App can still deliver.

    The App is how every push reaches the broker, so a broker that is up and
    serving but cannot authenticate delivers nothing while reporting a healthy
    status. That is the one condition a probe cannot be allowed to read as fine,
    so an App that is not usable degrades the status rather than being omitted.
    """
    body: dict[str, Any] = {"status": "ok", "build": current_build().as_dict()}
    metered = budget_snapshot(policy) if policy is not None else None
    if metered is not None:
        # read on a probe, not only in a refusal: a ceiling that can be seen only
        # once it refuses is a ceiling whose state is known only after it has cost
        # something
        body["metered"] = metered
    if fingerprint:
        # the digest of the policy this process loaded, so a broker still running
        # an old ConfigMap is distinguishable from a current one by a probe. Without
        # it the only way to tell is to read the pod, and a policy change produces
        # no other signal: the pod stays healthy and requests are decided exactly as
        # they were an hour ago.
        body["policy"] = {"fingerprint": fingerprint}
    if app_auth is not None:
        # named for what it is: the payload beside it carries build, policy and
        # metered, and a bare "app" next to "policy" does not say whether it means
        # the GitHub App or the application
        body["app_auth"] = app_auth
        if not app_auth.get("ok", False):
            body["status"] = "degraded"
    return body


class ObservabilityMixin:
    """Handlers for the read-only pool and metrics surfaces.

    ``/v1/pool`` takes the read-only pool token. The dispatch and agent tokens
    are refused: they are different capabilities and must not widen into queue
    visibility.
    ``/metrics`` is unauthenticated for Prometheus but stays same-origin, so a
    page a scraper visits cannot read pool state through the browser.
    """

    server: Any

    def _pool_authorized(self) -> bool:
        return self._token_authorized(self.server.pool_token)  # type: ignore[attr-defined]

    def do_OPTIONS(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] != POOL_PATH:  # type: ignore[attr-defined]
            self.send_response(405)  # type: ignore[attr-defined]
            self.send_header("Content-Length", "0")  # type: ignore[attr-defined]
            self.end_headers()  # type: ignore[attr-defined]
            return
        self.send_response(204)  # type: ignore[attr-defined]
        self.send_header("Access-Control-Allow-Origin", "*")  # type: ignore[attr-defined]
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")  # type: ignore[attr-defined]
        self.send_header(  # type: ignore[attr-defined]
            "Access-Control-Allow-Headers", "Content-Type, Authorization"
        )
        self.end_headers()  # type: ignore[attr-defined]

    def _pool(self) -> None:
        store = self.server.jobs
        if store is None:
            self._send(503, {"error": "job queue is not configured"})  # type: ignore[attr-defined]
            return
        if not self._pool_authorized():
            self._send(  # type: ignore[attr-defined]
                401, {"error": "pool snapshot requires the pool bearer token"}
            )
            return
        snapshot = store_snapshot(store, self.server.runners)  # type: ignore[attr-defined]
        snapshot["policy"] = {"fingerprint": self.server.policy.fingerprint}  # type: ignore[attr-defined]
        metered = budget_snapshot(self.server.policy)  # type: ignore[attr-defined]
        if metered is not None:
            snapshot["metered"] = metered
        self._send(200, snapshot, cors=True)

    def _metrics(self) -> None:
        store = self.server.jobs
        text = (
            render_metrics(self.server.runners, None)
            if store is None
            else store_metrics(store, self.server.runners)
        )
        if self.server.cache is not None:
            text += self.server.cache.metrics()
        self._send_text(200, text)  # type: ignore[attr-defined]
