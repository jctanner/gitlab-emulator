# CI Pipeline Implementation Plan

This plan turns the validated runner smoke path into persisted GitLab-style
pipeline execution.

## Current Baseline

Already working:

- official `gitlab-runner` registration and verification
- runner polling through `POST /api/v4/jobs/request`
- persisted single-job pipeline creation
- persisted pipeline creation from a minimal `.gitlab-ci.yml`
- persisted job assignment through the runner coordinator
- trace append through `PATCH /api/v4/jobs/:job_id/trace`
- status updates through `PUT /api/v4/jobs/:job_id`
- artifact upload acceptance through `POST /api/v4/jobs/:job_id/artifacts`
- official GitLab Runner 19.0.1 can execute an Alpine Docker job end to end
- official GitLab Runner 19.0.1 has executed a persisted pipeline job end to
  end and the project APIs returned `success` plus the stored trace
- official GitLab Runner 19.0.1 has executed a two-job YAML-defined pipeline
  with stage-ordered jobs and stored traces
- official GitLab Runner 19.0.1 has fetched and checked out a private project
  using the persisted job's CI job token
- official GitLab Runner 19.0.1 has uploaded an artifact archive for a
  YAML-defined job, and the emulator persisted and served it back through the
  project job API
- persisted jobs are stage-gated: later stages wait for earlier stages to
  succeed, same-stage jobs remain eligible for parallel runners, and later
  pending jobs are skipped after an earlier required stage fails
- minimal `needs` and common rules/ref filters are implemented: jobs can unlock
  from named dependencies, `needs: []` can run immediately, invalid needs are
  rejected early, `needs:artifacts` dependencies follow declared needs order,
  and pipeline creation applies `rules`, `only`, and `except` filters. Current
  `rules` support covers common `if` expressions, regex, simple boolean
  operators with grouped parentheses, `exists`, commit-local `changes`,
  `exists`/`changes` path-object forms, variable-expanded rule path patterns,
  `when: never`, and persisted non-runnable `manual` jobs. Current
  `only`/`except` support covers
  scalar/list refs plus mapping-form `refs`, `variables`, and `changes`.
- runner tag matching is implemented: jobs can carry `tags`, tagged jobs require
  matching runner tags, and untagged jobs honor the runner's `run_untagged`
  setting
- minimal cache support is implemented: jobs can carry GitLab Runner cache
  metadata, runner payloads include `cache` entries, and cache archives can be
  uploaded, inspected, and downloaded through project cache endpoints
- cache key list, prefix/files parsing, and emulator cache fallback-key lookup
  are implemented for API-level cache coverage; cache keys, cache policies, and
  fallback keys expand merged CI variables before reaching the runner payload;
  official runner validation remains on GitLab Runner's S3 cache adapter backed
  by MinIO
- artifact metadata and expiry are implemented: runner payloads preserve
  artifact name, exclude, untracked, when, and expire_in values; uploaded
  artifacts store type/format/expiry metadata and expired artifacts return 404
- CI `include` support is implemented for local files, nested local files, and
  `include:project`: pipeline creation resolves included files before parsing,
  guards include depth/cycles, and supports included hidden jobs used by
  `extends`
- CI `extends` support now covers local hidden-template inheritance,
  multi-parent reverse deep merge for common job keys, `default:` inheritance,
  `inherit: default`, `inherit: variables`, invalid shape errors, and an
  extends depth guard
- CI variables preserve runner-facing metadata for raw, masked/public, and file
  variables across pipeline-level, top-level YAML, and job-level YAML variables
- Pipeline trigger tokens and pipeline schedules are persisted. Trigger tokens
  can create `source=trigger` pipelines, and schedule `play` creates
  `source=schedule` pipelines using the same persisted runner job path.

Temporary parts replaced:

- the in-memory smoke job queue and `/api/v4/admin/runner/jobs` test enqueue
  endpoint have been removed

## Target Milestone

The first target milestone is complete: a pipeline can be created through the
GitLab REST API, the official runner executes its job, and success/trace are
observable through project APIs.

The second target milestone is also complete for a minimal YAML subset:
creating a pipeline from a project ref reads `.gitlab-ci.yml`, creates multiple
persisted jobs, orders them by stage, and the official runner executes them.

The third target milestone is complete for project checkout: persisted jobs
return authenticated clone metadata, Git Smart HTTP accepts matching CI job
tokens for read-only fetch, and the official runner can check out a private
project at the pipeline SHA.

The fourth target milestone is complete for minimal artifacts: `.gitlab-ci.yml`
`artifacts.paths` are parsed, the runner receives artifact collection
instructions, uploaded archives are persisted under `DATA_DIR/artifacts`, and
project job APIs can download the archive.

The fifth target milestone is complete for stage dependency scheduling:
persisted jobs carry a `stage_index`, the coordinator only assigns jobs whose
earlier stages have succeeded, same-stage jobs can be assigned before peers
finish, and later pending stages are skipped after an earlier required failure.

The next milestone is richer CI YAML semantics.

Minimum target flow:

1. Create or identify a project.
2. Create a pipeline with one job.
3. Official runner polls and receives that job.
4. Runner executes the job in Docker.
5. Runner uploads trace chunks.
6. Runner reports final status.
7. API clients can list the pipeline, list jobs, fetch one job, and fetch trace.

## Slice 1: Persisted CI Models

Status: implemented for the single-job pipeline MVP.

Add new SQLAlchemy models:

- `Pipeline`
  - `id`
  - `project_id`
  - `iid`
  - `ref`
  - `sha`
  - `status`
  - `source`
  - `created_at`
  - `updated_at`
  - `started_at`
  - `finished_at`
- `PipelineJob`
  - `id`
  - `pipeline_id`
  - `project_id`
  - `name`
  - `stage`
  - `status`
  - `image`
  - `script`
  - `variables`
  - `job_token_hash` or generated token field
  - `runner_name`
  - `queued_at`
  - `started_at`
  - `finished_at`
  - `failure_reason`
  - `exit_code`
  - `trace_checksum`
  - `trace_size`
- `JobTrace`
  - `job_id`
  - `content`
  - `size`
  - `updated_at`
- `JobArtifact`
  - `job_id`
  - `filename`
  - `content_type`
  - `size`
  - `storage_path`
  - `created_at`

Keep storage simple at first:

- trace can be stored as text/blob in SQLite
- artifact bytes can be stored under `DATA_DIR/artifacts`

## Slice 2: Project Pipeline APIs

Status: implemented for direct single-job pipeline creation and trace/status
inspection.

Add a new router, likely `app/api/pipelines.py`.

Implement:

- `POST /api/v4/projects/{project_id}/pipeline`
- `GET /api/v4/projects/{project_id}/pipelines`
- `GET /api/v4/projects/{project_id}/pipelines/{pipeline_id}`
- `GET /api/v4/projects/{project_id}/pipelines/{pipeline_id}/jobs`
- `GET /api/v4/projects/{project_id}/jobs`
- `GET /api/v4/projects/{project_id}/jobs/{job_id}`
- `GET /api/v4/projects/{project_id}/jobs/{job_id}/trace`

Initial `POST /pipeline` request body:

```json
{
  "ref": "main",
  "variables": [
    {"key": "EXAMPLE", "value": "1"}
  ]
}
```

For the first implementation, allow a test-only fallback when no
`.gitlab-ci.yml` exists:

```json
{
  "ref": "main",
  "job": {
    "name": "smoke",
    "image": "alpine:3.20",
    "script": ["echo hello from persisted pipeline"]
  }
}
```

This keeps the first persisted runner loop independent from YAML parsing.

## Slice 3: Runner Coordinator Uses Persisted Jobs

Status: implemented and validated with official GitLab Runner.

Replace the in-memory queue inside `app/api/runner.py`.

Behavior:

- `POST /api/v4/jobs/request`
  - finds the oldest `PipelineJob.status == "pending"`
  - marks it `running`
  - records runner metadata
  - returns a persisted-job payload for official GitLab Runner
- `PATCH /api/v4/jobs/:job_id/trace`
  - validates `JOB-TOKEN`
  - appends bytes to `JobTrace`
  - updates `PipelineJob.trace_size`
  - returns `202` with `Job-Status`, `Range`, and update interval headers
- `PUT /api/v4/jobs/:job_id`
  - validates job token
  - updates job status, failure reason, output metadata, exit code
  - updates pipeline derived status
- `POST /api/v4/jobs/:job_id/artifacts`
  - validates job token
  - stores uploaded archive metadata and bytes

The old smoke endpoint has been removed now that the persisted path passes VM
validation.

## Slice 4: Pipeline Status Derivation

Status: implemented for single-job and simple multi-job status aggregation.

Implement simple status transitions:

- pipeline starts as `pending`
- when a job is assigned, pipeline becomes `running`
- if all jobs are `success`, pipeline becomes `success`
- if any job is `failed`, pipeline becomes `failed`
- if pending jobs remain and no jobs are running, pipeline remains `pending`

Single-job pipelines are enough for this milestone.

## Slice 5: VM Integration Test

Status: implemented manually and validated.

Manual validation first:

```bash
cd gitlab_emulator
make vm-deploy
make vm-runner-install-ca
vagrant ssh runner -c "sudo systemctl restart gitlab-runner && sudo gitlab-runner verify"
```

Create a pipeline:

```bash
curl -sk -X POST https://glemu.local/api/v4/projects/1/pipeline \
  -H 'Content-Type: application/json' \
  -d '{"ref":"main","job":{"name":"smoke","image":"alpine:3.20","script":["echo hello from persisted pipeline"]}}'
```

Inspect completion:

```bash
curl -sk https://glemu.local/api/v4/projects/1/pipelines
curl -sk https://glemu.local/api/v4/projects/1/jobs
curl -sk https://glemu.local/api/v4/projects/1/jobs/1/trace
```

Expected result:

- runner logs show `Job succeeded`
- pipeline status becomes `success`
- job status becomes `success`
- trace includes `hello from persisted pipeline`

## Slice 6: `.gitlab-ci.yml` Parser

Status: implemented for a minimal subset and validated with official GitLab
Runner.

Implemented:

- read `.gitlab-ci.yml` at the pipeline ref
- parse YAML with a real YAML parser
- support minimal keys:
  - `stages`
  - job name
  - `stage`
  - `image`
  - `script`
  - `before_script`
  - `after_script`
  - `variables`
- create one `PipelineJob` per parsed job
- order jobs by stage order before exposing them to the runner coordinator

Still needed:

- remaining long-tail `rules` / `only` / `except` edge cases

## Slice 7: Repository Checkout

Status: implemented and validated.

Implemented:

- return clone URLs and job variables that let the official runner fetch the
  project repository at the pipeline SHA
- validate checkout against the VM runner without disabling TLS verification
- accept CI job tokens for read-only Git Smart HTTP fetches
- return `401` with a Basic auth challenge for unauthenticated private Git
  fetches so Git retries with embedded job-token credentials
- configure runner Docker executor containers with `glemu.local` host
  resolution and the emulator CA

## Slice 8: Artifact Persistence and Download

Status: implemented and validated with official GitLab Runner.

Implemented:

- write uploaded artifact archives to `DATA_DIR/artifacts`
- store artifact filename, content type, size, and storage path
- expose job artifact download endpoints for project/job API clients
- parse minimal `artifacts.paths` from `.gitlab-ci.yml` into runner job payloads
- validate with an official runner job that writes a file, uploads artifacts,
  and downloads the archive through the emulator API

## Slice 9: Stage Scheduling

Status: implemented and validated.

Implemented:

- only schedule jobs from the first runnable stage until that stage completes
- unlock later stages when all previous-stage jobs succeed
- fail or skip later-stage jobs when an earlier required stage fails
- allow same-stage jobs to be assigned before their same-stage peers finish

## Slice 10: Needs, Rules, Tags, and Cache

Status: implemented and validated for minimal `needs`, common ref filters,
runner tag matching, cache metadata/storage, and VM runner S3 cache through
MinIO.

Implemented:

- parse common `needs` forms from `.gitlab-ci.yml`
- persist job `needs` metadata
- let jobs with explicit `needs` unlock from dependency completion instead of
  pure stage gating
- let `needs: []` jobs run immediately
- support `needs: [{ job: ..., optional: true }]` for missing optional jobs
- reject pipelines with missing required `needs` references
- reject duplicate needs, self-needs, future-stage needs, and unsupported
  cross-project/pipeline needs
- support `needs: [{ job: ..., artifacts: true|false }]` artifact dependency
  payloads for official runner downloads
- preserve declared needs order in official runner dependency payloads
- apply common `rules`, `only`, and `except` filters during pipeline creation,
  including MVP `if`, `exists`, `changes`, `never`, and `manual` behavior
- support local `extends`, multi-parent template merge, `default:` inheritance,
  `inherit: default`, and `inherit: variables`
- resolve local, nested local, project, controlled remote, and built-in
  template includes before parsing
- merge pipeline-level, top-level YAML, and job-level YAML variables with
  runner-facing metadata for raw, masked/public, and file variables
- create `source=trigger` pipelines from project trigger tokens
- create `source=schedule` pipelines from manually played pipeline schedules
- parse and persist job `tags`
- match tagged jobs only to runners whose tag list covers the job tags
- honor `run_untagged` for untagged persisted jobs
- parse and persist job `cache` entries
- include GitLab Runner-shaped cache entries in persisted runner job payloads
- store and serve cache archives through project cache endpoints
- run a MinIO S3-compatible cache service in the server VM compose stack
- configure the runner VM registration helper for GitLab Runner's S3 cache
  adapter by default
- validate an official runner VM job pair that uploads cache through MinIO and
  restores it in a later stage
- validate an official runner VM pipeline that uploads multiple artifacts and
  downloads them through `needs:artifacts` in declared needs order

Still needed:

- support remaining richer cache options and edge cases beyond current list,
  prefix/files, fallback-key, policy, and MinIO/S3 validation coverage; GitLab
  Runner supports S3/GCS/Azure distributed cache adapters, not an arbitrary HTTP
  cache endpoint

## Done Criteria

The milestone is done when:

- local tests cover model creation, pipeline APIs, runner polling, trace append,
  status update, and trace fetch
- full test suite passes
- official runner VM executes persisted direct-job and YAML-defined pipeline
  jobs successfully, including real repository checkout and artifact upload/download
- runner testing docs are updated with the persisted pipeline flow
- the temporary in-memory queue is removed
