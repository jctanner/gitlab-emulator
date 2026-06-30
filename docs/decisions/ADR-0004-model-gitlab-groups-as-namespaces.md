# ADR-0004: Model GitLab Groups as Namespace Paths

## Status

Accepted.

## Context

GitLab projects commonly live under nested group paths such as
`redhat/rhel-ai/agentic-ci`, where `redhat` is a top-level group,
`redhat/rhel-ai` is a subgroup, and `agentic-ci` is the project.

The emulator already uses the inherited `Organization` table for group-like
namespaces and stores nested groups as slash-delimited `Organization.login`
values. GitLab-facing APIs expose these values as `full_path` and
`path_with_namespace`.

## Decision

Continue representing GitLab groups and subgroups as organization-backed
namespace paths. A nested subgroup is stored as one organization row whose
`login` is the full path, for example `redhat/rhel-ai`.

Project creation may target any user namespace or organization-backed group
namespace. The project `full_name` remains the complete GitLab path, such as
`redhat/rhel-ai/agentic-ci`.

## Consequences

Positive:

- The API and UI can share one namespace representation.
- Nested GitLab project paths remain simple to query by full path.
- The model matches existing API validation and `glab` smoke coverage.

Negative:

- Parent/child group relationships are derived from slash-delimited paths
  instead of a dedicated parent foreign key.
- UI routes that operate on projects must use full project paths when nested
  namespaces are involved.
