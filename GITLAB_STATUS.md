# GitLab Emulator Status

This project was scaffolded from `github_emulator` to keep the same underlying
FastAPI, SQLAlchemy, SQLite, Git Smart HTTP, GraphQL, admin UI, Docker, and test
harness architecture.

## Current State

- Project metadata, environment variable prefix, container service names, default
  database name, hostname, and public docs have been renamed for GitLab.
- REST routes are mounted under `/api/v4`, matching GitLab's API version path.
- Git Smart HTTP, SSH transport, persistence, auth, admin UI, web UI, and tests
  are copied from the GitHub emulator as a starting point.
- Minimal official GitLab Runner validation endpoints exist for registration,
  verification, unregister, no-job polling, persisted job assignment, trace
  append, job status update, artifact upload, artifact download, and cache
  upload/download.
- The runner VM has been validated with official `gitlab-runner` 19.0.1:
  registration against `https://glemu.local` succeeds, the emulator-issued
  runner token is stored, and `/api/v4/jobs/request` polling returns
  `204 No Content`.
- Minimal persisted pipeline/job models and project pipeline APIs exist. The
  official runner VM has executed a persisted pipeline job and the project APIs
  returned `success` with the stored trace.
- Minimal `.gitlab-ci.yml` parsing exists for `stages`, `image`, `variables`,
  `before_script`, `script`, and `after_script`. Creating a pipeline from a
  project ref now reads the repository CI file, creates one persisted job per
  parsed job, and orders jobs by stage for the runner coordinator.
- The official runner VM has executed a two-job YAML-defined pipeline through
  the Docker executor. The project pipeline reached `success` and both stored
  traces included the expected scripts.
- Persisted runner jobs now fetch the project repository with a CI job token
  instead of forcing `GIT_STRATEGY=none`. The official runner VM has checked out
  a private project at the pipeline SHA and executed a script against committed
  files.
- Minimal `artifacts.paths` parsing exists. The official runner VM has uploaded
  an artifact archive for a YAML-defined job, the emulator persisted it under
  `DATA_DIR/artifacts`, project job APIs returned artifact metadata, and the
  archive was downloaded back through the emulator API.
- Persisted jobs now use stage dependency gating. Later-stage jobs are not
  assigned until all earlier-stage jobs succeed, same-stage jobs remain eligible
  for parallel runners, and later pending jobs are skipped after an earlier
  required stage fails unless they are `when: always` cleanup jobs, which remain
  runnable before the pipeline finalizes. `when: on_failure` cleanup jobs wait
  for an earlier required failure and are skipped when previous dependencies
  finish successfully.
- Integer `parallel` and `parallel:matrix` jobs are expanded into per-node
  persisted jobs with runner variables for each node or matrix value.
- Minimal `needs` and rules/ref-filter support exists. Jobs can declare common
  `needs` forms to unlock from dependency completion instead of pure stage
  gating, `needs: []` can run immediately, optional missing needs do not block,
  missing required needs, duplicate needs, self-needs, future-stage needs, and
  unsupported cross-project/pipeline and `needs:parallel:matrix` needs are
  rejected, `needs:artifacts` feeds official runner dependency downloads in
  declared needs order, and pipeline creation applies `rules`, `only`, and
  `except` filters. Current
  `rules` support includes common `if` expressions, `&&`/`||`, grouped
  boolean expressions, unary negation, null and empty-string variable
  comparisons, regex match/non-match operators, `exists`, commit-local
  `changes`, `when: never`, `when: always`, `when: on_failure`, persisted
  non-runnable `manual` jobs, boolean `allow_failure`,
  `allow_failure:exit_codes` matching against runner-reported exit codes, and
  matched workflow-level variables. Unsupported delayed jobs are rejected
  clearly instead of running
  immediately. Legacy `only`/`except` filters are
  branch/tag/source-aware, and tag-ref pipelines expose `CI_COMMIT_TAG` while
  branch-ref pipelines expose `CI_COMMIT_BRANCH`.
- Manual jobs can be played through the GitLab job play endpoint. Playing a
  manual job changes it to `pending` and requeues it through the persisted
  runner coordinator.
- Pipeline-level variables, top-level YAML variables, and job-level YAML
  variables are merged into persisted runner job payloads with GitLab-style
  metadata for raw, masked/public, and file variables. Local tests validate the
  precedence and metadata payloads, and the official runner VM has validated the
  resulting values and file variables in a real job trace.
- Minimal pipeline triggers and schedules exist. Project trigger tokens can be
  created, listed, deleted, and used to create `source=trigger` pipelines.
  Project pipeline schedules can be created, listed, updated, deleted, and
  played manually to create `source=schedule` pipelines.
- GitLab-style pipeline/job cancel and retry endpoints exist. Pipeline cancel
  marks runnable jobs canceled, job retry clears prior runner state and trace
  content, `retry:exit_codes` filters automatic retries by runner-reported exit
  code, and pipeline retry requeues failed, canceled, and skipped jobs
  through the persisted runner coordinator.
- The admin CI Lab exposes runner diagnostics, pending-job eligibility reasons,
  and a requeue control for pending/running jobs so stuck jobs can be diagnosed
  and recovered from the operator UI.
- Runner registrations and diagnostics are persisted in `ci_runners`; runner
  registration, verify, unregister, and job polling update stored tags,
  `run_untagged`, contact timestamps, runner metadata, and last assigned job.
  CI Lab diagnostics now survive app restarts/deploys.
- Minimal runner/pipeline inspection APIs exist: runners can be listed,
  fetched, and queried for recent jobs, and pipeline diagnostics expose shared
  scheduler explanations for eligible, blocked, running, manual, and terminal
  jobs. CI Lab uses the same scheduler explanation helper as the API.
- Stale running jobs are surfaced through the shared scheduler diagnostics.
  Recovery is intentionally operator-driven: CI Lab `Requeue` resets
  pending/running jobs for another runner poll, clears runner-facing trace
  offsets, and issues a new job token, while GitLab-shaped clients use cancel
  plus retry.
- `make vm-ci-lab-smoke` provides a fast client-VM smoke path for the CI Lab:
  it creates or reuses a smoke project, writes CI YAML, creates a pipeline,
  waits for the official runner to execute the job, checks trace markers and
  artifact metadata, and prints the admin CI Lab URL.
- VM deploy now preserves Docker volumes by default. Destructive deploys are
  explicit through `make vm-deploy-reset`, which avoids routine data loss and
  unnecessary Caddy CA rotation during emulator iteration.
- `docs/runbooks/operations.md` documents the normal deploy path, fast CI Lab
  smoke, full VM validation, and recovery checklists for runner TLS failures,
  registration token mismatch, Docker image pull failures, stuck pending jobs,
  and stale running jobs.
- Minimal runner tag matching exists. YAML and direct jobs can carry tags,
  tagged jobs only assign to runners whose tags cover the job tags, and
  untagged jobs honor the runner's `run_untagged` setting.
- Minimal cache support exists. YAML and direct jobs can carry GitLab Runner
  cache metadata, runner job payloads include `cache` entries, and project cache
  archives can be uploaded, inspected, and downloaded through emulator API
  endpoints. The VM compose stack now includes MinIO for GitLab Runner's S3
  distributed cache adapter, and runner registration defaults to that cache
  backend. Official GitLab Runner 19.0.1 has validated cache upload and restore
  through MinIO across a two-stage pipeline, and dependency artifact download
  through `needs:artifacts` across a two-stage pipeline.
- Artifact metadata now preserves runner-facing `name`, `exclude`, `untracked`,
  `when`, and `expire_in` settings. Uploaded artifact records store file type,
  file format, size, creation time, and expiration time, and expired artifacts
  are no longer downloadable.
- Cache key parsing supports list-form keys, common `key: { prefix, files }`,
  and `files_commits` forms, cache paths/keys/policies/when/fallback keys expand
  variables before reaching runner payloads, and the emulator cache endpoints
  support fallback-key lookup for API-level cache coverage. Official runner
  cache validation still uses GitLab Runner's S3 adapter backed by MinIO.
- User responses now include GitLab-native fields such as `username`, `web_url`,
  `state`, `locked`, and `is_admin`, while retaining legacy compatibility
  fields. Public users can be looked up by numeric ID or username, and newly
  generated emulator PATs use a GitLab-style `glpat-` prefix. Existing token
  validation continues to support `PRIVATE-TOKEN`, bearer, token, and basic
  authentication headers.
- GitLab-shaped project issue APIs exist for list, create, get, and update by
  numeric project ID or URL-encoded project path. Responses expose GitLab-style
  `iid`, `project_id`, `description`, `author`, string labels, assignees,
  `state` as `opened`/`closed`, references, and `web_url`, while reusing the
  existing issue storage.
- GitLab-shaped project and group member APIs exist for list, get, add, and
  delete. Project members map GitLab access levels onto existing repository
  collaborator permissions, group members map access levels onto existing
  organization membership roles, both support numeric IDs plus path refs where
  applicable, and member lists now support `query`, exact pagination headers,
  and common GitLab member compatibility fields.
- GitLab-shaped project APIs exist for creating, listing, getting, and deleting
  projects, listing a user's projects by numeric user ID, listing project
  branches/tags from the bare repository, and getting/creating/deleting project
  branches and tags. Project get, delete, branch, and tag endpoints also accept
  URL-encoded `path_with_namespace` refs. Project creation supports user
  namespaces by default and organization-backed group namespaces via
  `namespace_id` or `namespace_path`.
- GitLab-shaped protected branch APIs exist for list, get, protect, and
  unprotect by numeric project ID or URL-encoded project path. Protection
  metadata reuses the existing branch protection storage and preserves common
  push, merge, unprotect, force-push, and code-owner approval settings for
  client compatibility; Git Smart HTTP enforcement is still deferred.
- GitLab-shaped project release APIs exist for list, create, get, update, and
  delete by numeric project ID or URL-encoded project path. Releases reuse the
  existing release storage, create a lightweight git tag from `ref` when needed,
  and expose GitLab-style assets/source archive metadata for `glab release`
  compatibility.
- GitLab-shaped project and group webhook APIs exist for list, create, get,
  update, and delete. Project hooks reuse repository webhook storage, group
  hooks reuse organization webhook storage, and common GitLab event booleans
  map to persisted event names. Actual outbound delivery remains limited to the
  existing delivery scaffolding.
- GitLab-shaped global search exists at `/api/v4/search` for `projects`,
  `issues`, `merge_requests`, and indexed code `blobs`/`code`. The endpoint
  returns GitLab-style arrays while the older GitHub-shaped `/search/*`
  endpoints remain available for scaffold compatibility.
- Main GitLab-facing list endpoints return exact GitLab pagination headers and
  query-preserving RFC 5988 `Link` headers. Current coverage includes projects,
  user projects, groups, group projects, project issues, merge requests,
  branches, tags, protected branches, repository tree, commits, releases,
  pipelines, jobs, search, project/group hooks, and project/group members;
  middleware still supplies baseline pagination header names for any remaining
  paginated `/api/v4` endpoints.
- GitLab-shaped group APIs exist for creating, listing, getting groups by
  numeric ID or path, and listing group projects.
- Nested group namespaces can be created with `parent_id`, are resolved by
  URL-encoded full path, and can own projects through `namespace_path` using the
  existing organization-backed namespace storage.
- GitLab repository files APIs exist for reading metadata, reading raw content,
  listing repository trees, creating, updating, and deleting files by numeric
  project ID or URL-encoded project path. File changes create real commits in
  the backing bare repository.
- Minimal GitLab repository commits APIs exist for listing commits, getting a
  commit, and reading commit diff metadata by numeric project ID or URL-encoded
  project path.
- GitLab merge request APIs exist for creating, listing, getting, updating,
  merging, listing commits, and reading changed files by numeric project ID or
  URL-encoded project path.
- The bounded deeper resource compatibility pass has expanded GitLab-shaped
  groups, projects, repository files/tree, commits, branches, tags, protected
  branches, and merge requests. Current coverage includes broader response
  fields, common list filters, encoded and nested path handling, repository
  file HEAD metadata, tree pagination, commit filters/stats/diff metadata, and
  richer merge request state/diff/merge validation. Focused local resource
  tests passed together, `make test-affected` passed, and client-VM `make
  vm-test` passed 90 checks including targeted `glab api` checks for the
  expanded resource surfaces.
- The shared real-git merge helper now sets deterministic author/committer
  identity so GitLab merge requests and inherited pull request merges work in
  the VM container even when global git config is absent.
- GitLab-facing project, group, merge request, project issue, and project
  member routes now import `Project`, `Group`, and `MergeRequest` model aliases
  instead of the legacy `Repository`, `Organization`, and `PullRequest` model
  names directly where a GitLab-facing alias exists. Underlying table names and
  compatibility discriminator values remain unchanged to preserve inherited
  GitHub-compatible routes and storage. Project issue tests also assert the
  GitLab payload does not expose inherited GitHub issue fields such as
  `repository_url`, `html_url`, or `pull_request`.
- GitLab pipeline routes now import the `Project` alias as well, and specific
  trigger/schedule routes are registered before the encoded project-path
  catch-all pipeline route so trigger pipeline creation is not shadowed.
- `.gitlab-ci.yml` parsing supports local `extends` inheritance from template
  jobs for common job fields, multi-parent reverse deep merge, `default:`
  inheritance for common runner keys, `inherit: default`, `inherit: variables`,
  invalid extends-shape errors, and an extends depth guard. Pipeline creation
  resolves local includes, nested local includes, `include:project`, controlled
  `include:remote`, and built-in template includes before parsing, including
  list-valued local/project/remote/template entries.
- The runner coordinator now uses persisted jobs only. The old in-memory smoke
  queue and `/api/v4/admin/runner/jobs` debug endpoints have been removed.
- The admin UI now includes `/admin/ci-lab`, a compact CI job lab for creating
  projects, editing `.gitlab-ci.yml`, creating pipelines, inspecting jobs and
  traces, and playing/canceling/retrying jobs without leaving the emulator.
- CI Lab exposes runner readiness, contextual create/error messages, selected
  job URLs, trace refresh links, job/trace API links, artifact download links,
  and artifact metadata to make job creation and runner debugging faster.
- Client VM `glab` validation exists through `make vm-test`; it installs a
  pinned `glab` release inside the client VM and runs isolated `glab api`, Git
  Smart HTTP, merge request, pipeline, high-level `glab repo`, high-level
  `glab issue`, high-level `glab mr`, high-level `glab ci`/`glab pipeline`,
  high-level `glab ci trigger`, high-level `glab ci cancel`, high-level
  `glab ci retry`, high-level `glab job artifact`, and high-level
  `glab release` verification against `glemu.local`.
- Current validation has passed locally and in VMs: `make test-affected` passed
  190 tests, and `make vm-validate` passed after deploying the server VM. The
  VM path included 90 client `glab`/Git checks plus official runner variable,
  rules, extends, include, cache, and `needs:artifacts` validations.
- Local validation operations are exposed through `make test-focused`,
  `make test-affected`, and `make test-full`; VM validation remains
  `make vm-validate` for deploy-plus-validation and `make vm-validate-current`
  for an already deployed server.

## Compatibility Work Remaining

- Continue widening GitLab-shaped REST and runner compatibility only when a
  target client workflow needs behavior beyond the current MVP.
- Expand GitLab CI models and routes for deeper pipeline event behavior beyond
  the current runner execution, scheduling, variables, rules, includes,
  extends, cache, and artifacts coverage.
- Continue isolating internal pull request concepts from GitLab-facing merge
  request behavior where the distinction affects API behavior or persisted
  data. The first pass isolates GitLab project, group, and MR surfaces behind
  GitLab-facing model aliases.
- Continue replacing GitHub-style response headers, error payloads, webhook
  payloads, and GraphQL schema details where they leak into GitLab-facing
  behavior. Pagination, token handling, and the main tested REST payloads now
  have GitLab-shaped coverage.
- Keep tests aligned to GitLab and `glab` expectations for the implemented
  surfaces while retaining explicit compatibility coverage for inherited
  GitHub-shaped routes.

## Suggested First Vertical Slice

1. Add deeper project and `glab` compatibility beyond the current implemented
   GitLab-shaped REST surfaces.
2. Add richer `.gitlab-ci.yml` semantics and deeper pipeline event behavior.
3. Add integration tests that exercise the above through raw HTTP, `git`, and
   an official `gitlab-runner` smoke test.
