# Task: Match GitLab Project Shell and Sidebar Layout

## Goal

Update the web UI shell so project pages visually match GitLab's page chrome
more closely.

## Context

Reference screenshots from real GitLab show:

- The top app chrome and left sidebar share a pale blue-gray background.
- The main project content sits inside a rounded white panel.
- The left sidebar has a fixed available height.
- The sidebar menu scrolls independently.
- `Help` and `Collapse sidebar` are pinned below the scrollable menu region.

The emulator currently approximates GitLab navigation but does not fully match
this shell structure.

## Acceptance Criteria

- [ ] Top chrome and left sidebar use the same pale shell background.
- [ ] Main content is presented as a rounded project panel.
- [ ] Sidebar menu scrolls independently from page content.
- [ ] `Help` and `Collapse sidebar` remain pinned below the scroll region.
- [ ] Long project pages do not push sidebar footer controls off-screen.
- [ ] Layout remains usable on narrow viewports.

## Files Likely Involved

- `app/web/templates/base.html`
- `app/web/templates/_repo_nav.html`
- `app/web/static/css/web.css`

## Status

Done

## Notes

The screenshots in `/tmp/screenshots` are local reference material and are not
checked into the repository.

Implemented shell/sidebar changes in `base.html`, `_repo_nav.html`, and
`web.css`.

Verification evidence:

- `tests/test_web_ui.py::test_gitlab_project_shell_sidebar_css_contract` passes.
- Jinja parsing passes for `base.html` and `_repo_nav.html`.
