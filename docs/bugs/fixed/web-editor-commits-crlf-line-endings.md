# Bug: Web File Editor Commits CRLF Line Endings

**Date observed:** 2026-07-01
**Date fixed:** 2026-07-01
**Severity:** High
**Component:** `app/web/routes.py`

## Summary

Repository file edits made through the GitLab emulator web UI can commit text
with CRLF line endings. Shell scripts edited through the browser can then fail
under Bash because tokens include a trailing carriage return.

## Reproduction

1. Open a shell script in a project through the emulator web UI file editor.
2. Edit and commit the file from the browser.
3. Run the script in a CI job.
4. Observe Bash parse errors such as:

   ```text
   set: pipefail\r: invalid option name
   ```

## Expected

Web file editors normalize submitted text content to LF before writing Git
blobs, matching normal Unix source file expectations for scripts and CI files.

## Actual

The web routes write textarea form content directly to the repository with:

```python
content=content.encode("utf-8")
```

Browser form submission can provide textarea newlines as CRLF, so those CRLFs
are committed as-is.

## Impact

High for CI usability. Editing a shell script through the emulator can break a
previously valid job with non-obvious line-ending errors.

## Likely Files

- `app/web/routes.py`
- `app/web/templates/edit_file.html`
- `app/web/templates/new_file.html`
- `app/web/templates/repo_pipeline_editor.html`
- `tests/test_web_ui.py`

## Notes

## Resolution

Fixed in `app/web/routes.py`.

- Added `_normalize_editor_content()` to convert CRLF and bare CR to LF.
- Applied it only to browser editor write paths before `write_file`.
- Covered direct and nested repository file creation, direct and nested file
  editing, and direct and nested pipeline editor saves.
- Git Smart HTTP pushes continue to preserve bytes exactly.

## Verification

- Added `tests/test_web_ui.py::test_web_editor_content_normalizes_crlf_before_git_writes`.
- Verified there are no remaining raw `content=content.encode("utf-8")` web
  editor writes in `app/web/routes.py`.

## Notes

The normalization helper is:

```python
def _normalize_editor_content(content: str) -> str:
    return content.replace("\r\n", "\n").replace("\r", "\n")
```
