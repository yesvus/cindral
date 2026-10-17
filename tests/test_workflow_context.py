"""Tests for the context a direct run resolves, and the fields it refuses by name."""
import tempfile
import unittest
from pathlib import Path

from cindral.actions import (
    WorkflowError,
    WorkflowUnsupported,
    resolve_workflow_contract,
)
from cindral.act import ACT_BINARY, CONTAINER_REPO
from cindral.agent import JobSpec
from cindral.workflow_context import (
    ContextError,
    RunContext,
    create_event_payload,
    event_name_for,
    run_context,
)


def push(ref: str = "main") -> RunContext:
    return RunContext.from_job(
        JobSpec("job1", "my-org/my-app", "abc1234", ref, (), 60),
        "push",
        workflow="CI",
        job_id="test",
        env={"NODE_ENV": "test"},
    )


def pull_request(number: int = 42) -> RunContext:
    return RunContext.from_job(
        JobSpec("job1", "my-org/my-app", "def5678", f"refs/pull/{number}/merge", (), 60),
        "pull_request",
        workflow="CI",
        job_id="test",
    )


class EventPayloadTest(unittest.TestCase):
    def test_a_push_event_carries_the_commit_it_moved_to(self) -> None:
        payload = create_event_payload(
            JobSpec("job1", "my-org/my-app", "abc1234", "main", (), 60), "push"
        )
        self.assertEqual(payload["after"], "abc1234")
        self.assertEqual(payload["sha"], "abc1234")
        self.assertNotIn("pull_request", payload)
        # the queue records a push as a branch name, and a reader of the event
        # looks for the full ref
        self.assertEqual(payload["ref"], "refs/heads/main")
        self.assertEqual(payload["repository"]["owner"]["login"], "my-org")
        self.assertEqual(payload["repository"]["name"], "my-app")

    def test_a_pull_request_event_carries_the_number_and_nothing_it_cannot_know(self) -> None:
        payload = create_event_payload(
            JobSpec("job1", "my-org/my-app", "def5678", "refs/pull/42/merge", (), 60),
            "pull_request",
        )
        self.assertEqual(payload["number"], 42)
        self.assertEqual(payload["pull_request"]["number"], 42)
        self.assertEqual(payload["pull_request"]["head"]["sha"], "def5678")
        self.assertNotIn("after", payload)
        # the record does not say who opened the pull request, which branch it
        # came from, or which branch it merges into
        for fabricated in ("action", "default_branch"):
            self.assertNotIn(fabricated, payload["repository"])
            self.assertNotIn(fabricated, payload)
        self.assertNotIn("base", payload["pull_request"])
        self.assertNotIn("ref", payload["pull_request"]["head"])

    def test_a_pull_request_ref_without_a_number_is_refused(self) -> None:
        with self.assertRaises(ContextError) as cm:
            create_event_payload(JobSpec("job1", "o/r", "sha", "refs/pull/head", (), 60), "pull_request")
        self.assertIn("refs/pull/head", str(cm.exception))

    def test_a_merge_ref_the_agent_wrote_either_way_reads_the_same(self) -> None:
        # the agent accepts a merge ref written either way, while a step reading
        # github.ref expects the full one
        for ref in ("refs/pull/42/merge", "pull/42/merge"):
            payload = create_event_payload(JobSpec("job1", "o/r", "sha", ref, (), 60), "pull_request")
            self.assertEqual(payload["ref"], "refs/pull/42/merge")
            self.assertEqual(payload["number"], 42)

    def test_the_event_follows_the_ref_the_run_was_queued_for(self) -> None:
        self.assertEqual(event_name_for(JobSpec("j", "o/r", "s", "main", (), 60)), "push")
        self.assertEqual(event_name_for(JobSpec("j", "o/r", "s", "refs/tags/v1", (), 60)), "push")
        self.assertEqual(
            event_name_for(JobSpec("j", "o/r", "s", "refs/pull/7/merge", (), 60)), "pull_request"
        )
        self.assertEqual(event_name_for(JobSpec("j", "o/r", "s", "pull/7/head", (), 60)), "pull_request")


class ValueTest(unittest.TestCase):
    def test_a_push_run_resolves_the_repository_ref_and_commit(self) -> None:
        context = push()
        self.assertEqual(context.value("github.sha"), "abc1234")
        self.assertEqual(context.value("github.ref"), "refs/heads/main")
        self.assertEqual(context.value("github.ref_name"), "main")
        self.assertEqual(context.value("github.ref_type"), "branch")
        self.assertEqual(context.value("github.repository"), "my-org/my-app")
        self.assertEqual(context.value("github.repository_owner"), "my-org")
        self.assertEqual(context.value("github.event_name"), "push")
        self.assertEqual(context.value("github.workflow"), "CI")
        self.assertEqual(context.value("github.job"), "test")
        self.assertEqual(context.value("github.workspace"), CONTAINER_REPO)
        self.assertEqual(context.value("github.event.after"), "abc1234")
        self.assertEqual(context.value("env.NODE_ENV"), "test")

    def test_a_pull_request_run_resolves_the_number_and_the_merge_ref(self) -> None:
        context = pull_request()
        self.assertEqual(context.value("github.event_name"), "pull_request")
        self.assertEqual(context.value("github.ref"), "refs/pull/42/merge")
        # the short name of a merge ref is the number and the word merge
        self.assertEqual(context.value("github.ref_name"), "42/merge")
        self.assertEqual(context.value("github.ref_type"), "branch")
        self.assertEqual(context.value("github.event.pull_request.number"), "42")
        self.assertEqual(context.value("github.event.number"), "42")
        self.assertEqual(context.value("github.sha"), "def5678")

    def test_a_tag_run_resolves_a_tag_name_and_type(self) -> None:
        context = push("refs/tags/v1.2.3")
        self.assertEqual(context.value("github.ref"), "refs/tags/v1.2.3")
        self.assertEqual(context.value("github.ref_name"), "v1.2.3")
        self.assertEqual(context.value("github.ref_type"), "tag")

    def test_a_field_the_run_does_not_carry_resolves_to_nothing(self) -> None:
        context = push()
        for path in (
            "github.actor",
            "github.triggering_actor",
            "github.run_id",
            "github.head_ref",
            "github.base_ref",
            "github.event.pull_request.title",
            "github.event.action",
            "env.MISSING",
            "steps.build.outputs.tag",
            "secrets.NPM_TOKEN",
        ):
            self.assertIsNone(context.value(path), path)

    def test_a_nested_object_is_not_a_value(self) -> None:
        self.assertIsNone(push().value("github.event.repository"))


class ExpandTest(unittest.TestCase):
    def test_a_run_receives_the_values_it_reads(self) -> None:
        context = pull_request()
        self.assertEqual(
            context.expand("echo ${{ github.sha }} on ${{ github.ref_name }} (#${{ github.event.pull_request.number }})", "step 1"),
            "echo def5678 on 42/merge (#42)",
        )
        self.assertEqual(context.expand("echo no context here", "step 1"), "echo no context here")
        self.assertEqual(context.expand("${{", "step 1"), "${{")

    def test_a_value_is_inserted_as_git_would_insert_it(self) -> None:
        # GitHub substitutes raw, so a command written against github.ref behaves
        # the same here rather than gaining quoting the workflow never asked for
        context = RunContext(event_name="push", event={"ref": "refs/heads/feature/x"}, job_id="test")
        self.assertEqual(
            context.expand("git checkout ${{ github.ref_name }}", "step 1"),
            "git checkout feature/x",
        )

    def test_a_substituted_value_is_not_evaluated_again(self) -> None:
        context = RunContext(event_name="push", event={"ref": "refs/heads/${{ github.actor }}"}, job_id="test")
        self.assertEqual(context.expand("echo ${{ github.ref }}", "step 1"), "echo refs/heads/${{ github.actor }}")

    def test_the_environment_resolves_against_the_same_context(self) -> None:
        context = push()
        self.assertEqual(
            context.expand_env({"TAG": "v${{ github.ref_name }}", "PLAIN": "1"}, "job 'test'"),
            {"TAG": "vmain", "PLAIN": "1"},
        )

    def test_a_run_context_resolves_its_environment_before_a_step_reads_it(self) -> None:
        context = run_context(
            JobSpec("job1", "org/repo", "abc1234", "main", (), 60),
            workflow="CI",
            job_id="test",
            env={"TAG": "v${{ github.ref_name }}"},
        )
        # a step reads the value the run declares, not the expression behind it
        self.assertEqual(context.value("env.TAG"), "vmain")
        self.assertEqual(context.expand("echo ${{ env.TAG }}", "step 1"), "echo vmain")

    def test_an_expression_this_run_cannot_evaluate_is_refused_by_name(self) -> None:
        context = push()
        for expression in (
            "github.actor",
            "github.head_ref",
            "github.event.action",
            "env.MISSING",
            "secrets.NPM_TOKEN",
            "needs.build.result",
            "matrix.node",
            "runner.os",
        ):
            with self.assertRaises(ContextError) as cm:
                context.expand(f"echo ${{{{ {expression} }}}}", "job 'test' step 2 'run'")
            self.assertIn(expression, str(cm.exception))
            self.assertIn("step 2 'run'", str(cm.exception))

    def test_an_expression_outside_a_bare_path_is_refused(self) -> None:
        context = push()
        for expression in (
            "github.sha || 'fallback'",
            "format('{0}', github.sha)",
            "github['sha']",
            "github.event.repository",
        ):
            with self.assertRaises(ContextError) as cm:
                context.expand(f"echo ${{{{ {expression} }}}}", "step 1 'run'")
            self.assertIn(expression, str(cm.exception))


class RequireResolvableTest(unittest.TestCase):
    def test_what_the_engine_evaluates_itself_is_left_to_it(self) -> None:
        context = pull_request()
        for text in (
            "echo ${{ format('{0}', github.sha) }}",
            "echo ${{ hashFiles('pnpm-lock.yaml') }}",
            "echo ${{ github.event_name == 'pull_request' && 'a' || 'b' }}",
            "echo ${{ steps.build.outputs.tag }}",
            "echo ${{ runner.os }}",
            "echo ${{ job.status }}",
            "echo ${{ github.repository == 'my-org/my-app' }}",
        ):
            context.require_resolvable(text, "step 1 'run'")

    def test_a_context_the_engine_cannot_fill_is_refused_by_name(self) -> None:
        context = pull_request()
        for expression in (
            "github.actor",
            "github.head_ref",
            "github.event.pull_request.title",
            "github.event.action",
            "secrets.NPM_TOKEN",
            "needs.build.result",
            "matrix.node",
            "workflow_call.inputs.version",
            "vars.CACHE_URL",
            "env.MISSING",
        ):
            with self.assertRaises(ContextError) as cm:
                context.require_resolvable(f"echo ${{{{ {expression} }}}}", "step 3 'run'")
            self.assertIn(expression, str(cm.exception))
            self.assertIn("step 3 'run'", str(cm.exception))

    def test_a_bare_root_the_run_cannot_supply_is_refused(self) -> None:
        with self.assertRaises(ContextError) as cm:
            push().require_resolvable("echo ${{ secrets }}", "step 1 'run'")
        self.assertIn("secrets", str(cm.exception))

    def test_an_index_is_read_as_the_path_it_stands_for(self) -> None:
        context = pull_request()
        for text in (
            "echo ${{ github['sha'] }}",
            "echo ${{ github[\"sha\"] }}",
            "echo ${{ github['event']['pull_request']['number'] }}",
        ):
            context.require_resolvable(text, "step 1 'run'")
        context.require_expression("github['event']['pull_request']['number'] == 42", "step 1 'if'")

    def test_an_index_of_a_field_the_run_does_not_carry_is_refused(self) -> None:
        for expression in (
            "github['actor']",
            'github["actor"]',
            "github['triggering_actor']",
            "github['event']['pull_request']['title']",
            "secrets['NPM_TOKEN']",
            "env['MISSING']",
        ):
            with self.assertRaises(ContextError) as cm:
                pull_request().require_expression(expression, "step 1 'run'")
            self.assertIn("step 1 'run'", str(cm.exception))

    def test_an_index_into_a_value_is_refused(self) -> None:
        # a subscript the engine resolves on a value rather than on the context
        with self.assertRaises(ContextError) as cm:
            push().require_expression("github.sha[0]", "step 1 'run'")
        self.assertIn("github.sha.0", str(cm.exception))

    def test_a_step_condition_is_read_as_an_expression_already(self) -> None:
        context = pull_request()
        context.require_expression("github.event.pull_request.number == 42", "step 2 'if'")
        context.require_resolvable("${{ github.ref == 'refs/pull/42/merge' }}", "step 2 'if'")
        with self.assertRaises(ContextError) as cm:
            context.require_expression("github.actor != ''", "step 2 'if'")
        self.assertIn("github.actor", str(cm.exception))

    def test_a_quoted_literal_is_text_rather_than_a_context_read(self) -> None:
        push().require_expression("env.NODE_ENV == 'github.actor'", "step 1 'if'")
        push().require_expression("github.event_name == 'github.actor'", "step 1 'if'")


class TranslationTest(unittest.TestCase):
    """What a workflow job that reads the context resolves to, and what it refuses."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.workflows = self.repo / ".github" / "workflows"
        self.workflows.mkdir(parents=True)

    def contract_for(self, ref: str = "main", sha: str = "abc1234", body: str = ""):
        (self.workflows / "ci.yml").write_text(body)
        job = JobSpec("job1", "org/repo", sha, ref, (), 60)
        return resolve_workflow_contract(self.repo, job)

    def test_a_translated_step_resolves_the_context_it_reads(self) -> None:
        contract = self.contract_for(
            body="""
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    env:
      TAG: ${{ github.ref_name }}-${{ github.sha }}
    steps:
      - uses: actions/checkout@v4
      - run: echo "${{ github.repository }} at ${{ github.sha }} on ${{ github.ref }}"
      - run: echo ${{ env.TAG }}
"""
        )
        # a run-only job still translates into one capped container
        self.assertEqual(contract.image, "ubuntu:24.04")
        self.assertFalse(contract.docker)
        self.assertEqual(
            contract.steps,
            ('echo "org/repo at abc1234 on refs/heads/main"', "echo main-abc1234"),
        )
        self.assertEqual(contract.env["TAG"], "main-abc1234")

    def test_a_pull_request_step_resolves_the_number_it_ran_for(self) -> None:
        contract = self.contract_for(
            ref="refs/pull/42/merge",
            sha="def5678",
            body="""
name: CI
on: [pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo "#${{ github.event.pull_request.number }} on ${{ github.ref_name }}"
""",
        )
        self.assertEqual(contract.steps, ('echo "#42 on 42/merge"',))

    def test_a_translated_job_keeps_its_image_and_timeout(self) -> None:
        contract = self.contract_for(
            body="""
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - run: make test
      - run: echo "${{ github.sha }}"
"""
        )
        self.assertEqual(contract.timeout_minutes, 10)
        self.assertEqual(contract.steps, ("make test", 'echo "abc1234"'))

    def test_a_step_reading_a_field_the_run_does_not_carry_is_refused_by_name(self) -> None:
        with self.assertRaises(WorkflowError) as cm:
            self.contract_for(
                body="""
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: make test
      - run: echo "by ${{ github.actor }}"
"""
            )
        self.assertIn("github.actor", str(cm.exception))
        self.assertIn("step 2", str(cm.exception))

    def test_an_engine_run_reads_the_context_from_the_payload_it_is_given(self) -> None:
        contract = self.contract_for(
            ref="refs/pull/7/merge",
            body="""
name: CI
on: [pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-node@v4
      - run: echo "#${{ github.event.pull_request.number }} on ${{ github.ref_name }}"
        if: github.event_name == 'pull_request'
""",
        )
        # the engine evaluates the run text from the checkout itself, against this
        # payload, so the fields that run names have to be in here
        script = contract.steps[0]
        self.assertIn(f"exec {ACT_BINARY} pull_request", script)
        self.assertIn('"ref": "refs/pull/7/merge"', script)
        self.assertIn('"pull_request": {"head": {"sha": "abc1234"}, "number": 7}', script)

    def test_an_engine_step_reading_the_actor_is_refused_by_name(self) -> None:
        with self.assertRaises(WorkflowError) as cm:
            self.contract_for(
                ref="refs/pull/7/merge",
                body="""
name: CI
on: [pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-node@v4
      - run: echo "${{ github.actor }}"
        if: github.triggering_actor == 'example-org'
""",
            )
        self.assertIn("github.actor", str(cm.exception))
        self.assertIn("step 2", str(cm.exception))

    def test_an_action_input_cannot_read_a_missing_actor(self) -> None:
        with self.assertRaises(WorkflowError) as cm:
            self.contract_for(body="""
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-node@v4
        with:
          node-version: ${{ github['actor'] }}
""")
        self.assertIn("github.actor", str(cm.exception))
        self.assertIn("with.node-version", str(cm.exception))

    def test_a_missing_working_directory_field_fails_before_act(self) -> None:
        with self.assertRaisesRegex(WorkflowUnsupported, "github.actor"):
            self.contract_for(body="""
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pnpm test
        working-directory: ${{ github.actor }}
""")

    def test_native_expression_failure_uses_workflow_error(self) -> None:
        with self.assertRaisesRegex(WorkflowError, "cannot evaluate"):
            self.contract_for(body="""
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo ${{ format('{0}', github.sha) }}
""")

    def test_service_environment_resolves_or_refuses_before_start(self) -> None:
        base = """
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      db:
        image: postgres:16
        env:
          PASSWORD: VALUE
    steps:
      - run: pnpm test
"""
        contract = self.contract_for(body=base.replace("VALUE", "${{ github.sha }}"))
        self.assertEqual(contract.services[0].env["PASSWORD"], "abc1234")
        with self.assertRaisesRegex(WorkflowError, "secrets.PG_PASSWORD"):
            self.contract_for(body=base.replace("VALUE", "${{ secrets.PG_PASSWORD }}"))

    def test_shell_and_failure_settings_do_not_disappear_in_native_translation(self) -> None:
        for option in ("shell: bash", "continue-on-error: true", "timeout-minutes: 5"):
            with self.subTest(option=option):
                contract = self.contract_for(body=f"""
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
        {option}
""")
                self.assertTrue(contract.docker)

        contract = self.contract_for(body="""
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: echo hi
        continue-on-error: false
""")
        self.assertFalse(contract.docker)

    def test_job_execution_settings_are_refused_instead_of_discarded(self) -> None:
        for key, setting in (
            ("defaults", "defaults:\n      run:\n        working-directory: web"),
            ("container", "container: node:18"),
        ):
            with self.subTest(setting=setting):
                with self.assertRaises(WorkflowError) as cm:
                    self.contract_for(body=f"""
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    {setting}
    steps:
      - run: pnpm test
""")
                self.assertIn(f"('{key}')", str(cm.exception))

    def test_a_checkout_input_is_refused_on_either_path(self) -> None:
        # the agent checks the commit out itself, so a subdirectory or a ref the
        # step names would silently not apply
        for body in (
            """
name: CI
on: [pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          path: web
      - run: pnpm test
""",
            """
name: CI
on: [pull_request]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.head_ref }}
      - uses: actions/setup-node@v4
""",
        ):
            with self.assertRaises(WorkflowError) as cm:
                self.contract_for(ref="refs/pull/7/merge", body=body)
            self.assertIn("does not apply", str(cm.exception))
            self.assertIn("step 1", str(cm.exception))

    def test_a_checkout_step_without_inputs_still_translates_natively(self) -> None:
        contract = self.contract_for(
            body="""
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: make test
"""
        )
        self.assertFalse(contract.docker)
        self.assertEqual(contract.steps, ("make test",))

    def test_a_step_environment_runs_through_the_engine_rather_than_being_dropped(self) -> None:
        contract = self.contract_for(
            body="""
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: pnpm test
        env:
          NODE_ENV: test
"""
        )
        # the engine applies a step environment, the native path cannot, so the
        # job goes where the setting survives
        self.assertTrue(contract.docker)
        self.assertIn("env", contract.steps[0])

    def test_an_environment_value_naming_a_secret_is_refused_by_name(self) -> None:
        with self.assertRaises(WorkflowError) as cm:
            self.contract_for(
                body="""
name: CI
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    env:
      NODE_AUTH_TOKEN: ${{ secrets.NPM_TOKEN }}
    steps:
      - run: npm publish
"""
            )
        self.assertIn("secrets.NPM_TOKEN", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
