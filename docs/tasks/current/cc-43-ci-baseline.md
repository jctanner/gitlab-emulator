# CC-43: Make the existing GitLab suite reliable in a clean CI runner

## Status

Current. Concrete failing workflow: newly added GitHub CI full pytest suite.

## Evidence and scope

Actions run 43761951759: 745 passed, four failed. No failed workflow was retried.

- Git Data commit creation requires host Git identity, absent in clean runners
  and images. Use the authenticated user's identity for commit-tree; retain
  Developer authorization and verify attribution in the existing permission test.
- rules:if incorrectly treats an undefined variable as a defined empty string.
  Fix the empty literal comparison, covered by the existing richer-rules test.
- Two runner-payload tests expect YAML variables to outrank pipeline/project
  variables. The merge already prioritizes pipeline variables but incorrectly places
  project values below YAML. Move project metadata above YAML and correct the stale
  expectations against GitLab's documented pipeline > project > job YAML >
  default YAML precedence.

References:
https://docs.gitlab.com/ci/variables/#ci-cd-variable-precedence
https://docs.gitlab.com/ci/jobs/job_rules/#ci-cd-variable-expressions

## Acceptance criteria

- [x] The four failed tests pass with host Git configuration disabled.
- [ ] Full CI remains mandatory and passes before merge.
- [ ] Container smoke still passes; no source-system API parity expansion.

The existing authoritative pipeline ref context remains main in runner payloads; this change does not expand predefined-variable override behavior.

Local focused validation: 95 tests passed, including the full CI YAML parser file, Git Data permission/attribution test, and both precedence tests. Full remote CI remains required before merging.
