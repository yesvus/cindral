"""Pool snapshot and metrics endpoints for the control panel and scrapers."""
from typing import Any

from .pool import render_metrics

POOL_PATH = "/v1/pool"
METRICS_PATH = "/metrics"


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
        self._send(200, store.pool_snapshot(self.server.runners), cors=True)  # type: ignore[attr-defined]

    def _metrics(self) -> None:
        store = self.server.jobs
        text = (
            render_metrics(self.server.runners, None)
            if store is None
            else store.metrics(self.server.runners)
        )
        self._send_text(200, text)  # type: ignore[attr-defined]
