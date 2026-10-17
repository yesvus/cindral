"""Tests for the broker's startup configuration warnings."""

from __future__ import annotations

import contextlib
import inspect
import io
import types
import unittest

from cindral import service
from cindral.startup import warn_on_missing_configuration


def _server(**overrides: object) -> types.SimpleNamespace:
    configured = {
        "webhook_secret": "secret",
        "deliveries": object(),
        "dispatch_token": "token",
        "github": None,
        "jobs": None,
        "agent_token": "agent",
        # set, not omitted: the broker carries it, so a stub that leaves it out
        # would be testing a server that cannot exist. getattr in the warning
        # would hide exactly that class of gap.
        "agent_tokens": None,
        "pool_token": "pool",
        "status_target_url": "https://cindral.example/run",
        "policy": types.SimpleNamespace(dispatch_targets=[]),
    }
    configured.update(overrides)
    return types.SimpleNamespace(**configured)


def _warnings(server: types.SimpleNamespace) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        warn_on_missing_configuration(server)
    return buffer.getvalue()


class WarnOnMissingConfigurationTest(unittest.TestCase):
    def test_a_bare_startup_names_every_refusing_path(self) -> None:
        out = _warnings(
            _server(
                webhook_secret=None,
                deliveries=None,
                dispatch_token=None,
                github=object(),
                jobs=object(),
                agent_token=None,
                pool_token=None,
                status_target_url="",
                policy=types.SimpleNamespace(
                    dispatch_targets=[types.SimpleNamespace(executes_on_device=False)]
                ),
            )
        )
        for name in (
            "CINDRAL_WEBHOOK_SECRET",
            "CINDRAL_DISPATCH_TOKEN",
            "CINDRAL_AGENT_TOKEN",
            "CINDRAL_POOL_TOKEN",
            "CINDRAL_STATUS_TARGET_URL",
            "mode device or both",
        ):
            self.assertIn(name, out)

    def test_a_fully_configured_broker_is_quiet(self) -> None:
        self.assertEqual(_warnings(_server()), "")

    def test_a_shared_token_beside_per_device_tokens_is_called_out(self) -> None:
        """The configuration where the hole is open but reads as closed.

        With a per-device map every device-facing path checks the credential against
        the named device, so the shared value no longer speaks for anyone. That is
        the end state, and saying so at startup is what turns a silent overlap into
        a line an operator can act on.
        """
        out = _warnings(
            _server(jobs=object(), agent_token="shared", agent_tokens={"desktop": "own"})
        )
        self.assertIn("CINDRAL_AGENT_TOKEN", out)
        self.assertIn("CINDRAL_AGENT_TOKENS", out)
        self.assertIn("Remove it once every device holds its own", out)

    def test_per_device_tokens_alone_do_not_warn(self) -> None:
        out = _warnings(
            _server(jobs=object(), agent_token=None, agent_tokens={"desktop": "own"})
        )
        self.assertNotIn("CINDRAL_AGENT_TOKEN is set alongside", out)

    def test_a_broker_without_a_job_store_does_not_blame_the_agent_tokens(self) -> None:
        self.assertEqual(_warnings(_server(jobs=None, agent_token=None, pool_token=None)), "")

    def test_a_missing_dispatch_target_is_named_only_when_github_is_wired(self) -> None:
        without_github = _server(policy=types.SimpleNamespace(dispatch_targets=[]))
        with_github = _server(github=object(), policy=types.SimpleNamespace(dispatch_targets=[]))
        self.assertNotIn("no [dispatch] repository is configured", _warnings(without_github))
        self.assertIn("no [dispatch] repository is configured", _warnings(with_github))

    def test_signed_pushes_without_a_jobs_store_warn_about_the_store(self) -> None:
        out = _warnings(_server(deliveries=None))
        self.assertIn("CINDRAL_JOBS_DB is unset", out)

    def test_a_pool_without_a_device_repository_warns_about_the_pool(self) -> None:
        target = types.SimpleNamespace(executes_on_device=False)
        out = _warnings(_server(jobs=object(), policy=types.SimpleNamespace(dispatch_targets=[target])))
        self.assertIn("the device pool will refuse every delivery", out)

    def test_serve_still_runs_the_warnings_before_serving(self) -> None:
        # A moved-but-orphaned helper is how this repo lost a fix three times, so the
        # call site is pinned even though the extraction changes no behaviour. The
        # order is the point: after serve_forever the warning never reaches an
        # operator, and a presence check alone would not notice.
        source = inspect.getsource(service.serve)
        self.assertIn("warn_on_missing_configuration(server)", source)
        self.assertLess(
            source.index("warn_on_missing_configuration(server)"),
            source.index("serve_forever()"),
        )


if __name__ == "__main__":
    unittest.main()
