# Runner Relay

Quota-aware GitHub Actions routing across GitHub-hosted and self-hosted runners.

Runner Relay chooses an execution lane before repository code runs. GitHub-hosted runners are preferred while quota is available. Local runners are selected only when the request is eligible, the quota policy allows fallback, and a healthy idle runner exists.

## Policy

The default policy is in [`config/policy.toml`](config/policy.toml). Package-manager detection and repository integration rules are documented in [`docs/package-manager-policy.md`](docs/package-manager-policy.md) and [`docs/repository-integration.md`](docs/repository-integration.md).

- Public repositories use hosted runners.
- Private repositories use hosted runners while quota is explicitly available.
- Exhausted or unknown private-repository quota selects the first healthy local fallback.
- Explicit `device`, `burst`, and `fallback` lanes are supported.
- Ordinary test failures never trigger a rerun on another runner.

The policy engine is deterministic and receives runner state as data. It does not execute repository code and does not store credentials.

## Service

The service exposes:

- `GET /healthz`
- `POST /v1/route`

The route request accepts:

```json
{
  "requested_lane": "auto",
  "target": null,
  "repository_visibility": "private",
  "quota_status": "available",
  "remaining_minutes": 120,
  "estimated_minutes": 20
}
```

The response contains a JSON runner label array:

```json
{
  "lane": "hosted",
  "runs_on": ["ubuntu-24.04"],
  "reason": "hosted quota is available",
  "runner": null
}
```

## Reusable workflow

[`route.yml`](.github/workflows/route.yml) defines the reusable workflow contract. It expects a future dedicated `relay-router` runner and a `RUNNER_RELAY_URL` repository variable.

A repository opts in with a routing job and a workload job:

```yaml
jobs:
  route:
    uses: yesvus/runner-relay/.github/workflows/route.yml@main
    with:
      requested_lane: auto
      repository_visibility: private

  test:
    needs: route
    runs-on: ${{ fromJSON(needs.route.outputs.runs_on) }}
    steps:
      - uses: actions/checkout@v5
      - run: ./scripts/ci
```

Do not route untrusted pull-request code to self-hosted runners. Keep the routing job free of checkout and repository commands.

## Repository boundaries

- `runner-relay` owns policy, routing code, reusable workflows, and tests.
- `ops` owns Gurbet deployment, resource limits, monitoring, and alerts.
- `fleet` owns host identity, hardware metadata, and inventory tags.
- GitHub organization runner groups own cross-repository runner access.

## Development

```sh
python -m unittest discover -s tests -v
python -m runner_relay.cli route --policy config/policy.toml --state examples/state.json --request examples/request.json
```

The current implementation is the policy and service foundation. Live GitHub quota collection, Fleet/Beszel state ingestion, organization runner registration, and automatic repository migration are activation steps that belong in the Ops deployment and rollout plan.
