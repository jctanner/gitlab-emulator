# k3s Stack Deployment Guide

This guide is a reference for deploying the GitLab emulator and an official
GitLab Runner into a k3s-based system. It is written for agents integrating
this repository into a larger stack, not for the local Vagrant validation path.

For runner-only details, see `docs/runbooks/runner-deployment.md`.

## Target Architecture

Use the emulator as the GitLab server/coordinator and official GitLab Runner as
the execution engine.

```text
k3s cluster
  namespace: gitlab-emulator
    Deployment/StatefulSet: gitlab-emulator
    Service: gitlab-emulator
    Ingress/HTTPRoute: https://glemu.example.test
    PVC: emulator data
    optional: MinIO or external S3-compatible cache/artifact backing

  namespace: gitlab-runner
    Deployment: gitlab-runner manager
    ServiceAccount/Role/RoleBinding
    Secret: runner token and emulator CA

  CI job pods
    created by the runner manager through the Kubernetes executor
```

Traffic flow:

1. Users, clients, and runners reach the emulator at a stable HTTPS URL.
2. The runner manager polls the emulator's `/api/v4/jobs/request` endpoint.
3. For each eligible job, the runner manager creates a CI job pod.
4. The job pod fetches source from the emulator over Git Smart HTTP.
5. The runner streams logs, status, artifacts, and cache traffic back to the
   emulator.

## Required Inputs

Decide these values before deployment:

| Setting | Example | Notes |
|---|---|---|
| External URL | `https://glemu.example.test` | Must be reachable from users, runner manager pods, and CI job pods. |
| Namespace | `gitlab-emulator` | Holds the emulator app and persistent data. |
| Runner namespace | `gitlab-runner` | Holds the official runner manager. |
| Storage class | `local-path` | Any ReadWriteOnce class is enough for a single emulator replica. |
| Data size | `20Gi` or larger | Stores SQLite DB, Git repos, traces, artifacts, and Caddy data if bundled. |
| Registration token | `runner-registration-token` | Default emulator token unless configured otherwise. |
| Runner tags | `k8s-incluster` | Use explicit tags and `run_untagged=false`. |

## Emulator Deployment

The emulator needs persistent storage for:

- SQLite database
- bare Git repositories
- traces
- artifacts
- cache data
- TLS/Caddy state if Caddy is part of the container deployment

Minimal deployment shape:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: gitlab-emulator
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: gitlab-emulator-data
  namespace: gitlab-emulator
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 20Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: gitlab-emulator
  namespace: gitlab-emulator
spec:
  replicas: 1
  selector:
    matchLabels:
      app: gitlab-emulator
  template:
    metadata:
      labels:
        app: gitlab-emulator
    spec:
      containers:
        - name: gitlab-emulator
          image: <your-registry>/gitlab-emulator:<tag>
          ports:
            - name: http
              containerPort: 8000
            - name: https
              containerPort: 443
            - name: ssh
              containerPort: 2222
          env:
            - name: GITLAB_EMULATOR_BASE_URL
              value: https://glemu.example.test
            - name: GITLAB_EMULATOR_DATA_DIR
              value: /data
            - name: GITLAB_EMULATOR_ADMIN_USERNAME
              value: admin
            - name: GITLAB_EMULATOR_ADMIN_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: gitlab-emulator-admin
                  key: password
          volumeMounts:
            - name: data
              mountPath: /data
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: gitlab-emulator-data
---
apiVersion: v1
kind: Service
metadata:
  name: gitlab-emulator
  namespace: gitlab-emulator
spec:
  selector:
    app: gitlab-emulator
  ports:
    - name: http
      port: 8000
      targetPort: http
    - name: https
      port: 443
      targetPort: https
    - name: ssh
      port: 2222
      targetPort: ssh
```

Use your cluster's ingress controller, Gateway API, or a `LoadBalancer` service
to expose the emulator. The external hostname must match the value of
`GITLAB_EMULATOR_BASE_URL`.

## TLS and DNS

Runner manager pods and CI job pods must both resolve and trust the emulator
URL.

Preferred options:

- Use a real certificate chain trusted by the runner image and common job
  images.
- Use internal cluster DNS plus an ingress certificate issued by your platform
  CA, then inject that CA into the runner manager and job pods.

Validation-only options:

- Add runner Kubernetes executor `host_aliases` for the emulator hostname.
- Mount a CA secret into the runner manager.
- Configure runner Kubernetes executor CA options or volume mounts so job pods
  can trust the emulator.

Name resolution and CA trust are separate. A host alias only solves DNS; it
does not make TLS trusted.

## Runner Manager Deployment

Use the official `gitlab/gitlab-runner` image with the Kubernetes executor.
The runner manager can run inside k3s and create separate CI job pods.

Recommended defaults:

- executor: `kubernetes`
- runner tag: `k8s-incluster`
- `run_untagged=false`
- namespace: `gitlab-runner`
- default job image: `alpine:3.20`

The emulator supports the legacy registration-token exchange. Registration
must call the emulator and store the returned `glrt-...` runner token. Do not
hard-code the registration token as the runner authentication token.

Registration flow:

```bash
gitlab-runner register --non-interactive \
  --url https://glemu.example.test \
  --registration-token runner-registration-token \
  --name glemu-k3s-runner \
  --executor kubernetes \
  --tag-list k8s-incluster \
  --run-untagged=false \
  --locked=false \
  --kubernetes-namespace gitlab-runner \
  --kubernetes-image alpine:3.20
```

For GitOps-style deployment, run registration as an init job or one-time
bootstrap step, then store the generated `/etc/gitlab-runner/config.toml` as a
Secret or ConfigMap. Re-running registration creates another runner record.
Use `/admin/runners` to pause or remove stale registrations.

## Minimal RBAC

The runner service account needs permissions to create and watch Kubernetes
executor resources. A minimal starting point:

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: gitlab-runner
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: gitlab-runner
  namespace: gitlab-runner
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: gitlab-runner
  namespace: gitlab-runner
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/exec", "pods/log", "secrets", "configmaps", "services", "events"]
    verbs: ["get", "list", "watch", "create", "patch", "update", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: gitlab-runner
  namespace: gitlab-runner
subjects:
  - kind: ServiceAccount
    name: gitlab-runner
    namespace: gitlab-runner
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: gitlab-runner
```

## Runner `config.toml` Reference

Use this as a starting point after registration has produced a real runner
token:

```toml
concurrent = 4
check_interval = 3

[[runners]]
  name = "glemu-k3s-runner"
  url = "https://glemu.example.test"
  token = "glrt-generated-by-registration"
  executor = "kubernetes"

  [runners.kubernetes]
    namespace = "gitlab-runner"
    image = "alpine:3.20"
    poll_timeout = 180

  [[runners.kubernetes.host_aliases]]
    ip = "<emulator-ingress-or-service-ip>"
    hostnames = ["glemu.example.test"]
```

If the emulator uses a private CA, mount it where GitLab Runner expects host
certificates:

```text
/etc/gitlab-runner/certs/glemu.example.test.crt
```

Job pods also need CA trust for HTTPS Git clone and artifact/cache traffic.
For arbitrary job images, prefer a platform-wide CA injection mechanism or
runner Kubernetes volume mounts that place the CA into each job pod.

## Cache and Artifacts

The emulator implements artifact and cache coordinator endpoints. For simple
stacks, persist emulator data on the emulator PVC and let artifacts/traces/cache
round trip through the emulator.

For larger stacks, decide whether runner cache should use:

- emulator-backed cache endpoints,
- the emulator's MinIO service, if deployed,
- an existing S3-compatible object store.

Keep this decision explicit because cache behavior affects performance and
failure modes, but it is not required for first job execution.

## Validation Job

Create a project in the emulator with this `.gitlab-ci.yml`:

```yaml
k3s_probe:
  image: alpine:3.20
  tags:
    - k8s-incluster
  script:
    - echo k3s runner started
    - uname -a
    - mkdir -p out
    - echo k3s artifact > out/result.txt
  artifacts:
    paths:
      - out/result.txt
```

Expected result:

- pipeline appears in the project CI/CD page;
- job moves `pending -> running -> success`;
- runner appears in `/admin/runners`;
- job pod appears in `kubectl get pods -n gitlab-runner`;
- trace contains `k3s runner started`;
- artifact metadata appears in the project job API/UI.

## API and UI Validation

Useful checks:

```bash
curl -sk https://glemu.example.test/api/v4/version
curl -sk https://glemu.example.test/api/v4/runners
curl -sk https://glemu.example.test/api/v4/projects
```

Useful UI pages:

```text
https://glemu.example.test/admin/runners
https://glemu.example.test/admin/ci-lab
https://glemu.example.test/ui/<owner>/<project>/-/pipelines
https://glemu.example.test/ui/<owner>/<project>/-/jobs
```

## Troubleshooting

Job stays pending:

- Confirm the runner is online in `/admin/runners`.
- Confirm runner tags match the job tags.
- Confirm `run_untagged` is correct for the job.
- Check runner manager logs.

Runner cannot register:

- Confirm the emulator URL is reachable from the runner manager pod.
- Confirm TLS trust for the emulator certificate.
- Confirm the registration token matches emulator configuration.

Job pod cannot clone source:

- Confirm the job pod can resolve the emulator hostname.
- Confirm the job pod trusts the emulator certificate.
- Confirm the emulator external URL is the same URL advertised in job payloads.

Artifacts or traces missing:

- Check runner logs for upload failures.
- Confirm emulator persistent storage is writable.
- Confirm the job token in runner requests matches the assigned job.

Repeated runner registrations:

- This is expected if bootstrap registration runs repeatedly.
- Keep the generated `glrt-...` token after first registration.
- Pause or delete stale runners from `/admin/runners`.

## Deployment Checklist

- [ ] Emulator image is built and pushed to a registry reachable by k3s.
- [ ] Emulator has persistent storage mounted at `/data`.
- [ ] `GITLAB_EMULATOR_BASE_URL` matches the external HTTPS URL.
- [ ] External URL resolves from users, runner manager pods, and CI job pods.
- [ ] TLS is trusted by runner manager pods and CI job pods.
- [ ] Official runner manager is deployed with Kubernetes executor.
- [ ] Runner registered and stored its returned `glrt-...` token.
- [ ] Runner tags match validation job tags.
- [ ] Validation pipeline succeeds.
- [ ] Trace, status, artifact, and runner diagnostics are visible in the
      emulator UI/API.
