"""The gate has to exercise the interpreter the project declares it supports.

A lower bound nothing runs does not mean anything. The import check provisions the
declared minimum and imports every module there, which catches a module that cannot
even be loaded on the floor; it does not run the tests there, so a call, a match or a
comprehension that behaves differently on the floor is exercised on whatever
interpreter the resolver picked and passes.

That is not theoretical. A checkout whose environment was created against the floor
runs the floor, and a freshly created one resolved 3.14. Both are correct answers to
"which interpreter is available" and only one of them is the one the package promises
to support, so the gate says which it used and repeats the suite on the floor when it
is not already there.

The floor is 3.12 because that is what the image runs and what every container here
runs. It was 3.11, which nothing deployed and which is past security-fix end of life,
so the declaration was a claim about an interpreter the product had never run on. The
repository is private, so raising a floor drops support for interpreters nobody
installs on, and it cannot break the code: every 3.11 feature exists in 3.12.
"""
import pathlib
import sys
import unittest
from unittest.mock import patch

SCRIPTS = pathlib.Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import check  # noqa: E402 - the scripts directory is not a package
from check_imports import FALLBACK_MINIMUM, declared_minimum  # noqa: E402


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


class DeclaredMinimumTest(unittest.TestCase):
    def test_the_gate_reads_the_bound_rather_than_repeating_it(self) -> None:
        """One source of truth: a floor in two places is a floor that disagrees.

        The fallback in check_imports is a second declaration, so it is held to the
        same value rather than left to drift.
        """
        self.assertEqual(check.MINIMUM, declared_minimum())
        self.assertEqual(check.MINIMUM, FALLBACK_MINIMUM)

    def test_the_floor_is_the_interpreter_the_image_runs(self) -> None:
        """A floor nothing deploys is a claim about an interpreter never exercised.

        The broker ships as python:3.12-slim and every container here runs 3.12, so
        a lower floor is a promise about a runtime that does not exist in production.
        """
        dockerfile = (
            pathlib.Path(__file__).resolve().parent.parent / "Dockerfile"
        ).read_text(encoding="utf-8")
        base = next(line for line in dockerfile.splitlines() if line.startswith("FROM "))
        self.assertIn(
            f"python:{check.MINIMUM}-slim", base,
            f"the image runs {base!r} while the declared floor is {check.MINIMUM}",
        )

    def test_the_gate_names_the_interpreter_it_is_using(self) -> None:
        """A gate that ran the suite somewhere and said nothing is how this hides."""
        source = (SCRIPTS / "check.py").read_text(encoding="utf-8")
        # the whole statement, not the words: an unrelated line also reads
        # "the floor is this interpreter: ...", so a bare substring check is
        # satisfied by the wrong line and deleting the real one still passes
        self.assertIn(
            'print(f"==> interpreter: {RUNNING} (declared minimum {MINIMUM})"', source
        )

    def test_a_newer_interpreter_schedules_the_suite_on_the_floor(self) -> None:
        with patch.object(check, "RUNNING", "3.99"):
            step = check.minimum_suite()
        self.assertIsNotNone(step, "a newer interpreter did not schedule the floor run")
        name, command = step
        self.assertIn(check.MINIMUM, name)
        self.assertIn("--python", command)
        self.assertIn(check.MINIMUM, command)
        self.assertIn("unittest", command)
        self.assertIn("tests", command)

    def test_the_floor_is_not_run_twice_when_it_is_already_underneath(self) -> None:
        with patch.object(check, "RUNNING", check.MINIMUM):
            self.assertIsNone(
                check.minimum_suite(),
                "the suite would run twice on the interpreter already in use",
            )

    def test_the_interpreter_in_use_is_at_least_the_declared_floor(self) -> None:
        """A gate running below the floor tests something the package disclaims.

        Less likely than running above it and easier to miss: the suite passes, every
        check is green, and the environment is older than any interpreter the package
        says it supports, so a green result says nothing about the declared bound in
        either direction.
        """
        self.assertGreaterEqual(
            _version(check.RUNNING),
            _version(check.MINIMUM),
            f"the gate is running {check.RUNNING}, below the declared {check.MINIMUM}",
        )

    def test_an_unavailable_uv_fails_rather_than_skipping_quietly(self) -> None:
        """A floor that could not be exercised must be said, not swallowed.

        The import check skips when uv is missing, which is reasonable for a
        convenience check. This one is the only thing standing between the declared
        bound and a module that only works on the newest interpreter, so a skip here
        would be the exact silence the change exists to end.
        """
        source = (SCRIPTS / "check.py").read_text(encoding="utf-8")
        self.assertIn("NOT exercised", source)
        self.assertIn("FileNotFoundError", source)
        self.assertIn("return 1", source)
