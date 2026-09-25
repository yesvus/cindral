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

[env]
NODE_VERSION = "22"

[[services]]
name = "postgres"
image = "pgvector/pgvector:pg16"
env = { POSTGRES_USER = "postgres", POSTGRES_PASSWORD = "postgres" }

[[steps]]
run = "pnpm install --frozen-lockfile"

[[steps]]
run = "pnpm run ci"
```

- `[image]` sets exactly one of `ref` (pull) or `dockerfile` (build the checkout).
- `[[steps]]` run in order, each with `/bin/sh -lc` in a container, working
  directory `/workspace` bound to the checkout. The first non-zero step fails
  the job and the remaining steps are skipped.
- `[env]` is passed to every step. `[[services]]` start first on an isolated
  network and are reachable by their `name` alias, then removed with the network.
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
