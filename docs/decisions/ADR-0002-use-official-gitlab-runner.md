# ADR-0002: Use Official GitLab Runner for Job Execution

## Status

Accepted.

## Context

Executing CI jobs is a primary goal. Reimplementing runner behavior inside the
emulator would duplicate a large and changing surface: registration, polling,
trace streaming, artifacts, cache, source checkout, Docker executor behavior,
and Kubernetes executor behavior.

## Decision

Use the official `gitlab-runner` process as the execution engine. The emulator
implements enough GitLab coordinator, project, pipeline, job, trace, artifact,
and cache APIs for official runners to execute controlled integration jobs.

## Consequences

Positive:

- Job execution behavior is much closer to real GitLab Runner behavior.
- Docker and Kubernetes executor validation use the same client process that
  real GitLab uses.
- The emulator can focus on coordinator APIs and persisted state.

Negative:

- VM and k3s validation are heavier than an in-process fake runner.
- Network, TLS, image pulls, and runner configuration become part of the
  operational surface.
