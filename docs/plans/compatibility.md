# GitLab Compatibility Plan

## Goal

Move the scaffold from GitHub-shaped behavior to GitLab-shaped behavior while
preserving the shared FastAPI, SQLAlchemy, SQLite, Git Smart HTTP, Docker, and
Vagrant architecture.

## Current Compatibility Baseline

- Routes are mounted under `/api/v4`.
- Core persistence, auth, Git Smart HTTP, SSH transport, admin UI, and test
  harness are inherited from the GitHub emulator.
- Some public docs and examples have been renamed for GitLab.
- CI runner coordinator endpoints have been implemented first because job
  execution is a primary goal.
- Minimal GitLab-shaped project APIs exist for numeric project IDs:
  `POST /projects`, `GET /projects/:id`,
  `GET /projects/:id/repository/branches`,
  `GET /projects/:id/repository/branches/:branch`,
  `POST /projects/:id/repository/branches`,
  `DELETE /projects/:id/repository/branches/:branch`,
  `GET /projects/:id/repository/tags`,
  `GET /projects/:id/repository/tags/:tag_name`,
  `POST /projects/:id/repository/tags`,
  `DELETE /projects/:id/repository/tags/:tag_name`, and
  `GET /users/:id/projects`.
- Project lookup, project branch, and project tag surfaces also accept
  URL-encoded `path_with_namespace` project references such as
  `testuser%2Fexample-project`.
- Project creation supports group namespaces with `namespace_id` or
  `namespace_path`, backed by the existing organization model.
- Minimal GitLab-shaped group APIs exist for create, get by ID/path, group
  project listing, nested group paths, and nested group project creation.
- Minimal GitLab repository files APIs exist for get, create, update, and
  delete by numeric project ID or URL-encoded project path.
- Minimal GitLab repository commits APIs exist for list, get, and diff metadata
  by numeric project ID or URL-encoded project path.
- Minimal GitLab merge request APIs exist for create, list, get, update, and
  merge by numeric project ID or URL-encoded project path.
- GitLab global search exists for projects, users, issues, merge requests,
  milestones, indexed blobs/code, and indexed commits.

## Work Remaining

### Resources

Replace or add GitLab-shaped resources for:

- users
- groups/namespaces: MVP group create/list/get/list-projects, namespace
  list/get, and nested namespace paths implemented
- projects: MVP create/list/get/delete/list-branches/list-tags/
  list-user-projects implemented
- issues: MVP project list/create/get/update implemented with labels,
  assignees, and milestone assignment
- merge requests: MVP create/list/get/update/merge/commits/changes implemented
- repository files: MVP get/raw/tree/create/update/delete implemented, with
  protected-branch push access enforcement on writes
- commits: MVP list/get/diff implemented
- branches: MVP list/get/create/delete implemented
- tags: MVP list/get/create/delete implemented
- members: MVP project/group list/get/add/delete implemented
- protected branches: MVP list/get/protect/unprotect plus Git Smart HTTP, SSH,
  repository file, and source-editor write enforcement implemented
- releases: MVP list/create/get/update/delete implemented
- webhooks: MVP project/group list/create/get/update/delete implemented
- search: MVP global projects/users/issues/merge_requests/milestones/blobs/
  commits implemented

### Behavior

Replace GitHub-shaped behavior with GitLab behavior for:

- response schemas
- request schemas
- pagination
- error payloads: MVP JSON envelope and route-specific string `detail`
  preservation implemented
- auth token handling
- response headers: MVP API version, rate limit, ETag, pagination, security,
  `X-Request-Id`, and `X-GitLab-Request-Id` headers implemented
- webhook event names and payloads: MVP project/group hook CRUD and GitLab
  delivery headers for hook event names, event UUIDs, and hook tokens
  implemented
- merge request behavior versus pull request behavior
- project path and namespace handling
- GraphQL schema details where clients require them: MVP `currentUser`,
  `project(fullPath:)`, project URL/path fields, and merge request aliases
  implemented

### Tests

Update tests so they assert GitLab compatibility:

- raw HTTP API tests for GitLab-shaped resources
- `git` clone, fetch, and push tests against GitLab project paths
- `glab` CLI smoke tests for supported commands
- official runner smoke tests for CI execution paths

## First Vertical Slice

Implement project APIs before broad resource migration:

- `POST /api/v4/projects`
- `GET /api/v4/projects/:id`
- `GET /api/v4/users/:id/projects`
- branch list/get/create/delete and tag list/get/create/delete for a project

Status: implemented for the MVP numeric-ID surface.

URL-encoded `path_with_namespace` lookup is also implemented for project get,
branch list/get/create/delete, and tag list/get/create/delete.

Group namespace project creation is implemented for single-level organization
namespaces through `namespace_id` and `namespace_path`.

Minimal group APIs are implemented for:

- `POST /api/v4/groups`
- `GET /api/v4/groups/:id`
- `GET /api/v4/groups/:path`
- `GET /api/v4/groups/:id/projects`
- `GET /api/v4/groups/:path/projects`

Minimal repository files APIs are implemented for:

- `GET /api/v4/projects/:id/repository/files/:file_path`
- `POST /api/v4/projects/:id/repository/files/:file_path`
- `PUT /api/v4/projects/:id/repository/files/:file_path`
- `DELETE /api/v4/projects/:id/repository/files/:file_path`

The same endpoints accept URL-encoded `path_with_namespace` project references.

Minimal repository commits APIs are implemented for:

- `GET /api/v4/projects/:id/repository/commits`
- `GET /api/v4/projects/:id/repository/commits/:sha`
- `GET /api/v4/projects/:id/repository/commits/:sha/diff`

The same endpoints accept URL-encoded `path_with_namespace` project references.

Minimal repository branches APIs are implemented for:

- `GET /api/v4/projects/:id/repository/branches`
- `GET /api/v4/projects/:id/repository/branches/:branch`
- `POST /api/v4/projects/:id/repository/branches`
- `DELETE /api/v4/projects/:id/repository/branches/:branch`

The same endpoints accept URL-encoded `path_with_namespace` project references.

Minimal repository tags APIs are implemented for:

- `GET /api/v4/projects/:id/repository/tags`
- `GET /api/v4/projects/:id/repository/tags/:tag_name`
- `POST /api/v4/projects/:id/repository/tags`
- `DELETE /api/v4/projects/:id/repository/tags/:tag_name`

The same endpoints accept URL-encoded `path_with_namespace` project references.

Minimal merge request APIs are implemented for:

- `GET /api/v4/projects/:id/merge_requests`
- `POST /api/v4/projects/:id/merge_requests`
- `GET /api/v4/projects/:id/merge_requests/:iid`
- `PUT /api/v4/projects/:id/merge_requests/:iid`
- `PUT /api/v4/projects/:id/merge_requests/:iid/merge`

The same endpoints accept URL-encoded `path_with_namespace` project references.
Merge request create/update accepts GitLab-style comma-separated or list labels,
returns labels in merge request responses, and exposes those labels to merge
request event pipelines as `CI_MERGE_REQUEST_LABELS`.

Validated:

- project creation response shape
- clone/fetch/push against the created project
- `glab` behavior where the implemented surface is enough
- nested group/subgroup namespace behavior used by the current client workflow

Latest evidence: `make vm-test` passed from the client VM on June 30, 2026
with 140 `glab` smoke checks covering auth, users, projects, nested groups,
repo create/view/list/search/update/contributors/clone/delete/member add/remove, Git Smart HTTP
push/fetch, repository files, direct CI pipeline/job/trace APIs, issues,
high-level label CLI workflows, high-level milestone CLI workflows, branches,
protected branches, tags, releases, project CI/CD variable CLI workflows, commit
APIs, repository compare, commit statuses, merge request APIs, merge request CLI
workflows, pipeline APIs, CI trace/artifacts, manual jobs, cancel, and retry.

## Done Criteria

- GitLab-shaped project APIs are the primary path for repository creation.
- New tests avoid GitHub client assumptions.
- GitHub-shaped names and payloads are either removed or explicitly documented
  as temporary scaffold compatibility.
