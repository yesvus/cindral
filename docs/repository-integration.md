# Repository integration guide

This guide is the migration contract for adding Runner Relay to a repository.

## Goal

Runner Relay chooses the execution lane. The repository continues to own its build, lint, test, and release commands.

```text
repository job
    -> Runner Relay route decision
    -> selected GitHub-hosted or self-hosted runner
    -> repository-owned pnpm commands
```

## Standard pnpm contract

A repository that wants one reliable command for constrained runners can define:

```json
{
  "scripts": {
    "ci": "pnpm lint && pnpm test && pnpm build"
  }
}
```

The sequential form is intentional. A constrained device should install once, then lint, test, and build without three competing package installations or compiler processes.

Repositories with expensive independent jobs can keep separate scripts and jobs. The route output is shared by those jobs, so they still use the same selected lane unless a later policy change adds task-aware routing.

## Minimal workflow

```yaml
name: ci

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: read

concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  route:
    uses: yesvus/runner-relay/.github/workflows/route.yml@main
    with:
      requested_lane: auto
      repository_visibility: private
      quota_status: unknown

  verify:
    needs: route
    runs-on: ${{ fromJSON(needs.route.outputs.runs_on) }}
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v5
      - uses: pnpm/action-setup@v4
        with:
          version: 10
      - uses: actions/setup-node@v4
        with:
          node-version-file: .nvmrc
          cache: pnpm
      - run: pnpm install --frozen-lockfile
      - run: pnpm lint
      - run: pnpm test
      - run: pnpm build
```

Use the repository's actual pnpm and Node versions. The values above are examples, not a global pin.

## Task classes

The intended policy vocabulary is:

| Task | Default | Local fallback guidance |
| --- | --- | --- |
| `lint` | GitHub-hosted | Trusted device, preferably a small runner |
| `test` | GitHub-hosted | Trusted device or workstation, subject to health |
| `build` | GitHub-hosted | workstation for constrained builds, burst-node for burst work |
| `fallback` | Never the default | host when explicitly selected or quota policy allows it |

The current reusable route contract selects a lane, not a task class. Task-aware routing can be added without moving repository commands into Runner Relay.

## Trust boundary

Self-hosted runners must not execute untrusted pull-request code.

Use one of these patterns:

- Run the workflow on `push` to a trusted branch and on maintainer-controlled events.
- Keep `pull_request` jobs on GitHub-hosted runners.
- Use a trusted workflow file from the default branch for metadata-only routing, without checking out pull-request code.

A route job should call Runner Relay as a service. It should not check out the target repository or execute repository scripts.

## Quota behavior

The relay prefers hosted execution for public repositories and whenever private-repository quota is explicitly available. If quota is exhausted or unknown and paid overage is disabled, it selects an eligible local fallback before the workload starts.

A failed test is not a capacity signal. The relay must not rerun an ordinary test failure on another runner.

## Migration checklist

- Confirm the repository has a lockfile and reproducible install command.
- Add a repository-owned `ci` script or document separate task scripts.
- Add the reusable route job.
- Set the real repository visibility and quota inputs.
- Add timeouts and concurrency limits.
- Keep self-hosted work off untrusted pull requests.
- Run the hosted lane first.
- Run the explicit local lane only after the hosted path is verified.
- Record the selected lane and runner in workflow logs.
- Do not migrate every repository until the first repository has a stable run history.
