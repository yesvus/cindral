"""Every command and file the agent surface names must resolve to something real.

The surface hands an agent strings to act on: a next action, a tool description,
a README section. A name that resolves to nothing is an agent that runs a
command and gets exit 2, or waits for a check run no workflow creates, and no
behavioural test here would notice. So the names are checked against the
production parser and the shipped templates rather than against a second list
that could fall behind them.
"""
import argparse
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from cindral import cli, mcpserver, repositorystate
from cindral.policy import Policy

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
README_SECTION = "## Asking Cindral from a tool or an agent"
REPO = "example-org/example-app"
REFUSED = "refs/heads/attacker"

# `cindral <word>`, with the space, so the cindral-dispatch check run and the
# cindral_run tool names are not read as subcommands
SUBCOMMAND = re.compile(r"\bcindral ([a-z][a-z0-9-]*)")
TEMPLATE_PATH = re.compile(r"templates/([A-Za-z0-9._-]+\.ya?ml)")

POLICY = f"""
[hosted]
runner = "ubuntu-24.04"

[[dispatch.repositories]]
mode = "both"
repository = "{REPO}"
workflows = ["{repositorystate.ADAPTER_WORKFLOW}"]
refs = ["refs/heads/main"]
"""


def real_parser() -> tuple[argparse.ArgumentParser, Any]:
    """The production parser, captured without running a command.

    ``--help`` is the one argv the root parser answers and then exits on, so the
    whole argument table is registered and nothing is dispatched. Reading the
    real registration rather than re-listing the names in a test is what keeps a
    renamed or dropped subcommand from leaving a string that looks fine.
    """
    captured: list[tuple[argparse.ArgumentParser, Any]] = []
    add_subparsers = argparse.ArgumentParser.add_subparsers

    def record(self, **kwargs):
        subparsers = add_subparsers(self, **kwargs)
        captured.append((self, subparsers))
        return subparsers

    with (
        mock.patch.object(argparse.ArgumentParser, "add_subparsers", record),
        mock.patch.object(sys, "argv", ["cindral", "--help"]),
        mock.patch("sys.stdout", io.StringIO()),
    ):
        try:
            cli.main()
        except SystemExit:
            pass
    return captured[0]


def readme_agent_section() -> str:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    return readme.split(README_SECTION, 1)[1].split("\n## ", 1)[0]


def commands(text: str) -> list[str]:
    """The command invocations a next action names, split at the next one.

    Each ``cindral `` opens a fragment running to the next, so a step that offers
    two commands is checked twice rather than parsed once with the second
    command's words trailing the first.
    """
    cleaned = text.replace(",", " ")
    starts = [match.start() for match in re.finditer(r"\bcindral ", cleaned)]
    return [cleaned[start:starts[i + 1] if i + 1 < len(starts) else None]
            for i, start in enumerate(starts)]


def declared_workflows() -> set[str]:
    """The workflow filenames the shipped templates declare themselves as.

    A target repository installs a template under a filename of its own, so the
    name a dispatch is addressed by is the ``name:`` inside the template rather
    than the file on disk.
    """
    names = set()
    for path in sorted(p for p in TEMPLATES.iterdir() if p.suffix in {".yml", ".yaml"}):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("name:"):
                names.add(f"{line.split(':', 1)[1].strip()}.yml")
                break
    return names


class SurfaceStringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parser, subparsers = real_parser()
        self.subcommands = set(subparsers.choices)
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = Path(self._tmp.name) / "policy.toml"
        path.write_text(POLICY, encoding="utf-8")
        self.policy = Policy.load(path)

    def github(self, adapter=True, hooks=("https://broker.example.com/cindral/dispatch",)):
        client = mock.Mock()
        client.workflow_exists.return_value = adapter
        client.push_webhook_urls.return_value = hooks
        return client

    def next_steps(self) -> dict[str, str]:
        """The one next action per branch, so a new branch cannot go unchecked."""
        def read(github, ref="main"):
            return repositorystate.read_repository_state(
                REPO, github=github, policy=self.policy, ref=ref
            ).next_step

        return {
            "ready": read(self.github()),
            "adapter": read(self.github(adapter=False)),
            "webhook": read(self.github(hooks=())),
            "dispatch": read(self.github(), ref=REFUSED),
        }

    def surfaces(self) -> dict[str, str]:
        return {
            "mcpserver.INSTRUCTIONS": mcpserver.INSTRUCTIONS,
            **{f"{tool['name']} description": tool["description"] for tool in mcpserver.TOOLS},
            "README agent section": readme_agent_section(),
        }

    def accepts(self, argv: list[str]) -> bool:
        with mock.patch("sys.stderr", io.StringIO()):
            try:
                self.parser.parse_args(argv)
            except SystemExit:
                return False
        return True

    def longest_accepted(self, fragment: str) -> str:
        """The longest leading run of a command fragment the parser accepts.

        A next action is a command followed by an English sentence, so the whole
        string is not an argv and trimming to the accepted prefix is what makes
        the rest of the sentence prose rather than a parse error. A command the
        parser rejects at every length has no accepted prefix at all, which is
        the case worth failing on.
        """
        tokens = fragment.replace(",", " ").split()[1:]
        for size in range(len(tokens), 0, -1):
            if self.accepts(tokens[:size]):
                return " ".join(tokens[:size])
        return ""

    def test_every_command_the_surface_names_is_a_real_subcommand(self) -> None:
        for name, text in self.surfaces().items():
            for command in SUBCOMMAND.findall(text):
                self.assertIn(command, self.subcommands, f"{name} names cindral {command}")

    def test_every_next_step_names_a_command_the_parser_accepts(self) -> None:
        steps = self.next_steps()
        self.assertEqual(set(steps), {"ready", "adapter", "webhook", "dispatch"})
        for name, step in steps.items():
            fragments = commands(step or "")
            if not fragments:
                # the dispatch branch is an edit to the policy file, so it names
                # no command and there is nothing for the parser to reject
                self.assertNotIn("cindral ", step or "", name)
                continue
            for fragment in fragments:
                self.assertTrue(
                    self.longest_accepted(fragment),
                    f"the {name} next step names a command argparse rejects: {fragment}",
                )

    def test_every_template_the_surface_names_exists(self) -> None:
        for name, text in {**self.surfaces(), **self.next_steps()}.items():
            for path in TEMPLATE_PATH.findall(text):
                self.assertTrue((TEMPLATES / path).is_file(), f"{name} names templates/{path}")

    def test_the_default_workflow_the_surface_advertises_is_one_cindral_ships(self) -> None:
        # a default naming an adapter no template declares points an agent at a
        # filename that exists neither in the target repository nor in this one
        shipped = declared_workflows()
        tool = next(tool for tool in mcpserver.TOOLS if tool["name"] == "cindral_run")
        from_tool = tool["inputSchema"]["properties"]["workflow"]["description"].rsplit(" ", 1)[-1]
        from_cli = self.parser.parse_args(["run", REPO]).workflow
        self.assertIn(from_tool, shipped, "the cindral_run workflow default")
        self.assertEqual(from_cli, from_tool)


if __name__ == "__main__":
    unittest.main()
