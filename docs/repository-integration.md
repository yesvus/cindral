# Repository integration guide

This guide is the migration contract for adding Cindral to a personal-account repository.

## Goal

Cindral chooses the execution lane in the k3s broker before repository code runs. The repository owns its build, lint, test, and release commands.

```text
GitHub event
    -> k3s Cindral
    -> repository workflow_dispatch with cindral_lane
    -> fixed hosted, fallback, device, or burst job
```

GitHub personal accounts do not provide a cross-repository organization runner scope. Each repository therefore has a small dispatch adapter and its own repository-scoped runner registrations.

## Adapter workflow

Start from [`templates/personal-dispatch.yml`](../templates/personal-dispatch.yml). The adapter must:

- accept `workflow_dispatch` inputs,
- define fixed `hosted`, `fallback`, `device`, and `burst` jobs,
- require the `fallback` custom label on the repository's fallback runner,
- select jobs with `if: inputs.cindral_lane == ...`,
- keep repository commands in the repository,
- set timeouts and concurrency limits,
- keep untrusted pull-request code off self-hosted jobs.

The broker dispatches these inputs:

```text
cindral_lane
cindral_target
cindral_reason
cindral_ref
```

The adapter should not call the broker recursively and should not accept arbitrary shell commands as dispatch inputs.

## Onboarding check

Run onboarding from the Cindral checkout after the adapter is merged to the
repository's default branch:

```sh
GITHUB_TOKEN="$(gh auth token)" \
uv run cindral onboard OWNER/REPOSITORY \
  --checkout /path/to/checkout
```

The command validates the local adapter against the policy the broker dispatches
with, including adapters that delegate to a local reusable workflow. It confirms
the adapter is present on the repository's default branch, checks that online
runners carry the required lane labels, and with `--register-webhook` creates or
updates the signed `push` webhook. The token needs permission to read workflows
and runners and manage repository webhooks. Without `--register-webhook`,
onboarding only validates and reports readiness.

The adapter path defaults to `.github/workflows/cindral-dispatch.yml` under
`--checkout`. The webhook URL defaults to
`https://cindral.example.com/cindral/dispatch`; use `--webhook-url` to override it.
Runner registration itself remains managed by the deployment.

### The policy that gets validated

The policy that decides a dispatch is deployment state, not repository state. A
containerized broker is started with the policy file its deployment mounts, and
onboarding reads that same file. The first of these that is set wins:

- `--policy PATH`.
- `CINDRAL_POLICY`, the path the broker deployment names.
- `config/policy.toml` in the working directory, the repository default.

The report names the file it loaded and the device targets that policy requires,
so a verdict about a broker that is not running shows up in the output instead
of reading as a defect in the adapter:

```text
Policy: /policy/policy.toml (device targets: device-gamma, device-delta, device-epsilon)
```

`--checkout` names the repository whose adapter is validated, so a checkout
containing `config/policy.toml` says nothing about the policy the broker routes
with. On a containerized deployment, export `CINDRAL_POLICY` or pass `--policy`
with the path the deployment mounts. Without it, onboarding compares the adapter
against the repository default, and an adapter written for the deployment's
device targets is reported as invalid.

### Webhook secret

`--register-webhook` signs the webhook with `CINDRAL_WEBHOOK_SECRET`, which must
hold the same value the broker reads from its own environment. A broker without
it answers `503` to every delivery, so a webhook registered with a different
value never dispatches anything. The value is never printed.

The secret lives wherever the deployment keeps it. A containerized broker
normally keeps it in a Kubernetes Secret, so read that value out of the cluster
with the namespace, secret name, and key the deployment mounts:

```sh
kubectl -n cindral get secret cindral-webhook \
  -o jsonpath='{.data.webhook-secret}' | base64 -d
```

Read it in the same command that runs onboarding, so the secret stays out of the
shell's history:

```sh
GITHUB_TOKEN="$(gh auth token)" \
CINDRAL_WEBHOOK_SECRET="$(kubectl -n cindral get secret cindral-webhook \
  -o jsonpath='{.data.webhook-secret}' | base64 -d)" \
uv run cindral onboard OWNER/REPOSITORY \
  --checkout /path/to/checkout \
  --register-webhook
```

A broker started directly on a host, outside a container, may keep the same value
in a file the operator owns, which is read with
`CINDRAL_WEBHOOK_SECRET="$(cat path/to/webhook-secret)"`.

Onboarding refuses to register a webhook when `CINDRAL_WEBHOOK_SECRET` is unset
or empty, so a secret that was never read stops at onboarding instead of
surfacing later as a delivery the broker refuses.

## Adoption

`cindral adopt` registers or updates the signed `push` webhook on private
repositories the operator names, from the repositories the credential owns.
It adopts nothing on its own: a run without a selection prints what it would
do and touches nothing, and `--apply` refuses to run until the operator
passes a selection.

Discovery reads only repositories the credential owns. Being a collaborator
or an organization member is not consent to be hooked, so a repository the
credential can reach but does not own is never listed and never adopted. To
adopt a repository Cindral's credential does not own, use a credential that
owns it; `adopt` has no way to include it.

Dry run first, then apply:

```sh
GITHUB_TOKEN="$(gh auth token)" \
CINDRAL_WEBHOOK_URL="https://cindral.example.com/cindral/dispatch" \
uv run cindral adopt --repository OWNER/REPOSITORY
```

Name each repository by repeating `--repository OWNER/NAME` or by setting
`CINDRAL_ADOPT_REPOSITORIES` to a comma-separated list, but not both: an
ambient variable must not widen a selection the operator typed. To opt in to
every owned private repository instead, pass `--all-owned`. Each of the three
opt-ins is exclusive. Without `--apply` the command only reads and prints the
plan. `--apply` also reads the webhook secret from
`CINDRAL_WEBHOOK_SECRET`, which must hold the same value the broker reads,
exactly as for `onboard --register-webhook`.

Rerunning is safe: an existing hook for the same URL is updated in place,
never duplicated, and hooks are never deleted. A named repository that cannot
be adopted is reported with its reason and fails the `--apply` run, so a typo
or a permission gap cannot read as success. A repository's own CI commands
stay in the repository; adoption never edits a deployment manifest, a policy
file, or the repository that owns deployment policy.

### What is eligible

A named repository is adopted when all of these hold:

- the credential owns it, because discovery reads only the token's own
  repositories,
- it is private: public repositories route to hosted runners,
- it is not archived,
- it has a default branch whose reported name is a trusted branch ref. The
  name is checked, not the branch: GitHub does not list branches here, and a
  repository whose default branch was deleted is adopted with its hook inert
  until policy names the repository,
- it does not own deployment policy and is not on the exclusion list.

Everything else is skipped with the reason printed. A repository that is empty
reports no default branch and is skipped like any other ineligible
repository, rather than stopping discovery for the account.

Applying skips the per-repository tree probes that only feed the printed
definition note, which is two fewer requests per repository and a few hundred
fewer on a large account. Adoption only registers
webhook delivery. The broker still refuses a signed push from any repository
the ops policy does not name, and untrusted pull-request code stays on hosted
runners, so a hook on a not-yet-listed repository is inert until the policy
adds it. Listing a repository in `[dispatch.repositories]` remains the ops
change that turns adoption into routed work.

The report lists every named repository with its outcome and states how many
owned repositories outside the selection were not touched. Discovery reads at
most 500 repositories (5 pages of 100) and refuses to run when more remain,
rather than adopting a prefix of the account.

### Excluding a repository

Set `CINDRAL_ADOPT_EXCLUDE` to a comma-separated list of `owner/name` slugs to
turn repositories off. Comparison is case-insensitive. The exclusion applies
to `--all-owned` and to a repository named explicitly, so a standing
exclusion cannot be overridden by accident. Excluded repositories are skipped
with that reason and keep any hook they already have; removing a hook is a
manual step. To turn a repository back on, drop it from the variable and
rerun `--apply`.

The repository containing the deployment policy must be excluded from adoption.
Set `CINDRAL_OPS_REPOSITORY` or pass `--ops-repository` to name it. The value is
required so an account-wide adoption cannot accidentally register the policy
repository.

| Variable | Used by | Effect if unset |
| --- | --- | --- |
| `CINDRAL_ADOPT_REPOSITORIES` | `adopt` | no repository is named, so `--apply` needs `--repository` or `--all-owned` |
| `CINDRAL_ADOPT_EXCLUDE` | `adopt` | no repositories are excluded |
| `CINDRAL_OPS_REPOSITORY` | `adopt` | `adopt` refuses to run until the deployment-policy repository is named |

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

Repositories with an established npm workflow keep npm. Cindral detects the package manager from repository instructions, `packageManager`, and the committed lockfile. It never changes lockfiles during integration.

## Trust boundary

Self-hosted runners must not execute untrusted pull-request code.

Cindral treats pull requests from `OWNER`, `MEMBER`, and `COLLABORATOR` authors as trusted, and a pull request from a dependency bot as trusted when its head repository is the target repository. A bot reaches a same-repository head branch only with write access it was already granted, so the code under test is repository code. All other author associations are forced to the hosted lane, regardless of quota policy. Trusted pull requests use the normal repository visibility and quota policy. The broker dispatches the adapter from the repository's default branch and passes `refs/pull/<number>/merge` as `cindral_ref`, which the adapter checks out.

Configure the GitHub webhook for both `push` and `pull_request` events. Cindral routes `opened`, `reopened`, `synchronize`, and `ready_for_review` actions. Other pull-request actions are acknowledged and ignored.

The broker posts a pending `cindral/ci` status on the pull request head commit before it dispatches, so the check is visible on the pull request even though the run starts from the default branch. A dispatch GitHub rejects resolves that status as an error.

The broker is a dispatcher. It does not check out repository code or run repository commands.

## Broker endpoints and their authentication

Three paths, and they are not equally exposed. Only the first is meant to be
public.

| Path | Who may call it | Gate |
| --- | --- | --- |
| `POST /cindral/dispatch` | GitHub, from a webhook | `X-Hub-Signature-256` HMAC, supported event and action, and an opted-in cindral workflow |
| `POST /v1/dispatch` | an operator, or the agent surface on an operator's behalf | `Authorization: Bearer $CINDRAL_DISPATCH_TOKEN` and a `[dispatch]` target in the policy |
| `POST /v1/route` | an operator, or the agent surface on an operator's behalf | none, it only returns a decision and has no side effects |

`CINDRAL_DISPATCH_TOKEN` is one bearer for the whole broker. It carries no
repository, actor, ref, or rate scoping, so every holder can dispatch to every
repository in the allowlist, and the allowlist is the only thing bounding it. The
`cindral status`, `cindral run`, and `cindral explain` clients read the same token
from the environment, which is why `cindral mcp` belongs only in a session where
untrusted code cannot read that environment. A token scoped per repository is the
narrower design and is not implemented.

Every gate **fails closed**. If the relevant environment variable is unset the
endpoint refuses the request rather than serving it, so a missing Secret cannot
silently turn a gated endpoint into an open one. `serve` prints a warning at
startup for each one that is unset.

`cindral status`, `cindral run`, and `cindral explain` are clients for these
paths, and `cindral mcp` serves the same operations to an agent host. See the
README's "Asking Cindral from a tool or an agent". They add no broker capability:
they send the same requests an operator would, and the placement stays the
broker's decision rather than a client-supplied lane.

### `/cindral/dispatch` is the public entry point

It accepts signed GitHub `push` and `pull_request` payloads and dispatches the
repository's adapter on the lane the policy selects. Other events are
acknowledged and ignored:

- a missing or wrong signature is `401`, and the comparison is constant-time
- a repository without the configured cindral workflow is `202` ignored, the
  reason naming the file that was looked for, and the commit carrying it as an
  `error` status. A repository the policy also serves on a device is queued on
  that device instead, because the adapter is what is missing and not the pool
- a branch other than the repository's default branch, a tag, or a deleted ref
  is `202` ignored, with no dispatch
- pull requests with actions other than `opened`, `reopened`, `synchronize`,
  and `ready_for_review` are `202` ignored
- untrusted pull requests are dispatched to the hosted lane

Repository visibility is read from the payload, so a public repository still
routes to hosted runners. Quota is deliberately reported as unknown, because
only the caller knows the remaining minutes and the policy default for unknown
quota is the local lane.

A repository opts in by installing the webhook and adding the cindral adapter.
The broker confirms the adapter exists through GitHub's workflow API before
dispatching. Before a local dispatch, it reads the target repository's current
runner registrations and busy state from GitHub. Offline, busy, unregistered,
or temporarily reserved runners are ineligible. A failed capacity lookup fails
closed and does not dispatch a local job. Brief in-memory reservations prevent
simultaneous webhook requests from oversubscribing an idle lane while GitHub
assigns the dispatched workflow. The reservation window defaults to 10 seconds
and can be changed with `CINDRAL_RUNNER_RESERVATION_SECONDS`. Reservations are
process-local, so the broker must run one replica; GitHub's busy state remains
the source of truth across restarts.

Pushes dispatch on the signed payload's default branch, and pull requests
dispatch the adapter from that same default branch and pass the PR merge ref as
`cindral_ref`, keeping the workflow definition on trusted default-branch code
while testing the proposed merge. Neither repository names nor branch names
need to be copied into the broker deployment.

### `/v1/dispatch` may only target the policy allowlist

A bearer token says who is calling, not what they are allowed to start. The
policy decides the target, one repository at a time:

```toml
[[dispatch.repositories]]
mode = "both"
repository = "example-org/example-app"
workflows = ["cindral-dispatch.yml"]
refs = ["refs/heads/main", "refs/heads/feature/*"]
```

- `repository` matches exactly, as `owner/name`
- `workflows` names the workflow files that repository may be dispatched at, so
  its CI adapter is dispatchable and a release workflow in the same repository is
  not
- `refs` names the branches, each exact or with one trailing `*` naming a
  branch-name prefix. A request may send either `main` or `refs/heads/main`

The check runs before any GitHub request, so a refused target never reaches the
API and never costs a dispatch, a runner lookup, or a capacity reservation. A
refusal is `403` and names the refused dimension without repeating a configured
value:

- a repository outside the section, including a malformed repository name
- a workflow outside that repository's list
- a ref outside the list, which covers a tag, `refs/pull/<n>/merge`, a commit
  SHA, and a branch prefix walked with `..`

**A policy with no `[dispatch]` section refuses every `/v1/dispatch` request.**
That is the point: a broker that was never told which repositories it may
dispatch cannot dispatch any. `serve` prints a warning at startup when the
section is empty, and listing a repository is part of onboarding it.

The ref policy limits the code a dispatch can start, not only the workflow.
For a signed default-branch push, a matching online repository runner may be
busy: the broker dispatches the adapter to that runner's lane and GitHub queues
it until a runner is free. An offline or mismatched runner still refuses the
dispatch. An idle runner held by the broker's short reservation still refuses
another fresh dispatch. A `ready` delivery replay uses its recorded lane and
lets GitHub queue it without a new broker reservation; check pending deliveries
when changing a repository's lane policy. Explicit `/v1/dispatch` requests
still require available capacity.
Signed push dispatches record the delivery ID (or a payload hash when the
header is absent), selected workflow, ref, and lane inputs in the queue database.
The broker needs `CINDRAL_JOBS_DB` on this path; without it, a signed dispatch
returns `503` instead of accepting an event it cannot retain.
Distinct pushes can each queue on GitHub. A duplicate delivery returns the
recorded result across broker restarts. An unattempted `ready` delivery is
replayed on startup or the next one-minute replay pass, oldest first and at most
30 per pass. The broker records `attempting` before contacting GitHub,
then `accepted` after GitHub responds successfully; an interrupted attempt or
an ambiguous transport failure becomes `uncertain` rather than being dispatched
twice. A definite GitHub rejection becomes `rejected`. These states are retained
in `webhook_deliveries` in `CINDRAL_JOBS_DB`; inspect unresolved work with:

```sh
python -m sqlite3 "$CINDRAL_JOBS_DB" "SELECT repository, delivery, state, error FROM webhook_deliveries WHERE state IN ('ready', 'attempting', 'uncertain', 'rejected');"
```

Run it in the broker container, which is `python:3.12-slim` and ships no
`sqlite3` CLI. It prints rows as tuples rather than as the CLI's aligned table.

A replay re-checks authorization, and a delivery the policy no longer permits,
or one whose adapter workflow has been removed, becomes `rejected` with that
reason recorded. A single bad policy rollout or a renamed branch can therefore
reject every pending delivery in one pass. Each row records why, and the
remedy below recovers it.

A delivery left `attempting` is one whose result could not be written, so it
stays in that state until the broker restarts, where startup resolves it to
`uncertain`. Its recorded error is `broker stopped during dispatch` whether the
cause was a stop or a failed write.

GitHub accepts `workflow_dispatch` without returning a run ID. `accepted`
records API acceptance, not a check or completed run. Investigate `uncertain`
deliveries against GitHub runs before manually replaying one: an accepted POST
may have lost its response. To authorize a retry after that investigation,
change that one row back to `ready` in the broker database; the next replay
pass will attempt it:

```sh
python -m sqlite3 "$CINDRAL_JOBS_DB" "UPDATE webhook_deliveries SET state = 'ready', error = NULL
WHERE repository = 'owner/repository' AND delivery = 'delivery-id'
  AND state IN ('uncertain', 'rejected');"
```

The operator records the delivery ID and the evidence for retry separately;
the retry may start a second GitHub run. The broker must run one replica: a
second replica resolves `attempting` rows at startup, including the live
replica's in-flight ones. Pull-request adapter dispatches and the explicit
`/v1/dispatch` path have separate delivery semantics.
`workflow_dispatch` runs the workflow as it stands at the ref you name, so a
branch somebody can push is a branch somebody can redefine the adapter on. List
the branches the operator uses rather than a wildcard.

Two gates protect two different callers, and neither replaces the other. Both
callers must clear the allowlist: a signed push whose repository, workflow, or
ref is not named is refused `403` exactly as an explicit `/v1/dispatch` is, and
the refusal message names the dimension without naming a configured value. The
opt-in adds a second gate on the webhook path, bounding what a signed delivery
can start, because a repository reaches `/cindral/dispatch` by installing the
adapter and that path is authenticated as GitHub.

A repository that installed the adapter but is absent from the policy, or whose
default branch is not in `refs`, therefore gets `403` on every default-branch
push rather than a dispatch. Adding the `[[dispatch.repositories]]` entry is
part of onboarding a repository.

Each `[[dispatch.repositories]]` entry also names which path its repository is
served on, through a required `mode`:

| `mode` | `POST /v1/dispatch` may target it | its trusted push and same-repository pull request events run on a device |
| --- | --- | --- |
| `dispatch` | yes | no |
| `device` | no | yes |
| `both` | yes | yes, when `push_lane` says so |

A push is the one event with a choice of lane, and an optional `push_lane` on
the entry names which it tries first:

```toml
[[dispatch.repositories]]
mode = "both"
repository = "example-org/example-app"
workflows = ["cindral-dispatch.yml"]
refs = ["refs/heads/main"]
push_lane = "device"     # optional, defaults to hosted
```

`push_lane` is `hosted` unless the entry says otherwise, because GitHub's own
runners cost an account nothing until its included minutes run out. The other
lane is the fallback, so it names a preference rather than an exclusion. A
`dispatch` repository has no device to fall back to, so a refused dispatch for
one is not queued. A repository with `mode = "device"` always takes the device
lane, having named no dispatch workflow to prefer.

The fallback covers a refusal from GitHub, and a transport failure that never
reached it. It does not cover a 5xx, where a run may already exist and running
the same commit on a device as well would report it twice.

The fallback follows the delivery, not the request. A push the broker registered
and then died before dispatching is replayed at startup, and a refusal found
there is queued on the device the same way a refusal on the first attempt would
have been. The rule is one predicate for all three paths, so the same commit
cannot be queued on a device when it arrives and dropped when it is retried. A
delivery whose ledger row predates the commit column cannot be queued, because
a device checks a commit out rather than re-reading a payload it never has;
those are logged by delivery id rather than dropped quietly.

A repository whose adapter is absent gets the same treatment. `GET
actions/workflows/{workflow}` answering 404 is a missing file, not a decision,
and the two usual causes are an adapter installed under another name and one
that has not synced yet. So:

- a repository the policy serves on a device is queued there, because the
  adapter is what is missing and not the pool
- a `dispatch` repository has no device to queue, so the push is `202` ignored
  with the workflow file named in the reason, and the same reason is written as
  an `error` status on the pushed commit, since a `202` is a success code and a
  check that never appears is indistinguishable from one still running

`mode` is required rather than defaulted, so what a repository may do is a line
a reader can see and a later edit cannot widen by omission. The mode selects
which check applies; it does not replace a check. A `device` repository is still
refused by `check_dispatch`, a `dispatch` repository is still never queued, and
the device path's own trust gate is the event's `direct_eligible` rather than
the list. An absent or empty `[dispatch]` section serves nothing on either path,
and `serve` warns at startup when no listed repository has the device mode.

### Required environment

| Variable | Required for | Effect if unset |
| --- | --- | --- |
| `GITHUB_TOKEN` | any dispatch | `/v1/dispatch` and `/cindral/dispatch` return `503` |
| `CINDRAL_WEBHOOK_SECRET` | `/cindral/dispatch` | returns `503` for every delivery |
| `CINDRAL_DISPATCH_TOKEN` | `/v1/dispatch` | returns `401` for every request |
| `CINDRAL_POLICY` | the policy `onboard` validates against | `onboard` falls back to `config/policy.toml` in the working directory |
| `CINDRAL_WORKFLOW_FILE` | `/cindral/dispatch` | defaults to `cindral-dispatch.yml` |
| `CINDRAL_RUNNER_RESERVATION_SECONDS` | local dispatch capacity | defaults to `10`; must be positive |

### Narrow the dispatch token

The token is the strongest limit on the broker's impact, and a value taken from
a broad `gh auth token` is not one: its reach is every repository that
credential can reach. Give the broker a token scoped to the repositories it
dispatches to, with only what the broker uses:

- a fine-grained personal access token, or a GitHub App installation token,
  limited to those repositories
- permission to write Actions, which is the dispatch
- permission to write commit statuses, which is the `cindral/ci` status on a
  pull request
- permission to read and write check runs, which is the step table, the log, and
  the failing-step annotation beside that status. The Checks API is available to
  App installations only, and without this permission the broker keeps posting the
  commit status alone.
- permission to read self-hosted runner state, which the local lanes select on

Drop everything else, including contents write, packages, and every repository
the broker does not dispatch to. A repository outside the token's scope is
refused by GitHub and surfaces as `502`, which is a deployment mistake to fix in
the token. The value lives wherever the deployment keeps its Secrets, and is
never written into a workflow or a policy file.

The narrow token and the allowlist fail in different directions, so keep both.
The token bounds what a leaked credential can reach; the allowlist bounds what a
token that turned out broader than intended can start.

## Quota behavior

The broker prefers hosted execution for public repositories and whenever private-repository quota is explicitly available. If quota is exhausted or unknown and paid overage is disabled, it selects an eligible local fallback from the target repository's live runner capacity before dispatching the workflow. GitHub assigns the workflow to a runner matching the adapter's lane labels.

A failed test is not a capacity signal. The broker must not dispatch a second runner after an ordinary test failure.

## Migration checklist

- Confirm the repository has a lockfile and reproducible install command.
- Add a repository-owned `ci` script or document separate task scripts.
- Copy and customize the personal dispatch adapter.
- Point `CINDRAL_POLICY` or `--policy` at the policy the broker reads, and check the device targets the onboarding report names against the adapter.
- Add the repository to `[[dispatch.repositories]]` in that policy, or `/v1/dispatch` refuses it.
- Confirm the repository has a repository-scoped runner registration for each local lane it uses.
- Add timeouts and concurrency limits.
- Keep self-hosted work off untrusted pull requests.
- Verify the hosted lane when quota is available.
- Verify the local fallback lane with unknown quota.
- Record the selected lane and runner in workflow logs.
- Do not migrate every repository until the first repository has a stable run history.
