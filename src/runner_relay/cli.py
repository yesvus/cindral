"""Command-line interface for Runner Relay."""
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="runner-relay")
    subparsers = parser.add_subparsers(dest="command", required=True)

    route = subparsers.add_parser("route")
    route.add_argument("--policy", default="config/policy.toml")
    route.add_argument("--state", default="examples/state.json")
    route.add_argument("--request", default="examples/request.json")

    onboard = subparsers.add_parser("onboard")
    onboard.add_argument("repository", help="GitHub repository in owner/name format")
    onboard.add_argument("--policy", default="config/policy.toml")
    onboard.add_argument("--adapter", required=True, help="path to the repository's workflow_dispatch adapter")
    onboard.add_argument("--github-token-env", default="GITHUB_TOKEN")

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
            parser.error(f"set {args.github_token_env} to a token with repository Administration read access")
        policy = Policy.load(args.policy)
        errors = validate_adapter(args.adapter, policy)
        print(f"Repository: {args.repository}")
        print(f"Adapter: {'valid' if not errors else 'needs changes'} ({args.adapter})")
        for error in errors:
            print(f"  - {error}")
        try:
            runners = GitHubClient(token).list_runners(args.repository)
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
