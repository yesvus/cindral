# Repository integration guide

This guide is the migration contract for adding Runner Relay to a personal-account repository.

## Goal

Runner Relay chooses the execution lane in the k3s broker before repository code runs. The repository owns its build, lint, test, and release commands.

```text
GitHub event
    -> k3s Runner Relay
    -> repository workflow_dispatch with relay_lane
    -> fixed hosted, fallback, device, or burst job
```

GitHub personal accounts do not provide a cross-repository organization runner scope. Each repository therefore has a small dispatch adapter and its own repository-scoped runner registrations.

## Adapter workflow

Start from [`templates/personal-dispatch.yml`](../templates/personal-dispatch.yml). The adapter must:

- accept `workflow_dispatch` inputs,
- define fixed `hosted`, `fallback`, `device`, and `burst` jobs,
- require the `fallback` custom label on the repository's fallback runner,
- select jobs with `if: inputs.relay_lane == ...`,
- keep repository commands in the repository,
- set timeouts and concurrency limits,
- keep untrusted pull-request code off self-hosted jobs.

The broker dispatches these inputs:

```text
relay_lane
relay_target
relay_reason
relay_ref
```

The adapter should not call the broker recursively and should not accept arbitrary shell commands as dispatch inputs.

## Onboarding check

Run onboarding from the Runner Relay checkout after the adapter is merged to the
repository's default branch:

```sh
GITHUB_TOKEN="$(gh auth token)" \
RELAY_WEBHOOK_SECRET="$(cat ~/.relay-webhook-secret)" \
uv run runner-relay onboard OWNER/REPOSITORY \
  --checkout /path/to/checkout \
  --register-webhook
```

The command validates the local adapter against `config/policy.toml`, including
adapters that delegate to a local reusable workflow. It confirms the adapter is
present on the repository's default branch, checks that online runners carry
the required lane labels, then
creates or updates the signed `push` webhook. The token needs permission to read
workflows and runners and manage repository webhooks. Without
`--register-webhook`, onboarding only validates and reports readiness.

The adapter path defaults to `.github/workflows/relay-dispatch.yml` under
`--checkout`. The webhook URL defaults to
`https://cindral.example.com/relay/dispatch`; use `--webhook-url` to override it.
The webhook secret is read from `RELAY_WEBHOOK_SECRET` and is never printed.
Runner registration itself remains managed by the deployment.

## pnpm contract

A repository that wants one reliable command for constrained runners can define:

```json
{
  "scripts": {
    "ci": "pnpm lint && pnpm test && pnpm build"
  }
}
```

The sequential form avoids three competing package installations and compiler processes on a constrained device. Repositories with expensive independent jobs can keep separate scripts and jobs.

Repositories with an established npm workflow keep npm. Runner Relay detects the package manager from repository instructions, `packageManager`, and the committed lockfile. It never changes lockfiles during integration.

## Trust boundary

Self-hosted runners must not execute untrusted pull-request code.

Runner Relay treats pull requests from `OWNER`, `MEMBER`, and `COLLABORATOR` authors as trusted. All other author associations are forced to the hosted lane, regardless of quota policy. Trusted pull requests use the normal repository visibility and quota policy. The broker dispatches the adapter from the repository's default branch and passes `refs/pull/<number>/merge` as `relay_ref`, which the adapter checks out.

Configure the GitHub webhook for both `push` and `pull_request` events. Relay routes `opened`, `reopened`, `synchronize`, and `ready_for_review` actions. Other pull-request actions are acknowledged and ignored.

The broker is a dispatcher. It does not check out repository code or run repository commands.

## Broker endpoints and their authentication

Three paths, and they are not equally exposed. Only the first is meant to be
public.

| Path | Who may call it | Gate |
| --- | --- | --- |
| `POST /relay/dispatch` | GitHub, from a webhook | `X-Hub-Signature-256` HMAC, supported event and action, and an opted-in relay workflow |
| `POST /v1/dispatch` | operator, in-cluster | `Authorization: Bearer $RELAY_DISPATCH_TOKEN` |
| `POST /v1/route` | operator, in-cluster | none, it only returns a decision and has no side effects |

Every gate **fails closed**. If the relevant environment variable is unset the
endpoint refuses the request rather than serving it, so a missing Secret cannot
silently turn a gated endpoint into an open one. `serve` prints a warning at
startup for each one that is unset.

### `/relay/dispatch` is the public entry point

It accepts signed GitHub `push` and `pull_request` payloads and dispatches the
repository's adapter on the lane the policy selects. Other events are
acknowledged and ignored:

- a missing or wrong signature is `401`, and the comparison is constant-time
- a repository without the configured relay workflow is `202` ignored
- a branch other than the repository's default branch, a tag, or a deleted ref
  is `202` ignored, with no dispatch
- pull requests with actions other than `opened`, `reopened`, `synchronize`,
  and `ready_for_review` are `202` ignored
- untrusted pull requests are dispatched to the hosted lane

Repository visibility is read from the payload, so a public repository still
routes to hosted runners. Quota is deliberately reported as unknown, because
only the caller knows the remaining minutes and the policy default for unknown
quota is the local lane.

A repository opts in by installing the webhook and adding the relay adapter.
The broker confirms the adapter exists through GitHub's workflow API before
dispatching. Before a local dispatch, it reads the target repository's current
runner registrations and busy state from GitHub. Offline, busy, unregistered,
or temporarily reserved runners are ineligible. A failed capacity lookup fails
closed and does not dispatch a local job. Brief in-memory reservations prevent
simultaneous webhook requests from oversubscribing an idle lane while GitHub
assigns the dispatched workflow. The reservation window defaults to 10 seconds
and can be changed with `RELAY_RUNNER_RESERVATION_SECONDS`. Reservations are
process-local, so the broker must run one replica; GitHub's busy state remains
the source of truth across restarts.

Pushes dispatch on the signed payload's default branch, and pull requests
dispatch the adapter from that same default branch and pass the PR merge ref as
`relay_ref`, keeping the workflow definition on trusted default-branch code
while testing the proposed merge. Neither repository names nor branch names
need to be copied into the broker deployment.

### Required environment

| Variable | Required for | Effect if unset |
| --- | --- | --- |
| `GITHUB_TOKEN` | any dispatch | `/v1/dispatch` and `/relay/dispatch` return `503` |
| `RELAY_WEBHOOK_SECRET` | `/relay/dispatch` | returns `503` for every delivery |
| `RELAY_DISPATCH_TOKEN` | `/v1/dispatch` | returns `401` for every request |
| `RELAY_WORKFLOW_FILE` | `/relay/dispatch` | defaults to `relay-dispatch.yml` |
| `RELAY_RUNNER_RESERVATION_SECONDS` | local dispatch capacity | defaults to `10`; must be positive |

Narrow `GITHUB_TOKEN` to the repositories using Relay with permission to
dispatch workflows. The workflow opt-in prevents accidental dispatch to
unconfigured repositories, but a narrowly scoped credential is still the
strongest limit on the broker's impact if its webhook secret is exposed.

## Quota behavior

The broker prefers hosted execution for public repositories and whenever private-repository quota is explicitly available. If quota is exhausted or unknown and paid overage is disabled, it selects an eligible local fallback from the target repository's live runner capacity before dispatching the workflow. GitHub assigns the workflow to a runner matching the adapter's lane labels.

A failed test is not a capacity signal. The broker must not dispatch a second runner after an ordinary test failure.

## Migration checklist

- Confirm the repository has a lockfile and reproducible install command.
- Add a repository-owned `ci` script or document separate task scripts.
- Copy and customize the personal dispatch adapter.
- Confirm the repository has a repository-scoped runner registration for each local lane it uses.
- Add timeouts and concurrency limits.
- Keep self-hosted work off untrusted pull requests.
- Verify the hosted lane when quota is available.
- Verify the local fallback lane with unknown quota.
- Record the selected lane and runner in workflow logs.
- Do not migrate every repository until the first repository has a stable run history.
