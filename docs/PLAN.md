# GitLab Emulator Work Index

This directory is the filesystem-native work ledger for the GitLab emulator.
Task state is represented by file location.

## Current Plan

- [Project overview](plans/000-overview.md)
- [Compatibility plan](plans/compatibility.md)
- [CI pipeline implementation plan](plans/ci-pipeline-implementation.md)
- [CI runner plan](plans/ci-runner.md)
- [CI variables, secrets, and security plan](plans/ci-variables-secrets-security.md)
- [Validation plan](plans/validation.md)

## Milestones

- [M1 MVP GitLab Emulator](milestones/M1-mvp-gitlab-emulator.md)

## Active Tasks

No task files are currently checked into `docs/tasks/current/`.

## Pending Tasks

- [Targeted GitLab parity follow-ups](tasks/pending/targeted-gitlab-parity-followups.md)

## Completed Task Ledgers

- [Consolidate web project routing around full project paths](tasks/done/consolidate-web-project-routing.md)
- [Match GitLab project shell and sidebar layout](tasks/done/match-gitlab-project-shell-and-sidebar.md)
- [Implement GitLab-style pipeline pages](tasks/done/gitlab-style-pipeline-pages.md)
- [MVP backlog completion](tasks/done/mvp-backlog-completion.md)
- [Kubernetes runner validation](tasks/done/kubernetes-runner-validation.md)

## Runbooks

- [Docker Compose stack runbook](runbooks/compose-stack.md)
- [Operations runbook](runbooks/operations.md)
- [k3s stack deployment guide](runbooks/k3s-stack-deployment.md)
- [Runner deployment guide](runbooks/runner-deployment.md)
- [Runner testing runbook](runbooks/runner-testing.md)

## Notes

- [Agentic work ledger conventions](notes/agentic-work-ledger.md)
- [Session log](notes/session-log.md)

## Bugs

No bug files are currently checked into `docs/bugs/open/`.

## Fixed Bugs

- [Bridge target pipeline creation 500 and MissingGreenlet](bugs/fixed/bridge-target-500-and-missing-greenlet.md)
- [Nested project Git HTTP clone not found](bugs/fixed/nested-project-git-http-clone-not-found.md)

## Decisions

- [ADR-0001: Preserve the GitHub emulator architecture](decisions/ADR-0001-preserve-github-emulator-architecture.md)
- [ADR-0002: Use official GitLab Runner for job execution](decisions/ADR-0002-use-official-gitlab-runner.md)
- [ADR-0003: Keep full GitLab parity deferred](decisions/ADR-0003-defer-full-gitlab-parity.md)
- [ADR-0004: Model GitLab groups as namespace paths](decisions/ADR-0004-model-gitlab-groups-as-namespaces.md)
- [ADR-0005: Use full project paths for web UI routes](decisions/ADR-0005-use-full-project-paths-for-web-ui-routes.md)
- [ADR-0006: Surface runner scheduling diagnostics in web UI](decisions/ADR-0006-surface-runner-scheduling-diagnostics-in-web-ui.md)
