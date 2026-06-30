# M1: MVP GitLab Emulator

## Status

Complete.

## Goal

Provide a usable GitLab-like integration fixture for tools that need GitLab
REST APIs, Git Smart HTTP, and real GitLab CI job execution without depending
on a live GitLab instance.

## Completed Capabilities

- GitLab-shaped REST routes under `/api/v4`.
- Git Smart HTTP clone, fetch, and push for emulator repositories.
- Official GitLab Runner coordinator support for registration, polling, trace
  append, status updates, artifacts, and cache interactions.
- Persisted pipelines and jobs created from API calls, pushes, schedules,
  triggers, merge requests, and committed `.gitlab-ci.yml`.
- Docker executor runner validation from the runner VM.
- Kubernetes executor runner validation from a k3s VM and in-cluster runner
  manager.
- Server-rendered web UI for repository/source editing, CI/CD variables,
  secrets, runners, pipelines, jobs, schedules, artifacts, issues/work items,
  merge requests, labels, milestones, releases, webhooks, deploy keys, and the
  admin CI Lab.
- Client VM `glab` smoke coverage for the implemented high-level workflows.

## Evidence

- `docs/tasks/done/mvp-backlog-completion.md`
- `docs/tasks/done/kubernetes-runner-validation.md`
- `docs/plans/validation.md`
- `GITLAB_STATUS.md`

## Follow-Up Boundary

Future work is targeted compatibility, not an open-ended mandate to become
identical to GitLab. Pull pending tasks only when a concrete client workflow
requires more behavior.
