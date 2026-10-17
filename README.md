# Cindral

Cindral is a smart resource allocator for CI. It places each job on the best
available resource - GitHub-hosted runners or your own device pool - from policy,
quota, and live capacity.

## How it allocates

- Public repositories, and private ones with available quota, use GitHub-hosted runners.
- Private repositories with exhausted or unknown quota use a healthy idle device.
- With `CINDRAL_BILLING_ORG` set, the broker reads the organization's Actions billing on an interval and routes private pushes on it; a reading it cannot establish, including an unauthorized credential, routes as unknown quota.
- `device`, `burst`, and `fallback` lanes give explicit placement to selected work.
- Placement reads live runner capacity and busy state, then reserves the slot before the job starts.
- A failed test never triggers a rerun on another resource.

The policy engine is deterministic, takes runner state as data, and never executes repository code or stores credentials.

## Two ways to run

**Dispatch adapter** - a repository-local `workflow_dispatch` adapter ([`templates/personal-dispatch.yml`](templates/personal-dispatch.yml)) lets Cindral select the lane and hand the job to GitHub Actions.

**Direct execution** - Cindral queues the job, a device agent runs it in bounded Docker, and the result is reported as a `cindral/ci` commit status. Default-branch pushes and trusted same-repository pull requests take this path; fork and untrusted pull requests stay on the hosted lane.

A repository declares its CI either as an existing workflow file, which is translated and run by an embedded [act](https://github.com/nektos/act) engine, or as a committed [`.cindral/ci.toml`](docs/direct-execution.md) for what no single workflow job expresses translatably here, such as step defaults, service containers, or a nested toolchain reached through the host daemon. A committed workflow wins when both are present. Constructs the engine does not implement - `strategy.matrix`, `needs`, `secrets` - are refused by name before a job starts, so a run never reports on a fraction of what it declares, and a repository with a contract runs the contract instead.

Direct jobs automatically use branch- and architecture-scoped dependency caches
for supported package and build tools. A step keeps its output by writing it
into `.cindral/artifacts/` in the checkout, and the run's archive is listed on
the job and served for download from the broker's API, which needs the pool
token. The panel does not offer it yet. See
[direct execution](docs/direct-execution.md) for cache adapters, artifact
upload and download, quotas, and broker settings.

## Service

`GET /healthz`, `POST /v1/route`, `POST /v1/dispatch`. A routing request carries a lane, target, visibility, quota status, and time estimate; the response carries the chosen lane, runner labels, and the reason. `/healthz` also reports the build, and, when a GitHub App is configured, the installation the broker is authenticated as.

## Asking Cindral from a tool or an agent

`cindral status`, `cindral run`, and `cindral explain` are clients for the service
above, so a caller can ask Cindral what it would do instead of reading the target
repository and inferring the answer. `cindral status --repo owner/name` reports
whether the adapter and webhook are in place, whether the policy permits the ref,
the live queue, and the one next action when something is missing. `cindral
explain owner/name` reports the lane a ref would take and why, without starting
anything. `cindral run owner/name --ref branch` starts it and reports the lane
the broker chose. Add `--json` for one machine-readable object.

None of them accepts a lane, a target, or a runner, because placement is the
broker's decision and a client that could set it would be re-implementing the
policy without live capacity. Read `explain` rather than trying to choose. They do
accept the repository's visibility, and `explain` also accepts a quota status and a
duration, because the policy reads those as facts about the job rather than as a
place to put it. Visibility does steer the lane: a public repository is hosted
before the quota rules are consulted, so naming one overrides the fallback.

`cindral run` assumes hosted quota is available when the caller states none, so its
default lane is a GitHub runner and never a maintainer's device. Both reports say
what the resolved lane means as `lane_risk`, and `run` also names that assumption
as `quota_assumption`. The policy reads an unknown quota as "use the local pool", so
assuming that instead would hand every default run to operator hardware.

`cindral mcp` serves the same three operations to an agent host over MCP on stdio,
as `cindral_status`, `cindral_explain`, and `cindral_run`. It reads the same
environment as the CLI: `CINDRAL_BROKER_URL`, `CINDRAL_DISPATCH_TOKEN`,
`CINDRAL_POOL_TOKEN`, `GITHUB_TOKEN`, `CINDRAL_POLICY`, `CINDRAL_WEBHOOK_URL`.
`CINDRAL_DISPATCH_TOKEN` and `CINDRAL_POOL_TOKEN` are different capabilities and
neither substitutes for the other, so a caller that only reads the queue cannot
dispatch. A token is never sent over plaintext HTTP to a host that is not
loopback: the dispatch token authorises a dispatch to every repository in the
allowlist, so cleartext would hand that to whoever is on the path. Use `https`,
or point `CINDRAL_BROKER_URL` at loopback, which is the default.

`CINDRAL_DISPATCH_TOKEN` is one bearer for the whole broker, not a per-repository
one. It is not scoped to a repository, an actor, a ref, or a rate, so anything
holding it can dispatch to every repository in the policy's allowlist. `cindral mcp`
is therefore a process to start only where untrusted code cannot read the
environment: an operator's own session, or a container whose filesystem and
environment are not reachable by the code it runs. Do not start it inside a
sandbox that the code under test can inspect. Narrowing the credential per
repository is the real fix and is not implemented here.

A dispatch reports the lane it chose but returns no run identity, because GitHub's
`workflow_dispatch` answers 204 and the broker does not read the run back. The
outcome is the `cindral-dispatch` check run on the ref, which is the run GitHub
started for the dispatch, or the check run of whichever other workflow you
dispatched. GitHub creates that run a moment after the dispatch returns rather
than with it, so an agent that polls immediately finds nothing.

## Configuration

- [`config/policy.toml`](config/policy.toml) - lanes, quota rules, device priority.
- GitHub App authentication, for account-wide discovery, is configured with `CINDRAL_APP_CLIENT_ID` and `CINDRAL_APP_PRIVATE_KEY`. An App is installed once for the account, so the repositories it can see are the ones it covers, rather than every repository a user token can read.
- [docs/direct-execution.md](docs/direct-execution.md) - contract schema, agent flags, broker settings.
- [docs/package-manager-policy.md](docs/package-manager-policy.md), [docs/repository-integration.md](docs/repository-integration.md) - repository rules, including `cindral adopt`, which hooks only the repositories the operator names from the ones the credential owns (`--all-owned` opts in to all of them), and `CINDRAL_ADOPT_EXCLUDE` to turn one off.
- [docs/roadmap.md](docs/roadmap.md) - product milestones and release gates.

## Boundaries

- Cindral owns routing policy, broker and agent behavior, templates, and tests.
- The deployment environment owns rollout, resource limits, monitoring, and alerts.
- A host inventory supplies device identity, hardware metadata, and tags.

## Development

The Python check runs file-size limits, unit tests, and bytecode compilation.

```sh
uv run python scripts/check.py
uv run cindral route --policy config/policy.toml --state examples/state.json --request examples/request.json
```

For changes under `web/`, run `pnpm install --frozen-lockfile && pnpm run ci` from that directory.

`VERSION` matches `pyproject.toml`; `scripts/release.py patch --write` prepares a bump, and the release is tagged after the deployment digest is verified.
