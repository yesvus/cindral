# Cindral agent contract

When asked to wire Cindral to a repository, make the repository use the shared routing contract without moving repository-specific commands into Cindral.

## Required workflow shape

1. Inspect the repository's package manager, lockfile, Node version, and existing scripts.
2. Keep `pnpm lint`, `pnpm test`, `pnpm build`, and other commands in the target repository.
3. Add a repository-local `workflow_dispatch` adapter based on `templates/personal-dispatch.yml`.
4. Let the k3s broker dispatch the adapter with `cindral_lane`, `cindral_target`, and `cindral_reason`.
5. Keep hosted execution as the default lane when quota is available; use local fallback when private-repository quota is exhausted or unknown.
6. Use explicit lanes only for trusted, selected work.
7. Keep untrusted pull-request code off self-hosted runners.
8. Never add a rerun of ordinary test failures on another runner.
9. Add timeouts, concurrency limits, and one-job limits appropriate to the target runner.
10. Verify the workflow locally and report which hosted, device, fallback, or burst lane was selected.

## pnpm convention

Prefer a repository-owned `ci` script when the repository is intended for constrained runners:

```json
{
  "scripts": {
    "ci": "pnpm lint && pnpm test && pnpm build"
  }
}
```

Run the steps sequentially on resource-limited runners. Use separate lint, test, and build jobs only when the repository benefits from parallelism and the selected runner can afford it.

## Trust and quota rules

- Public repositories use GitHub-hosted runners.
- Private repositories use hosted runners only when quota is explicitly available.
- Self-hosted lanes are for trusted branches, trusted maintainer events, and explicit device work.
- `fallback` is a last-resort lane, not the default build lane.
- `burst` is selected explicitly.
- Hosted quota exhaustion or unknown quota may select a local fallback before a job starts. A test failure does not trigger fallback selection.
- Unknown private-repository quota defaults to local fallback.

## Package manager detection

Use the target repository's existing package manager. The fleet default is pnpm for product and web repositories, while repositories with an established npm workflow remain on npm until they explicitly migrate. Detect the package manager from repository instructions, `packageManager`, and the committed lockfile. Never switch lockfiles during integration.

See [package manager policy](docs/package-manager-policy.md).

## Boundaries

- Cindral owns routing policy and the reusable route workflow.
- Ops owns the host deployment, resource limits, monitoring, and alerts.
- Fleet owns host identity, hardware metadata, and inventory tags.
- The target repository owns its source code, dependency setup, test commands, build commands, and release credentials.

Do not add credentials, registration tokens, or billing secrets to workflow files. Use GitHub environments, organization secrets, or the Ops deployment secret store.
