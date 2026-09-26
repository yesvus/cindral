# Direct execution

Repositories listed in `CINDRAL_DIRECT_REPOSITORIES` run their CI on the device
pool: Cindral queues the job and a device agent executes it in bounded Docker.
Two events enqueue a job:

- A signed push to the default branch, on the pushed commit.
- A trusted same-repository pull request (`opened`, `reopened`, `synchronize`,
  `ready_for_review`), on its merge commit, with the `cindral/ci` status posted
  on the PR head commit.

Trust boundary: only pull requests whose head repository is the target
repository, whose author is an owner, member, or collaborator, and that are not
drafts run on the device pool. Fork and untrusted pull requests stay on the
hosted lane.

## Repository contract

The repository owns its commands in `.cindral/ci.toml`, committed at the root:

```toml
timeout_minutes = 30

[image]
ref = "node:22-bookworm"
# or build from the repository:
# dockerfile = ".cindral/Dockerfile"

[runner]
# mount the host Docker daemon for steps that build or run images
docker = true

[env]
NODE_VERSION = "22"

[[services]]
name = "postgres"
image = "pgvector/pgvector:pg16"
env = { POSTGRES_USER = "postgres", POSTGRES_PASSWORD = "postgres" }
health_cmd = "pg_isready -U postgres"

[[steps]]
run = "pnpm install --frozen-lockfile"

[[steps]]
run = "pnpm run ci"
```

- `[image]` sets exactly one of `ref` (pull) or `dockerfile` (build the checkout).
- `[runner].docker` mounts the daemon socket and the Docker CLI into the job
  container, so a step can build or run images on the device. The socket is
  root-equivalent on the device, so this is for trusted repositories only.
  The contract fails before any step when the socket is absent.
- `[[steps]]` run in order, each with `/bin/sh -lc` in a container, working
  directory `/workspace` bound to the checkout. The first non-zero step fails
  the job and the remaining steps are skipped.
- `[env]` is passed to every step. `[[services]]` start first on an isolated
  network and are reachable by their `name` alias, then removed with the network.
  A service `health_cmd` holds the steps until the service reports healthy, up
  to 90 seconds.
- `timeout_minutes` bounds the job when lower than the broker default.

## Device agent

The agent pulls jobs, checks out the commit, runs the contract, and reports the
exit code and a log tail as the `cindral/ci` commit status.

```sh
CINDRAL_AGENT_TOKEN=... GITHUB_TOKEN=... \
  cindral agent --url https://hook.yesvus.com --device "$(hostname)"
```

| Flag / env | Meaning |
| --- | --- |
| `--url` / `CINDRAL_BROKER_URL` | broker base URL |
| `--token-env` / `CINDRAL_AGENT_TOKEN` | bearer token the agent presents |
| `--device` / `CINDRAL_DEVICE` | claim name, defaults to the hostname |
| `--labels` / `CINDRAL_AGENT_LABELS` | comma-separated labels advertised for job matching |
| `--workspace` / `CINDRAL_AGENT_WORKSPACE` | checkout root, default `/var/lib/cindral/work` |
| `--log-dir` / `CINDRAL_AGENT_LOG_DIR` | per-job log files (the tail also goes to the broker) |
| `--github-token-env` | env holding the token used to clone private repositories |

The agent needs Docker and permission to read the repositories it runs.

## Broker configuration

- `CINDRAL_JOBS_DB`: SQLite path; the queue stays off when unset.
- `CINDRAL_AGENT_TOKEN`: bearer token agents present.
- `CINDRAL_JOB_LEASE_SECONDS`: lease duration, default `300`.
- `CINDRAL_JOB_TIMEOUT`: per-job timeout, default `3600`.
- `CINDRAL_DIRECT_REPOSITORIES`: comma-separated `owner/name` list.
- `CINDRAL_STATUS_CONTEXT`: commit status context, default `cindral/ci`.

Leases are authoritative: a device holds a job until its lease expires, and an
expired lease returns the job to the queue for another device.

## Observability

| Endpoint | Auth | Content |
| --- | --- | --- |
| `GET /v1/pool` | agent bearer token | JSON pool snapshot: devices with their leases, queue depth, oldest pending age, expired lease count, reclaim total, recent jobs |
| `GET /metrics` | none, same-origin only | Prometheus text for the same gauges |

`/v1/pool` answers cross-origin preflight for the panel; the dispatch token is
refused because it is a narrower credential than the agent token. `/metrics`
carries no `Access-Control-Allow-Origin`, so a browser page a scraper visits
cannot read pool state.

An expired lease is counted as pending rather than running, and reported
separately as `expired_lease_count` / `cindral_expired_leases`. Both endpoints
stay off the public Ingress; only the signed `/cindral/dispatch` path is
published.

## Control panel

`web/` is a Next.js App Router app built on the Helmdeck admin shell. It reads
the snapshot server-side and renders Overview, Devices, and Queue views.

| Variable | Meaning |
| --- | --- |
| `CINDRAL_API_URL` | broker base URL, default `http://127.0.0.1:8095` |
| `CINDRAL_AGENT_TOKEN` | token the panel presents to the broker |
| `CINDRAL_CONTROL_PANEL_TOKEN` | token an operator must present to the panel; unset means every request is refused |
| `CINDRAL_API_TIMEOUT_MS` | broker request timeout, default `5000` |

The panel holds the agent token server-side, so it requires
`CINDRAL_CONTROL_PANEL_TOKEN` from the caller before rendering any lease or CI
log data. Without it, requests are refused rather than served open.
