# Remaining GitLab Emulator Slices

This is the working backlog after the current MVP support for GitLab-shaped
projects, Git Smart HTTP, persisted pipelines/jobs, official GitLab Runner
execution, artifact metadata and expiry, cache fallback behavior, variable
precedence and metadata, richer `rules` with rule variables and
workflow variables, boolean `allow_failure` plus clear unsupported
`allow_failure:exit_codes` rejection, `when: always` and `when: on_failure`
cleanup scheduling, clear unsupported delayed-job and unknown `when` rejection, grouped boolean
`rules:if` expressions, regex non-match operators, unary negation,
null/empty variable comparisons, variable-backed regex patterns in `rules:if`,
`exists`/`changes` path-object rules, variable-expanded rule path patterns,
directory-style `rules:exists` patterns ending in `/`, mapping-form
`only`/`except`,
branch/tag/source-aware legacy `only`/`except` ref filters, cache
glob-style legacy `only`/`except` ref filters, cache list/file-derived keys,
cache `key:files_commits`, cache path/key/policy/when/fallback-key variable
expansion, clear rejection of unsupported cache entry options and invalid cache
policy/when values, richer
`needs`, including clear rejection of unsupported `needs:parallel:matrix`, deeper
`extends`, local/project/remote/template includes with list-valued
remote/template entries, clear rejection of unsupported cross-ref
`rules:changes:compare_to` and `rules:exists` project/ref options,
trigger tokens, pipeline schedules,
persisted-only runner coordination, GitLab-shaped users/auth, GitLab-shaped
project issues, GitLab-shaped project/group members, and GitLab-shaped
protected branches, GitLab-shaped releases, and GitLab-shaped webhooks.
Pipeline schedule CRUD, manual Play, and automatic cron materialization of due
schedules create `source=schedule` pipelines through the persisted job path.
Runner-side job scheduling is implemented as persisted pending-job eligibility
over stage order, `needs`, manual state, runner tags, runner pause/lock state,
`run_untagged`, and delayed jobs promoted from `scheduled` to `pending` when
their `start_in` delay is due by the background schedule worker or runner
polling.
GitLab-shaped global search now covers projects, issues, merge requests, and
indexed code blobs. GitLab-shaped project labels and milestones now expose
MVP list/create/get/update/delete surfaces with pagination, encoded project
path lookup, and GitLab response fields. `/api/v4/version` exposes
GitLab-shaped server version metadata for CLI compatibility probes, and
`/api/v4/metadata` exposes the matching GitLab-shaped server metadata.
`/api/v4/application/settings` exposes a read-only admin-gated MVP application
settings payload for instance compatibility checks, and
`/api/v4/application/statistics` exposes admin-gated MVP instance counts.
GitLab-shaped users now cover current user, get-by-username-or-id, and list
with search, username filtering, pagination headers, and common GitLab user
profile fields.
GitLab-shaped commit status create/list routes are backed by the existing
commit status storage and covered by local tests plus client-VM `glab api`
smoke checks.
GitLab-shaped repository compare exposes commit, commits, diffs, timeout, and
same-ref fields for project IDs and encoded project paths.
Pipeline and job API/UI surfaces now expose non-sensitive CI secret metadata
for requested secrets without exposing secret values.
Nested group namespaces are represented as organization-backed full paths and
projects can be created under those nested namespaces. GitLab-shaped namespace
list/get APIs expose user and group namespaces with search and pagination.
Validation currently passes locally and in the VM stack. The latest validation
run passed `make test-affected` with 190 tests and `make vm-validate`, including
client `glab` smoke with high-level release workflow and official runner
validation.
The Kubernetes executor validation slice now provisions a `k8s-runner` VM with
k3s, registers an official GitLab Runner using `executor = "kubernetes"`,
passes `make vm-k8s-runner-validate` with trace and artifact round trips, and
also passes `make vm-k8s-incluster-validate` with the runner manager itself
running as a pod inside k3s. Kubernetes secret validation now also passes for
file-mode secrets, env-mode secrets, and trace redaction on the VM-service
runner, plus file-mode secrets on the in-cluster runner.
The first deeper resource-compatibility pass adds top-level project/group
listing, project deletion, repository tree/raw file reads, merge request
commits/changes, exact GitLab pagination totals plus query-preserving `Link`
headers for the main GitLab-facing list endpoints, and richer project/group
member list compatibility.

## 1. Next Operational Slices

These are the next slices to take before widening compatibility again. They
come directly from the current CI Lab, official runner, and VM workflow.

### 1.1 Persist Runner Registrations and Diagnostics

Status: implemented.

- `app/api/runner.py` no longer uses process-local `_registered_runner` state.
- `ci_runners` persists runner token, description, tags, `run_untagged`,
  paused/locked flags, runner metadata, contact timestamps, system id, and last
  assigned job id.
- Runner registration, verify, unregister, and job polling update persisted
  runner records.
- Admin CI Lab diagnostics read persisted runner data.
- Tests cover registration persistence, verify/poll timestamps, persisted tag
  matching, and admin diagnostics rendering.

### 1.2 Runner and Pipeline Inspection API Surface

Status: implemented.

- Added runner inspection endpoints:
  - `GET /api/v4/runners`
  - `GET /api/v4/runners/:id`
  - `GET /api/v4/runners/:id/jobs`
- Added `GET /api/v4/projects/:id/pipelines/:pipeline_id/diagnostics`.
- Pipeline diagnostics explain eligible pending jobs, stage blockers, `needs`
  blockers, runner tag blockers, `run_untagged` blockers, running jobs, manual
  jobs, and terminal jobs.
- Pipeline scheduling supports persisted schedule CRUD, manual Play, next-run
  calculation, and a background cron scheduler loop that materializes due
  schedules through the normal persisted job path.
- Admin CI Lab diagnostics now use the same `explain_job_scheduling` helper as
  the API endpoint, so UI and API scheduler explanations share one source.

### 1.3 Stale Job Recovery Semantics

Status: implemented.

- Stale running jobs are detected in shared scheduler diagnostics after the
  emulator stale threshold is exceeded.
- Automatic timeout/requeue is intentionally not enabled; recovery stays
  operator-driven so long-running test jobs are not killed unexpectedly.
- CI Lab `Requeue` remains the emulator operator recovery action for pending
  or running jobs. It resets the runner-facing attempt, clears trace offsets,
  issues a new job token, and returns the same job record to `pending`.
- External GitLab-shaped clients should use the compatible `cancel` then
  `retry` flow for running jobs.
- Tests cover diagnostics for stale running jobs and CI Lab requeue after
  runner assignment plus partial trace content.

### 1.4 CI Lab UX Tightening

Status: implemented.

- CI Lab remains an operator/debug UI rather than a full GitLab clone.
- Runner readiness now summarizes whether the runner is paused, has never
  contacted the emulator, has contacted but not polled recently, or is polling.
- Selected job detail now exposes a refresh link, a copyable CI Lab URL, job
  API and trace API links, and artifact download links with artifact metadata.
- Pipeline/job creation errors are rendered inline in the CI Lab panels as well
  as through the existing admin flash message.
- Tests cover the selected job URL, trace/API affordances, artifact download
  link rendering, and runner readiness text.

### 1.5 VM Operations Hardening

Status: implemented.

- Normal `make vm-deploy` remains non-destructive; destructive server state
  reset is explicit through `make vm-deploy-reset` or `make vm-reset`.
- Caddy CA persistence and runner trust refresh are covered by
  `vm-runner-ensure-ca`, `vm-runner-install-ca`, and the CI Lab smoke path.
- `make vm-ci-lab-smoke` remains a fast operational smoke for CI Lab project
  setup, pipeline creation, official runner execution, trace markers, and
  artifact metadata rather than a full validation suite.
- `gitlab_emulator/docs/runbooks/operations.md` documents recovery checklists
  for runner TLS failures, runner registration token mismatch, Docker image
  pull failures, stuck pending jobs, and stale running jobs.

### 1.6 Kubernetes Executor Runner Validation

Status: implemented.

- Added a `k8s-runner` Vagrant VM running single-node k3s.
- Added official GitLab Runner registration for the Kubernetes executor.
- Added distinct persisted runner tokens for multiple runner registrations
  after the first backward-compatible static token.
- Added `vm-k8s-runner-*` make targets for provisioning, CA install,
  registration, status/log/pod inspection, and validation.
- Added `scripts/k8s-runner-validation.sh`, which creates a tagged `k8s`
  pipeline job, waits for official runner execution, checks trace markers, and
  verifies artifact metadata.
- `make vm-k8s-runner-validate` passes and shows k3s runner pods in the
  `gitlab-runner` namespace.
- Added an in-cluster runner manager Deployment in namespace
  `gitlab-runner-incluster`, validated by `make vm-k8s-incluster-validate`
  with tag `k8s-incluster`.

## 2. Deeper Existing Resource Compatibility

This is a bounded compatibility pass, not full GitLab parity. The goal is to
make the existing MVP resources more GitLab-shaped for target clients while
keeping each slice tied to concrete REST behavior and tests.

### 2.1 Groups Compatibility

Status: implemented.

- Group responses include broader GitLab-shaped fields and links.
- Group list supports common filters and sorting such as `top_level_only`,
  `skip_groups`, `owned`, `min_access_level`, `all_available`, `order_by`, and
  `sort`.
- Nested groups preserve parent metadata, resolve URL-encoded full paths,
  support project ownership/listing under nested namespaces, and expose
  GitLab-shaped direct subgroup and descendant group listing endpoints.
- Group members support `/members/all`, query/pagination edge cases, and
  duplicate-member conflict handling.
- Focused tests cover numeric IDs, full paths, nested paths, member queries,
  hook listing, pagination headers, and response shape.

### 2.2 Projects Compatibility

Status: implemented.

- Project responses include broader GitLab-shaped namespace, timestamp,
  permissions/statistics, import status, CI/access, URL, and metadata fields.
- Project refs handle encoded and double-encoded path lookups more robustly.
- Project creation is covered under user, group, and nested group namespaces.
- Project list supports common filters such as `owned`, `ids`,
  `with_issues_enabled`, `visibility`, `order_by`, and `sort`.
- Focused tests cover namespace handling, encoded project paths, deletion,
  list filters, and response shape.

### 2.3 Repository Files and Tree Compatibility

Status: implemented.

- Repository tree/raw/file metadata behavior covers path/ref edge cases,
  missing refs, directory/file distinctions, raw content headers, tree
  pagination, and recursive tree paths.
- Repository file `HEAD` returns GitLab metadata headers without a body.
- Create/update/delete file responses include embedded commit metadata and
  branch/file fields.
- `start_branch`, `start_sha`, and `start_ref` are supported for file changes
  from another ref.
- Real git commit behavior is preserved for file changes.
- Focused tests cover URL-encoded paths, missing refs, directory/file
  distinctions, raw/header responses, pagination, and commit metadata.

### 2.4 Commits Compatibility

Status: implemented.

- Commit list supports `ref`, `ref_name`, `path`, `since`, `until`,
  pagination, and optional `with_stats`.
- Commit get supports `stats=true`.
- Commit diff metadata uses raw git parsing for added/deleted/renamed file
  flags and modes.
- Focused tests cover list filters, encoded project paths, diff shape, stats,
  created/deleted files, and pagination headers.

### 2.5 Branches, Tags, and Protected Branches

Status: implemented.

- Branch/tag commit response shapes include broader GitLab-shaped commit
  metadata such as `web_url`, `trailers`, and `extended_trailers`.
- Branch/tag create/get/delete behavior covers encoded project paths,
  duplicate refs, missing refs, and auth edge cases.
- Protected branch responses include broader GitLab-shaped access metadata.
- Git Smart HTTP enforcement remains deferred until a target workflow requires
  actual push blocking.
- Focused tests cover encoded project paths, existing/missing refs, protected
  branch fields, and pagination headers.

### 2.6 Merge Requests Compatibility

Status: implemented.

- Merge request responses include broader GitLab-shaped fields and state names
  (`opened`, `closed`, `merged`).
- Create/update/merge behavior validates source/target branches, same-branch
  cases, duplicate MRs, stale SHA, merge methods, and failed merge conditions.
- MR commits are paginated, `/changes` includes richer metadata and diff text,
  and `/diffs` exposes GitLab-shaped changed-file entries.
- Inherited GitHub-shaped pull request internals remain isolated behind
  GitLab-facing route/response behavior.
- Focused tests cover create/list/get/update/merge, commits, changes, diffs,
  pagination, encoded project paths, and common `glab api` expectations.

### 2.7 Labels and Milestones Compatibility

Status: implemented.

- Project labels support GitLab-shaped list/create/get/update/delete under
  `/projects/:id/labels` and encoded project full paths.
- Label list supports search, exact pagination headers, query-preserving
  `Link` headers, and optional open/closed issue counts.
- Project milestones support GitLab-shaped list/create/get/update/delete under
  `/projects/:id/milestones` and encoded project full paths.
- Milestone list supports active/closed/all state filters, title/search
  filters, exact pagination headers, and query-preserving `Link` headers.
- Existing GitHub-shaped `/repos/:owner/:repo/labels` and
  `/repos/:owner/:repo/milestones` compatibility remains intact.
- Focused tests cover CRUD, duplicate labels, encoded project paths,
  pagination headers, issue counts, filters, and response shape.

### 2.8 Cross-Resource Validation

Status: implemented.

- Focused local compatibility tests pass together for groups, projects,
  branches/tags/protected branches, repository files/tree, commits, commit
  statuses, repository compare, merge requests, labels, milestones, issues, and
  search.
- `make test-affected` passes after the full resource pass.
- Targeted client-VM `glab api` checks pass for the expanded resource
  surfaces.
- Keep exact GitLab-style total counts, page headers, and query-preserving
  `Link` headers intact for projects, user projects, groups, group projects,
  project issues, merge requests, branches, tags, protected branches,
  repository tree, commits, commit statuses, repository compare, releases,
  pipelines, jobs, search, project/group hooks, and project/group members.
- Keep project and group member list support for `query`, exact pagination
  headers, and common GitLab member response fields such as `created_at`,
  `created_by`, `invite_email`, and SAML/SCIM identity placeholders.

Done when:

- slices 2.1 through 2.6 have focused tests for their expanded surfaces:
  complete
- `make test-affected` passes: complete
- target client-VM `glab api` checks pass for the expanded resource surfaces:
  complete
- remaining gaps are documented as deferred full-parity work: complete

## 3. `glab` High-Level Workflow Coverage

- Move beyond `glab api` smoke coverage.
- Implemented in `make vm-test`:
  - `glab repo create`
  - `glab repo view --output json`
  - `glab repo list --output json`
  - `glab repo clone`
  - `glab repo delete`
  - `glab issue create`
  - `glab issue list --output json`
  - `glab issue view --output json`
  - `glab issue update`
  - `glab issue close`
  - `glab issue reopen`
  - `glab mr create`
  - `glab mr list --output json`
  - `glab mr view --output json`
  - `glab mr update`
  - `glab mr merge`
  - `glab ci run`
  - `glab ci list --output json`
  - `glab pipeline list --output json`
  - `glab ci status --output json`
  - `glab ci get --output json --with-job-details`
  - `glab ci trace`
  - `glab ci trigger`
  - `glab ci cancel pipeline`
  - `glab ci cancel job`
  - `glab ci retry`
  - `glab job artifact --list-paths`
- `glab release` high-level create/view/delete is implemented in `make vm-test`,
  and `glab api` release asset link create/list/update/delete is covered.
  Package-backed binary upload flows remain deferred until target workflows need
  them.
- REST support and high-level client-VM `glab` validation now exist for
  manual job trigger/play, pipeline/job cancel, and job retry.
- Client-VM `glab api` validation now includes project labels and milestones:
  create/list/get/update/delete coverage for the GitLab-shaped project routes.
- Keep each workflow tied to implemented REST surfaces.

## 4. Data Model Cleanup

- First cleanup pass complete: GitLab-facing project, group, merge request,
  project issue, and project member APIs now import `Project`, `Group`, and
  `MergeRequest` model aliases instead of the legacy `Repository`,
  `Organization`, and `PullRequest` model names directly where a GitLab-facing
  alias exists.
- GitLab project issue tests now assert the `/projects/:id/issues` payload stays
  GitLab-shaped and does not expose inherited GitHub issue fields such as
  `repository_url`, `html_url`, or `pull_request`.
- The GitLab pipeline API also imports the `Project` alias, and the catch-all
  encoded project-path pipeline route now sits after trigger/schedule routes so
  `/projects/:id/trigger/pipeline` is not shadowed.
- Inherited GitHub-compatible `/pulls/:number/commits` and
  `/pulls/:number/files` now resolve real git commits and changed-file metadata
  instead of placeholder head-SHA and empty-file responses.
- Inherited GitHub-compatible `/repos/:owner/:repo/compare/:base...:head`
  now resolves real commit and changed-file metadata instead of placeholder
  compare payloads.
- Inherited GitHub-compatible contents delete now creates a real git commit,
  updates the target branch, and rejects stale blob SHAs instead of returning a
  placeholder zero SHA.
- Inherited GitHub-compatible event feeds now include repository metadata, and
  `/users/:username/received_events` returns public events on repositories owned
  by the target user instead of an empty stub.
- The backing database tables and inherited GitHub-compatible repos/orgs/
  `/pulls`, review, review-comment, GraphQL, and admin/web scaffolding still
  use legacy naming intentionally for compatibility.
- Continue isolating GitHub-shaped internal concepts only where they leak into
  GitLab-facing behavior or create semantic drift.
- Deeper physical table/relationship renames are deferred until the project no
  longer needs to preserve the inherited GitHub emulator surfaces.

## 5. Validation and Operations

- Validation operations pass complete: local validation is exposed through
  `make test-focused`, `make test-affected`, and `make test-full`.
- `make vm-validate` remains the single command for full deploy-plus-validation.
  Current run passed after redeploying the server VM, refreshing client/runner
  CA trust, running client `glab` checks, and running official runner
  variables, rules, extends, includes, cache, and `needs:artifacts`
  validations.
- The latest focused client-VM `make vm-test` run passed 100 `glab` checks,
  including project label and milestone API coverage.
- `make vm-validate-current` remains the command for validating an already
  deployed server.
- `make vm-runner-validate` remains aligned with runner capabilities; current
  validation covers variables, rules, extends, includes, cache, and
  `needs:artifacts`.
- `docs/plans/validation.md` documents local commands, VM commands, Docker Hub
  pull-limit workarounds, TLS/runner operational risks, and the sandbox
  `aiosqlite` hang caveat after force-aborted async test runs.
- Keep `GITLAB_STATUS.md`, `docs/PLAN.md`, and `docs/plans/validation.md` updated
  after future slices.

## Deferred Work

- Full GitLab GraphQL parity. The current compatibility surface includes
  GitLab-shaped `currentUser`, `project(fullPath:)`, and repository
  `mergeRequests` aliases backed by the existing user/project/MR models.
  Repository `latestRelease` now resolves real release metadata, and repository
  issue connections support `filterBy.mentioned` against issue bodies and
  comments. Pull request `reviewDecision` now reflects active submitted
  approvals and change requests, and pull request diff stat fields resolve from
  git diffs. Pull request commit and changed-file connections list the actual
  commits and files between base and head. Repository `refs(refPrefix:)`
  distinguishes branch and tag refs, and repository language/topic and watcher
  connections resolve from stored project metadata and star data. Repository
  issue and pull request template fields, repository code of conduct metadata,
  and repository funding/contact links, and repository license metadata resolve
  from committed files. Repository assignable and mentionable user connections
  resolve from project owner/member data, and forked repository `parent`
  resolves from persisted fork metadata. Issue and pull request
  closing-reference connections resolve common same-repository closing keywords
  from merge request bodies. GraphQL search total counts report all matches
  independently of the returned node limit. The broader schema remains an
  incremental parity area.
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
- Complete long-tail `glab` command coverage beyond the smoke workflows above.
- Full timer parity beyond the current worker model. Current support covers
  pipeline schedule CRUD, manual Play, automatic cron materialization through
  the schedule worker, delayed jobs with `when: delayed`/`start_in`, background
  promotion of due delayed jobs, and runner-side pending-job eligibility.
  Broader production scheduling concerns such as distributed leader election
  remain deferred.
- Production security hardening. Baseline browser security headers are enabled
  across API, admin, web, and error responses, and admin bootstrap user/token
  helper endpoints require an authenticated site admin; broader hardening
  remains environment-specific.
