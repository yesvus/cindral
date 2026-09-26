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
- `CINDRAL_POOL_TOKEN`: read-only bearer token for `GET /v1/pool`.
- `CINDRAL_JOB_LEASE_SECONDS`: lease duration, default `300`.
- `CINDRAL_JOB_TIMEOUT`: per-job timeout, default `3600`.
- `CINDRAL_DIRECT_REPOSITORIES`: comma-separated `owner/name` list.
- `CINDRAL_STATUS_CONTEXT`: commit status context, default `cindral/ci`.

Leases are authoritative: a device holds a job until its lease expires, and an
expired lease returns the job to the queue for another device.

## Observability

| Endpoint | Auth | Content |
| --- | --- | --- |
| `GET /v1/pool` | `CINDRAL_POOL_TOKEN` | JSON pool snapshot: devices with their leases, queue depth, oldest pending age, expired lease count, reclaim total, recent jobs |
| `GET /metrics` | none, same-origin only | Prometheus text for the same gauges |

`/v1/pool` answers cross-origin preflight for the panel. It takes the read-only
pool token, not the agent token: a control panel that can read queue state
should not hold the credential that can claim, renew, and report jobs. The
agent and dispatch tokens are both refused, and an unset pool token refuses
every request rather than serving the snapshot open. The broker also refuses to
start when the pool token equals the agent or dispatch token, since a shared
value would hand the read-only consumer the write capability. `/metrics` carries
no `Access-Control-Allow-Origin`, so a browser page a scraper visits cannot read
pool state.

The pool token is also accepted on `GET /v1/jobs/{id}` so a panel can read a
single run. `claim`, `renew`, and `report` still take the agent token only.

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
| `CINDRAL_POOL_TOKEN` | read-only token the panel presents to the broker |
| `CINDRAL_PANEL_PASSWORD` | operator password for the panel login; unset means every login is refused |
| `CINDRAL_PANEL_EMAIL` | email shown on the login form and the session |
| `CINDRAL_SESSION_SECRET` | signs the session cookie |
| `CINDRAL_API_TIMEOUT_MS` | broker request timeout, default `5000` |

The panel is a single-operator surface: one password and a signed session
cookie, with no user database. The broker credential it holds is the read-only
pool token, so a compromised panel cannot claim or report jobs. An unset
password, session secret, or operator email refuses logins rather than serving
the panel open, and failed sign-ins are throttled per client.
