"""The App's block on `/healthz`, and the composition that produces it.

Kept apart from the App auth tests because the subject is not authentication: it is
what a probe can say about the installation, and who is responsible for saying it.
That responsibility is the point. `serve()` resolves the repository coverage, hands
it to the summary, and puts the result on the probe; the suite builds
`CindralServer` directly and never calls `serve()`, so that wiring can be wrong with
every other test green. Which is how the coverage count came to be computed, printed
to the log, and never reported anywhere an operator looks.
"""
import pathlib
import unittest

from cindral.app_auth import AppAuth, probe_block, startup_report
from cindral.observability import health_payload

from test_app_auth import FakeTransport, generate_key

CLIENT_ID = "Iv1.cindralapp"
REPOS = [{"full_name": f"example-org/repo-{n}"} for n in range(7)]


def auth() -> AppAuth:
    """An App whose installation and repository list the shared transport answers."""
    return AppAuth(
        CLIENT_ID, generate_key(), now=lambda: 1000.0, transport=FakeTransport(repositories=REPOS)
    )


class ProbeBlockTest(unittest.TestCase):
    def test_the_block_carries_the_coverage_startup_already_resolved(self) -> None:
        """The composition startup performs, reachable without starting a broker."""
        block = probe_block(auth())
        self.assertTrue(block["ok"])
        covered = len(auth().repositories())
        self.assertEqual(block["repositories"], covered)
        # and the probe reads it straight through, unaltered
        self.assertEqual(health_payload(block)["app_auth"]["repositories"], covered)

    def test_no_app_configured_yields_no_block_rather_than_an_empty_one(self) -> None:
        """Absent, not null: a null block reads as a configured App reporting nothing."""
        self.assertIsNone(probe_block(None))
        self.assertNotIn("app_auth", health_payload(None))

    def test_the_count_is_the_callers_and_not_invented(self) -> None:
        block = probe_block(auth())
        self.assertNotEqual(block["repositories"], 0)
        self.assertIsInstance(block["repositories"], int)

    def test_startup_delegates_its_app_wiring_to_the_named_composition(self) -> None:
        """A proxy for a gap worth naming: nothing calls `serve()`.

        The suite builds `CindralServer` by hand, so every line of startup's wiring
        is unexercised. Driving startup needs a socket, the environment and the
        policy, so this asserts the call site instead: startup must reach its App
        block through the named composition, which the tests above do exercise.
        """
        source = (
            pathlib.Path(__file__).resolve().parent.parent / "src" / "cindral" / "service.py"
        ).read_text(encoding="utf-8")
        self.assertIn("server.app = probe_block(app_auth)", source)


class StartupReportTest(unittest.TestCase):
    """The sentence startup prints, exercised rather than restated.

    A test that rebuilds the string grades its own copy: the copy keeps passing
    while the line an operator reads at boot is deleted, shortened, or made to raise
    on the degraded shape. Both mutations survived exactly that way before this was
    a named function, so it calls the one production uses.
    """

    def test_a_healthy_startup_names_the_installation_and_its_coverage(self) -> None:
        self.assertEqual(
            startup_report(
                {"ok": True, "account": "example-org", "installation": 42, "repositories": 7}
            ),
            "authenticating as the example-org App installation 42, covering 7 repositories",
        )

    def test_a_degraded_startup_says_why_rather_than_raising(self) -> None:
        """The shape health() actually returns on failure carries no repositories key.

        Indexing it unconditionally raised KeyError on the one path where somebody
        needs to be told, which is the same silence as printing nothing.
        """
        self.assertEqual(
            startup_report({"ok": False, "error": "installation is suspended"}),
            "the GitHub App is not usable: installation is suspended",
        )

    def test_a_degraded_startup_with_no_reason_still_says_something(self) -> None:
        line = startup_report({"ok": False})
        self.assertIn("not usable", line)
        self.assertIn("no reason given", line)

    def test_startup_prints_the_named_sentence_and_resolves_once(self) -> None:
        source = (
            pathlib.Path(__file__).resolve().parent.parent / "src" / "cindral" / "service.py"
        ).read_text(encoding="utf-8")
        self.assertIn("print(startup_report(server.app), flush=True)", source)
        # one resolution of the installation, not two: the block already holds it,
        # and asking again costs an API call and can disagree with itself
        self.assertNotIn("installation = app_auth.installation()", source)


class HealthPayloadTest(unittest.TestCase):
    def test_a_broken_app_degrades_the_status(self) -> None:
        # the whole point: a 200 with status ok would pass every probe
        body = health_payload({"ok": False, "error": "installation is suspended"})
        self.assertEqual(body["status"], "degraded")
        self.assertIn("suspended", body["app_auth"]["error"])

    def test_a_healthy_app_leaves_the_status_alone(self) -> None:
        body = health_payload({"ok": True, "installation": 42, "account": "example-org"})
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["app_auth"]["installation"], 42)

    def test_the_block_is_named_for_what_it_is(self) -> None:
        """The payload beside it carries build, policy and metered.

        A bare `app` next to `policy` does not say whether it means the GitHub App or
        the application, and the name is what an operator reads when deciding whether
        a broker can still receive anything.
        """
        body = health_payload({"ok": True})
        self.assertIn("app_auth", body)
        self.assertNotIn("app", body)

    def test_the_block_passes_through_exactly_what_the_app_reported(self) -> None:
        """The probe reports the App's own summary and filters nothing.

        A test asserting the token never reaches the probe belongs to
        `AppAuth.health()`, which decides not to include it, and `test_app_auth.py`
        already asserts that. Filtering here too would be a second implementation of
        the rule, and a pass-through that silently drops a field is its own defect.
        """
        reported = {"ok": True, "installation": 42, "token_expires_in_seconds": 1800}
        self.assertEqual(health_payload(reported)["app_auth"], reported)
