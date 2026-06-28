# GitLab Emulator Plan

## Purpose

Build a GitLab-compatible emulator for integration testing tools that need
GitLab REST APIs, Git Smart HTTP, and real GitLab CI job execution without
depending on a live GitLab instance.

This project intentionally keeps the underlying architecture of
`github_emulator`: FastAPI, SQLAlchemy, SQLite, Git Smart HTTP via the `git`
binary, server-rendered admin/web UI, Docker Compose, and Vagrant validation
VMs. The GitLab emulator should diverge where GitLab's API, data model, runner
coordinator, CI semantics, and CLI behavior differ from GitHub.

## Canonical Supporting Docs

- `docs/plans/compatibility.md`: GitLab REST/API compatibility work.
- `docs/plans/ci-runner.md`: GitLab CI, runner coordinator, and job execution
  work.
- `docs/plans/validation.md`: local, VM, official runner, and client validation
  strategy.
- `docs/runbooks/operations.md`: VM deploy, smoke, and recovery checklist for
  runner/TLS/token/image/pending/stale job issues.
- `docs/plans/ci-pipeline-implementation.md`: detailed CI implementation slices
  and current CI status.
- `docs/notes/runner-testing.md`: official GitLab Runner validation notes and known
  runner behavior.
- `docs/plans/kubernetes-runner-validation.md`: k3s-backed GitLab Runner
  Kubernetes executor validation plan and results.
- `docs/runbooks/runner-deployment.md`: supported runner deployment modes,
  registration flow, network/TLS requirements, and full k3s integration notes.
- `docs/plans/ci-variables-secrets-security.md`: CI/CD variables, GitLab
  Secrets Manager compatibility, log redaction, and pipeline security
  guardrail plan.
- `docs/tasks/done/mvp-slice-ledger.md`: completed MVP slice record and deferred work.
- `GITLAB_STATUS.md`: current status snapshot.

## Current State

- GitLab REST routes are mounted under `/api/v4`.
- Git Smart HTTP supports clone, fetch, and push against emulator repositories.
- The Vagrant environment has separate `server`, `client`, and `runner` VMs.
- The `runner` VM uses the official GitLab Runner with a Docker executor.
- The `k8s-runner` VM uses the official GitLab Runner with a Kubernetes
  executor backed by local k3s, both as a VM service and as an in-cluster
  manager Deployment.
- Runner registration, verification, unregister, no-job polling, job request,
  trace append, job status update, artifact upload, and artifact download have
  all been implemented at a minimal compatibility level.
- Persisted pipelines and jobs exist.
- Pipelines can be created directly or from a minimal `.gitlab-ci.yml`.
- The official runner VM has executed persisted jobs from the emulator.
- The Kubernetes executor runner VM has executed a tagged job from the emulator
  through a k3s job pod, with trace and artifact metadata round trips.
- The in-cluster Kubernetes executor runner manager has also executed a tagged
  job from inside k3s, with a separate CI job pod, trace, and artifacts.
- Runner jobs can fetch private project repositories through Git Smart HTTP
  using CI job tokens.
- Minimal CI support exists for stages, image, variables, before_script, script,
  after_script, artifacts, stage gating, needs, needs:artifacts, needs
  validation edge cases including clear rejection of unsupported
  `needs:parallel:matrix`, common ref filters, richer `rules` job selection with
  grouped boolean `if` expressions, unary negation, null/empty checks, and regex
  match/non-match operators including variable-backed regex patterns,
  `exists`/`changes` path-object rules,
  variable-expanded rule path patterns, branch/tag/source-aware
  `only`/`except` filters including glob-style refs and mapping forms, workflow/rule-level
  variables, boolean `allow_failure` scheduling/status behavior with clear
  rejection of unsupported `allow_failure:exit_codes`,
  `when: always` and `when: on_failure` cleanup scheduling, runner tags, cache
  metadata, delayed jobs with `when: delayed`/`start_in`, clear rejection of
  unknown `when` values, list and
  file-derived keys, variable-expanded paths, keys,
  policies, `when`, and fallback keys, deeper `extends` semantics, nested local
  `include`, `include:project`, list-valued controlled `include:remote`, and
  list-valued built-in template includes.
- Pipeline creation and runner job payloads merge pipeline-level variables,
  top-level YAML variables, and job-level YAML variables with MVP precedence.
  Variable metadata for raw, masked/public, and file variables is preserved in
  persisted runner payloads and validated through the official runner VM.
- GitLab-shaped commit status create/list routes exist for project commits and
  are included in client-VM `glab api` smoke validation.
- GitLab-shaped repository compare returns commit, commits, diffs, timeout, and
  same-ref fields for project IDs and encoded project paths.
- Project-scoped CI/CD variables can be created, listed, read, updated, and
  deleted through GitLab-shaped project variable APIs. Environment-scope
  filtering, key validation, and hidden write-only read behavior are covered by
  tests.
- Default-scope project CI/CD variables are resolved into persisted pipeline
  jobs and reach official-runner-shaped payloads with file, masked, raw, and
  public metadata preserved. Project variables are lower precedence than
  pipeline request variables and YAML/job variables in the current MVP merge
  model.
- Masked project CI/CD variable values are redacted in the runner trace append
  path before traces are persisted or returned through job trace APIs.
- Project CI/CD variable resolution honors protected refs and job environment
  scopes, including exact and wildcard environment-scope matches.
- Group CI/CD variable CRUD and admin-only instance CI/CD variable APIs exist,
  and runner payload resolution applies instance, parent group, child group,
  and project variable precedence.
- Project and group CI/CD secrets have emulator CRUD APIs backed by
  `ci_secrets`; secret values are write-only on API reads, and access-event
  storage exists for the later job delivery slice.
- Minimal pipeline trigger token APIs and pipeline schedule APIs exist. Trigger
  tokens can create `source=trigger` pipelines, and schedule `play` can create
  `source=schedule` pipelines using the same persisted job/runner path.
  Pipeline schedule CRUD, manual Play, next-run calculation, and automatic
  cron materialization of due schedules are implemented through the persisted
  job/runner path.
- Job scheduling is runner-poll based: persisted `pending` jobs become
  eligible according to stage order, `needs`, manual state, runner tags,
  runner pause/lock state, and `run_untagged`. Delayed/timer jobs are not
  modeled yet and are rejected clearly instead of being queued silently.
- GitLab-style pipeline/job cancel and retry REST endpoints exist. Retry
  requeues persisted jobs through the existing runner coordinator.
- GitLab-style manual job play exists for persisted manual jobs. Play moves
  manual jobs to `pending` so the existing runner coordinator can assign them.
- The admin UI includes a CI Lab for fast job experiments: project selection,
  `.gitlab-ci.yml` editing, pipeline creation, job status inspection, traces,
  runner diagnostics, pending-job eligibility reasons, and
  play/cancel/retry/requeue controls. This is an emulator operator tool, not a
  full GitLab UI clone.
- CI Lab also surfaces runner readiness, selected job URLs, trace refresh/API
  links, artifact download links, artifact metadata, and inline create errors.
- MinIO backs the runner VM's S3 distributed cache configuration, and official
  runner cache upload/restore has been validated.
- Minimal GitLab-shaped project APIs now exist for project creation, project
  lookup by numeric ID or URL-encoded path, user project listing, branch
  listing/get/create/delete, and tag listing/get/create/delete.
- Project creation supports user namespaces by default and group namespaces via
  `namespace_id` or `namespace_path`.
- Minimal GitLab-shaped group APIs exist for creating groups, getting groups by
  numeric ID or path, and listing group projects.
- Minimal GitLab-shaped namespace APIs exist for listing/searching user and
  group namespaces and getting namespaces by numeric ID or full path.
- Minimal GitLab repository files APIs exist for reading, creating, updating,
  and deleting files by numeric project ID or URL-encoded project path.
- Minimal GitLab repository commits APIs exist for listing commits, getting a
  commit, and reading commit diff metadata by numeric project ID or URL-encoded
  project path.
- Minimal GitLab merge request APIs exist for creating, listing, getting,
  updating, and merging merge requests by numeric project ID or URL-encoded
  project path.
- A bounded deeper resource compatibility pass is complete for groups,
  projects, repository files/tree, commits, branches, tags, protected branches,
  and merge requests. The pass expands common GitLab-shaped fields, filters,
  encoded/nested path behavior, repository file metadata, tree pagination,
  commit stats/diff metadata, and merge request diffs/merge validation.
- `/api/v4/version` exposes configurable GitLab-shaped server version metadata
  for CLI compatibility probes.

## Near-Term Slices

### 1. GitLab-Native Project API

Status: implemented for the MVP numeric-project-ID surface.

Implement and validate GitLab-shaped project resources instead of relying on
GitHub-shaped repository endpoints.

Target API surface:

- `POST /api/v4/projects`
- `GET /api/v4/projects/:id`
- `GET /api/v4/projects/:url_encoded_path_with_namespace`
- `GET /api/v4/projects/:id/repository/branches`
- `GET /api/v4/projects/:url_encoded_path_with_namespace/repository/branches`
- `GET /api/v4/projects/:id/repository/tags`
- `GET /api/v4/projects/:url_encoded_path_with_namespace/repository/tags`
- `GET /api/v4/users/:id/projects`

Done when:

- a project can be created through GitLab-shaped API input
- the response shape is close enough for raw HTTP clients and `glab`
- clone, push, and fetch work against a created project over HTTP
- tests cover API creation plus Git Smart HTTP against the created project

Implemented:

- `POST /api/v4/projects`
- `GET /api/v4/projects/:id`
- `GET /api/v4/projects/:url_encoded_path_with_namespace`
- `GET /api/v4/projects/:id/repository/branches`
- `GET /api/v4/projects/:url_encoded_path_with_namespace/repository/branches`
- `GET /api/v4/projects/:id/repository/tags`
- `GET /api/v4/projects/:url_encoded_path_with_namespace/repository/tags`
- `GET /api/v4/users/:id/projects`
- live `git clone`, push, and fetch validation through a project created by
  `POST /api/v4/projects`

Still needed:

- deeper `glab` compatibility checks

### 2. CI Semantics Hardening

Expand the current minimal `.gitlab-ci.yml` support toward common GitLab CI
behavior.

Target areas:

- deeper `rules` behavior beyond the current MVP expression grouping, exists,
  changes, exists/changes path-object, variable-expanded rule paths,
  mapping-form `only`/`except`, rule-variable, and remaining delayed/manual
  scheduling edge cases
- remaining richer cache policy/options beyond list and file-derived keys,
  variable-expanded paths, keys, policies, `when`, and fallback keys

Done when:

- common YAML patterns from real projects produce persisted pipeline/job records
- unsupported syntax fails clearly or is explicitly ignored
- local tests cover parser output and scheduler behavior
- official runner smoke tests still pass

### 3. Runner Coordinator Cleanup

Keep using the official GitLab Runner as the execution engine, and make the
emulator provide the minimum coordinator API surface it needs.

Target areas:

- remove the debug in-memory smoke queue entirely
- keep persisted jobs as the normal job source
- keep trace append and status transitions compatible with official runner
- persist enough job state for inspection and debugging
- persist runner registrations and diagnostics for admin/operator inspection
- expose pipeline/job state through GitLab-shaped APIs
- expose minimal runner and scheduler diagnostics for operator inspection
- keep pipeline scheduling explicit: CRUD, manual Play, next-run calculation,
  and automatic cron due-run execution are supported by the schedule worker
- surface stale running jobs in diagnostics and keep recovery explicit through
  CI Lab requeue or GitLab-compatible cancel plus retry

Done when:

- official runner executes persisted direct jobs and YAML pipeline jobs
- traces, artifacts, cache metadata, and statuses survive process restarts where
  persistence is expected
- runner registrations, contact timestamps, tags, and last assigned job survive
  process restarts where persistence is expected
- runner and pipeline diagnostics explain current scheduler state
- pipeline schedule CRUD/manual Play, automatic cron due-run behavior, and
  runner-side job eligibility are covered by tests
- stale running jobs have a documented operator recovery path without automatic
  timeout side effects
- debug-only runner paths have been removed

### 4. GitLab REST Compatibility

Replace remaining GitHub-shaped resources, names, headers, response payloads,
and tests with GitLab behavior.

Target resource areas:

- users
- groups: MVP create/list/get/list-projects, list-subgroups,
  list-descendant-groups, and nested namespace paths implemented
- projects: MVP create/list/get/delete/list-branches/list-tags/
  list-user-projects implemented
- issues: MVP project list/create/get/update implemented; response tests assert
  GitLab-shaped project issue payloads do not expose inherited GitHub issue
  fields
- merge requests: MVP create/list/get/update/merge/commits/changes implemented
- repository files: MVP get/raw/tree/create/update/delete implemented
- commits: MVP list/get/diff implemented
- branches: MVP list/get/create/delete implemented
- tags: MVP list/get/create/delete implemented
- members: MVP project/group list/get/add/delete implemented
- protected branches: MVP list/get/protect/unprotect implemented
- releases: MVP list/create/get/update/delete implemented
- webhooks: MVP project/group list/create/get/update/delete implemented
- labels: MVP project list/create/get/update/delete implemented with
  GitLab-shaped response fields, search, pagination, and issue counts
- milestones: MVP project list/create/get/update/delete implemented with
  GitLab-shaped response fields, filters, pagination, and encoded path lookup
- search: MVP global projects/issues/merge_requests/blobs implemented

Target behavior areas:

- GitLab-style pagination
- GitLab-style auth token handling
- GitLab-style error payloads
- GitLab-style response headers
- GitLab webhook payloads
- GitLab GraphQL schema details where needed

Done when:

- tests assert GitLab-shaped responses rather than GitHub compatibility,
  including regression coverage for GitLab project issue payloads
- `glab` smoke workflows pass for the implemented surface
- GitHub-specific naming remains only in shared historical scaffolding or has a
  clear compatibility reason; inherited `/pulls/:number/commits` and
  `/pulls/:number/files` compatibility endpoints now return real git-backed
  data rather than placeholder responses, and inherited repository compare
  compatibility returns real commit and changed-file data. Inherited contents
  delete now creates a real git commit and validates stale blob SHAs. Inherited
  event feeds include repository metadata, and received events now list public
  events on repositories owned by the target user.

### 5. Validation and Operations

Keep the VM split as the main realistic validation path:

- emulator on the `server` VM
- client tools on the `client` VM
- official GitLab Runner on the `runner` VM
- Docker executor job containers on the runner VM
- MinIO on the server side for S3-compatible runner cache validation

Done when:

- a single documented command path can deploy the emulator and register the
  runner: complete through `make vm-validate`
- runner cache and artifact-needs validations pass after a clean deploy:
  complete in the current `make vm-validate` run
- local tests are exposed through `make test-focused`, `make test-affected`,
  and `make test-full`, and VM tests are documented in
  `docs/plans/validation.md`: complete
- known operational issues, such as Docker Hub pull limits, have documented
  workarounds: complete
- VM deploy/reset behavior and runner recovery checklists are documented in
  `docs/runbooks/operations.md`: complete

## Deferred Work

- Full GitLab GraphQL parity. The current compatibility surface includes
  GitLab-shaped `currentUser`, `project(fullPath:)`, and repository
  `mergeRequests` aliases backed by the existing user/project/MR models.
  Repository `latestRelease` resolves real release metadata, and repository
  issue connections support `filterBy.mentioned` for issue bodies and comments,
  while pull request `reviewDecision` reflects active approvals and change
  requests, pull request diff stat fields resolve from git diffs, and pull
  request commit and changed-file connections list the actual commits and files
  between base and head. Repository `refs(refPrefix:)` distinguishes branch and
  tag refs, and repository language/topic and watcher connections resolve from
  stored project metadata and star data. Repository issue and pull request
  template fields, repository code of conduct metadata, and repository
  funding/contact links, and repository license metadata resolve from committed
  files. Repository assignable and mentionable user connections resolve from
  project owner/member data, and forked repository `parent` resolves from
  persisted fork metadata. Issue and pull request closing-reference connections
  resolve common same-repository closing keywords from merge request bodies.
  GraphQL search total counts report all matches independently of the returned
  node limit. The broader schema remains an incremental parity area.
- Full GitLab UI parity. The current UI covers repository/source editing,
  issues/work items, merge requests, branches, commits, tags, project settings,
  project members, labels, milestones, releases, webhooks, CI/CD variables,
  secrets, deploy keys, pipelines, jobs, pipeline schedules, artifacts,
  runners, and the admin CI Lab, but it is not a complete GitLab clone.
- Full GitLab authorization parity across all endpoints. The MVP CI
  variable/secret, pipeline-variable, repository write, Git object/ref write,
  GitLab repository file write gates, and protected-branch management gates now
  use GitLab-shaped access levels.
  Project member/collaborator writes require Maintainer or higher, while group
  member, org repository, organization-targeted fork, and team management writes require Owner. Subgroup
  creation requires Maintainer or higher on the parent group. Group namespace
  project creation requires Developer or higher. Project/repository
  destructive settings and org settings require Owner. Project/repository webhook and deploy-key writes
  require Maintainer or higher, repository Actions secret/variable management
  requires Maintainer or higher, and group webhook writes require Owner. Label
  and milestone definition writes require Maintainer or higher; issue label
  assignment, release writes, commit status writes, and check run/suite writes
  require Developer or higher. Issue, issue-comment, PR review-comment, PR review
  create/submit, and reaction writes require Reporter or higher; PR review
  dismissal requires Developer or higher;
  merge request create/update/merge requires Developer or higher. Project
  member access levels now preserve GitLab Guest separately from Reporter while
  keeping GitHub-compatible collaborator permissions for inherited routes.
  GitLab project, project list, project issue, project member, pipeline/job, and search API
  read access for private projects honors direct and group Reporter-or-higher access. Git Smart HTTP read access for
  private projects requires Reporter or higher, while push advertisement and
  receive-pack require Developer or higher. Implemented
  GraphQL issue/comment/reaction mutations use Reporter or higher, and GraphQL
  merge request mutations use Developer or higher. CI trigger token management
  requires Maintainer or higher; pipeline schedules and pipeline/job
  cancel/retry/play controls require Developer or higher. Direct API pipeline
  creation requires Developer or higher.
- Complete long-tail `glab` coverage beyond the smoke workflows.
- Full timer parity beyond the current worker model. Current support covers
  pipeline schedule CRUD, manual Play, automatic cron materialization through
  the schedule worker, delayed jobs with `when: delayed`/`start_in`, background
  promotion of due delayed jobs, and runner-side pending-job eligibility.
  Broader production scheduling concerns such as distributed leader election
  remain deferred.
- Production security hardening. Baseline browser security headers are enabled
  across API, admin, web, and error responses. Admin bootstrap user/token
  helper endpoints require an authenticated site admin. The emulator is still
  intended for controlled integration testing environments rather than open
  production exposure.

## High-Level Outcome

After the near-term slices, this project should provide a usable GitLab-like
test fixture:

- create GitLab-shaped projects through API calls
- push and fetch real Git repositories
- create pipelines from committed `.gitlab-ci.yml`
- execute selected jobs in sandboxed containers through the official GitLab
  Runner
- stream traces, statuses, artifacts, and cache interactions back into the
  emulator
- validate client tooling such as raw HTTP clients, `git`, `glab`, and CI
  automation against a local deterministic target
