import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from cindral.agent import JobSpec
from cindral.contract import CONTRACT_PATH
from cindral.executor import DockerExecutor, ProcessRunner, tail

CONTRACT = """
[image]
ref = "node:22-bookworm"

[[steps]]
run = "pnpm install --frozen-lockfile"

[[steps]]
run = "pnpm run ci"
"""


def spec(job_id="job-1", sha="a" * 40):
    return JobSpec(
        id=job_id,
        repository="example-org/example-app",
        sha=sha,
        ref="main",
        command=(),
        timeout=3600,
    )


class FakeRunner:
    """Records argv and materializes the checkout on `git init`."""

    def __init__(self, contract=CONTRACT, codes=None, cancel_on=None):
        self.calls = []
        self.contract = contract
        self.codes = codes or {}
        self.cancel_on = cancel_on
        self.cancel = None

    def _record(self, argv):
        self.calls.append(list(argv))
        for marker, code in self.codes.items():
            if marker in " ".join(argv):
                return code
        return 0

    def run(self, argv, log, env=None, cwd=None, cancel=None):
        code = self._record(argv)
        if "git init" in " ".join(argv) and self.contract is not None:
            repo = Path(argv[-1])
            (repo / ".cindral").mkdir(parents=True, exist_ok=True)
            (repo / CONTRACT_PATH).write_text(self.contract)
        if self.cancel_on and self.cancel_on in " ".join(argv) and cancel is not None:
            cancel.set()
        if code != 0:
            log.write(f"fake: {argv} -> {code}\n")
        return code

    def check(self, argv, log, env=None, cwd=None, cancel=None):
        code = self.run(argv, log, env=env, cwd=cwd, cancel=cancel)
        if code != 0:
            raise RuntimeError(f"command failed: {argv}")


def executor(runner, workspace):
    return DockerExecutor(
        token="tok",
        workspace_root=workspace,
        log_dir=str(Path(workspace) / "logs"),
        runner=runner,
    )


class DockerExecutorTest(unittest.TestCase):
    def test_runs_the_contract_steps(self) -> None:
        runner = FakeRunner()
        with TemporaryDirectory() as tmp:
            result = executor(runner, tmp)(spec(), threading.Event())
        self.assertEqual(result.exit_code, 0)
        self.assertIn("step 1: pnpm install --frozen-lockfile", result.log)
        self.assertIn("step 2: pnpm run ci", result.log)
        joined = [" ".join(call) for call in runner.calls]
        self.assertTrue(any("docker network create cindral-job-1" in c for c in joined))
        self.assertTrue(any("docker network rm cindral-job-1" in c for c in joined))
        self.assertTrue(any("checkout --detach" in c for c in joined))

    def test_a_failing_step_returns_its_code_and_stops(self) -> None:
        runner = FakeRunner(codes={"pnpm install": 3})
        with TemporaryDirectory() as tmp:
            result = executor(runner, tmp)(spec(), threading.Event())
        self.assertEqual(result.exit_code, 3)
        self.assertNotIn("step 2: pnpm run ci", result.log)

    def test_starts_and_stops_declared_services(self) -> None:
        contract = CONTRACT.replace(
            '[image]',
            '[[services]]\nname = "postgres"\nimage = "pgvector/pgvector:pg16"\n\n[image]',
        )
        runner = FakeRunner(contract=contract)
        with TemporaryDirectory() as tmp:
            result = executor(runner, tmp)(spec(), threading.Event())
        joined = [" ".join(call) for call in runner.calls]
        self.assertEqual(result.exit_code, 0)
        self.assertTrue(any("docker run -d --name cindral-job-1-postgres" in c for c in joined))
        self.assertTrue(any("docker rm -f cindral-job-1-postgres" in c for c in joined))

    def test_missing_contract_fails_the_job(self) -> None:
        runner = FakeRunner(contract=None)
        with TemporaryDirectory() as tmp:
            result = executor(runner, tmp)(spec(), threading.Event())
        self.assertEqual(result.exit_code, 1)
        self.assertIn("no .cindral/ci.toml", result.log)

    def test_cancel_after_a_step_returns_130(self) -> None:
        runner = FakeRunner(cancel_on="pnpm install")
        with TemporaryDirectory() as tmp:
            result = executor(runner, tmp)(spec(), threading.Event())
        self.assertEqual(result.exit_code, 130)

    def test_tail_keeps_the_end_of_long_logs(self) -> None:
        text = "x" * 100 + "END"
        self.assertTrue(tail(text, limit=10).endswith("END"))
        self.assertNotIn("x" * 100, tail(text, limit=10))


class ProcessRunnerTest(unittest.TestCase):
    def test_missing_binary_returns_127(self) -> None:
        import io

        runner = ProcessRunner()
        with TemporaryDirectory() as tmp:
            code = runner.run(["definitely-not-a-binary-xyz"], io.StringIO(), cwd=tmp)
        self.assertEqual(code, 127)


if __name__ == "__main__":
    unittest.main()
