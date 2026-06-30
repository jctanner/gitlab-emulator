# ADR-0005: Use Full Project Paths for Web UI Routes

## Status

Accepted.

## Context

GitLab project UI routes are scoped by the complete project path. A project such
as `redhat/rhel-ai/agentic-ci/strat-pipeline` keeps that full path before page
actions such as `/-/pipelines`, `/branches`, `/commits/main`, or
`/edit/main/.gitlab-ci.yml`.

The emulator inherited many web routes shaped as `/{owner}/{repo_name}/...`.
That works for user projects and one-level organization projects, but nested
group projects are parsed incorrectly. Recent compatibility handling added a
nested catch-all dispatcher, but the route model remains split between two
segment routes and GitLab-style full-path routes.

## Decision

Web UI project routing should resolve repositories by full GitLab project path
first, then dispatch the remaining path segments to the project page action.

New project-scoped web UI functionality should not assume that the owner is a
single path segment. Existing two-segment routes may remain for compatibility,
but nested project paths must be first-class behavior.

## Consequences

Positive:

- Nested GitLab group projects work consistently across repository pages.
- Left navigation links can use `repo.full_name` without special cases.
- New web UI pages are less likely to regress nested namespace support.

Negative:

- The current router contains transitional compatibility code until route
  handlers are consolidated around full project paths.
- Some existing helpers and templates still need cleanup to avoid reconstructing
  paths from separate `owner` and `repo_name` values.
