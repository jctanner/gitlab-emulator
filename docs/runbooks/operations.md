# GitLab Emulator Operations Runbook

This runbook is for the Vagrant validation stack:

- `server`: emulator, Caddy TLS, Docker Compose, MinIO
- `client`: CLI and `glab` validation
- `runner`: official GitLab Runner with Docker executor
- `k8s-runner`: official GitLab Runner with Kubernetes executor and local k3s

## Normal Deploys

Use the non-destructive deploy path for normal iteration:

```bash
make vm-deploy
```

This syncs code, rebuilds the emulator image, and recreates containers while
preserving Docker volumes. It preserves the SQLite database, repositories,
artifacts, MinIO data, and Caddy local CA.

Use a destructive reset only when you intentionally want fresh server-side
state:

```bash
make vm-deploy-reset
```

After a reset, reinstall runner trust before validating runner jobs:

```bash
make vm-runner-install-ca
make vm-k8s-runner-install-ca
```

## Fast Operational Smoke

Use the CI Lab smoke for a quick deploy check:

```bash
make vm-ci-lab-smoke
```

This is intentionally narrower than `make vm-validate`. It verifies that the
client can create/update a CI Lab project, create a pipeline, the official
runner can execute the job, the trace contains expected markers, and artifact
metadata is recorded.

Use full VM validation before larger handoffs:

```bash
make vm-validate
```

Use Kubernetes executor validation when changing runner coordinator behavior or
Kubernetes runner configuration:

```bash
make vm-k8s-runner-up
make vm-k8s-runner-validate
```

## Recovery Checklist

### Runner TLS Failures

Symptoms:

- CI jobs remain pending even though the runner service is running.
- Runner logs mention certificate verification or unknown authority errors.

Check:

```bash
make vm-runner-status
make vm-k8s-runner-status
vagrant ssh runner -c "sudo journalctl -u gitlab-runner -n 100 --no-pager"
vagrant ssh k8s-runner -c "sudo journalctl -u gitlab-runner -n 100 --no-pager"
```

Recover:

```bash
make vm-runner-install-ca
make vm-runner-register
make vm-k8s-runner-install-ca
make vm-k8s-runner-register
make vm-ci-lab-smoke
make vm-k8s-runner-validate
```

For fast smoke runs, `make vm-ci-lab-smoke` calls `vm-runner-ensure-ca`, which
only reinstalls the CA when runner-side verification of `https://glemu.local`
fails.

### Runner Registration Token Mismatch

Symptoms:

- `make vm-runner-register` fails.
- Runner logs or command output show forbidden registration responses.
- `/api/v4/runners/verify` does not accept the token in runner config.

Check the expected emulator registration token:

```bash
grep RUNNER_REGISTRATION_TOKEN .env 2>/dev/null || true
```

The default validation registration token is:

```text
runner-registration-token
```

Recover with the token expected by the current emulator:

```bash
make vm-runner-register RUNNER_TOKEN=runner-registration-token
make vm-runner-status
make vm-k8s-runner-register RUNNER_TOKEN=runner-registration-token
make vm-k8s-runner-status
```

### Docker Image Pull Failures

Symptoms:

- Jobs move to `running` and then fail before scripts run.
- Runner logs mention image pull failures, authentication, rate limits, or
  missing images.

Check:

```bash
vagrant ssh runner -c "sudo journalctl -u gitlab-runner -n 150 --no-pager"
vagrant ssh runner -c "docker images"
```

Recover by using cached/authenticated images, local mirrors, or a CI image that
already exists on the runner VM. For Docker Hub rate limits, authenticate Docker
inside the runner VM or switch the job image to a registry that the runner can
pull reliably.

### Stuck Pending Jobs

Symptoms:

- Pipeline/job stays `pending`.
- Runner is registered but does not receive the job.

Check:

```bash
make vm-runner-status
curl -sk https://glemu.local/api/v4/runners
curl -sk https://glemu.local/api/v4/projects/<project_id>/pipelines/<pipeline_id>/diagnostics
```

Then open the CI Lab URL for the job and inspect runner diagnostics. Common
causes are:

- runner has not contacted the emulator
- runner is paused
- runner tags do not cover job tags
- runner has `run_untagged=false` for an untagged job
- the job is blocked by earlier stages or `needs`

Recover the cause shown by diagnostics, then rerun:

```bash
make vm-ci-lab-smoke
```

### Stale Running Jobs

Symptoms:

- Job is `running` but runner logs show no active container or the runner was
  restarted during execution.
- Trace stops growing.
- Pipeline diagnostics mark the job as stale.

Recover from CI Lab:

1. Open `/admin/ci-lab`.
2. Select the project, pipeline, and job.
3. Inspect diagnostics and trace.
4. Use `Requeue` for a pending or running job.

Requeue resets the runner-facing attempt, clears trace offsets, issues a new
job token, and returns the same job record to `pending`. GitLab-shaped clients
should use cancel followed by retry for running jobs.

## Useful Inspection Commands

Server logs:

```bash
vagrant ssh server -c "cd /srv/gitlab_emulator && docker compose logs --tail=100 gitlab-emulator"
```

Runner logs:

```bash
vagrant ssh runner -c "sudo journalctl -u gitlab-runner -n 100 --no-pager"
```

Runner status and Docker containers:

```bash
make vm-runner-status
```

Runner cache config:

```bash
make vm-runner-cache-config
```

Client-side CLI validation:

```bash
make vm-test
```
