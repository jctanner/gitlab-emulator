# ADR-0001: Preserve the GitHub Emulator Architecture

## Status

Accepted.

## Context

The GitLab emulator was intentionally started from `github_emulator` so the
project could reuse a working FastAPI application, SQLAlchemy models, SQLite
persistence, Git Smart HTTP plumbing, Docker Compose packaging, Vagrant
validation, and server-rendered UI foundation.

## Decision

Keep the same underlying architecture and replace behavior at GitLab-facing
boundaries where clients observe a difference.

## Consequences

Positive:

- Faster path to a working GitLab-compatible integration fixture.
- Reuses proven repository, auth, UI, Docker, and VM scaffolding.
- Keeps the local development and validation story simple.

Negative:

- Some inherited GitHub-shaped internals remain below GitLab-facing adapters.
- Agents must be careful not to expose GitHub-shaped payloads through new
  GitLab-facing surfaces.
