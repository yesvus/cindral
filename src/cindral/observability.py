"""Pool snapshot and metrics endpoints for the control panel and scrapers."""
import hmac
from typing import Any

from .pool import render_metrics

POOL_PATH = "/v1/pool"
METRICS_PATH = "/metrics"


class ObservabilityMixin:
    """Handlers for the read-only pool and metrics surfaces.

    ``/v1/pool`` takes the read-only pool token, or the agent token for an
    operator running the broker locally. The dispatch token is refused: it is a
    different capability and must not widen into queue visibility.
    ``/metrics`` is unauthenticated for Prometheus but stays same-origin, so a
    page a scraper visits cannot read pool state through the browser.
    """

    server: Any

    def _pool_authorized(self) -> bool:
        expected = self.server.pool_token
        if not expected:
            # no pool token configured means the snapshot is refused rather
            # than open, so an unset secret cannot silently expose it
            return False
        header = self.headers.get("Authorization", "")  # type: ignore[attr-defined]
        prefix = "Bearer "
        if not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix):].strip(), expected)

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
