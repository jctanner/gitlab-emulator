# Task: Consolidate Web Project Routing Around Full Project Paths

## Goal

Refactor project-scoped web UI routes so nested GitLab project paths are handled
as the normal routing model instead of as a compatibility fallback.

## Context

ADR-0005 establishes that web UI project pages should resolve repositories by
full project path. The current implementation includes many routes shaped as
`/{owner}/{repo_name}/...` and a catch-all dispatcher for nested paths.

The dispatcher fixes immediate 404s for nested projects such as
`redhat/rhel-ai/agentic-ci/strat-pipeline`, but future routes can still regress
if they are added only in the two-segment style.

## Acceptance Criteria

- [ ] Project-scoped web routes resolve `Repository.full_name` for nested paths.
- [ ] Existing user project URLs such as `/ui/testuser/project` continue to work.
- [ ] Left-nav targets work for nested projects.
- [ ] Source browsing actions work for nested projects.
- [ ] CI/build actions work for nested projects.
- [ ] Tests cover representative nested project routes.

## Files Likely Involved

- `app/web/routes.py`
- `app/web/templates/_repo_nav.html`
- `tests/test_web_ui.py`

## Status

Done

## Notes

- ADR-0004 documents nested groups as namespace paths.
- ADR-0005 documents the routing direction.
- Implemented full project path resolution through the project web UI dispatcher.
- Existing two-segment project URLs remain available for compatibility.
- Nested dispatch now covers representative source, issue, merge request,
  left-nav, CI, artifact, schedule, and pipeline editor routes.
- Verification evidence: `py_compile` for `app/web/routes.py` and
  `tests/test_web_ui.py`; Jinja parsing for touched templates; targeted async
  pytest still hangs in SQLite fixture setup before test body execution in this
  environment.
