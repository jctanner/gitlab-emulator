# 500 ISE when creating pipeline with bridge job referencing missing target project

**Date observed:** 2026-06-30
**Severity:** High
**Endpoint:** `POST /ui/{namespace}/{project}/-/pipelines`
**Component:** `app/web/routes.py` (`nested_repo_page`) + `app/api/pipelines.py` (`_resolve_bridge_target`)

## Summary

Creating a pipeline via the web UI for a project whose `.gitlab-ci.yml` contains a `trigger:` (bridge) job referencing a project that does not exist on the emulator causes a 500 Internal Server Error instead of a user-friendly error redirect. The 500 is actually a cascade of two bugs:

1. **Bug A — Unhandled HTTPException from `_resolve_bridge_target`:** The function raises `HTTPException(400)` when the bridge target project is not found (`app/api/pipelines.py:1943`). The caller in `nested_repo_page` catches this with a bare `except Exception`, calls `db.rollback()`, then tries to build a redirect URL using `repo.name`.

2. **Bug B — SQLAlchemy MissingGreenlet on `repo.name` access after rollback:** After `db.rollback()`, accessing `repo.name` triggers a lazy attribute load. Because the async SQLAlchemy session's greenlet context has been invalidated by the rollback + exception propagation through Starlette middleware, this lazy load fails with `sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called`. This second exception replaces the handled first exception and propagates as the actual 500.

## Reproduction

1. Create a project (e.g. `redhat/rhel-ai/agentic-ci/strat-pipeline`) with a `.gitlab-ci.yml` that includes a bridge job:
   ```yaml
   build-dashboard:
     stage: deploy
     trigger:
       project: redhat/rhel-ai/agentic-ci/strat-dashboard
       branch: main
   ```
2. Do **not** create the target project `redhat/rhel-ai/agentic-ci/strat-dashboard` on the emulator.
3. Navigate to the project's pipeline page and click "Run Pipeline" (or POST to `/-/pipelines`).
4. Result: 500 Internal Server Error.

## Traceback (Bug A — bridge target not found)

```
INFO:     10.42.0.21:0 - "POST /ui/redhat/rhel-ai/agentic-ci/strat-pipeline/-/pipelines HTTP/1.1" 500 Internal Server Error
ERROR:    Exception in ASGI application
Traceback (most recent call last):
  File "/app/app/web/routes.py", line 4202, in nested_repo_page
    pipeline = await _create_pipeline(
               ^^^^^^^^^^^^^^^^^^^^^^^
  File "/app/app/api/pipelines.py", line 2136, in _create_pipeline
    target = await _resolve_bridge_target(project, parsed_job, body.ref, db)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/app/app/api/pipelines.py", line 1943, in _resolve_bridge_target
    raise HTTPException(
fastapi.exceptions.HTTPException: 400: Bridge job build-dashboard target project not found: redhat/rhel-ai/agentic-ci/strat-dashboard
```

## Traceback (Bug B — MissingGreenlet cascade)

```
The above exception was the direct cause of the following exception:

  File "/app/app/web/routes.py", line 4214, in nested_repo_page
    repo.name,
    ^^^^^^^^^
  File ".../sqlalchemy/orm/attributes.py", line 569, in __get__
    return self.impl.get(state, dict_)
  ...
  File ".../sqlalchemy/dialects/sqlite/aiosqlite.py", line 160, in execute
    _cursor = self.await_(self._connection.cursor())
  File ".../sqlalchemy/util/_concurrency_py3k.py", line 123, in await_only
    raise exc.MissingGreenlet(
sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called; can't call await_only() here.
Was IO attempted in an unexpected place?
(Background on this error at: https://sqlalche.me/e/20/xd2s)
```

## Root cause

In `app/web/routes.py`, `nested_repo_page` calls `_create_pipeline()` inside a try/except block. When `_resolve_bridge_target()` raises `HTTPException(400)` for a missing target project, the except handler at approximately line 4484-4489 does:

```python
except Exception as exc:
    await db.rollback()
    detail = exc.detail if hasattr(exc, "detail") else str(exc)
    return RedirectResponse(
        url=f"/ui/{owner}/{repo.name}/-/pipelines/new?{urlencode({'error': detail})}",
        status_code=302,
    )
```

The `repo.name` access triggers a lazy load on the SQLAlchemy ORM object. After `db.rollback()` in an async context where the greenlet has been disrupted by the exception propagation through Starlette's BaseHTTPMiddleware stack, this lazy load cannot execute — resulting in `MissingGreenlet`.

## Suggested fix

Two changes needed:

1. **Eagerly load `repo.name` before the try block** (or use `repo.full_name` which was already loaded by `_resolve_repo_and_remainder`):
   ```python
   repo_name = repo.name  # eagerly capture before entering try block
   ```
   Then use `repo_name` in the except handler's redirect URL.

2. **Alternatively, use `repo.full_name`** which is already loaded. The redirect URL can be built from `repo.full_name` directly:
   ```python
   url=f"/ui/{repo.full_name}/-/pipelines/new?{urlencode({'error': detail})}"
   ```

A similar pattern should be audited across all except handlers in `nested_repo_page` that access `repo` attributes after `db.rollback()`.
