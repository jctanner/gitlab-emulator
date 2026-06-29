# GitLab CI and Runner Plan

## Goal

Use the official GitLab Runner as the job execution engine while the emulator
implements enough GitLab coordinator, project, pipeline, job, artifact, trace,
and cache APIs for controlled integration testing.

## Architecture

- The emulator runs on the server VM or in a k3s-backed environment.
- The official GitLab Runner runs on a separate runner VM.
- The runner VM talks to the emulator over normal GitLab coordinator APIs.
- The runner uses the Docker executor for sandboxed job containers.
- Job containers fetch source through Git Smart HTTP from the emulator.
- Runner cache validation uses GitLab Runner's S3 distributed cache adapter
  backed by MinIO.

## Implemented Minimal Surface

- runner registration, verify, unregister
- no-job polling
- persisted job assignment
- trace append
- job status update
- artifact upload, persistence, metadata listing, and download
- project repository checkout with CI job token
- persisted pipelines and jobs
- direct job creation for validation
- minimal `.gitlab-ci.yml` parsing
- stage gating
- minimal `needs`
- optional missing `needs`
- missing required `needs` validation
- duplicate, self, and future-stage same-pipeline needs validation
- `needs:project` and `needs:pipeline:job` artifact dependency lookup against
  successful stored jobs
- `needs:artifacts`
- `needs:artifacts` dependency payloads follow declared needs order
- job `dependencies` artifact selection, including `dependencies: []` and
  default prior-stage artifact dependency payloads
- common `rules`, `only`, and `except` filters
- `rules:if` with variable truthiness, equality, inequality, regex, and simple
  `&&`/`||`
- `rules:exists`, commit-local `rules:changes`, `when: never`, and persisted
  non-runnable `manual` jobs
- compound `timeout` and delayed `start_in` duration values such as
  `1 hour 30 minutes` and `1h 15m`
- local `include` entries resolved from the same repository ref
- nested local includes and `include:project` entries resolved with depth and
  cycle guards
- controlled `include:remote` entries resolved from allowlisted HTTP(S) hosts
- built-in GitLab-style template includes for local testing
- local `extends` with multi-parent merge, `default:` inheritance,
  including runtime defaults for `retry`, `timeout`, and `interruptible`,
  `inherit: default`, `inherit: variables`, and depth/error guards
- pipeline-level variables, top-level YAML variables, and job-level YAML
  variables merged into official runner job payloads with documented precedence
  and metadata for raw, masked/public, and file variables
- project trigger tokens and trigger-created `source=trigger` pipelines
- project pipeline schedules and manually played `source=schedule` pipelines
- job `rules:if` can match `CI_PIPELINE_SOURCE` for API, trigger, and
  schedule-created pipelines
- top-level `workflow:rules` can allow or skip pipeline creation with the same
  MVP rule evaluator used by jobs
- merge request event pipelines can be created through
  `POST /projects/:id/merge_requests/:iid/pipelines`; they use
  `source=merge_request_event`, evaluate MR-specific rules, and expose common
  `CI_MERGE_REQUEST_*` variables to runner payloads
- runner tag matching and `run_untagged`
- cache metadata and archive upload/download endpoints
- cache key prefix/files parsing and emulator cache fallback-key lookup
- MinIO-backed official runner cache upload/restore validation
- structured global/default/job `image:` metadata for `entrypoint` and
  `pull_policy` in official-runner-shaped payloads
- common CI service container definitions are parsed, persisted, exposed in
  job API payloads, and sent to official-runner-shaped job payloads for string
  entries and mapping entries with `name`, `alias`, `command`, `entrypoint`,
  `pull_policy`, and variables
- persisted jobs are the only runner coordinator source; the debug in-memory
  smoke queue has been removed
- stale running jobs are exposed in pipeline/CI Lab diagnostics; recovery is
  manual through the CI Lab requeue action or, for GitLab-shaped clients,
  cancel followed by retry
- bridge `trigger` jobs create same-emulator downstream
  `source=parent_pipeline` pipelines and expose the downstream pipeline ID on
  the bridge job; integer `parallel` and `parallel:matrix` jobs expand into
  per-node persisted jobs, and same-pipeline `needs:parallel:matrix` expands
  to the selected matrix job names

## Remaining CI Work

### YAML Semantics

- current event support covers API, push, trigger, schedule, and merge request
  event pipelines; the MVP now creates `source=push` pipelines after successful
  Git Smart HTTP and SSH branch pushes when `.gitlab-ci.yml` is present and
  workflow rules include the pushed branch, persists the pushed branch's
  previous SHA as pipeline `before_sha`, sends that value in runner
  `git_info.before_sha`, and merge request creation or branch-target updates
  opportunistically create `source=merge_request_event` pipelines when the MR
  head `.gitlab-ci.yml` allows the event
- deeper pipeline event behavior beyond those MVP paths remains future work

### Runner Coordinator

- persist and expose richer long-tail job transitions where target workflows
  require them
- expose additional runner/job/pipeline inspection APIs in GitLab-compatible
  shapes where target clients require them; runner job inspection now returns
  GitLab job-shaped fields including pipeline, commit, runner, artifacts,
  timestamps, duration, queued duration, tags, status, and web URL
- keep compatibility with official runner trace, status, artifact, and cache
  behavior

## Done Criteria

- local tests cover parser output, pipeline creation, scheduling, runner
  polling, trace append, status update, artifact download, and cache paths
- official runner VM executes persisted direct jobs and YAML-defined pipelines
- official runner validates source checkout, artifacts, `needs:artifacts`, and
  S3 cache upload/restore
- runner testing docs describe the supported flow and known limits
