# GitLab Runner Testing Notes

This records the first validation pass for using an official GitLab Runner VM
against the GitLab emulator.

## Goal

Validate that the emulator can act as enough of the GitLab Runner coordinator
API for a real `gitlab-runner` process to:

1. Reach the emulator over the Vagrant private network.
2. Trust the emulator TLS endpoint.
3. Register with the emulator.
4. Store the emulator-issued runner token.
5. Poll the job coordinator endpoint without authentication failures.
6. Execute smoke, persisted direct-job, and minimal YAML-defined pipeline jobs.

## Environment

The Vagrant setup uses four VMs on `glemu124_net`:

| VM | IP | Purpose |
|---|---:|---|
| `server` | `192.168.124.10` | Runs the emulator container behind Caddy TLS |
| `client` | `192.168.124.11` | Runs CLI smoke tests |
| `runner` | `192.168.124.12` | Runs official `gitlab-runner` and Docker |
| `k8s-runner` | `192.168.124.13` | Runs official `gitlab-runner` with Kubernetes executor and local k3s |

The runner VM has:

- Debian Bookworm
- Docker CE
- official `gitlab-runner` package
- GitLab Runner version validated: `19.0.1`
- `/etc/hosts` entry mapping `glemu.local` to `192.168.124.10`

The emulator is reached by the runner at:

```text
https://glemu.local
```

For current recovery commands covering TLS, token mismatch, image pulls, stuck
pending jobs, and stale running jobs, see `docs/operations-runbook.md`.

## Emulator API Surface Used

The current minimal runner coordinator API is implemented in
`app/api/runner.py` and mounted under `/api/v4`.

Supported for this smoke test:

- `POST /api/v4/runners`
- `POST /api/v4/runners/verify`
- `DELETE /api/v4/runners`
- `POST /api/v4/jobs/request`
- `PATCH /api/v4/jobs/:job_id/trace`
- `PUT /api/v4/jobs/:job_id`
- `POST /api/v4/jobs/:job_id/artifacts`
- `GET /api/v4/jobs/:job_id/artifacts`
- `PUT /api/v4/projects/:project_id/cache/:cache_key`
- `GET /api/v4/projects/:project_id/cache/:cache_key`
- `HEAD /api/v4/projects/:project_id/cache/:cache_key`

Current static tokens:

| Token | Purpose |
|---|---|
| `runner-registration-token` | Registration token accepted by the emulator |
| `glrt-emulator-runner-token` | Backward-compatible first runner authentication token returned by the emulator |

When no persisted pipeline jobs are eligible, `POST /api/v4/jobs/request`
returns `204 No Content`. Jobs are created through project pipeline APIs, either
from direct validation job input or from committed `.gitlab-ci.yml`.

Each additional registration with `runner-registration-token` now receives a
distinct persisted `glrt-...` runner token so the Docker executor runner and
Kubernetes executor runner can coexist.

## Commands That Worked

Deploy or refresh the emulator on the server VM:

```bash
cd gitlab_emulator
make vm-deploy
```

Install the emulator's Caddy local root CA into the runner VM:

```bash
make vm-runner-install-ca
```

Register the runner:

```bash
make vm-runner-register RUNNER_TOKEN=runner-registration-token
```

Check runner state:

```bash
make vm-runner-status
```

Inspect distributed cache configuration:

```bash
make vm-runner-cache-config
```

Run the official runner cache validation:

```bash
make vm-runner-cache-test
```

Run the official runner `needs:artifacts` validation:

```bash
make vm-runner-artifact-needs-test
```

Run the official runner Kubernetes executor validation:

```bash
make vm-k8s-runner-up
make vm-k8s-runner-validate
make vm-k8s-incluster-validate
```

The validated Kubernetes path uses GitLab Runner `19.1.1`, k3s
`v1.35.5+k3s1`, and a tagged `k8s_probe` job. The job creates a runner pod in
the `gitlab-runner` namespace, streams trace output back to the emulator,
finishes successfully, and uploads artifact metadata.

The in-cluster path deploys `gitlab/gitlab-runner:v19.1.1` as
`glemu-k8s-incluster-runner` in namespace `gitlab-runner-incluster`, then runs
a tagged `k8s-incluster` job. The validation shows both the manager pod and a
separate CI job pod in that namespace.

Create a persisted direct job for the runner:

```bash
curl -sk -X POST https://glemu.local/api/v4/projects/1/pipeline \
  -H 'Content-Type: application/json' \
  -d '{"ref":"main","job":{"name":"smoke","image":"alpine:3.20","script":["echo hello from gitlab emulator"]}}'
```

Inspect runner logs:

```bash
vagrant ssh runner -c "sudo journalctl -u gitlab-runner -n 100 --no-pager"
```

Inspect emulator logs:

```bash
vagrant ssh server -c "cd /srv/gitlab_emulator && docker compose logs --tail=100 gitlab-emulator"
```

Inspect MinIO cache service logs:

```bash
vagrant ssh server -c "cd /srv/gitlab_emulator && docker compose logs --tail=100 minio minio-init"
```

Recover a stale running job:

```text
Open /admin/ci-lab, select the project/pipeline/job, inspect the diagnostics,
then use Requeue for pending or running jobs.
```

The emulator does not automatically time out running jobs. CI Lab requeue is
the operator recovery path for a runner that died after assignment; it clears
runner-facing trace offsets and issues a new job token so the official runner
can safely pick up the same job record again. GitLab-shaped clients should use
cancel followed by retry for a running job.

## What Worked

Network path:

- Runner VM can reach `glemu.local`.
- `glemu.local` resolves to `192.168.124.10` inside the runner VM.
- HTTPS traffic from the runner reaches the emulator through Caddy.

TLS:

- The runner can verify `https://glemu.local` after installing Caddy's local
  root CA from the emulator container.
- GitLab Runner registration works without disabling TLS verification.

Registration:

- `make vm-runner-register RUNNER_TOKEN=runner-registration-token` succeeds.
- The runner calls `POST /api/v4/runners`.
- The emulator returns `201 Created`.
- The runner stores the emulator-issued token:

```text
glrt-emulator-runner-token
```

Polling:

- The runner repeatedly calls `POST /api/v4/jobs/request`.
- The emulator returns `204 No Content`.
- The runner does not report forbidden authentication errors after the correct
  token exchange path is used.

Persisted job coordinator loop:

- The emulator creates persisted `Pipeline` and `PipelineJob` records through
  `POST /api/v4/projects/:id/pipeline`.
- The next runner poll receives a `201 Created` job payload for an eligible
  persisted job.
- The runner can append logs through `PATCH /api/v4/jobs/:job_id/trace`.
- The runner can report state through `PUT /api/v4/jobs/:job_id`.
- Artifact uploads are accepted through
  `POST /api/v4/jobs/:job_id/artifacts`.
- Official GitLab Runner 19.0.1 successfully executed an Alpine Docker job:
  `echo hello from gitlab emulator`.
- The stored trace includes image pull, skipped Git checkout via
  `GIT_STRATEGY=none`, script execution, cleanup, and `Job succeeded`.
- Official GitLab Runner 19.0.1 also executed a persisted pipeline job created
  through `POST /api/v4/projects/:id/pipeline`; project pipeline/job/trace APIs
  returned `success` and the uploaded trace.
- Official GitLab Runner 19.0.1 executed a two-job pipeline created from a
  committed `.gitlab-ci.yml`. The emulator parsed the file, created `compile`
  and `unit` jobs, exposed them to the runner in stage order, stored both
  traces, and derived the pipeline status as `success`.
- Official GitLab Runner 19.0.1 executed a private-project checkout job with
  `GIT_STRATEGY=fetch`. The runner fetched from Git Smart HTTP using the CI job
  token, checked out the pipeline SHA, and ran scripts against committed files.
- Official GitLab Runner 19.0.1 executed a YAML-defined artifact job. The
  runner collected `out/result.txt`, uploaded a zip archive to the emulator,
  and the archive was downloaded back through
  `GET /api/v4/projects/:id/jobs/:job_id/artifacts`.
- The deployed coordinator was validated with manual runner polling for
  stage-gated scheduling: a `test` stage job returned `204 No Content` while
  the `build` stage job was running, then became assignable after the build job
  reported `success`.
- Local API tests now validate minimal `needs` scheduling and common ref
  filtering: a `test` job with `needs: [compile_a]` can become assignable after
  `compile_a` succeeds while another `build` job is still running, and
  `rules`/`only`/`except` filters are applied at pipeline creation.
- Local API tests now validate runner tag matching: tagged jobs wait for a
  runner whose tag list covers the job tags, and untagged jobs honor the
  runner's `run_untagged` setting.
- Local API tests now validate cache metadata and cache archive endpoints:
  runner job payloads include GitLab Runner-shaped `cache` entries, and cache
  archives can be uploaded, inspected with `HEAD`, and downloaded.
- The server VM compose stack now runs MinIO as an S3-compatible distributed
  cache backend on `glemu.local:9000`, with an initialized
  `gitlab-runner-cache` bucket.
- The runner VM registration helper now defaults to GitLab Runner's S3 cache
  adapter:

```toml
[runners.cache]
  Type = "s3"
  Path = "gitlab-runner"
  Shared = true
  [runners.cache.s3]
    ServerAddress = "glemu.local:9000"
    BucketName = "gitlab-runner-cache"
    Insecure = true
    PathStyle = true
```
- Official GitLab Runner 19.0.1 executed a two-job cache validation pipeline
  against MinIO/S3. The `cache_write` job created a cache archive, and the
  later-stage `cache_read` job restored it and read `cache-dir/value.txt`.
- Official GitLab Runner 19.0.1 executed a two-job `needs:artifacts`
  validation pipeline. The build job uploaded `out/result.txt`, and the
  downstream job downloaded it through the runner dependency artifact path before
  running its script.

YAML-defined pipeline validation from June 10, 2026:

```text
pipeline: {"id":1,"status":"success","ref":"main","sha":"dd02a175b1aed3f28d2015ad98140d291eaadd3a"}
job 1: compile / build / success
job 2: unit / test / success
trace 1 includes: echo before, echo yaml build, Job succeeded
trace 2 includes: echo before, echo yaml test, Job succeeded
```

Private checkout validation from June 10, 2026:

```text
pipeline: {"id":1,"status":"success","ref":"main","sha":"7b4224fe6d71226998e3a6a7340e58015fb925ba"}
job 1: checkout_probe / test / success
trace includes: Fetching changes, Checking out 7b4224fe as detached HEAD, test -f README.md, checkout ok, Job succeeded
```

Artifact validation from June 10, 2026:

```text
pipeline: {"id":1,"status":"success","ref":"main","sha":"4e50f1851794d886b23e486d8eea9ad3b9656969"}
job 1: artifact_probe / test / success
artifact metadata: {"filename":"job-1-artifacts.zip","size":471}
downloaded archive contains: out/result.txt
out/result.txt contents: artifact ok
trace includes: Uploading artifacts as "archive" to coordinator... 201 Created
```

Stage scheduling validation from June 10, 2026:

```text
pipeline: {"id":1,"status":"pending","ref":"main","sha":"b4d944ed92d5a7c9d5790a341ebe182172c734ac"}
first request: compile / build
second request before build success: 204 No Content
second request after build success: unit / test
jobs: compile success, unit running
```

Runner cache validation from June 10, 2026:

```text
pipeline: 1
job 1: cache_write / build / success
job 2: cache_read / test / success
checks: 9 passed
write trace includes: Creating cache
read trace includes: Successfully extracted cache
read trace includes: cache-hit
```

Runner `needs:artifacts` validation from June 11, 2026:

```text
pipeline: 4
job 7: build_artifact / build / success
job 8: consume_artifact / test / success
checks: 8 passed
consume trace includes: Downloading artifacts for build_artifact
consume trace includes: from-build
```

Observed emulator log lines:

```text
POST /api/v4/runners HTTP/1.1" 201 Created
POST /api/v4/jobs/request HTTP/1.1" 204 No Content
```

Local test suite:

```text
242 passed, 4 warnings
```

## What Did Not Work

Direct `:8000` URL:

- Initial registration helpers pointed at `http://glemu.local:8000`.
- That is not the right external endpoint for this VM setup.
- Uvicorn listens on `127.0.0.1:8000` inside the emulator container.
- Caddy is the external endpoint on ports `80` and `443`.
- Caddy redirects plain HTTP to HTTPS.

Correct endpoint:

```text
https://glemu.local
```

TLS without CA installation:

- GitLab Runner 19.0.1 does not expose a simple `--tls-skip-verify` flag in
  `gitlab-runner register --help`.
- The clean validation path is to install the emulator's Caddy root CA into the
  runner VM and/or pass `--tls-ca-file`.

Using `--token` with the registration token:

- The first successful-looking registration used `--token`.
- In GitLab Runner 19.0.1, `--token` is treated as a runner authentication token.
- That caused the runner to store `runner-registration-token` directly.
- The runner then polled `POST /api/v4/jobs/request` with the wrong token.
- The emulator correctly returned `403 Forbidden`.

Correct flag for this validation:

```text
--registration-token runner-registration-token
```

That path is deprecated by GitLab Runner, but it performs the registration-token
exchange we need for the current emulator API. It made the runner store
`glrt-emulator-runner-token`.

Wrong `inputs` payload shape:

- The first smoke job payload sent `"inputs": {}`.
- GitLab Runner 19.0.1 rejected it with:

```text
json: cannot unmarshal object into Go struct field Job.inputs of type []spec.JobInput
```

- The correct empty value is:

```json
"inputs": []
```

After that fix, the runner decoded and executed the job.

GitLab Runner distributed cache cannot use the emulator's custom HTTP cache
endpoint:

- The runner source and configuration docs expose built-in distributed cache
  adapters for S3, GCS, and Azure.
- The existing emulator cache endpoint remains useful for API-level coverage and
  direct compatibility tests.
- A real official-runner cache validation should use the MinIO S3-compatible
  service added to the VM compose stack.

Docker executor could not resolve `glemu.local`:

- The runner VM had `/etc/hosts`, but helper/job containers created by the
  Docker executor did not inherit that mapping.
- The first checkout attempt failed during `get_sources` with:

```text
Could not resolve host: glemu.local
```

- The fix was to configure the runner Docker executor with:

```toml
extra_hosts = ["glemu.local:192.168.124.10"]
```

- `make vm-runner-install-ca` now also ensures that setting exists in
  `/etc/gitlab-runner/config.toml`.

Private Git fetch returned 404 before credentials were retried:

- Git did not send the embedded credentials before the first private
  `git-upload-pack` refs request.
- Returning `404 Not Found` made Git stop without retrying.
- The fix was to return `401` with `WWW-Authenticate: Basic ...` for
  unauthenticated private fetches, while still accepting the CI job token for
  read-only fetch after Git retries with credentials.

## Current Limits

The runner is registered, polling, and the coordinator can hand out in-memory
smoke jobs plus persisted jobs created directly or from a minimal
`.gitlab-ci.yml`. Persisted jobs can now fetch and check out their project
repository through Git Smart HTTP using their CI job token. YAML-defined jobs
can upload artifact archives and project job APIs can download them. Persisted
jobs are gated by stage order, with same-stage jobs remaining eligible for
parallel runners. Minimal `needs`, optional missing needs, missing required
needs validation, `needs:artifacts`, common ref filters, rule-level variables,
`exists`/`changes` path-object rule parsing, mapping-form `only`/`except`, and
`allow_failure` scheduling/status behavior are covered by local tests. Runner
tag matching is also covered by local API tests. The smoke queue is
intentionally temporary. Cache metadata, variable-expanded cache keys/policies/
fallback keys, and archive endpoints are covered by local API tests. VM runner
cache adapter configuration points at MinIO/S3 by default, and the official
runner has validated cache upload/restore plus dependency artifact download
across two-stage pipelines.

Missing behavior for fuller GitLab CI execution:

- broader `.gitlab-ci.yml` support such as richer `needs` edge cases and
  remaining long-tail `rules`, `extends`, and `include` semantics
- remaining richer cache options and edge cases beyond the current MinIO/S3
  validation path
- pipeline/job UI or richer API state transitions

## Implications

The VM split is viable:

- emulator runs on the server VM or k3s-backed environment
- official runner runs on its own VM
- runner talks to the emulator over normal GitLab coordinator APIs
- Docker executor can be used on the runner VM for sandboxed job containers

The next meaningful validation slice should expand `needs`/`rules` toward full
GitLab semantics or add richer pipeline/job UI and API state transitions.
