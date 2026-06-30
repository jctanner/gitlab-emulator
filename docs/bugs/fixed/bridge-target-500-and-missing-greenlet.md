# 500 ISE when creating pipeline with bridge job referencing missing target project

**Date observed:** 2026-06-30
**Date fixed:** 2026-06-30
**Severity:** High
**Endpoint:** `POST /ui/{namespace}/{project}/-/pipelines`
**Component:** `app/web/routes.py` (`nested_repo_page`) + `app/api/pipelines.py` (`_resolve_bridge_target`)

## Summary

Creating a pipeline via the web UI for a project whose `.gitlab-ci.yml`
contains a `trigger:` bridge job referencing a project that does not exist on
the emulator caused a 500 Internal Server Error instead of a user-friendly
error redirect.

The 500 was a cascade of two bugs:

1. `_resolve_bridge_target()` correctly raised `HTTPException(400)` when the
   bridge target project was not found.
2. The web pipeline creation handler caught that exception, called
   `db.rollback()`, then accessed ORM attributes such as `repo.name` while
   building the redirect URL. After rollback, that attribute access could
   trigger an async SQLAlchemy lazy load outside a greenlet context, producing
   `sqlalchemy.exc.MissingGreenlet` and replacing the intended 400-style error.

## Reproduction

1. Create a project such as `redhat/rhel-ai/agentic-ci/strat-pipeline` with a
   `.gitlab-ci.yml` that includes a bridge job:

   ```yaml
   build-dashboard:
     stage: deploy
     trigger:
       project: redhat/rhel-ai/agentic-ci/strat-dashboard
       branch: main
   ```

2. Do not create the target project
   `redhat/rhel-ai/agentic-ci/strat-dashboard`.
3. Navigate to the project's pipeline page and click "Run pipeline" or POST to
   `/-/pipelines`.

## Expected

The web UI redirects back to `/-/pipelines/new` with an error message:

```text
Bridge job build-dashboard target project not found: redhat/rhel-ai/agentic-ci/strat-dashboard
```

## Actual

The request returned 500 Internal Server Error after the rollback handler tried
to access `repo.name` and triggered `MissingGreenlet`.

## Resolution

Fixed in `app/web/routes.py`.

- Capture scalar repository values (`repo.id`, `repo.name`, `repo.full_name`,
  and default branch) before pipeline creation try/except blocks.
- Use the captured `repo.full_name` to build error redirects after rollback.
- Apply the same scalar redirect pattern to the direct two-segment pipeline
  creation route and nested `/-/ci/editor` redirects.
- Added a nested web UI regression test that posts a bridge pipeline with a
  missing target and asserts a redirect to `/-/pipelines/new` with the bridge
  error query.

## Verification

- `py_compile` passes for `app/web/routes.py` and `tests/test_web_ui.py`.
- `git diff --check` passes.
- `tests/test_web_ui.py::test_gitlab_project_shell_sidebar_css_contract`
  passes.
- `tests/test_web_ui.py::test_ui_nested_bridge_pipeline_missing_target_redirects_error`
  was added but could not complete in this environment because the async pytest
  client fixture still hangs before producing test output.
