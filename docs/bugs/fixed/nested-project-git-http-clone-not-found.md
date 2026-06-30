# Bug: Nested Project Git HTTP Clone Not Found

## Summary

Git Smart HTTP routes only resolved `owner/project` paths, so runner checkout
failed for projects stored under nested GitLab group namespaces.

## Reproduction

1. Create a project under a nested group path such as
   `redhat/rhel-ai/agentic-ci/strat-dashboard`.
2. Run a CI job that checks out the project through Git Smart HTTP.
3. Observe checkout against
   `/redhat/rhel-ai/agentic-ci/strat-dashboard.git`.

## Expected

Git Smart HTTP resolves the full project namespace path and serves
`git-upload-pack`/`git-receive-pack` for clone, fetch, and push.

## Actual

The Smart HTTP route matched only two path components and returned repository
not found for nested namespace project paths.

## Impact

High for nested group CI execution: runners could be configured correctly and
still fail before job scripts ran because source checkout could not find the
project.

## Fix

The Smart HTTP routes now capture the full project path before the Git service
endpoint and resolve `Repository.full_name` after stripping the optional `.git`
suffix.

## Evidence

- Added regression coverage for
  `/transport-parent/transport-child/nested-git-http.git/info/refs?service=git-upload-pack`.
