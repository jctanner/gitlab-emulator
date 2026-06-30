# Task: Targeted GitLab Parity Follow-Ups

## Goal

Track future GitLab compatibility work without treating full GitLab parity as
the active project goal.

## Context

The MVP emulator is complete for the current integration target: GitLab-shaped
project/repository APIs, Git Smart HTTP, real GitLab Runner job execution,
CI/CD variables and secrets management, schedules, runner diagnostics, `glab`
smoke coverage, and the web/admin UI needed to create and inspect jobs.

Remaining parity should be demand-driven. A future agent should only move this
or a split-out child task into `docs/tasks/current/` after a concrete client
workflow fails or a user explicitly asks for a specific GitLab feature.

## Acceptance Criteria

- [ ] A target workflow is named.
- [ ] The missing GitLab surface is documented with request/response examples
      or client behavior.
- [ ] The task is split into a focused implementation file before work begins.
- [ ] Validation evidence is recorded before moving the task to done.

## Candidate Follow-Ups

- Deeper GitLab GraphQL parity beyond the implemented `currentUser`,
  `project(fullPath:)`, repository fields, issue/MR aliases, refs, release, and
  search coverage.
- UI parity for GitLab pages not needed by the current job/source workflows.
- Full authorization parity across all inherited GitHub-shaped endpoints.
- Long-tail `glab` subcommands beyond the smoke workflows already validated.
- Production timer concerns beyond the current pipeline schedule and delayed
  job workers.
- Production security hardening beyond the controlled integration-test
  assumptions.

## Files Likely Involved

- `docs/plans/compatibility.md`
- `docs/plans/ci-runner.md`
- `docs/plans/ci-pipeline-implementation.md`
- `GITLAB_STATUS.md`

## Status

Pending.

## Notes

Do not treat this as "make the emulator 100% identical to GitLab." The project
is an emulator for controlled integration testing, so the correct scope is the
smallest additional compatibility surface that unblocks a real workflow.
