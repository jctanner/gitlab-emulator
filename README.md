# GitLab Emulator

A lightweight, self-contained emulator of the GitLab API designed for integration testing.
Run it locally or in CI to exercise client libraries, `glab` CLI workflows, and automation
scripts without touching real GitLab.

> Scaffold status: this project currently preserves the architecture of
> `github_emulator` and has been renamed for GitLab. GitLab-specific REST
> routes, response schemas, and CI pipeline APIs are the next implementation
> work. See `STATUS.md` and `GITLAB_STATUS.md`.

## Features

- **REST API** -- scaffolded under GitLab REST API v4 paths
- **GraphQL API** -- Strawberry-based implementation of common GitLab GraphQL queries and mutations
- **Git Smart HTTP** -- clone, fetch, and push over HTTP/HTTPS against bare repositories
- **Git SSH Transport** -- clone and push over SSH (port 2222 by default)
- **Web UI** (`/ui/`) -- browse repositories, files, commits, issues, and merge-request-style review objects
- **Admin Panel** (`/admin/`) -- manage users, tokens, organisations, repositories, imports, and CI Lab job experiments
- **GitLab Import** -- clone a single repo by URL or bulk-import all repos from a GitLab user/org via the admin panel
- **Webhooks** -- event delivery with recorded payloads
- **`glab` CLI Target** -- intended to become compatible with GitLab CLI workflows
- **TLS via Caddy** -- automatic HTTPS with a local CA for realistic `glab`/git testing
- **SQLite + aiosqlite** -- zero-dependency storage; no external database server required

## Quick Start

### Docker Compose (recommended)

```bash
make up
# or:
docker compose up -d
```

The server will be available at:

| Endpoint | URL |
|---|---|
| REST API | `http://localhost:8000/api/v4` |
| Web UI | `http://localhost:8000/ui/` |
| Admin Panel | `http://localhost:8000/admin/` |
| GraphQL | `http://localhost:8000/api/graphql` |

Default admin credentials: `admin` / `admin`.

### Vagrant (three-VM setup with TLS and runner)

For full `glab` CLI and GitLab Runner validation with TLS, a Vagrantfile provisions:

| VM | IP | Purpose |
|---|---:|---|
| `server` | `192.168.124.10` | Runs the emulator in Docker |
| `client` | `192.168.124.11` | Runs `glab` and git smoke tests |
| `runner` | `192.168.124.12` | Runs official `gitlab-runner` with a privileged Docker executor |

```bash
# Add the hostname to /etc/hosts
echo "192.168.124.10  glemu.local" | sudo tee -a /etc/hosts

# Boot VMs, sync code, build, and start without resetting data
make vm-deploy

# The server is now reachable at https://glemu.local
```

`make vm-deploy` preserves Docker volumes, including emulator data, MinIO cache
data, and Caddy's local CA. Use `make vm-deploy-reset` only when you explicitly
want a fresh database and regenerated service volumes.

Once the emulator implements runner coordinator endpoints and exposes a runner
token, register the runner VM:

```bash
make vm-runner-install-ca
make vm-runner-register RUNNER_TOKEN=runner-registration-token
make vm-runner-status
make vm-runner-cache-test
make vm-runner-artifact-needs-test
```

The runner registration helper defaults to `https://glemu.local`. The
`vm-runner-install-ca` target copies Caddy's local root CA from the emulator
container into the runner VM so the official runner can verify TLS.
Registration also defaults to GitLab Runner distributed cache over S3, backed by
the MinIO service exposed from the server VM at `glemu.local:9000`. Set
`RUNNER_CACHE_TYPE=` when running `make vm-runner-register` to disable cache
configuration, or override the `RUNNER_CACHE_S3_*` variables for another
S3-compatible backend. The helper replaces the runner VM's local
`/etc/gitlab-runner/config.toml` by default so stale no-cache registrations do
not keep polling; set `RUNNER_REPLACE_EXISTING=false` to append instead.

Current runner validation scope: the emulator accepts runner registration,
verification, unregister, no-job polling, persisted job assignment, persisted
single-job pipelines, minimal `.gitlab-ci.yml` parsing, stage-ordered multi-job
pipelines, CI job-token repository checkout, trace upload, status updates, and
artifact upload/persistence/download. Stage-gated scheduling is implemented for
persisted jobs. Minimal `needs`, optional missing needs, missing required needs
validation, `needs:artifacts`, and common ref filters are implemented for
persisted jobs. Runner tag matching is implemented for persisted jobs. Cache
metadata parsing, emulator cache archive upload/download endpoints, and VM
runner S3 cache upload/restore through MinIO are implemented and validated.
Richer GitLab CI YAML semantics are the next CI slices.

## Development Setup

```bash
# Create a virtual environment and install dependencies
uv venv
uv pip install -e ".[dev]"

# Run the test suite
uv run pytest tests/ -v

# Start the server locally (without Docker)
uv run uvicorn app.main:app --reload
```

## Configuration

All settings are driven by environment variables with the `GITLAB_EMULATOR_` prefix:

| Variable | Default | Description |
|---|---|---|
| `GITLAB_EMULATOR_BASE_URL` | `http://localhost:8000` | Base URL used in API response URLs |
| `GITLAB_EMULATOR_DATA_DIR` | `./data` | Directory for bare git repos and the SQLite DB |
| `GITLAB_EMULATOR_DATABASE_URL` | `sqlite+aiosqlite:///{DATA_DIR}/gitlab_emulator.db` | SQLAlchemy database URL |
| `GITLAB_EMULATOR_SECRET_KEY` | `change-me-in-production` | Secret for JWT/session signing |
| `GITLAB_EMULATOR_ADMIN_USERNAME` | `admin` | Admin user created on first startup |
| `GITLAB_EMULATOR_ADMIN_PASSWORD` | `admin` | Admin user password |
| `GITLAB_EMULATOR_HOSTNAME` | `glemu.local` | Hostname for Caddy TLS certificate |
| `GITLAB_EMULATOR_CI_REMOTE_INCLUDE_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Comma-separated host allowlist for `include:remote` |
| `GITLAB_EMULATOR_PIPELINE_SCHEDULE_WORKER_ENABLED` | `true` | Enable automatic materialization of due pipeline schedules |
| `GITLAB_EMULATOR_PIPELINE_SCHEDULE_WORKER_INTERVAL_SECONDS` | `60.0` | Poll interval for due pipeline schedule checks |
| `GITLAB_EMULATOR_SSH_ENABLED` | `true` | Enable/disable the SSH transport |
| `GITLAB_EMULATOR_SSH_PORT` | `2222` | SSH server listen port |

## Database Migrations (Alembic)

The project uses Alembic with async SQLAlchemy for schema migrations.

```bash
# Generate a new migration after changing models
uv run alembic revision --autogenerate -m "describe the change"

# Apply all pending migrations
uv run alembic upgrade head

# Downgrade one revision
uv run alembic downgrade -1
```

## API Usage Examples

### Create a personal access token

```bash
curl -s -X POST http://localhost:8000/admin/tokens \
  -H "Content-Type: application/json" \
  -d '{"login":"admin","name":"my-token","scopes":["repo","user"]}' \
  | python3 -m json.tool
```

### Create a repository

```bash
TOKEN="<token-from-above>"

curl -s -X POST http://localhost:8000/user/repos \
  -H "Authorization: token $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"my-repo","description":"Test repo"}' \
  | python3 -m json.tool
```

### Clone and push

```bash
git clone http://localhost:8000/admin/my-repo.git /tmp/my-repo
cd /tmp/my-repo
echo "# Hello" > README.md
git add README.md && git commit -m "initial commit"
git push http://admin:$TOKEN@localhost:8000/admin/my-repo.git main
```

### Create an issue

```bash
curl -s -X POST http://localhost:8000/repos/admin/my-repo/issues \
  -H "Authorization: token $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"Bug report","body":"Something is broken"}' \
  | python3 -m json.tool
```

### Validate with `glab` CLI

Run `glab` validation inside the isolated Vagrant client VM so host `glab`,
Git, and auth configuration are not modified:

```bash
make vm-deploy
make vm-test
```

The Makefile installs a pinned `glab` release into the client VM at
`/srv/bin/glab`, installs the emulator Caddy root CA into the client VM trust
store, then the VM script writes temporary `glab` config inside the client VM
only and validates the current MVP surface with `glab api` plus Git Smart HTTP
clone/push/fetch.

For a faster CI Lab and official-runner check, run:

```bash
make vm-ci-lab-smoke
```

This syncs the smoke script to the client VM, refreshes client CA trust,
refreshes runner CA trust only if runner TLS verification fails, creates or
reuses the `ci-lab-smoke` project, writes `.gitlab-ci.yml`, creates a pipeline,
waits for the official runner to execute the job, checks trace markers and
artifact metadata, and prints the admin CI Lab URL.

## Makefile Targets

### Docker (local)

| Target | Description |
|---|---|
| `build` | Build the Docker image |
| `up` | Build and start the container, preserving volumes |
| `down` | Stop and remove containers and volumes |
| `reset` | Reset local containers and volumes, then start fresh |
| `restart` | Rebuild and restart, preserving volumes |
| `logs` | Tail container logs |
| `test` | Run pytest locally |
| `smoke` | End-to-end smoke test against the running server |
| `clean` | Remove containers, images, and build artifacts |

### Vagrant

| Target | Description |
|---|---|
| `vm-up` | Boot the server, client, and runner VMs |
| `vm-deploy` | Sync, build, and start containers in the server VM, preserving volumes |
| `vm-deploy-reset` | Sync, build, reset server VM volumes, and start fresh |
| `vm-validate` | Deploy and run client plus official runner VM validation |
| `vm-validate-current` | Validate the currently deployed server with client and runner VMs |
| `vm-sync` | Rsync the codebase into the server VM |
| `vm-build` | Build the container image inside the server VM |
| `vm-start` | Start containers inside the server VM, preserving volumes |
| `vm-reset` | Reset server VM containers and volumes, then start fresh |
| `vm-stop` | Stop containers inside the server VM |
| `vm-logs` | Tail container logs inside the server VM |
| `vm-destroy` | Destroy all VMs |
| `vm-ssh` | SSH into the server VM |
| `vm-client-ssh` | SSH into the client VM |
| `vm-runner-sync` | Rsync runner helper scripts to the runner VM |
| `vm-runner-ssh` | SSH into the runner VM |
| `vm-runner-status` | Show GitLab Runner status on the runner VM |
| `vm-runner-cache-config` | Show GitLab Runner distributed cache config |
| `vm-runner-ensure-ca` | Install runner CA only when runner TLS verification fails |
| `vm-runner-install-ca` | Install the emulator Caddy root CA into the runner VM |
| `vm-runner-register` | Register the runner VM with the emulator |
| `vm-runner-validate` | Validate official runner registration, variables, rules, extends, includes, cache, and `needs:artifacts` |
| `vm-runner-variable-test` | Validate official runner CI variable precedence and metadata |
| `vm-runner-include-test` | Validate official runner local, project, remote, and template CI includes |
| `vm-ci-lab-smoke` | Validate CI Lab pipeline/job execution through the official runner |
| `vm-test` | Install `glab` and run CLI integration tests from the client VM |
| `vm-git-test` | Run git CLI integration tests from the client VM |
| `vm-glab` | Quick `glab api user` from the client VM |
| `vm-client-install-ca` | Install the emulator Caddy root CA into the client VM |
| `vm-runner-cache-test` | Validate official runner cache upload/restore through MinIO |
| `vm-runner-artifact-needs-test` | Validate official runner `needs:artifacts` downloads |

## Project Structure

```
app/
  api/            # REST API route handlers
  admin/          # Admin panel (Jinja2 templates, static assets, routes)
  git/            # Git Smart HTTP and SSH transport
  graphql/        # Strawberry GraphQL schema, queries, mutations, types
  middleware/     # FastAPI middleware (auth, rate limiting, ETag, error handling)
  models/         # SQLAlchemy ORM models
  schemas/        # Pydantic request/response schemas
  services/       # Business-logic layer (CI YAML parsing, import, webhooks, search, etc.)
  web/            # Web UI (Jinja2 templates with Primer CSS)
  config.py       # Settings (env-driven via pydantic-settings)
  database.py     # Async engine, session factory, Base
  main.py         # Application entrypoint
alembic/          # Database migration scripts
tests/            # Pytest test suite (242 tests)
scripts/          # Integration test scripts for glab/git CLI
Dockerfile
docker-compose.yml
Caddyfile
supervisord.conf  # Runs Caddy + Uvicorn inside the container
Vagrantfile       # Three-VM dev environment (server + client + runner)
Makefile
pyproject.toml
```

## Important Note

This project is intended **for integration testing only**. It implements enough
of the GitLab API surface to exercise client libraries, CI tooling, and
automation scripts in isolated environments. It is **not** a production-grade
GitLab replacement and should never be exposed to untrusted networks.
