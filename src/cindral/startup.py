"""Startup diagnostics for configuration the broker needs but cannot enforce."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .service import CindralServer


def warn_on_missing_configuration(server: "CindralServer") -> None:
    """Name every unset value that makes a path refuse, before serving on it.

    Each check ties one piece of configuration to the requests it disables, so an
    operator reads the reason in the log instead of inferring it from refusals.
    """
    if not server.webhook_secret:
        # refuse loudly at startup rather than serving a path that 503s later
        print("warning: CINDRAL_WEBHOOK_SECRET is unset, /cindral/dispatch will refuse every delivery")
    if server.webhook_secret and server.deliveries is None:
        print("warning: CINDRAL_JOBS_DB is unset, signed push dispatches will be refused")
    if not server.dispatch_token:
        print("warning: CINDRAL_DISPATCH_TOKEN is unset, /v1/dispatch will refuse every request")
    if server.github is not None and not server.policy.dispatch_targets:
        print("warning: no [dispatch] repository is configured, /v1/dispatch will refuse every target")
    if server.jobs is not None and not any(
        target.executes_on_device for target in server.policy.dispatch_targets
    ):
        print("warning: no [dispatch] repository has mode device or both, the device pool will refuse every delivery")
    if server.jobs is not None and not server.agent_token and not server.agent_tokens:
        print("warning: CINDRAL_JOBS_DB is set but CINDRAL_AGENT_TOKEN is unset, /v1/jobs and /v1/agents will refuse every request")
    if server.jobs is not None and server.agent_tokens and server.agent_token:
        print(
            "warning: CINDRAL_AGENT_TOKEN is set alongside CINDRAL_AGENT_TOKENS. Per-device tokens"
            " take precedence on every device-facing path, so the shared value no longer speaks for"
            " any device. Remove it once every device holds its own."
        )
    if server.jobs is not None and not server.pool_token:
        print("warning: CINDRAL_JOBS_DB is set but CINDRAL_POOL_TOKEN is unset, /v1/pool will refuse every request")
    if not server.status_target_url:
        print("warning: CINDRAL_STATUS_TARGET_URL is unset, direct-execution statuses carry no link to the run")
