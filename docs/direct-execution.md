# Direct execution

Repositories the policy lists with `mode = "device"`, or with `mode = "both"`
and `push_lane = "device"`, run their
CI on the device pool: Cindral queues the job and a device agent executes it in
bounded Docker. Two events enqueue a job:

To register webhooks for every device repository named in the broker policy,
run `cindral reconcile-webhooks --policy /policy/policy.toml` to preview the
targets, then run it with `--apply`. Set `GITHUB_TOKEN` with webhook access and
`CINDRAL_WEBHOOK_SECRET` to the same value the broker reads, and
`CINDRAL_WEBHOOK_URL` to its public HTTPS webhook endpoint first. The command
creates or updates the signed push and pull-request hook at `CINDRAL_WEBHOOK_URL`
and reports each repository's result. It exits nonzero if any repository fails.
Run it again when policy adds a repository; the reconcile is safe to repeat.

- A signed push to the default branch, on the pushed commit.
- A trusted same-repository pull request (`opened`, `reopened`, `synchronize`,
  `ready_for_review`), on its merge commit, with the `cindral/ci` status posted
  on the PR head commit.

Trust boundary: only pull requests whose head repository is the target
repository, that are not drafts, and that are opened either by an owner, member,
or collaborator or by a dependency bot run against the repository, run on the
device pool. Fork and untrusted pull requests stay on the hosted lane. A
dependency bot opens its branch inside the repository and can only write where
it was granted access, so its pull request is repository code.

A signed push reaches the device pool three ways, and the trust boundary above
is the same for all of them: the lane its `push_lane` names, a refusal from
GitHub for a repository that has a device lane as its fallback, and a delivery
replayed at startup that GitHub refuses. The replay is the one that is easy to
miss: nothing re-sends a delivery the broker already answered, so a refusal
found there has to be queued or the push is lost with no run anywhere. A 5xx is
not queued, because a run may already exist under it.

A device repository whose cindral adapter is missing still runs its pushes on
the device. The broker asks GitHub whether the adapter is there first, and a 404
is a missing file rather than a decision, so the push is queued rather than
ignored. What is missing is the adapter and not the pool.

## Repository contract

When a repository has neither `.cindral/ci.toml` nor a workflow file under
`.github/workflows`, the device derives CI from its committed files:

| Files | Steps | Image |
| --- | --- | --- |
| `pnpm-lock.yaml` and `package.json` with a `ci` script | `corepack enable && pnpm install --frozen-lockfile`, then `pnpm run ci` | `node:22-bookworm` |
| `uv.lock`, `pyproject.toml`, and `scripts/check.py` | `uv sync --frozen`, then `uv run python scripts/check.py` | `ghcr.io/astral-sh/uv:python3.12-bookworm` |

Ambiguous or incomplete trees fail with the missing file or script named.
A committed workflow file takes precedence. A committed
[`.cindral/ci.toml`](#repository-contract) is the escape hatch for what no single
workflow job expresses translatably here, and is consulted when the workflow asks
for one of those, or when nothing the repository committed handles the event at
all. Either way the job log names both definitions. A targeted workflow job never
falls back to inferred commands. A workflow that is broken YAML, a dispatch
naming a job no workflow defines, and a workflow that declares the event but
excludes the ref being built are each reported rather than answered by a
contract: a branch filter is a decision, and a broken file is a mistake. A
workflow file that fails to parse is skipped when another file matches the event,
and named in the error when none does.

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
  directory `/workspace` bound to the checkout. A step returns non-zero and the
  job fails with the remaining steps skipped.
- A step's status is its script's exit status, so a multi-command step that
  ends with a successful command reports success even if an earlier command
  failed. A step that must stop at its own first failure chains its commands
  with `&&` or checks each one; `set -e` on its own does not, because the step
  runs as the condition of an `if`, which suppresses `errexit` for everything
  the step executes.
- Each step runs in its own subshell. That keeps a step's own `set +e` from
  disabling the run's abort for every step after it, and keeps a step's `cd` or
  `export` out of the steps that follow.
- Each step is announced in the log before it runs and marked complete after it
  returns, so a run that stopped mid-step is distinguishable from one whose
  steps all finished. The markers carry a per-run random token, so a line a step
  prints that looks like a marker stays part of that step's output. A step can
  read the run script and forge a marker, which is acceptable here because
  direct execution is reserved for trusted repositories.
- A reported step record is validated before it is stored, so a malformed one is
  dropped rather than stored. The device bounds the record set it builds, so a
  chatty run still produces a report the broker accepts: a set over budget keeps
  the step the run stopped in and records how many steps were dropped, because a
  bounded set is otherwise indistinguishable from a short run. The pool snapshot
  carries step names and outcomes but not their output, because it is polled; the
  job page serves the output.
- `[env]` is passed to every step. `[[services]]` start first on an isolated
  network and are reachable by their `name` alias, then removed with the network.
  A service `health_cmd` holds the steps until the service reports healthy, up
  to 90 seconds.
- `timeout_minutes` bounds the job when lower than the broker default.

## Workflows as a config source

A repository can declare its CI in `.github/workflows/*.yml`. Cindral resolves
the workflow and job that match the event and the ref
the job was queued for, and a job named in the dispatch command wins over the `ci`
or `test` job when more than one matches. A `runs-on` label maps to a container
image, `ubuntu-latest` to `ubuntu:24.04` and the pinned Ubuntu tags to their
counterparts. A label with no image behind it, such as `windows-latest` or a
self-hosted label, is refused rather than run somewhere unexpected, which fails
the job unless the repository carries a contract to run instead.

A job whose steps can be translated runs in one capped container; other
supported steps use the [act engine](#act-engine). The step settings and
context fields Cindral can resolve are listed below.

### Toolchain setup steps

A setup action installs the toolchain it names, and the install runs as steps
of the same container the job's own steps run in, at the position the setup
step held. `src/cindral/toolchain.py` owns the command each action becomes.

| Action | Version it pins | Installs |
| --- | --- | --- |
| `actions/setup-go` | `go-version`, or `go-version-file` read from the checkout | the release archive from `go.dev`, with `go` and `gofmt` on the container's PATH |
| `actions/setup-node` | `node-version` | the release archive from `nodejs.org`, with `node`, `npm` and `npx` on the PATH |
| `actions/setup-python` | `python-version` | that minor from the archive that builds every Python for every Ubuntu, with `python` and `python3` on the PATH |
| `pnpm/action-setup` | `version`, or the repository's `packageManager` field | the release archive from the pnpm repository, with `pnpm` on the PATH |

- A version the action writes as a line, such as `node-version: 24` or a `go
  1.27` directive in `go.mod`, resolves the way the action resolves it: to the
  newest release of that line, read from the distributor's own index. An exact
  version names one archive. The two distributors answer that question from
  different documents, because the Go index is a list of every release and the
  node index is needed for a `node-version: 22.14` the `latest-v22.x` directory
  no longer lists.
- The version the setup step pins is the version installed. A Python patch
  release is refused by name, because the package archive carries one package
  per minor and installing it would quietly run a different interpreter.
- An input Cindral does not apply is refused by name, so a step never runs as
  something other than what it asked for. `cache` and `cache-dependency-path`
  are accepted on the three setup actions that declare them, because the agent
  restores and writes those caches itself, from the same lockfile.
  `pnpm/action-setup` declares neither, so setting one there is refused rather
  than honoured.
- Every install reads its architecture from the container, so a device and a
  broker that disagree about it cannot install a build that will not run.
- Each install fetches its own prerequisites, because the job image carries
  `apt`, `tar` and `gzip` and nothing else.

### Reusable workflows and job conditions

A job that calls a reusable workflow in the same repository, `uses:
./.github/workflows/ci-jobs.yml`, brings in the jobs that workflow defines,
under the calling job's id. The called jobs carry the caller's `with:` values
where the called workflow reads `inputs.`, the called workflow's own
environment, and the caller's `if:`. A dispatch names such a job by the name it
has inside the called workflow, so `lint` selects what GitHub calls
`fork-ci / lint`. A call to another repository's workflow, a call that reaches
outside the checkout, and a call the repository reads into itself are all
refused by name. Such a call refuses the whole repository rather than only the
file that holds it, because the call is expanded while that file is read and
there is no separately unreadable file to collect.

A job-level `if:` is read for the event the run was queued for.

- A condition that settles for that event is honoured, so a job GitHub would
  skip is skipped here too.
- A condition naming a context field the queue record does not carry is
  reported as a decision for the repository to make, naming the field. Running
  the job anyway would report a build for work the condition may have declined.
  A committed contract answers it.
- Every job that matched the event and was skipped by its own condition is
  reported, rather than replaced by a run of the lockfile's commands.
- A condition wrapped in `${{ }}` is refused by name. The wrapper is legal on any
  condition GitHub runs, so refusing it is a real gap, and it is refused rather
  than unwrapped because a condition this cannot read must be named: stripping
  the delimiters would make almost every one of them readable, and an evaluator
  that handles the shapes it recognises would silently disagree with GitHub on
  the ones it does not. A job GitHub would run is not run here, and the message
  says why. The one exception is `${{ inputs.x }}` on a reusable-workflow call,
  which is resolved below.
- In a reusable workflow, the caller's condition and the called job's own are
  combined with `&&`, so both have to hold. A condition reading `${{ inputs.x }}`
  is resolved against the *calling* workflow's declared default, because the
  dispatch's own value arrives with the run and the translation happens before
  it. A bare `inputs.x`, which GitHub also accepts, is not resolved and is
  refused by name.

### act engine

- act runs the job, and its version and the checksums act publishes for its
  tarballs are pinned in the repository `Dockerfile.act`. Build that image once
  per device and keep it there:

  ```sh
  docker build -f Dockerfile.act -t cindral-act:0.2.89 .
  ```

  A device that does not have the image fails the job when it tries to pull it,
  so the image is a device prerequisite, and a version bump is a new tag to
  build. No job downloads act.
- `src/cindral/act.py` names the image a job asks for, and a test fails when that
  moves away from the pinned release.
- act reads the workflow, copies the checkout into a container it creates, and
  runs the steps there. The log names the engine version and every action the job
  pulls in, before they run.
- `actions/checkout` never runs: the agent already checked the commit out, and act
  copies that checkout in place of the step. A checkout step's `with:` inputs,
  submodules and a path filter among them, are therefore not applied.
- The workflow's own `env:` reaches the step containers and not the engine
  container, so a workflow cannot point the engine at another Docker daemon
  through it.
- A job's own `services:` are started by act, on the network act creates for
  them, rather than as Cindral sidecars, so the device caps do not reach them.

Trust boundary: an act job is in the socket-mounting tier. act needs the device
Docker socket to create the container the steps run in, so the job asks for the
same mount as `[runner].docker = true`, and a device without a socket fails the
job before any step runs. The socket stays in the engine container: act is told
not to mount it into the containers it creates, so a workflow's steps get less
than the steps of a contract that sets `[runner].docker = true`, and a step that
shells out to Docker fails instead of reaching the daemon. Only a job the trust
gate above admits reaches this path, a signed push to a listed repository or a
trusted same-repository pull request. Two things about the tier are worth naming:
the containers act creates are outside the device caps, and a job that names an
action downloads third-party code from github.com while it runs, where the ref
the workflow names, `actions/setup-node@v4` for one, is a tag GitHub can move.
Deciding which actions a job may use is not built.

`github.ref`, `github.ref_name`, `github.ref_type`, `github.sha`, the repository
name and owner, the event name, workflow and job names, workspace, and event
fields present in the queued record resolve in steps, in a step's version
input, and in a job's own `if:`. A run-only job resolves
bare context paths natively; the act engine evaluates functions and conditions
against the same event. A missing field such as `github.actor` or a pull-request
head/base branch fails by name before the job starts, rather than reading as an
empty value. The queue does not yet carry those fields. `strategy.matrix`
fan-out, `needs` across jobs, and `secrets` remain unsupported and fail before
execution, unless a committed contract answers for them. Step-level environments, working directories, shells, failure
settings, and timeouts use the act engine, whose step containers are outside
the device caps. Job-level `defaults` and `container` settings are refused
until they can be applied, and a repository carrying a contract runs it instead,
which the job log names. Checkout inputs are refused: act copies the agent's
checkout instead of applying the requested checkout settings.

A refusal names the step and the construct that stopped it, and a job carrying
more than one blocker names each of them. A job on a self-hosted label that
also names an action Cindral does not run is refused for the label and for the
action, because the label is often only the first of the two.

## Device agent

The agent pulls jobs, checks out the commit, runs the contract, and reports the
exit code and a log tail as the `cindral/ci` commit status. A failure names the
step the run stopped on, and links to the run when `CINDRAL_STATUS_TARGET_URL`
is set.

```sh
CINDRAL_AGENT_TOKEN=... GITHUB_TOKEN=... \
  cindral agent --url https://cindral.example.com --device "$(hostname)"
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
| `--max-concurrency` / `CINDRAL_AGENT_MAX_CONCURRENCY` | jobs this device runs at once, default `1` |
| `--job-cpus` / `CINDRAL_JOB_CPUS` | cpu cap for each job container, unset for no cap |
| `--job-memory` / `CINDRAL_JOB_MEMORY` | memory cap for each job container, such as `12g` |
| `--heartbeat-seconds` / `CINDRAL_AGENT_HEARTBEAT_SECONDS` | device heartbeat interval, default `15` |

The agent needs Docker and permission to read the repositories it runs.

### Preflight

Before its first claim, and then once a minute, the agent asks its own Docker
daemon for its version and claims nothing while the answer is no:

```text
cindral: docker cannot reach its daemon (exit 1): permission denied while trying to connect to the docker API at unix:///var/run/docker.sock, claiming no jobs
```

A device that cannot run its work still claims it, leases it, and spends a
checkout and a lease on failing. The check is what the claim depends on, so it
runs from the agent's own process: a systemd user manager created before the user
joined the `docker` group keeps the supplementary groups it was started with, so
`docker info` succeeds in a login session while the agent's process cannot open
the socket. Only a probe from the process that will do the work answers that
question.

- The check costs one short command on one thread, with a 10 second deadline. A
  Docker CLI that hangs on the socket gives up its turn rather than holding the
  claim loop, and the hung probe is left to finish alone instead of being
  replaced on every interval, so probes do not pile up.
- A missing CLI is named as such, a daemon that refuses is named with its own
  error, and either is said once rather than on every poll. A healthy device says
  so once at startup.
- The check keeps running after a failure, so a device starts claiming again by
  itself once an operator fixes the service, within one interval. The cadence is
  not a flag: what a device can reach changes when a service changes, not when
  work arrives.
- The check covers what the executor needs to start a job, not what one job needs.
  A device can pass it and still fail on an image it cannot pull or a workspace
  it cannot write. Those failures arrive as a lost run, which the broker retries,
  rather than as a failed test.

A lapsed lease is reclaimed with the commit status it owes written in the same
SQLite transaction, so the run is never claimable again before its loss is
durable. That ordering is what stops a slow post about an earlier loss from
landing after a retry that already succeeded. A status that cannot be published
stays owed in `pending_status` and is republished by the next queue event, so a
commit that never resolved does not wait on another lease lapsing. A run's own
verdict clears whatever it still owed. A drain takes each owed status out of the
row before posting it and puts it back if the post fails, so two drains sending
the same queue event at once cannot post it twice, and a run re-claimed and
finished while a post is in flight is not told afterwards that it was lost.

A lease that lapses is not spent at once. A device that announced itself inside
the pool's online window is present, so a renewal or a report arriving late
belongs to a device that is working, and releasing its run would start the same
work a second time. That window is bounded, and past it the reclaim takes the run
whatever the device is doing: a device with a wedged executor and a dead
renewal thread keeps announcing the device while the run stops, and a heartbeat
is not evidence that a run is progressing. The reclaim counts those separately
as `live_device_reclaim_count` / `cindral_reclaims_live_device_total`, which is
the standing measure of how often the queue restarts a run a device still held.

- The heartbeat continues while the check fails, so the device stays in the pool
  with its slots advertised and takes no work. The broker records no state for
  that, and the pool shows an online device with free slots and no jobs, which is
  also what an idle device looks like. The device log is where the difference
  shows. A "degraded, not claiming" state in the pool needs a heartbeat field and
  a panel column, which no part of the broker carries today.

### Device capacity

One slot is the default, so an agent without capacity settings runs a single
job at a time exactly as before. A device that can take more work raises the
slot count and caps each container:

```sh
CINDRAL_AGENT_MAX_CONCURRENCY=4 CINDRAL_JOB_CPUS=2 CINDRAL_JOB_MEMORY=12g \
CINDRAL_AGENT_TOKEN=... GITHUB_TOKEN=... cindral agent --device device-beta
```

- Each slot claims and executes independently, so a device runs up to
  `max_concurrency` jobs at the same time and a slot finishes its own job
  before claiming another. A slot count above one is the only way a device
  runs parallel jobs; the broker does not split one job across slots.
- The caps reach the job container, each of its service containers, and the
  workspace cleanup container. A cpu cap is applied as a CFS quota inside a fixed
  period, which is what Docker's `--cpus` means, and the memory cap pins swap to
  the same value so a container cannot exceed its memory budget through swap.
- The caps do not reach a job that runs through the act engine. Its engine
  container is capped like any job container, while the containers act creates
  for the steps and the job's services are started by the daemon and are not.
- One container a job starts is not capped: the short-lived container that repairs
  cache directory ownership. Archiving a cache fails with a permission error
  when the job container left root-owned files in its cache directory, and the
  agent then chowns that directory back to the account it runs as, falling back
  to `chmod -R a+rX` when the chown fails, so the archive can be written. It runs
  once per cache whose archiving needed it, the agent does not pass the caps to
  that helper, and a device sizing itself by the caps below should leave room
  for it.
- The per-container cap is not the device's peak. A job runs its job container,
  every service container it declares, and later a cleanup container, each
  capped separately, so a job with three services can hold four times the
  memory cap at once. The device peak is at most:

  ```
  slots x (1 + declared services) x per-container cap
  ```

  plus a cleanup container per job that overlaps another container, plus the
  uncapped cache-ownership container, plus image build steps where the build is
  capped at all. Size a device by that arithmetic, not by slots times one cap,
  and prefer a per-job memory cap with no services where the two disagree. With
  no caps set, containers take whatever the device gives them and the device can
  oversubscribe itself.
- `CINDRAL_AGENT_MAX_CONCURRENCY` is at most 1024. The agent refuses to start
  above that, and the broker refuses such a heartbeat in turn, so the bound holds
  on both sides.
- An image build is capped only where the local Docker CLI takes a limit flag.
  The agent probes `docker build --help` once per process and uses
  `--resource key=value` where BuildKit is the builder, or the `docker run` flag
  form on a legacy builder. A Docker with neither form leaves the build uncapped
  and every build's log says so, rather than failing the job on an unknown flag.
- Build limits are not a device-wide guarantee. They apply per build step, not
  to the build as a whole, so a build running steps in parallel can exceed the
  cap, and `--resource` is honored only by a BuildKit daemon that supports
  per-step resource limits on Linux. Where the daemon does not support them the
  flag is accepted and the build is not bounded, and the agent cannot detect
  that from the CLI's help text. A build step is also free to run outside the
  per-container accounting above, since it is not a job or service container.
- `CINDRAL_JOB_MEMORY` takes a Docker memory size with a unit and no inner
  space, such as `12g`, `512m`, or `2GiB`. A bare number is refused because
  Docker would read it as bytes, and `12 g` is refused because the space would
  reach Docker verbatim. `CINDRAL_JOB_CPUS` is at least 0.01, below which the
  quota rounds to no usable cpu and the container would stall, and
  `CINDRAL_AGENT_IDLE_SECONDS` is at least 0.1, below which an idle device
  polls the claim endpoint as fast as it can.
- Each job gets its own workspace, network, container names, and log file,
  keyed by job id, so slots do not collide. A device that is handed the same
  job id twice, which happens only when a lease is reclaimed while its previous
  attempt is still running, will have the two attempts fight over the same
  network and workspace names.
- Stopping the agent does not drain it, and no signal handler is installed.
  `stop()` asks every slot to stop claiming once the job it holds has finished
  and reported, so it never cancels a job in flight; a signal, in contrast, ends
  the process where it stands. Under `systemctl stop`, Ctrl-C, `docker kill`, or
  a `KillMode=process` unit, the cleanup path never runs. The job container and
  its service containers are started by the Docker daemon rather than forked
  from the agent, so they keep running on the device, still inside the caps,
  until their own steps finish or an operator removes them, while the job's
  lease expires and the job is retried elsewhere. A run that ends or fails
  inside the executor does get its job container, its services, and its network
  removed, by the executor rather than by the daemon, so a container outliving a
  hard kill is one an operator has to remove. Leftovers are named
  `cindral-<job id>-job` and `cindral-<job id>-<service>`, so
  `docker ps -a --filter name=cindral-` finds them. An act job leaves containers
  named `act-<hash>` instead, which `docker ps -a --filter name=act-` finds, and
  act removes its own containers when the run ends normally. Stop a device when
  it is idle, or after its queue has drained. Signal-based draining is not
  implemented yet.

### Device heartbeat

At startup and then every `CINDRAL_AGENT_HEARTBEAT_SECONDS` seconds, the agent
sends `POST /v1/agents/heartbeat` with its agent bearer token and a JSON body of
`{device, labels, max_concurrency}`, so the broker learns the labels and slot
count a device is actually running with. A broker without the route answers 404
and keeps no per-device record, so each beat is a logged no-op. The
interval defaults to 15 seconds and is capped at 30, so that once the window
exists every beat lands inside it and a working device cannot read as absent.

```json
{"device": "device-beta", "labels": ["arm64"], "max_concurrency": 4}
```

The agent reads nothing back from the response. A 2xx with an empty body or a
JSON object is accepted, and anything else counts as a failed beat, so the
broker should answer 204 or a JSON object rather than plain text.
`max_concurrency` is the slot count the device is actually running, which is the
value a device carries after an operator edits it, and is at least 1.

A heartbeat that fails is reported once and then retried quietly until it
succeeds, and the device keeps claiming and reporting jobs either way: a broker
without the route answers 404, and a device that can still take work should. A
beat that stalls or whose connection drops counts as a failed beat, so a
transport failure costs a beat rather than the device's presence.

An older broker without the heartbeat route answers 404; the agent logs the
first failure and continues working. Once the broker supports the route, the
pool snapshot shows the agent even while it is idle.

## Dependency caches

The agent automatically restores and writes tool caches when matching lockfiles
are present. Cache keys include the lockfile digest and agent architecture;
lookups stay within the exact repository and branch. A successful job creates
an immutable entry, while a failed job never updates it. Cache restore or write
errors are warnings and do not change the job result.

Adapters cover pnpm and npm package stores, uv and pip download caches, Go
module/build caches, and Next.js `.next/cache`. Cache archives are content
addressed by SHA-256. Prefix restores only consider entries for the same
repository, branch, and architecture.

The broker stores cache blobs next to `CINDRAL_JOBS_DB` by default. Configure
`CINDRAL_CACHE_DIR` to choose another persistent directory. Defaults are a
30-day idle TTL, 256 MiB per repository, 768 MiB total storage, and 256 MiB per
blob. Expired and least-recently-used entries are removed as cache requests
arrive. Cache requests use the agent bearer token.

BuildKit registry-layer caching remains configured by repository-owned Docker
build workflows; the Cindral cache API does not proxy registry credentials.

## Build artifacts

A step keeps its output by writing it into `.cindral/artifacts/` in the
checkout. The working directory is the checkout, so a step writes a relative
path:

```yaml
[[steps]]
run = "mkdir -p .cindral/artifacts/reports && vitest run --reporter=junit --outputFile=.cindral/artifacts/reports/junit.xml"
```

When the run ends the device tars that directory into one uncompressed
archive and uploads it to the broker, which keeps it and serves it back. A run
that wrote no `.cindral/artifacts` directory uploads nothing, and the job log
says so. A run whose directory is empty uploads nothing, and says that too, so
a reader can tell a run that produced nothing from a device that never looked.
A failed run uploads what it produced: a failing test is the run whose output
someone opens the artifact for.

- The archive is a plain tar. Every member is named under a single `artifacts/`
  root and from a path relative to the directory, so it holds no absolute path
  and no parent reference, and a symlink a step wrote is left out rather than
  followed.
- The device refuses to pack a directory the broker would not store: over
  20,000 files, or over `MAX_ARTIFACT_BYTES`. That refusal is a log line, not a
  failed run. A run's verdict is its exit code, and output the broker will not
  take is a missing artifact rather than a red commit.
- The device removes its local archive once the broker has it, including when
  the upload failed, so a device does not fill its disk one archive per run.
- A step that leaves root-owned files behind is handled the way a cache
  directory is: the device runs a short uncapped container to hand the
  directory back, then packs it.
- An artifact is per-run and immutable while the run is on it. A second upload
  for the same run keeps the first, so a device that re-sends after a lost reply
  is safe.
- A reclaim drops it. A run whose lease lapsed is executing again on another
  device, so the archive the lost attempt produced is discarded rather than
  served under the new attempt's verdict. The discard happens wherever the
  release happens: the claim endpoint and the broker's reclaim timer.

  An upload that finds its lease gone stores nothing. If it was the upload that
  published the run's archive, it also drops that archive, named by its own digest
  so the drop cannot reach a different one. If it was a repeat of an upload the run
  already had, it drops nothing: the standing archive belongs to whichever attempt
  stored it, that attempt was already told its upload succeeded, and it will never
  send it again. Deleting an archive a device believes it stored loses a run's
  output for good, which is a worse failure than serving the wrong one.

One archive per run is the whole model. There is no manifest, no per-file
addressing, no compression, and no way for one job to read another job's
output.

### Reading an artifact

An artifact is written by the code the run checked out, so reading it is
serving bytes the repository chose. The download takes the read-only pool
token, the same credential `GET /v1/jobs/{id}` and `/log` take, because the
panel that can already read a run's log is the panel that downloads its
output. It takes no CORS header, so a page a browser reaches cannot read it
through the browser, and the reply is an attachment with a fixed content type
and `X-Content-Type-Options: nosniff`, so the bytes are downloaded rather than
rendered. An unset pool token refuses every request rather than serving the
archive open.

The upload takes the agent token, because it is a mutation, and only from a
device that currently holds the run's lease, so a device cannot attach bytes
to a run another device is running.

| Endpoint | Auth | Content |
| --- | --- | --- |
| `POST /v1/jobs/{id}/artifacts` | agent bearer token, and the run's lease | the run's tar archive, as the request body |
| `GET /v1/jobs/{id}/artifacts` | `CINDRAL_POOL_TOKEN` | the archive, as an attachment |

`GET /v1/jobs/{id}` and the report response both carry what the panel needs, so
one request tells it whether there is anything to download:

```json
{
  "id": "6f1c...",
  "status": "failure",
  "steps": [],
  "artifacts": {
    "name": "6f1c....tar",
    "size": 20480,
    "digest": "9f2c...",
    "created_at": 1780000000.0,
    "expires_at": 1780604800.0,
    "url": "/v1/jobs/6f1c.../artifacts"
  }
}
```

`artifacts` is `null` for a run that produced none, and on a broker with no
artifact store configured. `url` is broker-relative, so a panel reads its base
URL once and joins this path. Render the download when `artifacts` is not
`null`, and nothing at all when it is: a run that produced no output is not a
run that failed to produce output.

The `recent_jobs` entries in `GET /v1/pool` carry no `artifacts` key. That
snapshot is polled and lists fifty runs at once, and whether each of them has
an archive is a question only the job page answers.

Archives are kept for a week and then removed, and the oldest go first once
the store is over its per-repository or total quota. An artifact is read once
by whoever the run was for, so age is the order things are dropped in; the
cache evicts on access instead, because a cache entry is reused.

| Variable | Meaning |
| --- | --- |
| `CINDRAL_ARTIFACT_DIR` | archive directory, defaults to `artifacts` beside the jobs database |
| `CINDRAL_ARTIFACT_TTL_SECONDS` | how long a run's archive is kept, default 7 days |
| `CINDRAL_ARTIFACT_REPOSITORY_QUOTA` | bytes one repository's archives may hold, default 256 MiB |
| `CINDRAL_ARTIFACT_STORAGE_QUOTA` | bytes all archives may hold, default 768 MiB |
| `CINDRAL_ARTIFACT_MAX_BYTES` | largest single archive, default 64 MiB |

## Broker configuration

- `CINDRAL_JOBS_DB`: SQLite path for the device queue and signed push dispatch
  ledger; both paths return `503` when unset.
- `CINDRAL_AGENT_TOKEN`: bearer token agents present.
- `CINDRAL_POOL_TOKEN`: read-only bearer token for `GET /v1/pool`.
- `CINDRAL_APP_CLIENT_ID`: GitHub App client ID, the `iss` claim of the App JWT.
- `CINDRAL_APP_PRIVATE_KEY`: path to the App private key, read as a file and
  never logged. A JWT signed with it authenticates the App itself; GitHub caps
  its lifetime at ten minutes.
- `CINDRAL_APP_API_URL`: override for the API base, for a test server.

With an App client ID and key configured the broker discovers repositories
through `GET /installation/repositories`, which returns only the repositories the
installation covers. A user token can instead see every repository the user can
see, including repositories the user merely collaborates on, so the App is what
makes account-wide adoption safe.

The installation is resolved once and the broker refuses to start rather than
serve nothing when the App is not installed, is suspended, is scoped to selected
repositories and covers none of them, or answers with more than one installation
and no way to choose between them. Installations and repositories are both read
in bounded pages, and a full page at the cap raises rather than returning a
silent prefix: an inventory that stops at the boundary is a set nobody chose to
leave out, and an App installed on more accounts than one page holds would
otherwise look unambiguous. A stated token expiry past the one-hour lifetime
GitHub documents is treated as the hour, so a surprising value cannot keep a
dead token cached.
- `CINDRAL_JOB_LEASE_SECONDS`: lease duration, default `300`.
- `CINDRAL_JOB_TIMEOUT`: per-job timeout, default `3600`.
- `CINDRAL_MAX_REPOSITORY_RUNNING`: active leases allowed per repository, default `2`; must be positive.
- `CINDRAL_STATUS_CONTEXT`: commit status context, default `cindral/ci`.
- `CINDRAL_STATUS_TARGET_URL`: page a commit status links to, with `{job_id}` where the run's id goes, for example `https://panel.example.com/api/jobs/{job_id}`. Without it, a status carries no link.
- `CINDRAL_BILLING_ORG`: organization whose Actions billing answers the hosted quota for private repositories; unset leaves the quota unknown.
- `CINDRAL_BILLING_REFRESH_SECONDS`: billing refresh interval in seconds, default `300`, between 60 and 3600.
- `CINDRAL_BILLING_MAX_AGE_SECONDS`: age at which a cached reading stops routing, default `900`, between 60 and 86400, at least the refresh interval.
- `CINDRAL_BILLING_STARTUP_TIMEOUT_SECONDS`: per-request timeout for the read taken before the broker starts listening, default `3`, between 1 and 30. It bounds that one read; the periodic refresh uses the client's own timeout, so a response that is slow but not unreachable is not reclassified on every cycle.
- `CINDRAL_CACHE_DIR`: persistent cache directory, defaults beside the jobs database.
- `CINDRAL_CACHE_TTL_SECONDS`: idle TTL, default 30 days.
- `CINDRAL_CACHE_REPOSITORY_QUOTA`: per-repository quota, default 256 MiB.
- `CINDRAL_CACHE_STORAGE_QUOTA`: total cache quota, default 768 MiB.
- `CINDRAL_CACHE_MAX_BLOB_BYTES`: largest archive, default 256 MiB.

Leases are authoritative: a device holds a job until its lease expires, and an
expired lease returns the job to the queue for another device. A device renews
its own lease on a thread at `lease_seconds / 3`, so a lease is spent when the
device stops renewing, not when a step finishes, and a renewal or a report that
arrives after the expiry is still accepted while the device is present. The
reclaim runs on a claim, and `reclaim_loop` in `lease_reclaim.py` runs the same
two steps on a timer, started by `serve()` whenever the broker has a queue. The
timer is what settles a lease for a pool that is not asking for work, and what
carries a parked run's verdict to its commit when no device comes back.
Among pending jobs a device can run, the broker first chooses a repository with
the fewest active leases, then its oldest job. A repository at its slot limit
waits while other eligible repositories continue. The selection and claim run
in one SQLite write transaction, so overlapping device claims see the same
limits. This applies to direct jobs, not GitHub-hosted dispatch adapters.

## Hosted quota source

`CINDRAL_BILLING_ORG` points the broker at an organization's GitHub Actions
billing, read with the GitHub token the broker already holds. Every route that
reaches `Policy.choose` with an unstated quota, the signed push webhook, the
pull-request webhook, `POST /v1/route`, and `POST /v1/dispatch`, is then routed
on that reading instead of unknown quota. A quota status a caller states
outright always wins.

- The legacy endpoint `GET /orgs/{org}/settings/billing/actions` is read first.
  It names the minutes included, used, and paid for the current billing cycle
  and requires an organization owner token. When it is gone (HTTP 410) or
  refuses the credential, the enhanced usage report
  `GET /organizations/{id}/settings/billing/usage` for the current month is
  read instead, which takes an organization admin.
- The enhanced billing platform publishes no included-minute allowance, so that
  source reports the minutes it observed and states no quota status at all. It
  cannot settle whether an allowance is gone: larger runners are always billed
  and never eligible for included minutes, so an organization with its whole
  allowance untouched that ran one larger-runner job reports billed minutes, and
  public-repository usage is listed the same way. Only the legacy endpoint
  reports an allowance, and only an exhausted reading it produces is subject to
  the policy's `paid_overage` setting.
- A credential that cannot read billing, or a reading past
  `CINDRAL_BILLING_MAX_AGE_SECONDS`, reports the quota as unknown, never as
  zero, so private pushes keep following the policy's `unknown_quota` setting.
- A cached reading from a previous billing month is refused, because the
  allowance reset on the first of the month and the minutes came back. The
  legacy endpoint reports no month, so such a reading is gated on age alone.
  Refusing it also reads afresh, so the first route after the reset does not wait
  out the refresh interval with the quota reported unknown. That read runs on
  the caller's thread, which is a webhook delivery or a route request, so it is
  bounded to 3 seconds per upstream call rather than the client's own timeout,
  and the fallback path makes at most three of them: a billing API that hangs
  must not hold a delivery open behind it. If that read
  fails, the previous reading stays cached and the route sees unknown with the
  month-reset reason, and the next caller tries again.
- The reading refreshes on a bounded interval. A failed refresh serves the last
  good reading, so a GitHub outage does not change routing until the reading
  goes stale.
- The route decision's reason carries the reading's source and age, for
  example `hosted quota is available; quota read 41s ago from actions_billing`.
- The reading describes one organization: a broker serving repositories under
  several owners routes all of them on that organization's quota, so name the
  organization that owns the private repositories being routed.
- The panel does not display quota readings yet.

## Check runs

A device run reaches the commit twice. The commit status it has always posted is
one line under the status context, and the same run is also published as a GitHub
check run through the Checks API, which is what puts a step table, a log, and the
failing step on the commit itself.

**Nothing publishes a check run yet.** `cindral.check_runs.publish_check_run` is
the whole feature and it has no caller, so today a device run reaches the commit
as the commit status alone. The call sites that would make it run belong in
`service.py` and `jobapi.py`.

The check run is named after the status context (`CINDRAL_STATUS_CONTEXT`,
default `cindral/ci`), so a branch protection rule that requires `cindral/ci`
keeps passing on the name it already requires. Both renderings come from one
stored job record, and the sentences are the same: the check run's title is the
text the commit status carries, and its annotation repeats that text.

A commit status has no conclusion vocabulary, so it cannot say why a run is red.
The check run can, and reads it off the loss the record kept rather than off the
state:

| the record | the loss | the conclusion |
| --- | --- | --- |
| every step passed | none | `success` |
| a step failed | the change | `failure` |
| exit 130, the code the executor reports at its own limit | the run's limit | `timed_out` |
| out of attempts, the device stopped answering | the run was taken away | `cancelled` |
| out of attempts, the device reported an error | the device could not run it | `failure` |
| waiting for another attempt | none yet | no conclusion |

One call publishes whatever the record says:

```python
from cindral.check_runs import publish_check_run

publish_check_run(server.github, job, name=server.status_context, target_url=server.job_target_url(job.id))
```

It opens the check run the first time it is called for a job and updates that one
check run after that. A run with no verdict yet is `queued` or `in_progress`; a
run with one is `completed` with its conclusion, its step table, the log of a run
that did not pass, and an annotation naming the step that did not finish.

The check opens on enqueue and closes on the verdict, with the step table in the
summary. It does not show a step as it finishes: the agent-facing API has exactly
three mutating actions, `claim`, `renew`, and `report`, a report with no exit code
is refused, and the agent reports only after the run ends, so there is no moment
at which the broker holds a mid-run record to publish. The update path is here
for a progress endpoint, and unused until one exists.

The log reaches the commit only for a run that did not pass, and it is scrubbed
of recognizable credentials first: tokens, private keys, `Authorization`
headers, and URLs carrying a password. A secret printed with no recognizable
shape is not caught, because a pattern cannot be sound about arbitrary text, and
the panel says when it removed something. A green run's log is not published at
all, and the raw log is on the job page for anyone who needs it.

The check run id is held per job and per name, recovered from GitHub by the run's
external id after a broker restart, and dropped when GitHub no longer has it, so
an update never opens a second check for the same run and never sticks on an id
that is gone. The listing it is recovered from is paged, because a commit the
same job is retried on holds one check per attempt under that name.

The Checks API is available to GitHub App installations only: a personal access
token and an OAuth token can read check runs and cannot create them. The App the
broker authenticates as needs the **Checks: Read and write** repository
permission. A broker with no App, or an App without that permission, keeps
posting its commit status: the call raises `CheckRunAPIError` and the call site
absorbs it, because the commit status is the surface branch protection reads.
A throttled App is a different answer from a missing permission, and the error
carries the code and the `Retry-After` wait so a caller can tell them apart.

## Observability

The broker reports which build is running, so an operator reading a page or a
metric can tell which deployment they are looking at. The values are build
metadata supplied to the image build, not a file read from the source tree: a
checkout has no build to describe, and a file in an image would report what it
said rather than what was actually built. Each unset value reports `unknown`.

| Variable | Set at build time from | Reported as |
| --- | --- | --- |
| `CINDRAL_VERSION` | `VERSION` in the publishing workflow | `build.version` |
| `CINDRAL_COMMIT` | `github.sha` | `build.commit` |
| `CINDRAL_BUILT_AT` | the build step's UTC timestamp | `build.built_at` |

`GET /healthz` carries the build and needs no credential, `GET /metrics` carries
it as `cindral_build{version,commit}`, and `GET /v1/pool` carries it as `build`
so the panel shows the broker's own version. The panel and the broker are
deployed separately and can differ, which is the reading worth being able to
check.

| Endpoint | Auth | Content |
| --- | --- | --- |
| `POST /v1/agents/heartbeat` | agent bearer token | device liveness with its labels and slot count; a broker without the route answers 404 |
| `GET /v1/pool` | `CINDRAL_POOL_TOKEN` | JSON pool snapshot: devices with their leases, queue depth, oldest pending age, stalled reason, expired lease count, reclaim total, reclaims taken from a connected device, recent jobs |
| `GET /v1/jobs/{id}` | `CINDRAL_POOL_TOKEN` | one run: state, exit code, device, attempts, reported log, per-step records, and the run's artifact or `null` |
| `GET /v1/jobs/{id}/log` | `CINDRAL_POOL_TOKEN` | the reported log as plain text, the tail the agent sent |
| `POST /v1/jobs/{id}/artifacts` | agent bearer token, and the run's lease | the run's tar archive, as the request body |
| `GET /v1/jobs/{id}/artifacts` | `CINDRAL_POOL_TOKEN` | the archive, as an attachment |
| `POST /v1/cache/lookup`, `PUT /v1/cache/entries`, `GET /v1/cache/blobs/{sha256}` | agent bearer token | scoped immutable cache lookup, upload, and blob restore |
| `GET /metrics` | none, same-origin only | Prometheus pool, queue, and cache counters and gauges |

A reported run carries `steps`: each step's index, command, output, whether it
finished, and whether its output was bounded. The device parses them from its own
complete log, because the log the broker receives is a bounded tail that would
drop the steps which ran first. An empty list means the run announced no step,
which is a run that ended in the checkout or the image pull, not a run with no
steps. The signed-in step view is built on this in a later slice.

Agent heartbeats are stored with a broker-generated `last_seen` time. The pool
snapshot includes idle agents and reports `agent_online` while their last beat
is at most 60 seconds old. Each device carries `max_concurrency`,
`active_jobs` from its current leases, and `available_slots`. The slot count is
`null` when the agent is disconnected or has never reported its capacity;
`max_concurrency` remains visible for a disconnected agent. Agent records older
than 30 days are removed when a heartbeat arrives and when a pool snapshot is
read, so a pool whose agents all go quiet still shrinks. Metrics distinguish
connected agents from configured runners and count only connected agents' free
slots.

When a pending job has waited at least five minutes, `stalled_reason` names the
one cause the queue can defend:

- `no_agents`: no device agent is connected
- `idle_devices`: every connected device is at its slot limit, so the queue is
  waiting on capacity
- `deferred`: nothing claimable is waiting, so the queue is inside a retry backoff
- `no_eligible_device`: a claimable run needs labels no connected device
  advertises

It stays null when capacity is free and a claimable run is waiting, because the
per-repository run cap and an agent that is not polling are both outside what
the snapshot holds, and a reason the queue cannot defend sends the reader after
hardware the pool does not need. The overview displays a warning naming the
reason and `cindral_pool_stalled{reason="..."}` exposes a gauge per reason for an
alerting rule. The oldest pending age remains available independently. A lease
the reclaim can take back is counted as waiting, and the age, the backoff check
and the label check all read that same set of rows, so the queue depth, the age
and the reason cannot disagree about what is waiting.

`/v1/pool` answers cross-origin preflight for the panel. It takes the read-only
pool token, not the agent token: a control panel that can read queue state
should not hold the credential that can claim, renew, and report jobs. The
agent and dispatch tokens are both refused, and an unset pool token refuses
every request rather than serving the snapshot open. The broker also refuses to
start when the pool token equals the agent or dispatch token, since a shared
value would hand the read-only consumer the write capability. `/metrics` carries
no `Access-Control-Allow-Origin`, so a browser page a scraper visits cannot read
pool state.

The pool token is also accepted on `GET /v1/jobs/{id}` and
`GET /v1/jobs/{id}/log` so a panel can read a single run and the log the status
links to, and on `GET /v1/jobs/{id}/artifacts` so it can download what that run
produced. `claim`, `renew`, `report`, the artifact upload, and the agent
heartbeat still take the agent token only.

A lease the reclaim can take back is counted as pending rather than running, and
reported separately as `expired_lease_count` / `cindral_expired_leases`. Both
endpoints stay off the public Ingress; only the signed `/cindral/dispatch` path
is published.

## Control panel

`web/` is a Next.js App Router app built on a shared admin shell. It reads
the snapshot server-side and renders Overview, Devices, and Queue views.

The snapshot carries no clock of its own, so the panel records the moment it
read the broker and shows that as the last-updated time on every view. A manual
refresh re-reads the broker, an optional auto-refresh interval does it on a
timer, and a view older than two minutes is marked stale. Lease expiry and queue
age are shown relative to the snapshot with the absolute time on hover; a job
dispatch time appears once the broker reports `created_at` in the job payload.

The panel follows the operating system colour preference and remembers an
explicit choice in the browser, applying it through the shell's
`data-admin-theme` attribute.

| Variable | Meaning |
| --- | --- |
| `CINDRAL_API_URL` | broker base URL, default `http://127.0.0.1:8095` |
| `CINDRAL_POOL_TOKEN` | read-only token the panel presents to the broker |
| `CINDRAL_PANEL_PASSWORD` | operator password for the panel login; unset means every login is refused |
| `CINDRAL_PANEL_PASSWORD_HASH` | `scrypt$salt$key` form of the same password, used in preference to `CINDRAL_PANEL_PASSWORD` |
| `CINDRAL_PANEL_EMAIL` | email shown on the login form and the session |
| `CINDRAL_SESSION_SECRET` | signs the session cookie; under 16 characters refuses every login |
| `CINDRAL_API_TIMEOUT_MS` | broker request timeout, default `5000` |

The panel is a single-operator surface: one password and a signed session
cookie, with no user database. The broker credential it holds is the read-only
pool token, so a compromised panel cannot claim or report jobs. An unset
password, session secret, or operator email refuses logins rather than serving
the panel open, and failed sign-ins are throttled per client.

The shell's credential adapter expects user and session records, so the panel
uses its session adapter over the single-operator credential instead: the
adapter signs and clears the cookie and bounds failed attempts, while the panel
decides who the operator is. The password is compared with scrypt either way,
`CINDRAL_PANEL_PASSWORD_HASH` keeps the plaintext out of the environment
entirely. The cookie names a session that carries the second it lapses, so a
cookie kept past its expiry is refused server-side rather than only by the
browser that holds it. A session cannot be revoked before it lapses, because
there is no session row to delete; a second operator is what puts one there.

### The write boundary

`web/src/lib/resources.ts` declares the two resources the panel holds, `devices`
and `jobs`, column for column as the broker's own payloads carry them. Every read
goes through `createAdminResourceActions`, so a write is held to that
declaration: a key no column names is refused by name rather than stored. The
declaration is the whole of the guarantee, so a broker field the file does not
name has to be added there before a write carrying it is accepted, and a
resource added with no definition is not checked at all.

The panel ships no write. The store's three write methods refuse, and the
permission rule grants `devices.read` and `jobs.read` and nothing else, so a
write is refused by the rule before the column check and before the store.

### The one permission rule

`web/src/lib/permissions.ts` holds the panel's only permission rule. The nav is
filtered by it, the store's guard asks it, and the API routes answer 401 and 403
from the same call, so a link that is drawn and the action behind it cannot
disagree. It switches on the role the panel's session carries, which comes from
the configuration rather than from anything a request sent.
