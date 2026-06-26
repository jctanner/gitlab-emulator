# GitLab Emulator - Project Status

Last updated: 2026-06-09

## Scaffold

- [x] Created `gitlab_emulator` as a sibling project to `github_emulator`.
- [x] Preserved the same underlying FastAPI, SQLAlchemy, SQLite, Git Smart HTTP,
  GraphQL, admin UI, Docker, and pytest architecture.
- [x] Renamed project metadata, environment variable prefix, database name,
  container service name, hostname, and documentation branding.
- [x] Mounted copied REST routers under `/api/v4`.
- [x] Verified Python syntax with `python -m compileall -q gitlab_emulator/app`.

## Not Complete Yet

- [ ] GitLab REST API route shapes and response schemas.
- [ ] GitLab group/project naming and ID/path lookup semantics.
- [ ] GitLab merge request behavior.
- [ ] GitLab CI pipeline, stage, job, official GitLab Runner coordination,
  variable, trigger, schedule, trace, and artifact behavior.
- [ ] GitLab pagination, error payloads, auth headers, webhook payloads, and
  GraphQL details.
- [ ] GitLab-specific tests and `glab` compatibility checks.

See `GITLAB_STATUS.md` for the recommended first vertical slice.
