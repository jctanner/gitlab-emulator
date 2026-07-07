# Docker Compose Stack Runbook

This runbook covers the local Docker Compose stack from this repository. Use it
when you want to run the emulator directly from the working tree without the
Vagrant client, Docker runner, or k3s runner VMs.

## What Compose Starts

`docker-compose.yml` starts:

| Service | Purpose |
|---|---|
| `gitlab-emulator` | FastAPI app, Git HTTP/SSH transport, Caddy TLS, web UI, admin UI |
| `minio` | S3-compatible object store for optional GitLab Runner cache validation |
| `minio-init` | One-shot bucket initializer for `gitlab-runner-cache` |

The emulator does not require MinIO for normal API, web UI, Git, trace, or
artifact storage. MinIO exists for the official GitLab Runner S3 cache path.

## Ports And URLs

| Port | URL | Purpose |
|---:|---|---|
| `8000` | `http://localhost:8000/api/v4` | Direct emulator API, bypassing Caddy |
| `8000` | `http://localhost:8000/ui/` | Web UI |
| `8000` | `http://localhost:8000/admin/` | Admin UI |
| `80` | `http://localhost/` | Caddy HTTP listener |
| `443` | `https://glemu.local/` | Caddy HTTPS listener with local CA |
| `2222` | `ssh://git@localhost:2222/...` | Git SSH transport |
| `9000` | `http://localhost:9000` | MinIO S3 API |
| `9001` | `http://localhost:9001` | MinIO console |

Default admin credentials are:

```text
admin / admin
```

Default MinIO credentials are:

```text
glemu / glemu-cache-secret
```

## First Start

Build and start the stack:

```bash
make up
```

Equivalent direct command:

```bash
docker compose up -d --build
```

Confirm the API is reachable:

```bash
curl -sf http://localhost:8000/api/v4
```

Open the UI:

```text
http://localhost:8000/ui/
http://localhost:8000/admin/
```

## Hostname And TLS

The Compose defaults advertise the emulator as:

```text
https://glemu.local
```

For browser or CLI traffic through Caddy TLS, add a host entry:

```bash
sudo sh -c 'printf "%s\n" "127.0.0.1 glemu.local" >> /etc/hosts'
```

Caddy generates an internal CA inside the `gitlab_emulator_data` volume. Tools
that use `https://glemu.local` need to trust that CA, or they need to use the
direct HTTP endpoint on `localhost:8000`.

For most local API checks, prefer the direct endpoint:

```text
http://localhost:8000/api/v4
```

Use `https://glemu.local` when validating clients that need realistic TLS or
GitLab Runner behavior.

## Day-To-Day Commands

Start or refresh containers while preserving volumes:

```bash
make up
```

Tail logs:

```bash
make logs
```

Run the local smoke test against the running stack:

```bash
make smoke
```

Rebuild and restart while preserving volumes:

```bash
make restart
```

Stop containers while preserving volumes:

```bash
docker compose stop
```

Start stopped containers again:

```bash
docker compose start
```

## Data And Reset Behavior

Persistent state lives in Docker volumes:

| Volume | Contains |
|---|---|
| `gitlab_emulator_data` | SQLite DB, repositories, artifacts, traces, Caddy local CA |
| `gitlab_emulator_minio_data` | MinIO cache bucket data |

Use a non-destructive stop when you want to keep local state:

```bash
docker compose stop
```

Use a destructive reset only when you intentionally want fresh state:

```bash
make reset
```

`make down` is also destructive in this repository because it runs:

```bash
docker compose down --volumes
```

That removes the emulator database, repositories, artifacts, Caddy CA, and
MinIO cache data.

## Basic API Workflow

Create a personal access token:

```bash
curl -sf -X POST http://localhost:8000/admin/tokens \
  -u admin:admin \
  -H "Content-Type: application/json" \
  -d '{"login":"admin","name":"local-token","scopes":["repo","user"]}'
```

Set the returned token:

```bash
TOKEN=<returned-token>
```

Check the current user:

```bash
curl -sf -H "Authorization: token $TOKEN" \
  http://localhost:8000/api/v4/user
```

Create a project:

```bash
curl -sf -X POST http://localhost:8000/api/v4/user/repos \
  -H "Authorization: token $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"name":"compose-smoke","description":"Compose smoke project"}'
```

Clone over Git HTTP:

```bash
git clone http://localhost:8000/admin/compose-smoke.git /tmp/compose-smoke
```

Push with token authentication:

```bash
cd /tmp/compose-smoke
git checkout -b main
printf "%s\n" "# Compose smoke" > README.md
git add README.md
git -c user.name="Compose Smoke" -c user.email="compose-smoke@example.test" \
  commit -m "Initial commit"
git push http://admin:$TOKEN@localhost:8000/admin/compose-smoke.git main
```

## Logs And Inspection

All service logs:

```bash
docker compose logs --tail=100
```

Emulator logs:

```bash
docker compose logs --tail=100 gitlab-emulator
```

MinIO logs:

```bash
docker compose logs --tail=100 minio minio-init
```

Container status:

```bash
docker compose ps
```

Shell inside the emulator container:

```bash
docker compose exec gitlab-emulator sh
```

## MinIO And Runner Cache

MinIO is only needed when testing GitLab Runner distributed cache behavior. The
standard app path stores CI artifacts under the emulator data directory, not in
MinIO.

The default runner cache settings used by the VM runner path are:

```text
RUNNER_CACHE_TYPE=s3
RUNNER_CACHE_S3_SERVER_ADDRESS=glemu.local:9000
RUNNER_CACHE_S3_ACCESS_KEY=glemu
RUNNER_CACHE_S3_SECRET_KEY=glemu-cache-secret
RUNNER_CACHE_S3_BUCKET_NAME=gitlab-runner-cache
RUNNER_CACHE_S3_INSECURE=true
RUNNER_CACHE_S3_PATH_STYLE=true
```

The k3s runner paths do not currently configure MinIO/S3 cache.

## Common Issues

### Port Conflicts

If ports `80`, `443`, `8000`, `2222`, `9000`, or `9001` are already in use,
Compose startup can fail. Check listeners:

```bash
ss -ltnp
```

Change the host-side port mapping in `docker-compose.yml`, or stop the service
using the conflicting port.

### TLS Trust Errors

Use `http://localhost:8000` for local API checks when TLS trust is not part of
the test. If a client must use `https://glemu.local`, install or point the
client at Caddy's generated local root CA from the emulator data volume.

### Stale Or Confusing State

If a project, runner, token, or repository behaves differently than expected,
first inspect the current data through the admin UI:

```text
http://localhost:8000/admin/
```

When you intentionally need a clean environment:

```bash
make reset
```

Remember that reset deletes all local Compose state.
