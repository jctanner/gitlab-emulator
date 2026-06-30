# ADR-0003: Defer Full GitLab Parity

## Status

Accepted.

## Context

GitLab is a large OSS product with extensive REST, GraphQL, UI, CI/CD,
authorization, security, package, registry, deployment, and administration
surfaces. Treating the emulator backlog as "be identical to GitLab" would turn
the project into an unbounded reimplementation.

The current MVP already supports the target integration workflows: repository
creation and source editing, Git Smart HTTP, pipeline/job creation, official
runner execution, traces, artifacts, cache, schedules, variables, secrets,
runner diagnostics, and broad `glab` smoke compatibility.

## Decision

Full GitLab parity is not the project goal. Future compatibility work must be
targeted to a named client workflow or explicit user request.

## Consequences

Positive:

- Keeps the emulator maintainable as an integration-test fixture.
- Prevents deferred parity notes from being mistaken for active backlog.
- Lets future agents choose small, validated compatibility slices.

Negative:

- Some GitLab clients and UI paths will remain unsupported until a real need is
  identified.
- Documentation must clearly distinguish completed MVP scope from deferred
  parity.
