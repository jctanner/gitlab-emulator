# ADR-0006: Surface Runner Scheduling Diagnostics in Web UI

## Status

Accepted.

## Context

The emulator supports persisted CI pipelines and an official GitLab Runner
polling flow. Jobs can remain pending even when a runner is actively polling if
the runner is not eligible to pick them up. Common examples include:

- The job is untagged while the available runner has `run_untagged` disabled.
- The job requires tags that no registered runner advertises.
- The job is waiting for an earlier stage or `needs` dependency.
- The job is delayed, manual, or waiting on a resource group.

Previously, project pipeline pages showed only the job status. A user could see
`pending`, but not why the job was not being assigned to a runner. The admin CI
diagnostics already had scheduling explanations through the runner coordinator
helper.

## Decision

Project pipeline and job detail pages should surface runner scheduling
diagnostics for pending or otherwise blocked jobs.

The web UI should reuse the same scheduling explanation logic used by the
runner/admin diagnostics instead of maintaining separate matching rules in
templates or project routes.

## Consequences

Positive:

- Users can distinguish normal queueing from actionable configuration problems.
- Tag and `run_untagged` mismatches are visible from the project pipeline page.
- The web UI stays aligned with the runner coordinator's actual job selection
  behavior.

Negative:

- Project CI pages now depend on runner diagnostic helpers from the runner API
  module.
- Diagnostics describe the currently registered runner state, so messages can
  change as runners register, pause, or update tags.
