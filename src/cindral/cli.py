"""Command-line interface for Cindral."""
import argparse
import json
import os
from pathlib import Path
import socket
import sys

from .build_info import checkout_build, current_build
from .freshness import checkout_root
from .agent import (
    DEFAULT_HEARTBEAT_SECONDS,
    MAX_HEARTBEAT_SECONDS,
    MIN_IDLE_SECONDS,
    Agent,
    CindralClient,
)
from .agentcli import parse_cindral_args
from .cache_client import CindralCacheClient
from .executor import MIN_JOB_CPUS, DockerExecutor
from .github import GitHubAPIError, GitHubClient, is_repository_slug
from .models import RouteRequest
from .agent import MAX_CONCURRENCY, ONLINE_WINDOW_SECONDS
from .options import (
    byte_rate,
    capacity_option,
    cpu_limit,
    heartbeat_interval,
    idle_interval,
    memory_size,
    positive_int,
    positive_number,
)
from .onboarding import (
    DEFAULT_POLICY_PATH,
    check_lane_readiness,
    describe_policy,
    resolve_policy_path,
    validate_adapter,
)
from .policy import Policy
from .preflight import DockerPreflight
from .reconcile import DEFAULT_OPS_REPOSITORY, adoption_allowlist, device_repositories, reconcile_webhooks, run_adoption, slug_set, validate_webhook_url
from .service import serve
from .state import load_runners

DEFAULT_WEBHOOK_URL = "https://cindral.example.com/cindral/dispatch"


def main() -> None:
    parser = argparse.ArgumentParser(prog="cindral")
    subparsers = parser.add_subparsers(dest="command", required=True)

    route = subparsers.add_parser("route")
    route.add_argument("--policy", default=DEFAULT_POLICY_PATH)
    route.add_argument("--state", default="examples/state.json")
    route.add_argument("--request", default="examples/request.json")

    onboard = subparsers.add_parser("onboard")
    onboard.add_argument("repository", help="GitHub repository in owner/name format")
    onboard.add_argument(
        "--policy",
        help="policy the broker dispatches with, defaults to $CINDRAL_POLICY then config/policy.toml",
    )
    onboard.add_argument("--checkout", default=".", help="repository checkout containing the adapter")
    onboard.add_argument("--adapter", help="path to the repository's workflow_dispatch adapter")
    onboard.add_argument("--github-token-env", default="GITHUB_TOKEN")
    onboard.add_argument("--register-webhook", action="store_true")
    onboard.add_argument("--webhook-url", default=os.environ.get("CINDRAL_WEBHOOK_URL", DEFAULT_WEBHOOK_URL))
    onboard.add_argument("--webhook-secret-env", default="CINDRAL_WEBHOOK_SECRET")

    reconcile = subparsers.add_parser("reconcile-webhooks")
    reconcile.add_argument("--policy", help="policy the broker uses, defaults to $CINDRAL_POLICY")
    reconcile.add_argument("--webhook-url", default=os.environ.get("CINDRAL_WEBHOOK_URL"))
    reconcile.add_argument("--webhook-secret-env", default="CINDRAL_WEBHOOK_SECRET")
    reconcile.add_argument("--github-token-env", default="GITHUB_TOKEN")
    reconcile.add_argument("--apply", action="store_true")

    adopt = subparsers.add_parser("adopt")
    adopt.add_argument("--repository", action="append", metavar="OWNER/NAME")
    adopt.add_argument("--all-owned", action="store_true", help="adopt every owned private repository instead of only the named ones")
    adopt.add_argument("--webhook-url", default=os.environ.get("CINDRAL_WEBHOOK_URL"))
    adopt.add_argument("--webhook-secret-env", default="CINDRAL_WEBHOOK_SECRET")
    adopt.add_argument("--github-token-env", default="GITHUB_TOKEN")
    adopt.add_argument("--ops-repository", default=capacity_option("CINDRAL_OPS_REPOSITORY", DEFAULT_OPS_REPOSITORY))
    adopt.add_argument("--apply", action="store_true")

    service = subparsers.add_parser("serve")
    service.add_argument("--policy", default=DEFAULT_POLICY_PATH)
    service.add_argument("--state", default="examples/state.json")
    service.add_argument("--host", default="127.0.0.1")
    service.add_argument("--port", type=int, default=8095)
    service.add_argument("--github-token-env", default="GITHUB_TOKEN")

    agent = subparsers.add_parser("agent")
    agent.add_argument("--url", default=os.environ.get("CINDRAL_BROKER_URL", "http://127.0.0.1:8095"))
    agent.add_argument("--token-env", default="CINDRAL_AGENT_TOKEN")
    agent.add_argument("--device", default=os.environ.get("CINDRAL_DEVICE") or socket.gethostname())
    agent.add_argument("--labels", default=os.environ.get("CINDRAL_AGENT_LABELS", ""))
    agent.add_argument("--workspace", default=os.environ.get("CINDRAL_AGENT_WORKSPACE", "/var/lib/cindral/work"))
    agent.add_argument("--log-dir", default=os.environ.get("CINDRAL_AGENT_LOG_DIR", ""))
    agent.add_argument("--docker", default=os.environ.get("CINDRAL_AGENT_DOCKER", "docker"))
    agent.add_argument("--github-token-env", default="GITHUB_TOKEN")
    agent.add_argument("--lease-seconds", type=int, default=int(os.environ.get("CINDRAL_JOB_LEASE_SECONDS", "300")))
    agent.add_argument(
        "--idle-seconds",
        type=idle_interval,
        default=capacity_option("CINDRAL_AGENT_IDLE_SECONDS", "15"),
        help=f"wait between empty claims, at least {MIN_IDLE_SECONDS} seconds",
    )
    agent.add_argument(
        "--max-concurrency",
        type=positive_int,
        default=capacity_option("CINDRAL_AGENT_MAX_CONCURRENCY", "1"),
        help="jobs this device runs at once (default 1)",
    )
    agent.add_argument(
        "--job-cpus",
        type=cpu_limit,
        default=capacity_option("CINDRAL_JOB_CPUS"),
        help=f"cpu cap per job container, at least {MIN_JOB_CPUS}, unset to leave it uncapped",
    )
    agent.add_argument(
        "--job-disk-write",
        type=byte_rate,
        default=capacity_option("CINDRAL_JOB_DISK_WRITE"),
        help="disk write cap per job container in bytes per second, unset to leave it uncapped",
    )
    agent.add_argument(
        "--job-disk-read",
        type=byte_rate,
        default=capacity_option("CINDRAL_JOB_DISK_READ"),
        help="disk read cap per job container in bytes per second, unset to leave it uncapped",
    )
    agent.add_argument(
        "--job-memory",
        type=memory_size,
        default=capacity_option("CINDRAL_JOB_MEMORY"),
        help="memory cap per job container, such as 12g; unset for no cap",
    )
    agent.add_argument(
        "--heartbeat-seconds",
        type=heartbeat_interval,
        default=capacity_option("CINDRAL_AGENT_HEARTBEAT_SECONDS", str(DEFAULT_HEARTBEAT_SECONDS)),
        help=f"device heartbeat interval, default {DEFAULT_HEARTBEAT_SECONDS:g}, at most {MAX_HEARTBEAT_SECONDS:g}",
    )
    agent.add_argument("--once", action="store_true")

    args = parse_cindral_args(parser, subparsers)
    if args.command == "agent":
        token = os.environ.get(args.token_env)
        if not token:
            parser.error(f"set {args.token_env} to the broker's agent token")
        labels = tuple(label for label in (part.strip() for part in args.labels.split(",")) if label)
        executor = DockerExecutor(
            token=os.environ.get(args.github_token_env),
            workspace_root=args.workspace,
            log_dir=args.log_dir or None,
            docker=args.docker,
            cache_client=CindralCacheClient(args.url, token),
            cpus=args.job_cpus,
            memory=args.job_memory,
            disk_write=args.job_disk_write,
            disk_read=args.job_disk_read,
            # the labels the agent advertises, so a job whose `runs-on` asks for
            # hardware this machine does not have is refused here rather than run
            device_labels=labels,
        )

        def log(message: str) -> None:
            print(message, file=sys.stderr, flush=True)

        # Printed once, here, because this is where the caps are decided. An
        # uncapped device was previously visible only by finding the word inside
        # the log of a build it happened to run, which nobody reads before pointing
        # a stream of jobs at a host.
        log(f"cindral: {executor.describe_limits()}")

        try:
            # the check is built from the executor that will do the work, so it
            # answers for the same docker binary and the same process
            preflight = DockerPreflight(executor.docker, executor.runner.capture, note=log)
            # An agent started from a checkout has no image to be asked what it
            # is, and the broker cannot order two commits without the history
            # between them. So it names its own commit and keeps the tree that
            # commit lives in, and the agent refuses work it is behind on.
            root = checkout_root()
            runner = Agent(
                CindralClient(
                    args.url,
                    token,
                    build=checkout_build(root) if root else current_build(),
                ),
                args.device,
                executor,
                labels=labels,
                lease_seconds=args.lease_seconds,
                idle_seconds=args.idle_seconds,
                max_concurrency=args.max_concurrency,
                heartbeat_seconds=args.heartbeat_seconds,
                preflight=preflight.ready,
                log=log,
                source_root=root,
            )
            if root is None:
                log(
                    "cindral: this agent is not running from a git checkout, so it cannot"
                    " tell the broker which commit it is. Install it from a checkout to"
                    " get the staleness check."
                )
        except ValueError as exc:
            # the agent guards the values that would otherwise misbehave quietly,
            # such as a non-positive idle wait spinning the claim endpoint
            parser.error(str(exc))
        print(f"cindral agent {args.device} on {args.url} with {args.max_concurrency} slot(s)")
        if args.once:
            runner.run_once()
        else:
            runner.serve_forever()
        return

    if args.command == "serve":
        serve(args.policy, args.state, args.host, args.port, os.environ.get(args.github_token_env))
        return

    if args.command == "reconcile-webhooks":
        policy_path = resolve_policy_path(args.policy)
        repositories = device_repositories(Policy.load(policy_path))
        if not repositories:
            parser.error(f"no device repositories in {policy_path}")
        print(f"Policy: {policy_path}")
        if not args.apply:
            for repository in repositories:
                print(f"  {repository}: would reconcile {args.webhook_url or '(set CINDRAL_WEBHOOK_URL)'}")
            return
        secret = capacity_option(args.webhook_secret_env)
        token = capacity_option(args.github_token_env)
        if not secret or not token or not args.webhook_url:
            parser.error(f"set {args.webhook_secret_env}, {args.github_token_env}, and CINDRAL_WEBHOOK_URL before applying")
        try:
            validate_webhook_url(args.webhook_url)
        except ValueError as exc:
            parser.error(str(exc))
        github = GitHubClient(token)
        changed, failed = reconcile_webhooks(
            repositories,
            lambda repository: github.ensure_push_webhook(repository, args.webhook_url, secret),
        )
        for repository, result in changed:
            print(f"  {repository}: {result}")
        for repository, error in failed:
            print(f"  {repository}: {error}", file=sys.stderr)
        if failed:
            raise SystemExit(1)
        return

    if args.command == "adopt":
        token = capacity_option(args.github_token_env)
        if not token:
            parser.error(f"set {args.github_token_env} to a token that reads repositories and manages webhooks")
        allowlist = adoption_allowlist(args.repository, capacity_option("CINDRAL_ADOPT_REPOSITORIES", ""), parser.error)
        exclusions = slug_set(capacity_option("CINDRAL_ADOPT_EXCLUDE", ""))
        if not run_adoption(args, GitHubClient(token), capacity_option(args.webhook_secret_env), exclusions, allowlist, parser.error):
            raise SystemExit(1)
        return

    if args.command == "onboard":
        if not is_repository_slug(args.repository):
            parser.error("repository must use the owner/name format")
        token = os.environ.get(args.github_token_env)
        if not token:
            parser.error(f"set {args.github_token_env} to a token with runner, workflow, and webhook access")
        policy_path = resolve_policy_path(args.policy)
        policy = Policy.load(policy_path)
        adapter = Path(args.adapter) if args.adapter else Path(args.checkout) / ".github/workflows/cindral-dispatch.yml"
        errors = validate_adapter(adapter, policy)
        print(f"Repository: {args.repository}")
        print(describe_policy(args.policy, policy_path, policy))
        print(f"Adapter: {'valid' if not errors else 'needs changes'} ({adapter})")
        for error in errors:
            print(f"  - {error}")
        github = GitHubClient(token)
        try:
            runners = github.list_runners(args.repository)
        except GitHubAPIError as exc:
            print(f"Runner lookup failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        readiness = check_lane_readiness(policy, runners)
        print("Lanes:")
        for lane in readiness:
            if lane.ready:
                detail = f"online: {', '.join(lane.online)}" if lane.online else "GitHub-hosted"
                print(f"  {lane.lane}: ready ({detail})")
            elif lane.registered:
                print(f"  {lane.lane}: setup required (registered but offline: {', '.join(lane.registered)})")
            else:
                labels = ", ".join(lane.required_labels)
                print(f"  {lane.lane}: setup required (register a runner with labels: {labels})")
        # hosted and fallback are the lanes a dispatch actually uses by default.
        # device and burst are opt-in extras that only exist where those runners
        # are registered, so their absence is information, not a blocker.
        by_lane = {lane.lane: lane for lane in readiness}
        default_lanes = [by_lane[name] for name in ("hosted", "fallback") if name in by_lane]
        default_ready = not errors and all(lane.ready for lane in default_lanes)
        optional_missing = [lane.lane for lane in readiness if not lane.ready and lane not in default_lanes]
        if errors:
            print("Overall: adapter needs changes (see above)")
            raise SystemExit(1)
        if default_ready:
            print("Overall: ready to wire up (hosted and fallback lanes both resolve)")
            if optional_missing:
                print(f"Optional lanes not available in this repository: {', '.join(optional_missing)}")
                print("  These are only used when a dispatch explicitly requests them.")
            if args.register_webhook:
                secret = capacity_option(args.webhook_secret_env, "")
                if not secret:
                    print(
                        f"Webhook setup failed: {args.webhook_secret_env} is unset or empty, and the "
                        "broker refuses every delivery without it. Onboarding must register the same "
                        "value the broker reads from its own environment.",
                        file=sys.stderr,
                    )
                    raise SystemExit(1)
                try:
                    if not github.workflow_exists(args.repository, "cindral-dispatch.yml"):
                        print("Webhook setup failed: merge the cindral adapter to the repository's default branch first", file=sys.stderr)
                        raise SystemExit(1)
                    result = github.ensure_push_webhook(args.repository, args.webhook_url, secret)
                except (GitHubAPIError, ValueError) as exc:
                    print(f"Webhook setup failed: {exc}", file=sys.stderr)
                    raise SystemExit(1) from exc
                print(f"Webhook: {result} ({args.webhook_url})")
            return
        print("Overall: not ready, the default hosted/fallback path does not resolve")
        raise SystemExit(1)

    policy = Policy.load(args.policy)
    request = RouteRequest.from_dict(json.loads(Path(args.request).read_text()))
    decision = policy.choose(request, load_runners(args.state))
    print(json.dumps(decision.as_dict(), sort_keys=True))


if __name__ == "__main__":
    main()
