"""What an agent gets from status, run, and explain, and what it cannot get.

The point of this surface is that a caller asks the broker instead of reading the
target repository. These tests pin the two halves of that: the reports answer
without GitHub, and no tool anywhere accepts a lane, a target, or a runner.
"""
import argparse
import inspect
import io
from io import BytesIO
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from cindral import agentapi, agentcli, brokerclient, mcpserver, repositorystate
from cindral.brokerclient import BrokerClient, BrokerError
from cindral.github import GitHubAPIError
from cindral.policy import Policy
from cindral.repositorystate import ADAPTER_WORKFLOW

REPO = "example-org/example-app"
REF = "main"

# the whole body a placement-free client is allowed to send. A key here is the
# broker's input, and a key absent from it is one a caller must never influence.
ROUTE_BODY_KEYS = {
    "requested_lane",
    "target",
    "repository_visibility",
    "quota_status",
    "estimated_minutes",
}

# every spelling a placement argument would take, including the nested ones. A
# schema that grows one of these is a schema that hands out placement.
PLACEMENT_PROPERTIES = (
    "lane",
    "requested_lane",
    "target",
    "device",
    "runs_on",
    "runner",
    "placement",
)

POLICY = f"""
[hosted]
runner = "ubuntu-24.04"

[[dispatch.repositories]]
mode = "both"
repository = "{REPO}"
workflows = ["{ADAPTER_WORKFLOW}"]
refs = ["refs/heads/main"]
"""


class FakeBroker:
    """Stands in for the broker so the tests never need a listener."""

    def __init__(self, **responses):
        self.responses = responses
        self.calls = []

    def health(self):
        return {"status": "ok", "build": {"version": "9.9.9", "commit": "abc", "built_at": "now"}}

    def pool(self):
        return {
            "queue_depth": {"pending": 1, "running": 2, "success": 5, "failure": 1},
            "devices": [
                {"name": "device-alpha", "status": "online", "available_slots": 0, "agent_online": True},
                {"name": "off", "status": "offline", "available_slots": 0, "agent_online": False},
            ],
            "stalled_reason": None,
        }

    def route(self, body):
        self.calls.append(("route", body))
        return {"lane": "hosted", "runs_on": ["ubuntu-24.04"], "reason": "quota available", "runner": None}

    def dispatch(self, repository, workflow, ref, body):
        self.calls.append(("dispatch", repository, workflow, ref, body))
        return {
            "lane": "hosted",
            "runs_on": ["ubuntu-24.04"],
            "reason": "quota available",
            "runner": None,
            "dispatched": True,
        }


def config_with(tmpdir: str, broker: FakeBroker | None = None, **kwargs) -> agentapi.AgentConfig:
    return agentapi.AgentConfig(
        broker_url="http://broker.invalid:8095",
        dispatch_token="dispatch-token",
        pool_token="pool-token",
        policy_path=str(Path(tmpdir) / "policy.toml"),
        **kwargs,
    )


class ReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        (Path(self._tmp.name) / "policy.toml").write_text(POLICY, encoding="utf-8")
        self.broker = FakeBroker()
        self.config = config_with(self._tmp.name)
        # these print a report, so the noise is captured rather than left in the
        # test output where it reads like a failure
        self._quiet = mock.patch("sys.stdout", io.StringIO())
        self._quiet.start()
        self.addCleanup(self._quiet.stop)
        patcher = mock.patch.object(agentapi.BrokerClient, "health", self.broker.health)
        pool = mock.patch.object(agentapi.BrokerClient, "pool", self.broker.pool)
        route = mock.patch.object(agentapi.BrokerClient, "route", self.broker.route)
        dispatch = mock.patch.object(agentapi.BrokerClient, "dispatch", self.broker.dispatch)
        for started in (patcher, pool, route, dispatch):
            started.start()
            self.addCleanup(started.stop)

    def test_status_answers_without_github(self) -> None:
        # no GitHub token is configured, so the repository half is answered from
        # the policy alone rather than failing on an absent credential
        report = agentapi.status_report(self.config, REPO, REF)
        self.assertTrue(report["reachable"])
        self.assertEqual(report["build"]["version"], "9.9.9")
        self.assertEqual(report["queue_depth"]["pending"], 1)
        self.assertEqual([device["name"] for device in report["devices"]], ["device-alpha", "off"])
        self.assertIsNone(report["repository"]["github_error"])

    def test_status_reports_a_missing_policy_instead_of_raising(self) -> None:
        config = agentapi.AgentConfig(policy_path=str(Path(self._tmp.name) / "absent.toml"))
        report = agentapi.status_report(config, REPO, REF)
        self.assertIn("no policy file", report["policy_error"])
        # the broker half still answers, so one missing file does not blank the report
        self.assertTrue(report["reachable"])
        self.assertIsNone(report["repository"]["permitted"])

    def test_explain_returns_the_decision_and_starts_nothing(self) -> None:
        report = agentapi.explain_report(self.config, REPO, REF)
        self.assertEqual(report["decision"]["lane"], "hosted")
        self.assertTrue(report["state"]["ready"])
        # a routing question must not dispatch, so only the route call appears
        self.assertEqual([call[0] for call in self.broker.calls], ["route"])

    def test_explain_asks_the_policy_without_ghosting_a_lane(self) -> None:
        agentapi.explain_report(self.config, REPO, REF, "public", "available", 25.0)
        body = next(call[1] for call in self.broker.calls if call[0] == "route")
        self.assertEqual(body["requested_lane"], "auto")
        self.assertIsNone(body["target"])
        self.assertEqual(body["repository_visibility"], "public")
        self.assertEqual(body["estimated_minutes"], 25.0)

    def test_run_reports_the_decision_the_broker_made(self) -> None:
        report = agentapi.run_report(self.config, REPO, REF)
        self.assertTrue(report["dispatched"])
        self.assertEqual(report["lane"], "hosted")
        self.assertEqual(report["reason"], "quota available")

    def test_run_dispatches_the_ref_the_caller_named(self) -> None:
        # the report names the ref the caller asked for, so dispatching a
        # different one leaves an agent polling a ref with no check run on it
        agentapi.run_report(self.config, REPO, "release-2026-09")
        call = next(c for c in self.broker.calls if c[0] == "dispatch")
        self.assertEqual(call[3], "release-2026-09")

    def test_explain_names_the_risk_of_the_lane_it_would_use(self) -> None:
        # explain is what the instructions push an agent to for the placement
        # question, so it is where whose hardware runs the code is most useful
        report = agentapi.explain_report(self.config, REPO, REF)
        self.assertIn("GitHub runner", report["decision"]["lane_risk"])

    def test_run_does_not_assume_a_local_pool(self) -> None:
        # `unknown` reads as the local pool in the policy, so a default of unknown
        # put every agent's run on a maintainer's device. The assumption is named
        # in the report so a reader can see it rather than infer it.
        agentapi.run_report(self.config, REPO, REF)
        body = next(call[4] for call in self.broker.calls if call[0] == "dispatch")
        self.assertEqual(body["quota_status"], "available")
        self.assertEqual(body["requested_lane"], "auto")

    def test_run_names_the_risk_of_the_lane_it_resolved(self) -> None:
        hosted = agentapi.run_report(self.config, REPO, REF)
        self.assertEqual(hosted["quota_assumption"], "available")
        self.assertIn("GitHub runner", hosted["lane_risk"])

    def test_a_self_hosted_lane_is_reported_as_a_maintainer_device(self) -> None:
        # the lane name alone does not say whose hardware the code runs on, and a
        # caller that asked for no placement in particular is the one reading this
        def local(_client, repository, workflow, ref, body):
            return {
                "lane": "fallback",
                "runs_on": ["self-hosted", "Linux", "ARM64", "fallback"],
                "reason": "hosted quota is unknown",
                "runner": "device-alpha",
                "dispatched": True,
            }

        with mock.patch.object(agentapi.BrokerClient, "dispatch", local):
            report = agentapi.run_report(self.config, REPO, REF)
        self.assertEqual(report["lane"], "fallback")
        self.assertIn("self-hosted", report["lane_risk"])
        self.assertIn("device", report["lane_risk"])

    def test_run_sends_no_placement_of_its_own(self) -> None:
        agentapi.run_report(self.config, REPO, REF)
        body = next(call[4] for call in self.broker.calls if call[0] == "dispatch")
        self.assertEqual(set(body), ROUTE_BODY_KEYS)
        self.assertEqual(body["requested_lane"], "auto")
        self.assertIsNone(body["target"])

    def test_a_hidden_lane_in_the_arguments_never_reaches_the_broker(self) -> None:
        # A client is free to send a property the schema never named, so the
        # schema is not where this is settled. This drives the real handler with
        # every placement spelling on the wire, which is the only way a caller
        # asking for the device lane can be caught, and reads what the broker
        # was actually asked for.
        out = io.StringIO()
        session = mcpserver.McpSession(self.config, io.StringIO(), out)
        session.handle(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "cindral_run",
                "arguments": {
                    "repository": REPO,
                    "ref": REF,
                    "requested_lane": "device",
                    "lane": "device",
                    "target": "desktop",
                    "runs_on": ["self-hosted", "desktop"],
                    "runner": "desktop",
                },
            },
        }))
        reply = json.loads(out.getvalue().splitlines()[-1])
        self.assertNotIn("error", reply)
        call = next(c for c in self.broker.calls if c[0] == "dispatch")
        self.assertEqual(set(call[4]), ROUTE_BODY_KEYS)
        self.assertEqual(call[4]["requested_lane"], "auto")
        self.assertIsNone(call[4]["target"])
        # the declared arguments too, so a hidden key routed into one of them dies
        self.assertEqual((call[1], call[2], call[3]), (REPO, ADAPTER_WORKFLOW, REF))

    def test_unreachable_broker_is_reported_not_raised(self) -> None:
        with mock.patch.object(
            agentapi.BrokerClient, "health", side_effect=BrokerError("cannot reach the broker")
        ):
            report = agentapi.broker_overview(self.config)
        self.assertFalse(report["reachable"])
        self.assertIn("cannot reach the broker", report["error"])

    def test_missing_pool_token_says_so_instead_of_failing(self) -> None:
        config = agentapi.AgentConfig(policy_path=self.config.policy_path)
        overview = agentapi.broker_overview(config)
        self.assertIn("CINDRAL_POOL_TOKEN is unset", overview["pool"])


class RepositoryStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "policy.toml"
        self.path.write_text(POLICY, encoding="utf-8")
        self.loaded = Policy.load(self.path)

    def github(self, adapter=True, hooks=("https://broker.example.com/cindral/dispatch",)):
        client = mock.Mock()
        client.workflow_exists.return_value = adapter
        client.push_webhook_urls.return_value = hooks
        return client

    def test_wired_repository_is_ready_and_says_how_to_run_it(self) -> None:
        state = repositorystate.read_repository_state(
            REPO, github=self.github(), policy=self.loaded, policy_path=str(self.path), ref=REF
        )
        self.assertTrue(state.ready)
        # the whole string, because `run` takes the repository positionally and a
        # substring check lets a `--repo` flag through to an agent that then gets
        # an unrecognized-argument error instead of a dispatch
        self.assertEqual(state.next_step, f"cindral run {REPO} --ref {REF}")

    def test_missing_adapter_is_a_blocker_with_the_onboarding_command(self) -> None:
        state = repositorystate.read_repository_state(
            REPO, github=self.github(adapter=False), policy=self.loaded, ref=REF
        )
        self.assertFalse(state.ready)
        self.assertIn(ADAPTER_WORKFLOW, state.blockers[0])
        self.assertIn("cindral onboard", state.next_step)

    def test_missing_webhook_is_a_blocker_naming_reconcile(self) -> None:
        state = repositorystate.read_repository_state(
            REPO, github=self.github(hooks=()), policy=self.loaded, ref=REF
        )
        self.assertFalse(state.ready)
        self.assertTrue(any("webhook" in blocker for blocker in state.blockers))
        self.assertIn("reconcile-webhooks", state.next_step)

    def test_refused_ref_blocks_and_names_the_policy(self) -> None:
        state = repositorystate.read_repository_state(
            REPO, github=self.github(), policy=self.loaded, ref="refs/heads/attacker"
        )
        self.assertFalse(state.ready)
        self.assertTrue(any("the policy refuses" in blocker for blocker in state.blockers))
        self.assertIn("[dispatch]", state.next_step)

    def test_a_refused_ref_blocks_without_a_github_token(self) -> None:
        # GITHUB_TOKEN is optional, so the refusal used to be computed only where
        # GitHub was read. A caller with no token was then told a repository the
        # policy refuses was ready, and handed a run instruction the broker
        # answers with a 403.
        state = repositorystate.read_repository_state(
            REPO, github=None, policy=self.loaded, ref="refs/heads/attacker"
        )
        self.assertFalse(state.ready)
        self.assertIn("[dispatch]", state.next_step)

    def test_github_failure_is_an_unknown_not_a_blocker(self) -> None:
        client = mock.Mock()
        client.workflow_exists.side_effect = GitHubAPIError("token lacks hook access")
        state = repositorystate.read_repository_state(REPO, github=client, policy=self.loaded, ref=REF)
        self.assertTrue(state.ready)
        self.assertIn("hook access", state.github_error)

    def test_malformed_repository_is_refused_before_any_lookup(self) -> None:
        with self.assertRaisesRegex(ValueError, "owner/name"):
            repositorystate.read_repository_state("../../etc/passwd", policy=self.loaded)


class NoPlacementTest(unittest.TestCase):
    """The guarantee this surface exists to establish, checked at every layer.

    A schema is only a claim: a client is free to send a property the schema
    never named, and a handler that reads one would honour it. The behavioural
    test in ReportTest drives a real session and asserts the body key set,
    which is the guarantee; these pin the surfaces a caller reads before it
    ever sends anything, and the boundary the client function is built with.
    """

    def test_auto_route_request_takes_no_placement_parameter(self) -> None:
        # the whole signature, not the parameters some tool happens to advertise:
        # a lane or target added here is a lane a caller can set even while every
        # tool schema still refuses to name it
        self.assertEqual(
            set(inspect.signature(brokerclient.auto_route_request).parameters),
            {"repository_visibility", "quota_status", "estimated_minutes"},
        )

    def test_the_route_body_carries_no_key_the_broker_did_not_ask_for(self) -> None:
        body = brokerclient.auto_route_request()
        # the whole key set, so a new body field is a review decision rather
        # than something an added line introduces silently
        self.assertEqual(set(body), ROUTE_BODY_KEYS)
        self.assertEqual(body["requested_lane"], "auto")
        self.assertIsNone(body["target"])

    def test_every_tool_schema_refuses_an_undeclared_property(self) -> None:
        # additionalProperties false is what makes a client drop an unknown
        # argument before sending it. Without it the schemas are documentation.
        for tool in mcpserver.TOOLS:
            self.assertIs(
                tool["inputSchema"]["additionalProperties"],
                False,
                f"{tool['name']} accepts an undeclared property",
            )

    def test_no_tool_schema_names_a_placement_property(self) -> None:
        for tool in mcpserver.TOOLS:
            properties = tool["inputSchema"]["properties"]
            for forbidden in PLACEMENT_PROPERTIES:
                self.assertNotIn(forbidden, properties, f"{tool['name']} exposes {forbidden}")

    def test_no_handler_names_a_placement_key_in_a_one_line_handler(self) -> None:
        # a lint, not the guarantee. It greps the source text of a handler for a
        # placement key, so it is blind to a single-quoted key and to a handler
        # that reads one inside a helper it delegates to. The behavioural test
        # that drives a real session and asserts the whole body key set is what
        # makes either of those red; this only catches the accidental direct
        # read, where an added argument line is the whole of the change.
        for name, handler in mcpserver.HANDLERS.items():
            source = inspect.getsource(handler)
            for forbidden in ("lane", "target", "runs_on", "runner"):
                self.assertNotIn(
                    f'"{forbidden}"', source, f"{name} reads the {forbidden} argument"
                )
class CliExitCodeTest(unittest.TestCase):
    def _run(self, argv, config):
        buffer = io.StringIO()
        with mock.patch("sys.stdout", buffer), mock.patch("sys.stderr", buffer):
            code = agentcli.dispatch_agent_command(config, _args(argv))
        return code, buffer.getvalue()

    def test_unreachable_broker_exits_nonzero(self) -> None:
        config = agentapi.AgentConfig(broker_url="http://broker.invalid")
        with mock.patch.object(
            agentapi.BrokerClient, "health", side_effect=BrokerError("unreachable")
        ):
            code, _ = self._run(["status"], config)
        self.assertEqual(code, 1)

    def test_explain_without_a_decision_exits_nonzero(self) -> None:
        report = {"repository": REPO, "ref": REF, "decision": None, "state": {"ready": False}}
        with mock.patch.object(agentapi, "explain_report", return_value=report):
            code, _ = self._run(["explain", REPO], agentapi.AgentConfig())
        self.assertEqual(code, 1)


def _args(argv):
    namespace = argparse.Namespace(command=argv[0], json=True, repo=None, ref=REF, mcp=False)
    if argv[0] == "run":
        namespace.repository, namespace.workflow, namespace.visibility = argv[1], ADAPTER_WORKFLOW, "private"
    if argv[0] == "explain":
        namespace.repository, namespace.visibility = argv[1], "private"
        namespace.quota, namespace.minutes = "unknown", 10.0
    return namespace


class McpSurfaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.out = io.StringIO()
        self.session = mcpserver.McpSession(agentapi.AgentConfig(), io.StringIO(), self.out)

    def send(self, payload) -> dict:
        self.session.handle(json.dumps(payload))
        return json.loads(self.out.getvalue().splitlines()[-1])

    def test_initialize_reports_instructions_naming_the_boundary(self) -> None:
        reply = self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        result = reply["result"]
        self.assertEqual(result["serverInfo"]["name"], "cindral")
        self.assertIn("none of these tools accepts a lane", result["instructions"])

    def test_tools_list_advertises_no_placement_argument(self) -> None:
        tools = self.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        names = {tool["name"] for tool in tools}
        self.assertEqual(names, {"cindral_status", "cindral_explain", "cindral_run"})
        for tool in tools:
            properties = tool["inputSchema"]["properties"]
            for forbidden in PLACEMENT_PROPERTIES:
                self.assertNotIn(forbidden, properties, f"{tool['name']} exposes {forbidden}")

    def test_unknown_tool_is_an_error_not_a_silent_empty(self) -> None:
        reply = self.send(
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "cindral_dispatch"}}
        )
        self.assertIn("unknown tool", reply["error"]["message"])

    def test_tool_result_carries_json_text_content(self) -> None:
        report = {"repository": REPO, "ready": True}
        with mock.patch.dict(mcpserver.HANDLERS, {"cindral_status": lambda *_: report}):
            reply = self.send(
                {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "method": "tools/call",
                    "params": {"name": "cindral_status", "arguments": {"repository": REPO}},
                }
            )
        content = reply["result"]["content"][0]
        self.assertEqual(content["type"], "text")
        self.assertEqual(json.loads(content["text"])["repository"], REPO)
        self.assertFalse(reply["result"]["isError"])

    def test_broker_refusal_is_a_result_the_agent_reads(self) -> None:
        with mock.patch.dict(
            mcpserver.HANDLERS,
            {"cindral_run": lambda *_: {"dispatched": False, "error": "ref is not permitted"}},
        ):
            reply = self.send(
                {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": "cindral_run", "arguments": {"repository": REPO}},
                }
            )
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("ref is not permitted", reply["result"]["content"][0]["text"])

    def test_malformed_json_gets_a_parse_error(self) -> None:
        self.session.handle("{not json")
        reply = json.loads(self.out.getvalue().splitlines()[-1])
        self.assertEqual(reply["error"]["code"], -32700)

    def test_a_notification_gets_no_reply(self) -> None:
        # JSON-RPC: a request with no id is a notification and takes no response.
        # Answering one desynchronises a host that matches replies by id, because
        # our unsolicited reply is read as the answer to whatever it sent next.
        for payload in (
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "method": "tools/list"},
            {"jsonrpc": "2.0", "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "unknown/thing"},
        ):
            with self.subTest(method=payload["method"]):
                self.out.truncate(0)
                self.out.seek(0)
                self.session.handle(json.dumps(payload))
                self.assertEqual(self.out.getvalue(), "")

    def test_the_session_still_answers_after_a_notification(self) -> None:
        self.session.handle(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        reply = self.send({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
        self.assertEqual(reply["id"], 9)

    def test_missing_argument_is_reported_by_name(self) -> None:
        reply = self.send(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "cindral_status", "arguments": {}},
            }
        )
        self.assertIn("repository", reply["error"]["message"])

    def test_a_null_estimate_is_a_tool_error_not_a_dead_session(self) -> None:
        # float(None) used to raise out of handle and out of serve, so one bad
        # argument killed the process and the next call got no reply at all
        reply = self.send(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "cindral_explain",
                    "arguments": {"repository": REPO, "estimated_minutes": None},
                },
            }
        )
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("estimated_minutes must be a number", reply["result"]["content"][0]["text"])
        # the session is still there afterwards
        self.assertIn("tools", self.send({"jsonrpc": "2.0", "id": 8, "method": "tools/list"})["result"])

    def test_a_non_numeric_estimate_is_refused_by_name(self) -> None:
        for value in (None, "ten", [10], {"minutes": 10}, True):
            with self.subTest(value=value):
                reply = self.send(
                    {
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "tools/call",
                        "params": {
                            "name": "cindral_explain",
                            "arguments": {"repository": REPO, "estimated_minutes": value},
                        },
                    }
                )
                self.assertIn("estimated_minutes must be a number", reply["result"]["content"][0]["text"])

    def test_serve_survives_a_handler_that_raises(self) -> None:
        # the protocol has an internal-error code, and losing the process
        # mid-conversation leaves an agent host unable to retry
        def boom(_config, _args):
            raise TypeError("simulated handler fault")

        stdin = io.StringIO(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "cindral_status"}})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            + "\n"
        )
        out = io.StringIO()
        with mock.patch.dict(mcpserver.HANDLERS, {"cindral_status": boom}):
            code = mcpserver.McpSession(agentapi.AgentConfig(), stdin, out).serve()
        self.assertEqual(code, 0)
        replies = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual(replies[0]["error"]["code"], -32603)
        # the second request is answered, so the loop did not stop at the first
        self.assertIn("tools", replies[1]["result"])


class OutcomeSignalTest(unittest.TestCase):
    """The guidance must name a signal a dispatch actually produces."""

    def test_instructions_point_at_the_check_run_the_dispatch_produces(self) -> None:
        # `cindral run` reaches /v1/dispatch, which never posts a commit status,
        # so pointing at cindral/ci sent an agent polling something that never
        # appears and concluding the run never happened
        self.assertIn("cindral-dispatch", mcpserver.INSTRUCTIONS)
        self.assertNotIn("cindral/ci", mcpserver.INSTRUCTIONS)

    def test_no_surface_string_promises_a_commit_status_a_dispatch_never_posts(self) -> None:
        surfaces = {
            "mcpserver.INSTRUCTIONS": mcpserver.INSTRUCTIONS,
            "cindral_run description": next(
                tool["description"] for tool in mcpserver.TOOLS if tool["name"] == "cindral_run"
            ),
            "run_report docstring": agentapi.run_report.__doc__ or "",
        }
        # the bare token, not the phrase "cindral/ci commit status": a reworded
        # promise survives a check that bans only one exact sentence
        for name, text in surfaces.items():
            self.assertIn("cindral-dispatch", text, name)
            self.assertNotIn("cindral/ci", text, name)

    def test_the_readme_says_where_the_outcome_is(self) -> None:
        # the README is the one guidance site with no code to pin it, so a
        # regression to the commit status a dispatch never posts is invisible
        # to the other four checks and reads as the full suite passing
        readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
        section = readme.split("## Asking Cindral from a tool or an agent", 1)[1].split("\n## ", 1)[0]
        self.assertIn("cindral-dispatch", section)
        self.assertNotIn("cindral/ci", section)

    def test_the_cli_says_where_the_outcome_is(self) -> None:
        buffer = io.StringIO()
        report = {"repository": REPO, "ref": REF, "dispatched": True, "lane": "hosted"}
        with mock.patch.object(agentcli, "run_report", return_value=report):
            with mock.patch("sys.stdout", buffer), mock.patch("sys.stderr", buffer):
                agentcli.command_run(agentapi.AgentConfig(), REPO, REF)
        self.assertIn("cindral-dispatch", buffer.getvalue())
        self.assertNotIn("cindral/ci", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
