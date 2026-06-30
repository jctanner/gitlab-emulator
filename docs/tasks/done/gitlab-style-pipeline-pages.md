# Task: Implement GitLab-Style Pipeline Pages

## Goal

Separate and refine pipeline-related web pages to more closely match GitLab's
project CI/CD UI.

## Context

Reference screenshots from real GitLab show distinct pages for:

- Pipeline list
- Run new pipeline
- Pipeline editor

The emulator currently provides functional CI views, but the pipelines page is
more of a combined list/detail inspector and the Pipeline editor left-nav item
links to the generic `.gitlab-ci.yml` file editor.

## Acceptance Criteria

- [ ] Pipelines list has GitLab-style tabs or filters for all/finished/branches/tags.
- [ ] Pipelines list shows status, pipeline metadata, creator, stage status, and actions.
- [ ] `New pipeline` opens a dedicated run form page.
- [ ] Run form supports branch/tag selection.
- [ ] Run form supports at least one ad hoc variable row.
- [ ] Pipeline editor has a dedicated page rather than only the generic file editor.
- [ ] Pipeline editor shows branch selection, status/config feedback, editor tabs, and commit controls.
- [ ] Existing API-backed pipeline creation continues to work.
- [ ] Nested project paths work for all pipeline pages.

## Files Likely Involved

- `app/web/routes.py`
- `app/web/templates/repo_pipelines.html`
- `app/web/templates/edit_file.html`
- `app/web/templates/_repo_nav.html`
- `app/web/static/css/web.css`
- `tests/test_web_ui.py`

## Status

Done

## Notes

Implemented a GitLab-shaped first pass while preserving existing functional
behavior.

Implemented pages:

- Pipeline list with scope tabs, filter row, stage dots, and new-pipeline link.
- Dedicated run-new-pipeline form with branch/tag and ad hoc variable controls.
- Dedicated pipeline editor for `.gitlab-ci.yml`.
- Dedicated pipeline/job detail template preserving job actions and trace view.

Verification evidence:

- `py_compile` passes for `app/web/routes.py`.
- Jinja parsing passes for pipeline templates.
- Targeted async pytest still hangs in SQLite fixture setup before test body
  execution in this environment.
