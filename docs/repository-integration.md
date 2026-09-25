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
- select jobs with `if: inputs.relay_lane == ...`,
- keep repository commands in the repository,
- set timeouts and concurrency limits,
- keep untrusted pull-request code off self-hosted jobs.

The broker dispatches these inputs:

```text
relay_lane
relay_target
relay_reason
```

The adapter should not call the broker recursively and should not accept arbitrary shell commands as dispatch inputs.

## Onboarding check

Run the onboarding check before enabling dispatch for a repository:

```sh
export GITHUB_TOKEN=...
runner-relay onboard OWNER/REPOSITORY --adapter /path/to/checkout/.github/workflows/relay-dispatch.yml
```

Run from the Runner Relay checkout so the default policy is available. The token needs repository Administration read access. The command validates the adapter file against `config/policy.toml` and lists hosted, fallback, device, and burst lane readiness. Missing or offline registrations are reported with the labels each lane requires. Runner registration is performed through the deployment's managed process.

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

Use one of these patterns:

- Run the adapter on `push` to a trusted branch and maintainer-controlled events.
- Keep `pull_request` jobs on GitHub-hosted runners.
- Skip local lanes for untrusted events.

The broker is a dispatcher. It does not check out repository code or run repository commands.

## Quota behavior

The broker prefers hosted execution for public repositories and whenever private-repository quota is explicitly available. If quota is exhausted or unknown and paid overage is disabled, it selects an eligible local fallback before dispatching the workflow.

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
