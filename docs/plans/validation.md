# GitLab Emulator Validation Plan

## Goal

Keep a repeatable validation path for the emulator, Git Smart HTTP, GitLab REST
APIs, and official GitLab Runner execution.

## Local Validation

Run focused pytest suites while developing:

```bash
make test-focused
```

Run the affected GitLab API regression set before handing off a slice that
touches REST routing, GitLab-shaped models, pipelines, projects, groups, merge
requests, files, releases, search, or webhooks:

```bash
make test-affected
```

Run the full local suite before larger handoffs:

```bash
make test-full
```

The current local targets expand to `uv run --extra dev python -m pytest` with
`UV_CACHE_DIR=/tmp/glemu-uv-cache`, so pytest dependencies come from the `dev`
extra and cache writes stay outside the repo.

Important local coverage areas:

- `.gitlab-ci.yml` parsing
- pipeline APIs
- runner coordinator APIs
- trace append and fetch
- job status transitions
- artifacts
- cache metadata and archive endpoints
- project, group, merge request, repository file, release, webhook, and search
  compatibility as it is added
- encoded project path route ordering, especially catch-all routes under
  `/projects/{project_ref:path}`
- exact GitLab pagination headers and query-preserving `Link` headers for main
  GitLab-facing list endpoints
- GitLab-style request ID response headers, including caller-provided
  `X-Request-Id` propagation to `X-GitLab-Request-Id`
- GitLab-style error payload envelopes preserve route-specific string messages

## VM Validation

The Vagrant setup uses four VMs:

- `server`: emulator, Caddy TLS, Docker Compose, MinIO
- `client`: `git`, `curl`, and `glab` validation
- `runner`: official GitLab Runner with Docker executor
- `k8s-runner`: official GitLab Runner with Kubernetes executor and local k3s;
  it validates both a VM-service runner and an in-cluster runner manager

Expected validation path:

```bash
make vm-validate
```

For an already deployed server, use:

```bash
make vm-validate-current
```

For client-side GitLab CLI compatibility only, use:

```bash
make vm-test
```

This syncs the client scripts, installs or reuses the pinned `glab` binary,
installs the emulator CA in the client VM, and runs the broad
`scripts/glab-integration-test.sh` smoke. The current smoke covers auth, users,
projects, nested groups, repository create/clone/delete, Git Smart HTTP,
repository files, issues, labels, milestones, branches, tags, releases, commits,
merge requests, pipelines, traces, artifacts, manual jobs, cancel, and retry.

For runner-only validation, use:

```bash
make vm-runner-validate
```

For Kubernetes executor validation, use:

```bash
make vm-k8s-runner-up
make vm-k8s-runner-validate
make vm-k8s-runner-secret-validate
make vm-k8s-incluster-validate
make vm-k8s-incluster-secret-file-test
```

This provisions k3s on the `k8s-runner` VM, registers an official GitLab
Runner with `executor = "kubernetes"`, creates a tagged `k8s` pipeline job,
waits for a k3s runner pod to execute it, checks trace markers, and verifies
artifact metadata. The in-cluster validation deploys a second official runner
manager as a Kubernetes Deployment, then runs a tagged `k8s-incluster` job.

For a faster CI Lab plus official-runner smoke check, use:

```bash
make vm-ci-lab-smoke
```

This creates or reuses the `ci-lab-smoke` project, writes a small
`.gitlab-ci.yml`, creates a pipeline, waits for the official runner to execute
the job, checks trace markers and artifact metadata, and prints the admin CI Lab
URL for inspection. The target refreshes runner CA trust only if the runner
cannot verify `https://glemu.local`.

For recovery commands and operational triage, see
`docs/runbooks/operations.md`.

The runner registration helper defaults to `https://glemu.local`, uses the
emulator validation registration token by default, and registers the runner with
S3 cache settings that point at the server-side MinIO service. Override
`RUNNER_TOKEN` when validating a non-default registration token.

## Official Runner Checks

Keep validating against the official runner instead of a hand-rolled executor:

- registration succeeds
- no-job polling returns expected no-content responses
- persisted jobs are assigned
- pipeline, YAML, and job-level variables reach official runner jobs with the
  expected precedence, including raw and file-variable metadata
- `rules` job selection creates only matching runnable jobs, persists manual
  jobs without handing them to the runner, and runs selected jobs
- `extends`, `default:`, and `inherit:` behavior reaches official runner jobs
- nested local, project, remote, and template CI includes resolve into official
  runner jobs
- source checkout works for private projects through CI job token auth
- trace streaming persists output
- job status updates move persisted jobs and pipelines forward
- artifact upload and download work
- `needs:artifacts` causes dependency artifact downloads, including multiple
  upstream artifacts in declared needs order
- S3 cache upload and restore work through MinIO

## Client Checks

Run `glab` validation from the isolated client VM, not the host:

```bash
make vm-test
```

`vm-test` syncs `scripts/`, installs a pinned `glab` release inside the client
VM as `/srv/bin/glab`, and installs the emulator Caddy root CA into the client
VM trust store. The default CLI release is controlled by `GLAB_VERSION` and
`GLAB_SHA256` in the Makefile, so validation does not require or modify a host
`glab` installation.

The script writes `glab` config under a temporary `$HOME` inside the client VM,
creates a temporary emulator token, and cleans up the test project on exit. It
does not install or configure `glab` on the host.

Current client smoke coverage:

- `glab auth status`
- `glab api user`
- `glab api` users search
- `glab api` project get/list surfaces
- `glab api` repository files, commits, branches, tags, merge requests,
  pipelines, and jobs
- high-level `glab repo create`, `glab repo view --output json`,
  `glab repo list --output json`, `glab repo clone`, and
  `glab repo delete`
- high-level `glab issue create`, `glab issue list --output json`,
  `glab issue view --output json`, `glab issue update`, `glab issue close`,
  and `glab issue reopen`
- high-level `glab mr create`, `glab mr list --output json`,
  `glab mr view --output json`, `glab mr update`, and `glab mr merge`
- high-level `glab ci run`, `glab ci list --output json`,
  `glab pipeline list --output json`, `glab ci status --output json`,
  `glab ci get --output json --with-job-details`, `glab ci trace`,
  `glab ci trigger`, `glab ci cancel pipeline`, `glab ci cancel job`,
  `glab ci retry`, and `glab job artifact --list-paths`
- high-level `glab release create`, `glab release upload --assets-links`,
  `glab release upload --use-package-registry`, `glab release view`, and
  `glab release delete`
- high-level `glab snippet create` against project snippets, with API
  visibility validation
- `glab api` release asset link create/list/update/delete coverage
- `git clone`, `git fetch`, and `git push` over Git Smart HTTP

Long-tail `glab` subcommands such as interactive views remain follow-up
validation targets. The smoke keeps each high-level command tied to concrete
GitLab REST compatibility gaps.

## Known Operational Risks

- Docker Hub unauthenticated pull rate limits can break VM validation when the
  runner or server needs public images. Work around this with cached images,
  authenticated Docker config, or locally mirrored images.
- TLS trust must be installed on the runner VM before the official runner can
  communicate with `https://glemu.local`.
- TLS trust must also be installed on the `k8s-runner` VM. The Kubernetes
  executor additionally needs pod-level name resolution for `glemu.local`,
  handled by the runner `host_aliases` configuration in the current validation
  script.
- `make vm-deploy` preserves Caddy's local CA. If the server VM is reset with
  `make vm-deploy-reset` or `make vm-reset`, refresh the runner trust with
  `make vm-runner-install-ca` and `make vm-k8s-runner-install-ca`; stale CA
  trust appears as pending jobs with runner logs reporting certificate
  verification failures.
- The runner VM must have Docker available and enough privileges for the Docker
  executor.
- Stuck pending jobs should be diagnosed through CI Lab runner diagnostics or
  `GET /api/v4/projects/:id/pipelines/:pipeline_id/diagnostics`; common causes
  are runner TLS, runner tags, `run_untagged`, earlier stages, or `needs`.
- Stale running jobs are recovered manually through CI Lab `Requeue`, while
  GitLab-shaped clients should use cancel followed by retry.
- If a sandboxed pytest run is force-aborted while async SQLite fixtures are
  active, subsequent sandboxed `aiosqlite.connect()` calls can hang during
  fixture setup. Treat that as an execution-environment issue, not an emulator
  compatibility failure; rerun validation in a fresh sandbox or outside the
  sandbox with the same `make test-*` command.

## Done Criteria

- every major slice lists its local test command and VM validation command
- runner validation docs are updated whenever the coordinator behavior changes
- failures distinguish emulator compatibility gaps from environmental issues
