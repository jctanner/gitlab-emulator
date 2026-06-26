# GitLab Runner Deployment Guide

This document explains the supported runner deployment methods for integrating
the GitLab emulator into a larger k3s stack. It is intended for agents or
operators wiring this project into an environment beyond the local Vagrant
validation stack.

## Coordinator Model

The emulator acts as a minimal GitLab Runner coordinator. Runners execute jobs;
the emulator does not run CI scripts itself.

Runner flow:

1. A runner registers with the emulator.
2. The runner stores an emulator-issued runner authentication token.
3. The runner polls `POST /api/v4/jobs/request`.
4. The emulator returns an eligible pending job.
5. The runner executes that job with its configured executor.
6. The runner streams trace chunks, status updates, artifacts, and cache traffic
   back to the emulator.

Primary coordinator endpoints:

- `POST /api/v4/runners`
- `POST /api/v4/runners/verify`
- `DELETE /api/v4/runners`
- `POST /api/v4/jobs/request`
- `PATCH /api/v4/jobs/:job_id/trace`
- `PUT /api/v4/jobs/:job_id`
- `POST /api/v4/jobs/:job_id/artifacts`
- `GET /api/v4/projects/:project_id/jobs/:job_id/artifacts`
- `PUT /api/v4/projects/:project_id/cache/:cache_key`
- `GET /api/v4/projects/:project_id/cache/:cache_key`
- `HEAD /api/v4/projects/:project_id/cache/:cache_key`

The emulator currently accepts this registration token by default:

```text
runner-registration-token
```

The first registered runner receives the backward-compatible static runner
authentication token:

```text
glrt-emulator-runner-token
```

Each later registration receives its own persisted `glrt-...` token. This is
important for full stacks: do not assume all runners share one token.

## Network and Trust Requirements

All runner deployment methods need these properties:

- The runner manager process can reach the emulator URL.
- Job execution environments can reach the emulator URL for Git clone/fetch,
  artifacts, traces, and cache.
- The runner manager trusts the emulator TLS certificate.
- Job execution environments trust the emulator TLS certificate when they use
  HTTPS clone/fetch or artifact/cache calls.
- Runner tags align with job tags.

The Vagrant validation stack uses:

```text
https://glemu.local -> 192.168.124.10
```

In a full k3s stack, prefer real cluster DNS or an ingress hostname. If a local
host alias is used, it must be applied to both runner manager pods and job pods.

## Supported Deployment Methods

### 1. Docker Executor Runner on a VM

This is the baseline official runner validation path.

Topology:

```text
runner VM
  gitlab-runner service
    executor = docker
    creates Docker containers for CI jobs
```

Current Vagrant target:

```bash
make vm-runner-register RUNNER_TOKEN=runner-registration-token
make vm-runner-validate
```

Registration helper:

```text
scripts/register-runner.sh
```

Default tags:

```text
aipcc-small-x86_64,vm,docker,podman
```

Use this when the full stack wants VM-level isolation or Docker/Podman behavior
similar to the existing agentic CI job image.

### 2. Kubernetes Executor Runner as a VM Service

This runner process runs on a host or VM, but its jobs run as Kubernetes pods.

Topology:

```text
k8s-runner VM or host
  gitlab-runner service
    executor = kubernetes
    talks to Kubernetes API

k3s cluster
  CI job pods
```

Validated target:

```bash
make vm-k8s-runner-up
make vm-k8s-runner-register RUNNER_TOKEN=runner-registration-token
make vm-k8s-runner-validate
```

Registration helper:

```text
scripts/register-k8s-runner.sh
```

Default runner settings:

```text
name = glemu-k8s-runner
executor = kubernetes
namespace = gitlab-runner
tags = k8s
run_untagged = false
image = alpine:3.20
```

This mode needs Kubernetes API credentials on the runner host. In the Vagrant
stack, the helper extracts k3s client certs from `/etc/rancher/k3s/k3s.yaml`
and writes them under `/etc/gitlab-runner/k3s/`.

The helper also adds a Kubernetes executor host alias so job pods can resolve
the emulator:

```toml
[[runners.kubernetes.host_aliases]]
  ip = "192.168.124.10"
  hostnames = ["glemu.local"]
```

For a real cluster, replace this with proper DNS when possible.

### 3. Kubernetes Executor Runner Inside k3s

This is the production-shaped Kubernetes mode. The runner manager itself runs
as a Kubernetes Deployment, and each CI job still runs as a separate pod.

Topology:

```text
k3s cluster
  Deployment: glemu-k8s-incluster-runner
    official gitlab/gitlab-runner image
    executor = kubernetes
    polls emulator coordinator API

  CI job pods
    created by the runner manager pod
```

Validated target:

```bash
make vm-k8s-incluster-deploy RUNNER_TOKEN=runner-registration-token
make vm-k8s-incluster-validate
```

Deployment helper:

```text
scripts/deploy-incluster-k8s-runner.sh
```

Default runner settings:

```text
name = glemu-k8s-incluster-runner
manager image = gitlab/gitlab-runner:v19.1.1
executor = kubernetes
namespace = gitlab-runner-incluster
tags = k8s-incluster
run_untagged = false
job image = alpine:3.20
```

The helper creates:

- namespace `gitlab-runner-incluster`
- service account `glemu-k8s-incluster-runner`
- role and role binding for pod/configmap/secret/service/event access
- configmap containing `config.toml`
- secret containing the emulator CA certificate
- deployment `glemu-k8s-incluster-runner`

The manager pod mounts:

```text
/etc/gitlab-runner/config.toml
/etc/gitlab-runner/certs/glemu.local.crt
```

The Deployment also sets `hostAliases` for the runner manager pod, and the
runner config sets Kubernetes executor `host_aliases` for job pods.

## Registration Process Details

Official `gitlab-runner register` is used for all runner types. Use the legacy
registration-token flow for this emulator:

```bash
gitlab-runner register --non-interactive \
  --url https://glemu.local \
  --registration-token runner-registration-token \
  --name <runner-name> \
  --executor <executor> \
  --tag-list <tags> \
  --run-untagged=false \
  --locked=false
```

Do not pass the registration token as the final stored runner token. The
official registration exchange must call `POST /api/v4/runners`, then store the
emulator-issued `glrt-...` token from the response.

Repeated registrations create new runner records. Use the admin UI to pause or
delete stale registrations:

```text
https://glemu.local/admin/runners
```

Runner records can also be inspected through:

```bash
curl -sk https://glemu.local/api/v4/runners
curl -sk https://glemu.local/api/v4/runners/<runner_id>
curl -sk https://glemu.local/api/v4/runners/<runner_id>/jobs
```

## Tags and Scheduling

Use tags to route jobs deliberately:

```yaml
docker_probe:
  tags:
    - docker
  script:
    - echo docker runner

k8s_probe:
  tags:
    - k8s
  script:
    - echo VM-service Kubernetes runner

k8s_incluster_probe:
  tags:
    - k8s-incluster
  script:
    - echo in-cluster Kubernetes runner
```

Recommended defaults:

- Docker VM runner: `aipcc-small-x86_64,vm,docker,podman`
- Kubernetes VM-service runner: `k8s`
- Kubernetes in-cluster runner: `k8s-incluster`

For integration stacks, set `run_untagged=false` on specialized runners. That
prevents an old or stale runner from taking jobs that should target a specific
executor.

## TLS and CA Integration

The emulator uses Caddy TLS in the Vagrant stack. The current CA install targets
copy Caddy's local root from the server container:

```bash
make vm-runner-install-ca
make vm-k8s-runner-install-ca
```

For a full k3s stack, choose one of these approaches:

- use a real certificate chain trusted by the runner manager and job images;
- mount the emulator CA into runner manager pods and configure
  `tls-ca-file`;
- inject the emulator CA into custom job images;
- configure runner Kubernetes executor volume mounts for CA distribution if
  generic job images must trust the emulator.

Name resolution and CA trust must be solved separately. A host alias makes
`glemu.local` resolve; it does not make TLS trusted.

## Artifacts, Trace, and Cache

All runner modes use the same emulator API surfaces for:

- trace append via `PATCH /api/v4/jobs/:id/trace`;
- final/intermediate state updates via `PUT /api/v4/jobs/:id`;
- artifact upload via `POST /api/v4/jobs/:id/artifacts`;
- artifact download via project job artifact routes;
- cache upload/download via project cache routes.

The Docker executor runner validation also configures S3-compatible cache
settings against server-side MinIO. Kubernetes cache validation can reuse the
same emulator cache endpoints, but a production stack should decide whether
runner cache should point to emulator MinIO, cluster object storage, or another
S3-compatible service.

## Validation Commands

Baseline Docker executor:

```bash
make vm-runner-validate
make vm-ci-lab-smoke
```

Kubernetes executor with runner on VM:

```bash
make vm-k8s-runner-validate
```

Kubernetes executor with runner manager in k3s:

```bash
make vm-k8s-incluster-validate
```

Useful inspection commands:

```bash
make vm-runner-status
make vm-k8s-runner-status
make vm-k8s-runner-pods
make vm-k8s-incluster-status
make vm-k8s-incluster-pods
make vm-k8s-incluster-logs
```

Validated versions in the current Vagrant stack:

- Docker executor runner: GitLab Runner `19.0.1`
- Kubernetes executor runner: GitLab Runner `19.1.1`
- In-cluster runner manager image: `gitlab/gitlab-runner:v19.1.1`
- k3s: `v1.35.5+k3s1`

## Full k3s Stack Integration Checklist

Use this checklist when moving from Vagrant to a larger k3s deployment:

1. Expose the emulator at a stable HTTPS URL.
2. Make that URL resolvable from runner manager pods and job pods.
3. Make the emulator CA trusted by runner manager pods and job pods, or use a
   publicly trusted certificate.
4. Choose runner modes:
   - Docker executor on VM for Docker/Podman workloads.
   - Kubernetes executor on host/VM for simple external manager validation.
   - Kubernetes executor in-cluster for production-shaped k3s integration.
5. Register each runner with `runner-registration-token` or the configured
   emulator registration token.
6. Store each returned `glrt-...` runner token in that runner's config or
   Kubernetes secret/config.
7. Assign unique tags per runner class.
8. Set `run_untagged=false` for specialized runners.
9. Validate one tagged job per runner class.
10. Use `/admin/runners` to pause/delete stale registrations.
11. Confirm traces, final status, artifacts, and cache behavior.

## Known Gaps

- Registration uses the legacy registration-token workflow because it is enough
  for current official runner validation.
- The in-cluster helper is a minimal manifest generator, not a full Helm chart.
- Repeated registration intentionally creates a new runner record; cleanup is
  operator-driven through `/admin/runners`.
- Job pod CA trust is simple in the validation scripts. A full stack may need a
  more systematic CA injection strategy for arbitrary job images.
- Exact GitLab Runner fleet management APIs are not fully implemented; the
  admin UI and minimal inspection endpoints are the current operator surface.
