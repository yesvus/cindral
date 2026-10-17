# Roadmap

Cindral connects repository events to hosted CI or trusted runner devices, then
reports the result through checks and the control panel. This roadmap describes
product capabilities and the order that makes them safe to deliver.

## Current capabilities

- Trusted pushes and same-repository pull requests can produce jobs.
- A durable queue dispatches work to eligible devices and tracks their leases.
- Repository policy controls which repositories and execution modes are served.
- Common repository scripts can be inferred when no workflow or CI contract is
  present.
- The panel shows device health, queue state, jobs, logs, and run artifacts.
- Unsupported workflow constructs are reported explicitly.

## Next milestones

### Reliable delivery

- Record each accepted event before acknowledging it and recover safely after a
  restart.
- Ensure distinct pushes each receive a result while repeated deliveries do not
  create duplicate runs.
- Reconcile dispatch attempts and status updates when a response is lost.
- Keep ordinary test failures on the lane where they ran.

### Repository adoption

- Discover eligible repositories through an account-wide installation.
- Apply repository selection and trusted-ref rules consistently.
- Make adoption and hook reconciliation repeatable and observable.
- Let operators review adopted repositories and exclude them safely.

### Workflow compatibility

- Resolve event context from the captured push or pull request.
- Run dependent jobs in order and expand bounded matrices into independent work.
- Support scoped secrets only after the execution boundary is validated.
- Map workflow caching and scheduled runs to repository-scoped broker behavior.
- Continue refusing unsupported constructs before they can produce a false
  successful check.

### Device readiness and operations

- Report device kind, runner registration, labels, capacity, and eligibility.
- Alert on stalled work and explain each hosted-versus-device routing decision.
- Drain active leases safely during device or broker maintenance.
- Provide authorized, auditable device lifecycle controls.

### Isolation

- Keep untrusted pull request code away from ordinary self-hosted runners.
- Validate a stronger isolation boundary before enabling fork pull request
  execution.
- Keep secrets and repository data outside jobs that are not authorized to use
  them.

## Release gates

1. **Reliable CI:** a fresh repository receives a check on its first supported
   push, busy runners do not drop distinct work, and a failed run can be
   diagnosed from the panel.
2. **Common workflows:** representative multi-job workflows expose event
   context, dependency results, matrix jobs, cache operations, and scheduled
   outcomes without silent omissions.
3. **Safe operations:** untrusted code runs only inside a validated isolation
   tier, routine maintenance preserves active work, and routing decisions can
   be explained from current evidence.

## Related documents

- [direct-execution.md](direct-execution.md): workflow contract and broker
  settings.
- [repository-integration.md](repository-integration.md): connecting a
  repository to Cindral.
- [package-manager-policy.md](package-manager-policy.md): package manager
  selection for integrated repositories.
