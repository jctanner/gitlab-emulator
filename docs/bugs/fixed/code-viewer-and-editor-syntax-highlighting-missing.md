# Fixed: Code Viewer and Editors Do Not Show Syntax Highlighting

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

## Actual Before Fix

The blob viewer renders plain escaped text lines:

```html
<span class="blob-code-inner">{{ line }}</span>
```

The editors are backed by textareas, with a YAML-only CodeMirror loader present
for some forms. In the deployed UI, the requested syntax coloring is not visible
in the code viewer or editors.

Playwright verification showed the CodeMirror modules were fetched, but editor
startup failed with:

```text
Unrecognized extension value in extension set. This sometimes happens because
multiple instances of @codemirror/state are loaded.
```

The loader mixed the aggregate `codemirror` package with separately imported
CodeMirror packages from the CDN. That produced more than one
`@codemirror/state` instance, so CodeMirror rejected the extension set and fell
back to the textarea.

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

## Resolution

- Replaced the aggregate `codemirror` import with direct CodeMirror package
  imports pinned to a shared dependency graph.
- Built the editor setup from those direct imports so every extension uses the
  same `@codemirror/state` instance.
- Added a CodeMirror decoration-based YAML token highlighter for visible key,
  string, literal, number, and comment colors. This keeps highlighting
  deterministic even when CDN module graph differences prevent the parser
  highlighter metadata from producing spans.
- Added read-only CodeMirror rendering for YAML repository blob views while
  keeping the plain `<pre>` text as a fallback.
- Added a static regression test that guards the dependency graph and YAML
  viewer/editor hooks.

## Notes

The current implementation remains intentionally YAML-focused:

- `repo_pipeline_editor.html` and YAML file editing include
  `/ui/static/js/codemirror-yaml.js`.
- YAML blob viewing uses the same loader in read-only mode.
- New file editing still starts as a plain textarea because the target filename
  is not known until the user types it.
- The CodeMirror loader still imports modules from a CDN, so local/offline or
  restricted-network deployments can fall back to plain text.
