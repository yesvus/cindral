# Runner Relay

Quota-aware GitHub Actions routing across GitHub-hosted and self-hosted runners.

Runner Relay chooses an execution lane before repository code runs. GitHub-hosted runners are preferred while quota is available. Local runners are selected only when the request is eligible, the quota policy allows fallback, and a healthy idle runner exists.

## Policy

The default policy is in [`config/policy.toml`](config/policy.toml). Package-manager detection and repository integration rules are documented in [`docs/package-manager-policy.md`](docs/package-manager-policy.md) and [`docs/repository-integration.md`](docs/repository-integration.md).

- Public repositories use hosted runners.
- Private repositories use hosted runners while quota is explicitly available.
- Exhausted or unknown private-repository quota selects the first healthy local fallback.
- Explicit `device`, `burst`, and `fallback` lanes are supported.
- Before local dispatch, Relay checks the target repository's live runner registrations and busy state, then reserves capacity while GitHub assigns the job.
- Ordinary test failures never trigger a rerun on another runner.

The policy engine is deterministic and receives runner state as data. It does not execute repository code and does not store credentials.

## Service

The service exposes:

- `GET /healthz`
- `POST /v1/route`
- `POST /v1/dispatch`

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

## Personal-account dispatch

This installation uses a personal GitHub account, not an organization. Runner Relay therefore uses a repository-local `workflow_dispatch` adapter. The k3s broker chooses the lane before dispatching the workflow.

Start from [`templates/personal-dispatch.yml`](templates/personal-dispatch.yml), replace `./scripts/ci` with the repository's real pnpm or npm command, and keep the fixed lane jobs. Runner Relay adds `relay_lane`, `relay_target`, and `relay_reason` to the dispatch inputs.

The broker calls:

```text
POST /v1/dispatch
```

with the target repository, workflow filename, ref, and repository-specific inputs. The broker requires a GitHub token for dispatching, supplied through a k3s Secret.

No organization runner group or cross-repository runner scope is required. Repository-scoped runner registrations are managed separately.

## Repository boundaries

- `runner-relay` owns policy, broker code, dispatch templates, and tests.
- `ops` owns the host k3s deployment, resource limits, monitoring, and alerts.
- `fleet` owns host identity, hardware metadata, and inventory tags.
- GitHub repository-scoped runner registrations are managed by the broker.

## Versioning

`VERSION` is the release source of truth and must match `pyproject.toml`. Prepare a semver bump locally:

```sh
python scripts/release.py patch
python scripts/release.py patch --write
```

The tool does not push commits or tags. Review the diff, commit it, tag it with the matching `vX.Y.Z` value, and publish the release after the k3s image digest has been updated and verified.

## Development

```sh
python -m unittest discover -s tests -v
python -m runner_relay.cli route --policy config/policy.toml --state examples/state.json --request examples/request.json
```

The current implementation is the policy and service foundation. Live GitHub quota collection, Fleet/Beszel state ingestion, repository-scoped runner registration, and automatic repository migration are activation steps that belong in the Ops deployment and rollout plan.
