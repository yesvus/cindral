"""The generated run script must record step boundaries a reader can trust.

Only a real run emits the markers, so these execute the script the executor
wrote rather than asserting on its text.
"""
import re
import shutil
import subprocess
import threading
import unittest
from tempfile import TemporaryDirectory

from cindral.steps import parse_steps

from test_executor import CONTRACT, FakeRunner, executor, spec

ECHO_CONTRACT = """
[image]
ref = "node:22-bookworm"

[[steps]]
run = "echo installing"

[[steps]]
run = "echo testing"
"""

FAILING_CONTRACT = """
[image]
ref = "node:22-bookworm"

[[steps]]
run = "echo ok"

[[steps]]
run = "exit 3"
"""

# a step that turns off errexit and then fails: the run must still fail
ERRNOFF_CONTRACT = """
[image]
ref = "node:22-bookworm"

[[steps]]
run = "echo ok"

[[steps]]
run = "set +e\\nfalse"
"""


def run_contract(contract: str, shell: str = "sh") -> list[tuple[int, str, bool, str]]:
    """Build a script from an inline contract and parse the result."""
    with TemporaryDirectory() as tmp:
        runner = FakeRunner(contract=contract)
        engine = executor(runner, tmp)
        engine(spec(), threading.Event())
        script = runner.script
        nonce = _nonce_in(script)
    assert script is not None
    completed = subprocess.run([shell, "-c", script], capture_output=True, text=True)
    return [
        (step.index, step.name, step.completed, step.output.strip())
        for step in parse_steps(completed.stdout + completed.stderr, nonce)
    ]


def run_generated_script(contract: str, shell: str = "sh") -> list[tuple[int, str, bool, str]]:
    """Run the script the executor produced and read its steps back.

    The nonce is read out of the script the run actually wrote, because it is a
    local inside the executor rather than something the test can ask for.
    """
    with TemporaryDirectory() as tmp:
        runner = FakeRunner(contract=contract)
        engine = executor(runner, tmp)
        engine(spec(), threading.Event())
        script = runner.script
        nonce = _nonce_in(runner.script)
    assert script is not None
    completed = subprocess.run([shell, "-c", script], capture_output=True, text=True)
    return [
        (step.index, step.name, step.completed, step.output.strip())
        for step in parse_steps(completed.stdout + completed.stderr, nonce)
    ]


def _nonce_in(script: str) -> str:
    match = re.search(r"cindral: step \d+ ([0-9a-f]{32}):", script)
    assert match is not None, script
    return match.group(1)


def failed_step_index_of(recorded) -> int | None:
    for index, _name, completed, _output in recorded:
        if not completed:
            return index
    return None


class StepMarkerScriptTest(unittest.TestCase):
    def test_the_script_announces_each_step_and_its_end(self) -> None:
        runner = FakeRunner()
        with TemporaryDirectory() as tmp:
            engine = executor(runner, tmp)
            engine(spec(), threading.Event())
        self.assertIn("if ( pnpm install --frozen-lockfile", runner.script)
        self.assertIn("if ( pnpm run ci", runner.script)
        # the run's own nonce is in every marker it writes
        nonce = _nonce_in(runner.script)
        self.assertIn(f"cindral: step 1 {nonce}:", runner.script)
        self.assertIn(f"printf '\\ncindral: step 1 {nonce} end", runner.script)
        # a failing step ends the script with its own status, so the run cannot
        # report success after a command failed
        self.assertIn('exit "$cindral_step_status"', runner.script)

    def test_two_overlapping_runs_of_one_executor_both_keep_their_steps(self) -> None:
        # the nonce is a local per run, not state on the executor: one executor
        # serves every slot on a device, and a shared nonce meant a run whose
        # container had finished but whose log was read after another run
        # reassigned it was parsed against the other run's markers and reported
        # no steps at all. The two runs here genuinely overlap, which a
        # sequential pair cannot show.
        held = threading.Event()
        release = threading.Event()
        results: dict[str, int] = {}
        errors: list[BaseException] = []

        class OverlappingRunner(FakeRunner):
            """Runs the generated script for real, so the log holds real markers."""

            def run(self, argv, log, env=None, cwd=None, cancel=None):
                joined = " ".join(argv)
                if "sh /workspace/run.sh" not in joined:
                    return super().run(argv, log, env=env, cwd=cwd, cancel=cancel)
                # This run's script, read from its own workspace before anything
                # else runs. The fake stores the script on the shared runner, and
                # the two runs share the runner, so reading `self.script` after
                # the barrier returns the *other* run's script whenever the other
                # run got there first. That is a race in the test, not in the
                # executor, and it showed up as an intermittent wrong step count.
                script = (self._workspace(argv) / "run.sh").read_text()
                code = super().run(argv, log, env=env, cwd=cwd, cancel=cancel)
                if "job-1" in joined:
                    held.set()
                    release.wait(10)
                completed = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
                log.write(completed.stdout)
                return completed.returncode

        with TemporaryDirectory() as tmp:
            engine = executor(OverlappingRunner(contract=ECHO_CONTRACT), tmp)

            def run_job(job_id: str) -> None:
                try:
                    results[job_id] = len(engine(spec(job_id), threading.Event()).steps)
                except BaseException as exc:  # noqa: BLE001 - reported below
                    errors.append(exc)

            first = threading.Thread(target=run_job, args=("job-1",))
            first.start()
            self.assertTrue(held.wait(10), "the first run never started")
            second = threading.Thread(target=run_job, args=("job-2",))
            second.start()
            release.set()
            # The joins are the assertion. A thread that has not finished leaves
            # no result, and reporting that as "recorded 3 steps instead of 2"
            # sends whoever reads the failure looking at the step parser instead
            # of at a loaded machine, which is where it is.
            first.join(timeout=60)
            second.join(timeout=60)
            self.assertFalse(errors, f"a run raised: {errors}")
            self.assertFalse(first.is_alive(), "the first run did not finish")
            self.assertFalse(second.is_alive(), "the second run did not finish")

        # both runs announced and ran two steps, so both must record two
        self.assertEqual(results.get("job-1"), 2, results)
        self.assertEqual(results.get("job-2"), 2, results)

    def test_a_step_that_disables_errexit_cannot_let_the_run_pass(self) -> None:
        # `set +e` inside a step used to disable the abort for every step after
        # it, so a run with a failing command exited zero and reported success.
        # A marker's name is the step's first line, so the rest of its text
        # belongs to the step's output.
        self.assertEqual(
            run_generated_script(ERRNOFF_CONTRACT),
            [(1, "echo ok", True, "ok"), (2, "set +e", False, "false")],
        )

    def test_a_passing_run_reports_every_step_complete(self) -> None:
        self.assertEqual(
            run_generated_script(ECHO_CONTRACT),
            [(1, "echo installing", True, "installing"), (2, "echo testing", True, "testing")],
        )

    def test_a_green_final_step_without_a_trailing_newline_still_completes(self) -> None:
        # build tools write progress without a newline; the end marker used to
        # land on that line, missed its anchor, and recorded a passing step as
        # unfinished, so a green run read as stopped in its last step
        contract = """
[image]
ref = "node:22-bookworm"

[[steps]]
run = "echo installing"

[[steps]]
run = "printf 'built 99 percent'"
"""
        # sh is the production shell; dash and bash are checked too because
        # /bin/sh is dash on Debian and bash on Alpine, and a device image is
        # whichever the repository built
        shells = [sh for sh in ("sh", "dash", "bash") if shutil.which(sh)]
        self.assertTrue(shells)
        for shell in shells:
            with self.subTest(shell=shell):
                recorded = run_contract(contract, shell)
                self.assertEqual(
                    [(index, completed) for index, _, completed, _ in recorded],
                    [(1, True), (2, True)],
                )
                self.assertIsNone(failed_step_index_of(recorded))

    def test_a_run_stopped_at_a_failing_step_leaves_that_step_incomplete(self) -> None:
        self.assertEqual(
            run_generated_script(FAILING_CONTRACT),
            [(1, "echo ok", True, "ok"), (2, "exit 3", False, "")],
        )

    def test_a_pnpm_contract_records_the_real_step_commands(self) -> None:
        recorded = run_generated_script(CONTRACT)
        self.assertEqual([name for _, name, _, _ in recorded][:1], ["pnpm install --frozen-lockfile"])
