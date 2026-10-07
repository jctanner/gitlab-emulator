# Task: Architectural Maintainability Follow-Ups

## Goal

Clarify the GitLab emulator's source and compatibility boundaries, reduce its
largest maintenance hotspots, and make persisted-schema upgrades safer while
preserving the official GitLab Runner execution model.

## Context

The emulator follows the same FastAPI, SQLAlchemy, bare-Git, Strawberry, and
Jinja2 architecture as the GitHub emulator, but its native GitLab CI coordinator
is substantially larger. Pipeline parsing, pipeline APIs, runner coordination,
and web routing now dominate the implementation and test surface.

The official `gitlab-runner` process and Kubernetes executor are deliberate
architecture choices. This task improves the emulator-side coordinator; it does
not introduce an in-process replacement runner.

## Proposed Work

1. Move production code from `app/` to `src/app/` and update packaging,
   containers, Alembic, tests, scripts, and breadboard deployment paths.
2. Split `web/routes.py` and `admin/routes.py` into feature-oriented routers
   without changing full-path project routing or authorization behavior.
3. Decompose `api/pipelines.py`, `api/runner.py`, and `services/ci_yaml.py` into
   explicit parsing, expansion, scheduling, coordinator-protocol, trace,
   artifact, cache, and persistence boundaries.
4. Document and enforce the boundary between native GitLab surfaces
   (`projects`, `groups`, `merge_requests`, and `pipelines`) and inherited
   compatibility surfaces (`repos`, `orgs`, `pulls`, and `actions`). Remove an
   inherited endpoint only through a separately reviewed compatibility decision.
5. Replace startup-time compatibility DDL with versioned Alembic revisions and
   test upgrades from representative existing SQLite databases.
6. Port the GitHub emulator's WAL, busy-timeout, and bounded write-retry policy
   where it is applicable to GitLab pipeline and runner update contention.
7. Split oversized tests, especially pipeline and CI YAML coverage, while
   retaining real GitLab Runner, `glab`, artifact, cache, secret-redaction, and
   Kubernetes-executor integration tests.

## Acceptance Criteria

- [ ] Each proposed area is represented by a focused child task before work begins.
- [ ] Source relocation does not change imports or container entry points.
- [ ] Native and compatibility API ownership is documented and covered by
  separate contract tests.
- [ ] Database changes use Alembic and upgrade at least one older persisted
  emulator schema successfully.
- [ ] Pipeline scheduling, traces, artifacts, cache, variables, and secrets
  continue to work with the official GitLab Runner.
- [ ] Existing GitLab Runner and Kubernetes validation runbooks remain valid.
- [ ] Breadboard can rebuild and redeploy the component and runner from scratch.

## Non-Goals

- Full GitLab feature parity without a concrete integration requirement.
- Extracting a shared GitHub/GitLab domain framework.
- Replacing Jinja2 with React or another client-side framework.
- Reimplementing GitLab Runner inside the emulator.
- Making the SQLite deployment horizontally scalable or production-grade.

## Status

Pending.

## Notes

Cross-emulator reuse should begin with narrow infrastructure contracts such as
Git storage helpers, middleware, migration-test fixtures, or template macros.
Provider-specific project, permission, event, merge, and runner behavior should
remain within this repository.
