# Cindral

Cindral is a smart resource allocator for CI. It places each job on the best
available resource - GitHub-hosted runners or your own device pool - from policy,
quota, and live capacity.

## How it allocates

- Public repositories, and private ones with available quota, use GitHub-hosted runners.
- Private repositories with exhausted or unknown quota use a healthy idle device.
- `device`, `burst`, and `fallback` lanes give explicit placement to selected work.
- Placement reads live runner capacity and busy state, then reserves the slot before the job starts.
- A failed test never triggers a rerun on another resource.

The policy engine is deterministic, takes runner state as data, and never executes repository code or stores credentials.

## Two ways to run

**Dispatch adapter** - a repository-local `workflow_dispatch` adapter ([`templates/personal-dispatch.yml`](templates/personal-dispatch.yml)) lets Cindral select the lane and hand the job to GitHub Actions.

**Direct execution** - a repository declares its CI in a committed [`.cindral/ci.toml`](docs/direct-execution.md). Cindral queues the job, a device agent runs it in bounded Docker, and the result is reported as a `cindral/ci` commit status. Default-branch pushes and trusted same-repository pull requests take this path; fork and untrusted pull requests stay on the hosted lane.

Direct jobs automatically use branch- and architecture-scoped dependency caches
for supported package and build tools. See [direct execution](docs/direct-execution.md)
for cache adapters, quotas, and broker settings.

## Service

`GET /healthz`, `POST /v1/route`, `POST /v1/dispatch`. A routing request carries a lane, target, visibility, quota status, and time estimate; the response carries the chosen lane, runner labels, and the reason.

## Configuration

- [`config/policy.toml`](config/policy.toml) - lanes, quota rules, device priority.
- [docs/direct-execution.md](docs/direct-execution.md) - contract schema, agent flags, broker settings.
- [docs/package-manager-policy.md](docs/package-manager-policy.md), [docs/repository-integration.md](docs/repository-integration.md) - repository rules.

## Boundaries

- `cindral` owns policy, broker, agent, templates, and tests.
- `ops` owns the deployment, resource limits, monitoring, and alerts.
- The host inventory owns device identity, hardware metadata, and tags.

## Development

```sh
python -m unittest discover -s tests -v
python -m cindral.cli route --policy config/policy.toml --state examples/state.json --request examples/request.json
```

`VERSION` matches `pyproject.toml`; `scripts/release.py patch --write` prepares a bump, and the release is tagged after the deployment digest is verified.
