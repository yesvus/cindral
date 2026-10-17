"""Check inferred CI only executes explicit, repository-owned commands."""
import tempfile
import unittest
from pathlib import Path

from cindral.contract import ContractError
from cindral.actions import (
    WorkflowAmbiguous,
    WorkflowError,
    WorkflowNoMatch,
    WorkflowUnsupported,
)
from cindral.inference import infer_contract, resolve_contract
from cindral.agent import JobSpec
from cindral.executor import DockerExecutor, ExecutorError


def job(job_id: str = "test", target: str = "", ref: str = "refs/heads/main") -> JobSpec:
    return JobSpec(
        id=job_id,
        repository="example-org/example",
        sha="a" * 40,
        ref=ref,
        command=(target,) if target else (),
        timeout=3600,
    )


class WorkflowPreferenceTest(unittest.TestCase):
    """A committed workflow is read before a contract, so writing one is enough."""

    PLAIN = """
name: ci
on:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-24.04
    steps:
      - run: echo one
      - run: echo two
"""
    BLOCKED = """
name: ci
on:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-24.04
    container:
      image: node:22
    steps:
      - run: echo one
"""
    CONTRACT = """
[image]
ref = "python:3.12-bookworm"

[[steps]]
run = "from-the-contract"
"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def repo(self, *, workflow: str | None, contract: str | None) -> Path:
        base = Path(self._tmp.name) / "repo"
        (base / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        if workflow is not None:
            (base / ".github" / "workflows" / "ci.yml").write_text(workflow)
        if contract is not None:
            (base / ".cindral").mkdir(parents=True, exist_ok=True)
            (base / ".cindral" / "ci.toml").write_text(contract)
        return base

    def test_two_workflows_on_one_event_do_not_refuse_the_run(self) -> None:
        """A repository with a release workflow beside its CI one is ordinary.

        It matched both jobs, nothing was preferred, and the push was refused with
        "specify an unambiguous target job" — which a push cannot do, because it
        carries no job name. The run now falls through to the contract, or to the
        inferred commands when there is none, and says which it chose.
        """
        base = Path(self._tmp.name) / "two"
        workflows = base / ".github" / "workflows"
        workflows.mkdir(parents=True, exist_ok=True)
        (workflows / "publish.yml").write_text(self.PLAIN)
        (workflows / "release.yml").write_text(self.PLAIN)
        (base / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        (base / "package.json").write_text(
            '{"name":"app","scripts":{"ci":"pnpm run lint && pnpm test"}}'
        )
        notes: list[str] = []
        contract = resolve_contract(base, job(), note=notes.append)
        self.assertTrue(any("multiple workflow jobs matched" in note for note in notes), notes)
        self.assertEqual(
            contract.steps,
            ("corepack enable && pnpm install --frozen-lockfile", "pnpm run ci"),
            "an ambiguous push runs what the repository's own files say, not neither",
        )

    def test_an_ambiguous_run_does_not_hand_the_push_to_an_undeclared_contract(self) -> None:
        # Inference answers an ambiguous push, and a contract that never declared itself
        # CI does not. Letting it answer here would give a push to a file nobody claimed
        # was the CI definition, which is the guard the self-declaration exists to hold,
        # and the control below would stop meaning anything.
        base = Path(self._tmp.name) / "two-contracted"
        workflows = base / ".github" / "workflows"
        workflows.mkdir(parents=True, exist_ok=True)
        (workflows / "publish.yml").write_text(self.PLAIN)
        (workflows / "release.yml").write_text(self.PLAIN)
        (base / ".cindral").mkdir(parents=True, exist_ok=True)
        (base / ".cindral" / "ci.toml").write_text(self.CONTRACT)
        # Nothing to infer from either, so there is genuinely nothing to run. What
        # matters is whose answer it is not: not the contract's.
        with self.assertRaises(ContractError):
            resolve_contract(base, job())

    def test_a_parse_failure_still_refuses_rather_than_falling_back(self) -> None:
        """A contract must not answer a workflow Cindral could not read.

        Ambiguity and a parse failure are both plain `WorkflowError`, which is why
        ambiguity has its own class: only it may fall through.
        """
        with self.assertRaises(WorkflowError) as raised:
            resolve_contract(self.repo(workflow=self.MALFORMED, contract=self.CONTRACT), job())
        self.assertNotIsInstance(raised.exception, WorkflowAmbiguous)

    def test_a_workflow_wins_over_a_contract_when_both_exist(self) -> None:
        # the old order ran the contract, so a repository that committed both
        # ended up with two CI definitions and only one of them live
        contract = resolve_contract(self.repo(workflow=self.PLAIN, contract=self.CONTRACT), job())
        self.assertIn("echo one", contract.steps)
        self.assertNotIn("from-the-contract", contract.steps)

    def test_the_contract_still_serves_a_workflow_this_build_cannot_honour(self) -> None:
        # a job container is a real thing a repository needs; a workflow asking
        # for one is refused rather than half-run, so the contract answers
        contract = resolve_contract(
            self.repo(workflow=self.BLOCKED, contract=self.CONTRACT), job()
        )
        self.assertIn("from-the-contract", contract.steps)

    MALFORMED = """
name: ci
on: [this is not valid yaml
jobs:
"""
    def test_a_malformed_workflow_is_reported_and_not_replaced_by_a_contract(self) -> None:
        # a broken ci.yml is a mistake to report, not a gap to route around; a
        # contract that answered it would report green while running something
        # the maintainer never wrote
        with self.assertRaises(WorkflowError) as caught:
            resolve_contract(self.repo(workflow=self.MALFORMED, contract=self.CONTRACT), job())
        self.assertIn("invalid workflow YAML", str(caught.exception))

    def test_a_target_naming_no_workflow_job_is_reported_not_replaced(self) -> None:
        with self.assertRaises(WorkflowError) as caught:
            resolve_contract(
                self.repo(workflow=self.PLAIN, contract=self.CONTRACT), job(target="nope")
            )
        self.assertIn("no workflow job matched", str(caught.exception))

    DISPATCH_ONLY = """
name: cindral-dispatch
on:
  workflow_dispatch:
    inputs:
      cindral_lane:
        required: true
        type: string
jobs:
  hosted:
    if: inputs.cindral_lane == 'hosted'
    runs-on: ubuntu-24.04
    steps:
      - run: ./scripts/ci
"""
    def test_a_dispatch_only_adapter_does_not_break_a_push(self) -> None:
        # the adapter cindral itself tells a repository to commit is
        # workflow_dispatch-only, with a job condition per lane. A push must
        # fall through to the lockfile rather than fail on a construct in a
        # workflow that never runs for a push.
        root = self.repo(workflow=self.DISPATCH_ONLY, contract=None)
        (root / "package.json").write_text(
            '{"name":"app","scripts":{"ci":"pnpm run lint && pnpm test"}}'
        )
        (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        contract = resolve_contract(root, job())
        self.assertIn("pnpm run ci", " ".join(contract.steps))

    def test_a_job_condition_in_a_workflow_that_does_run_is_still_refused(self) -> None:
        # moving the refusal after event matching must not lose it for the
        # workflow that is actually selected. The condition reads a field the
        # queue record does not carry, so it is the repository's to decide.
        root = self.repo(workflow=self.PLAIN.replace(
            "  test:",
            "  test:\n    if: github.event.pull_request.head.repo.full_name == github.repository",
        ), contract=None)
        with self.assertRaises(WorkflowUnsupported) as caught:
            resolve_contract(root, job())
        self.assertIn("head.repo.full_name", str(caught.exception))

    def test_a_dispatch_naming_a_job_no_workflow_defines_is_still_an_error(self) -> None:
        # a fall-through for "nothing runs this event" must not swallow the
        # operator naming something that is not there
        root = self.repo(workflow=self.DISPATCH_ONLY, contract=None)
        with self.assertRaises(WorkflowError) as caught:
            resolve_contract(root, job(target="nope"))
        self.assertNotIsInstance(caught.exception, WorkflowNoMatch)

    BRANCH_FILTERED = """
name: ci
on:
  push:
    branches: [main]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo one
"""
    def test_a_branch_filtered_workflow_is_reported_and_not_replaced(self) -> None:
        # a workflow that declares the event but excludes this ref is the
        # repository saying "not on this branch", which is a decision. Running
        # something else in its place would report a fraction of what the
        # repository declares.
        root = self.repo(workflow=self.BRANCH_FILTERED, contract=self.CONTRACT)
        with self.assertRaises(WorkflowError) as caught:
            resolve_contract(root, job(ref="refs/heads/feature"))
        self.assertNotIsInstance(caught.exception, WorkflowNoMatch)
        self.assertIn("no workflow job matched", str(caught.exception))

    def test_the_fall_through_names_both_definitions_in_the_job_log(self) -> None:
        # a run that quietly reports a different definition is the thing the
        # precedence order exists to prevent
        noted: list[str] = []
        root = self.repo(workflow=self.DISPATCH_ONLY, contract=self.CONTRACT)
        contract = resolve_contract(root, job(), note=noted.append)
        self.assertIn("from-the-contract", contract.steps)
        rendered = "".join(noted)
        self.assertIn("no committed workflow handles this event", rendered)
        self.assertIn(".cindral/ci.toml", rendered)
        for line in noted:
            self.assertTrue(line.endswith("\n"), line)

    def test_a_committed_contract_answers_when_nothing_handles_the_event(self) -> None:
        # the most consequential behaviour change here: on main a contract was
        # ignored whenever any workflow file existed, and it now runs
        root = self.repo(workflow=self.DISPATCH_ONLY, contract=self.CONTRACT)
        contract = resolve_contract(root, job())
        self.assertIn("from-the-contract", contract.steps)

    def test_a_literal_job_condition_is_read_like_any_other(self) -> None:
        # `if: true` is a condition that holds, so the job runs. Pinning the
        # direction, because reading it rather than refusing it is a choice.
        root = self.repo(
            workflow=self.PLAIN.replace("  test:", "  test:\n    if: true"), contract=None
        )
        self.assertIn("echo one", resolve_contract(root, job()).steps)

    def test_a_job_condition_that_holds_runs_the_job(self) -> None:
        # a job condition is the repository's own decision, and this is the one
        # both repositories in the fleet write: a push always satisfies the
        # event-name half, whatever the pull-request half would have said
        root = self.repo(workflow=self.PLAIN.replace(
            "  test:",
            "  test:\n    if: github.event_name != 'pull_request' "
            "|| github.event.pull_request.head.repo.full_name == github.repository",
        ), contract=None)
        self.assertIn("echo one", resolve_contract(root, job()).steps)

    def test_the_shipped_dispatch_template_does_not_break_a_push(self) -> None:
        # the incident itself, read from the file rather than transcribed: the
        # first version of this test used a hand-written stand-in and the real
        # template carries four lanes, a choice input and a concurrency group
        root = self.repo(workflow=None, contract=None)
        template = Path(__file__).resolve().parents[1] / "templates" / "personal-dispatch.yml"
        (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
        (root / ".github" / "workflows" / template.name).write_text(template.read_text())
        (root / "package.json").write_text('{"name":"a","scripts":{"ci":"pnpm run ci"}}')
        (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        contract = resolve_contract(root, job())
        self.assertIn("pnpm run ci", " ".join(contract.steps))

    def test_a_broken_sibling_workflow_does_not_disable_a_good_one(self) -> None:
        # an unrelated release.yml that fails to parse is not a reason to refuse
        # the ci.yml the maintainer committed and this job would have run
        root = self.repo(workflow=self.PLAIN, contract=self.CONTRACT)
        (root / ".github" / "workflows" / "release.yml").write_text(self.MALFORMED)
        contract = resolve_contract(root, job())
        self.assertIn("echo one", contract.steps)

    def test_a_broken_sibling_workflow_is_named_when_nothing_matches(self) -> None:
        root = self.repo(workflow=self.PLAIN, contract=self.CONTRACT)
        (root / ".github" / "workflows" / "release.yml").write_text(self.MALFORMED)
        with self.assertRaises(WorkflowError) as caught:
            resolve_contract(root, job(ref="refs/heads/nonexistent"))
        # both facts: nothing matched this event, and these files were skipped
        self.assertIn("no workflow job matched", str(caught.exception))
        self.assertIn("could not be parsed: release.yml", str(caught.exception))

    IF_CONDITION = """
name: ci
on: [push]
jobs:
  release:
    if: github.event.pull_request.head.repo.full_name == github.repository
    runs-on: ubuntu-latest
    steps:
      - run: echo released
"""
    def test_the_fallback_names_both_definitions_in_the_job_log(self) -> None:
        # the log handle is TextIO.write, which appends no newline, so a warning
        # without one runs into whatever the executor writes next
        noted: list[str] = []
        contract = resolve_contract(
            self.repo(workflow=self.BLOCKED, contract=self.CONTRACT), job(),
            note=noted.append,
        )
        self.assertIn("from-the-contract", contract.steps)
        rendered = "".join(noted)
        self.assertIn("container", rendered)
        self.assertIn("running .cindral/ci.toml instead of the committed workflow", rendered)
        for line in noted:
            self.assertTrue(line.endswith("\n"), line)
        # two lines, so the second is on a line of its own
        self.assertEqual(2, rendered.count("\n"), rendered)

    def test_a_job_condition_falls_back_rather_than_reading_as_unparseable(self) -> None:
        # the capability gap must not be collected as a parse failure, or a
        # valid file is reported as unreadable and the contract that would have
        # answered it never runs
        root = self.repo(workflow=self.IF_CONDITION, contract=self.CONTRACT)
        contract = resolve_contract(root, job(target="release"))
        self.assertIn("from-the-contract", contract.steps)

    def test_a_job_condition_falls_back_beside_a_good_workflow(self) -> None:
        # a second workflow file is what exercises the collect-don't-raise path,
        # and it is where a capability gap would be filed as a parse failure
        root = self.repo(workflow=self.PLAIN, contract=self.CONTRACT)
        (root / ".github" / "workflows" / "release.yml").write_text(self.IF_CONDITION)
        contract = resolve_contract(root, job(target="release.yml:release"))
        self.assertIn("from-the-contract", contract.steps)

    NOT_A_MAPPING = "just a string, not a mapping\n"
    NO_JOBS = "name: ci\non: [push]\n"
    BAD_TIMEOUT = """
name: ci
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 0
    steps:
      - run: echo one
"""
    def test_each_parse_failure_is_reported_and_not_answered_by_a_contract(self) -> None:
        # these three branches decide whether a committed contract is consulted
        # at all, so they are worth pinning rather than leaving to inspection
        for label, source, expected in (
            ("not a mapping", self.NOT_A_MAPPING, "must be a YAML mapping"),
            ("no jobs", self.NO_JOBS, "at least one job"),
            ("bad timeout", self.BAD_TIMEOUT, "positive integer"),
        ):
            with self.subTest(label):
                with self.assertRaises(WorkflowError) as caught:
                    resolve_contract(
                        self.repo(workflow=source, contract=self.CONTRACT), job()
                    )
                self.assertIn(expected, str(caught.exception))

    def test_a_targeted_dispatch_on_a_contract_only_repository_runs_the_contract(self) -> None:
        # no workflow exists for the target to name, and reporting a missing
        # .cindral/ci.toml while it sits right there is worse than running it
        contract = resolve_contract(self.repo(workflow=None, contract=self.CONTRACT), job(target="test"))
        self.assertIn("from-the-contract", contract.steps)

    def test_a_targeted_dispatch_uses_the_workflow_and_not_the_contract(self) -> None:
        # the case this ordering most improves: a targeted job previously ran
        # the contract, so --target was silently ignored
        contract = resolve_contract(
            self.repo(workflow=self.PLAIN, contract=self.CONTRACT), job(target="test")
        )
        self.assertIn("echo one", contract.steps)

    def test_an_unhonourable_workflow_still_fails_loudly_with_no_contract(self) -> None:
        with self.assertRaises(WorkflowError) as caught:
            resolve_contract(self.repo(workflow=self.BLOCKED, contract=None), job())
        self.assertIn("does not run yet", str(caught.exception))


class InferenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)

    def test_pnpm_ci_script(self) -> None:
        (self.repo / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        (self.repo / "package.json").write_text('{"scripts":{"ci":"pnpm lint && pnpm test"}}')
        contract = infer_contract(self.repo)
        self.assertEqual(contract.steps, ("corepack enable && pnpm install --frozen-lockfile", "pnpm run ci"))

    def test_pnpm_without_ci_fails_loudly(self) -> None:
        (self.repo / "pnpm-lock.yaml").touch()
        (self.repo / "package.json").write_text('{"scripts":{"test":"echo ok"}}')
        with self.assertRaisesRegex(ContractError, "ci script"):
            infer_contract(self.repo)

    def test_uv_check_script(self) -> None:
        (self.repo / "uv.lock").touch()
        (self.repo / "pyproject.toml").touch()
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "check.py").touch()
        self.assertEqual(infer_contract(self.repo).steps[-1], "uv run python scripts/check.py")

    def test_uv_without_check_script_fails_loudly(self) -> None:
        (self.repo / "uv.lock").touch()
        (self.repo / "pyproject.toml").touch()
        with self.assertRaisesRegex(ContractError, "scripts/check.py"):
            infer_contract(self.repo)

    def test_ambiguous_stack_fails_loudly(self) -> None:
        (self.repo / "uv.lock").touch()
        (self.repo / "pnpm-lock.yaml").touch()
        with self.assertRaisesRegex(ContractError, "both"):
            infer_contract(self.repo)

    def test_unknown_stack_fails_loudly(self) -> None:
        with self.assertRaisesRegex(ContractError, "no CI definition"):
            infer_contract(self.repo)

    def test_contract_overrides_inference(self) -> None:
        (self.repo / "pnpm-lock.yaml").touch()
        (self.repo / ".cindral").mkdir()
        (self.repo / ".cindral" / "ci.toml").write_text('[image]\nref = "alpine:3"\n[[steps]]\nrun = "echo override"\n')
        self.assertEqual(self.executor()._load_contract(self.repo, self.spec()).steps, ("echo override",))

    def test_targeted_job_never_runs_inferred_commands(self) -> None:
        (self.repo / "pnpm-lock.yaml").touch()
        (self.repo / "package.json").write_text('{"scripts":{"ci":"echo ok"}}')
        job = self.spec(command=("release",))
        with self.assertRaisesRegex(ExecutorError, "no .cindral/ci.toml"):
            self.executor()._load_contract(self.repo, job)

    def test_empty_workflows_directory_still_infers_repository_ci(self) -> None:
        (self.repo / ".github" / "workflows").mkdir(parents=True)
        (self.repo / "pnpm-lock.yaml").touch()
        (self.repo / "package.json").write_text('{"scripts":{"ci":"echo ok"}}')
        self.assertEqual(self.executor()._load_contract(self.repo, self.spec()).steps[-1], "pnpm run ci")

    def test_workflow_definition_is_not_overridden_by_inference(self) -> None:
        workflows = self.repo / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "ci.yml").write_text("on: [push]\njobs:\n  ci:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo workflow\n")
        (self.repo / "pnpm-lock.yaml").touch()
        (self.repo / "package.json").write_text('{"scripts":{"ci":"echo inferred"}}')
        self.assertEqual(self.executor()._load_contract(self.repo, self.spec()).steps, ("echo workflow",))

    @staticmethod
    def executor() -> DockerExecutor:
        return DockerExecutor.__new__(DockerExecutor)

    @staticmethod
    def spec(command: tuple[str, ...] = ()) -> JobSpec:
        return JobSpec(id="job-1", repository="example-org/app", sha="a" * 40, ref="main", command=command, timeout=300)


class ContractAuthorityTest(unittest.TestCase):
    """A contract can say it is the CI, which is the only way past an ambiguous push.

    Two release workflows on the pushed event is the shape that used to break it. The
    resolver has no way to tell a release workflow from a CI workflow, so both match
    and the ambiguity used to fail the push outright.

    It no longer does. Ambiguity falls through to what the repository's own files say,
    and a contract answers only when it declares itself CI, which is what this class
    covers: the declaration wins, and a contract without it still does not.
    """

    RELEASE = """
name: {name}
on:
  push:
    branches: [main]
jobs:
  {job}:
    runs-on: ubuntu-24.04
    steps:
      - run: echo {name}
"""

    CONTRACT = """
[ci]
contract_only = true

[image]
ref = "python:3.12-bookworm"

[[steps]]
run = "from-the-contract"
"""

    PLAIN_CONTRACT = """
[image]
ref = "python:3.12-bookworm"

[[steps]]
run = "from-the-contract"
"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def repo(self, contract: str, releases: int = 2) -> Path:
        base = Path(self._tmp.name) / "repo"
        workflows = base / ".github" / "workflows"
        workflows.mkdir(parents=True, exist_ok=True)
        for index in range(releases):
            (workflows / f"publish-{index}.yml").write_text(
                self.RELEASE.format(name=f"publish-{index}", job=f"publish{index}")
            )
        (base / ".cindral").mkdir(parents=True, exist_ok=True)
        (base / ".cindral" / "ci.toml").write_text(contract)
        return base

    def test_two_release_workflows_do_not_let_an_ordinary_contract_answer(self) -> None:
        # The control, restated for the behaviour that now exists. Ambiguity used to
        # raise, which proved the declaration was doing the work. It no longer raises:
        # it falls through to inference. What still has to be true is that the
        # contract sitting in the repository does not answer it, so this remains a
        # statement about the declaration and not about duplicates being tolerated.
        base = self.repo(self.PLAIN_CONTRACT)
        (base / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        (base / "package.json").write_text(
            '{"name":"app","scripts":{"ci":"pnpm run lint && pnpm test"}}'
        )
        contract = resolve_contract(base, job())
        self.assertNotIn("from-the-contract", contract.steps)

    def test_a_contract_that_declares_itself_answers_the_ambiguous_push(self) -> None:
        contract = resolve_contract(self.repo(self.CONTRACT), job())
        self.assertIn("from-the-contract", contract.steps)

    def test_the_declaration_also_wins_against_a_single_working_workflow(self) -> None:
        # Deliberate, not a shadow: the repository wrote the contract and said so
        # in it, which is a stronger statement than having a ci.yml on disk. The
        # guard worth keeping is the one above, that a contract *without* the
        # declaration still loses to a real workflow.
        base = Path(self._tmp.name) / "single"
        workflows = base / ".github" / "workflows"
        workflows.mkdir(parents=True, exist_ok=True)
        (workflows / "ci.yml").write_text(WorkflowPreferenceTest.PLAIN)
        (base / ".cindral").mkdir(parents=True, exist_ok=True)
        (base / ".cindral" / "ci.toml").write_text(self.CONTRACT)
        contract = resolve_contract(base, job())
        self.assertIn("from-the-contract", contract.steps)

    def test_a_broken_contract_that_declares_itself_is_reported(self) -> None:
        # The guard only reads one boolean, so a contract with a bad section
        # still says contract_only and is then parsed properly, which is what
        # reports the mistake. Asserting the *contract* error rather than a
        # generic one is the point: the reader is told the file is wrong.
        broken = self.CONTRACT.replace('ref = "python:3.12-bookworm"', "ref = 12")
        with self.assertRaises(ContractError):
            resolve_contract(self.repo(broken), job())

    def test_a_contract_that_is_not_toml_at_all_is_not_treated_as_authoritative(self) -> None:
        # Unparseable means the opt-in cannot be read, so resolution falls
        # through to the workflows rather than half-believing a file it could
        # not read. The broken file is still reported by the path that owns it.
        base = self.repo("this is not toml = =")
        (base / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        (base / "package.json").write_text(
            '{"name":"app","scripts":{"ci":"pnpm run lint && pnpm test"}}'
        )
        # Resolution falls through to the workflows, which are ambiguous, and then
        # to what the repository's own files say. What matters is that the broken
        # file is not half-believed on the way: it never reaches the answer.
        contract = resolve_contract(base, job())
        self.assertIn("pnpm run ci", " ".join(contract.steps))

    def test_a_named_job_still_reaches_the_workflow_even_with_the_declaration(self) -> None:
        # a dispatch naming a job is asking for that workflow job. A contract has
        # no jobs, so answering for one it never declared would run the wrong
        # thing under the right name.
        base = Path(self._tmp.name) / "dispatched"
        workflows = base / ".github" / "workflows"
        workflows.mkdir(parents=True, exist_ok=True)
        (workflows / "cindral-dispatch.yml").write_text(WorkflowPreferenceTest.PLAIN)
        (base / ".cindral").mkdir(parents=True, exist_ok=True)
        (base / ".cindral" / "ci.toml").write_text(self.CONTRACT)
        contract = resolve_contract(base, job(target="test"))
        self.assertIn("echo one", contract.steps)
        self.assertNotIn("from-the-contract", contract.steps)
