# CC-43: Releases and container images

## Status

Current: workflow implementation prepared; merge and publication require green CI.

## Contract

Human approved UTC CalVer YYYY.MM.DD[.N] on 2026-11-15. Existing release
schemes take precedence. Remote tags/releases and versioning documentation
were checked before implementation. Existing 2026.06.30 tag is preserved.
Package metadata versions remain unchanged. Default-branch images get latest
and sha-<full SHA>; immutable releases get the exact CalVer tag. PRs never
publish. The workflow publishes the smoke-tested local image, then creates
a GitHub release with its digest. Based on the Jira CC-43 reference pattern.

## Validation

- Workflow actionlint, publication regression tests, bash syntax and git diff --check passed locally.
- CI and container smoke: must pass in the PR before merge.
- First release and image digest: pending.

Container smoke diagnostics found a relative SQLite URL in the standalone Dockerfile. Fixed it to use the intended absolute /data path. No workflow was retried; the change is validated by a new PR commit.

The API intentionally binds loopback behind Caddy. The smoke test now supplies synthetic HTTP Caddy configuration and checks port 80, preserving that boundary.

The first full CI run exposed four clean-runner baseline failures (745 passed). See cc-43-ci-baseline.md for the scoped fixes, official GitLab contract references and 95 passing focused tests. Full CI remains mandatory.
