# Bug: Code Viewer and Editors Do Not Show Syntax Highlighting

## Summary

The repository blob viewer and web editors do not visibly render GitLab-like
syntax highlighting or token coloring.

## Reproduction

1. Open a source file or `.gitlab-ci.yml` in the repository blob viewer.
2. Open the file editor or the pipeline editor for the same file.
3. Inspect the rendered code.

## Expected

Code views and code editors show syntax-aware coloring for common file types,
including YAML and shell scripts. The experience should resemble GitLab's code
viewer/editor enough that comments, strings, keys, keywords, and literals are
visually distinguishable.

## Actual

The blob viewer renders plain escaped text lines:

```html
<span class="blob-code-inner">{{ line }}</span>
```

The editors are backed by textareas, with a YAML-only CodeMirror loader present
for some forms. In the deployed UI, the requested syntax coloring is not visible
in the code viewer or editors.

## Impact

Medium. The emulator remains usable, but repository editing and CI YAML editing
are materially worse than GitLab for inspection, review, and safe edits.

## Likely Files

- `app/web/templates/blob.html`
- `app/web/templates/edit_file.html`
- `app/web/templates/new_file.html`
- `app/web/templates/repo_pipeline_editor.html`
- `app/web/static/js/codemirror-yaml.js`
- `app/web/static/css/web.css`
- `tests/test_web_ui.py`

## Notes

Current implementation appears partial:

- `repo_pipeline_editor.html` and YAML file editing include
  `/ui/static/js/codemirror-yaml.js`.
- Blob viewing does not use a syntax highlighter.
- New file editing does not appear to opt into CodeMirror by filename.
- The CodeMirror loader imports modules from a CDN, so local/offline or
  restricted-network deployments may silently fall back to textarea behavior.

The fix should include a local or reliably bundled highlighting path and
Playwright verification that token elements/styles are present in both viewer
and editor pages.
