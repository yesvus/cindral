"""Command-line interface for Cindral."""
import argparse
import json
import os
from pathlib import Path
import sys

from .github import GitHubAPIError, GitHubClient, is_repository_slug
from .models import RouteRequest
from .onboarding import check_lane_readiness, validate_adapter
from .policy import Policy
from .service import serve
from .state import load_runners

DEFAULT_WEBHOOK_URL = "https://cindral.example.com/cindral/dispatch"


def main() -> None:
    parser = argparse.ArgumentParser(prog="cindral")
    subparsers = parser.add_subparsers(dest="command", required=True)

    route = subparsers.add_parser("route")
    route.add_argument("--policy", default="config/policy.toml")
    route.add_argument("--state", default="examples/state.json")
    route.add_argument("--request", default="examples/request.json")

    onboard = subparsers.add_parser("onboard")
    onboard.add_argument("repository", help="GitHub repository in owner/name format")
    onboard.add_argument("--policy", default="config/policy.toml")
    onboard.add_argument("--checkout", default=".", help="repository checkout containing the adapter")
    onboard.add_argument("--adapter", help="path to the repository's workflow_dispatch adapter")
    onboard.add_argument("--github-token-env", default="GITHUB_TOKEN")
    onboard.add_argument("--register-webhook", action="store_true")
    onboard.add_argument("--webhook-url", default=os.environ.get("CINDRAL_WEBHOOK_URL", DEFAULT_WEBHOOK_URL))
    onboard.add_argument("--webhook-secret-env", default="CINDRAL_WEBHOOK_SECRET")

    service = subparsers.add_parser("serve")
    service.add_argument("--policy", default="config/policy.toml")
    service.add_argument("--state", default="examples/state.json")
    service.add_argument("--host", default="127.0.0.1")
    service.add_argument("--port", type=int, default=8095)
    service.add_argument("--github-token-env", default="GITHUB_TOKEN")

    args = parser.parse_args()
    if args.command == "serve":
        serve(args.policy, args.state, args.host, args.port, os.environ.get(args.github_token_env))
        return

    if args.command == "onboard":
        if not is_repository_slug(args.repository):
            parser.error("repository must use the owner/name format")
        token = os.environ.get(args.github_token_env)
        if not token:
            parser.error(f"set {args.github_token_env} to a token with runner, workflow, and webhook access")
        policy = Policy.load(args.policy)
        adapter = Path(args.adapter) if args.adapter else Path(args.checkout) / ".github/workflows/cindral-dispatch.yml"
        errors = validate_adapter(adapter, policy)
        print(f"Repository: {args.repository}")
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
                secret = os.environ.get(args.webhook_secret_env)
                if not secret:
                    print(f"Webhook setup failed: set {args.webhook_secret_env}", file=sys.stderr)
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
        return

    policy = Policy.load(args.policy)
    request = RouteRequest.from_dict(json.loads(Path(args.request).read_text()))
    decision = policy.choose(request, load_runners(args.state))
    print(json.dumps(decision.as_dict(), sort_keys=True))


if __name__ == "__main__":
    main()
