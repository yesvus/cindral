"""Command-line interface for Runner Relay."""
import argparse
import json
import os
from pathlib import Path

from .models import RouteRequest
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

    policy = Policy.load(args.policy)
    request = RouteRequest.from_dict(json.loads(Path(args.request).read_text()))
    decision = policy.choose(request, load_runners(args.state))
    print(json.dumps(decision.as_dict(), sort_keys=True))


if __name__ == "__main__":
    main()
