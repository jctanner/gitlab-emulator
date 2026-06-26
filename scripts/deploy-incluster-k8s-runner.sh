#!/usr/bin/env bash
set -euo pipefail

RUNNER_TOKEN="${RUNNER_TOKEN:-}"
RUNNER_URL="${RUNNER_URL:-https://glemu.local}"
RUNNER_NAME="${RUNNER_NAME:-glemu-k8s-incluster-runner}"
RUNNER_TAGS="${RUNNER_TAGS:-k8s-incluster}"
RUNNER_IMAGE="${RUNNER_IMAGE:-alpine:3.20}"
RUNNER_TLS_CA_FILE="${RUNNER_TLS_CA_FILE:-/usr/local/share/ca-certificates/glemu-root.crt}"
RUNNER_RUN_UNTAGGED="${RUNNER_RUN_UNTAGGED:-false}"
RUNNER_MANAGER_IMAGE="${RUNNER_MANAGER_IMAGE:-gitlab/gitlab-runner:v19.1.1}"
K8S_NAMESPACE="${K8S_NAMESPACE:-gitlab-runner-incluster}"
GLEMU_HOST_ALIAS_IP="${GLEMU_HOST_ALIAS_IP:-192.168.124.10}"
CONFIG_WORKDIR="${CONFIG_WORKDIR:-/tmp/glemu-incluster-runner}"

if [ -z "$RUNNER_TOKEN" ]; then
  echo "RUNNER_TOKEN is required" >&2
  exit 2
fi

if [ ! -f "$RUNNER_TLS_CA_FILE" ]; then
  echo "Runner CA file not found: $RUNNER_TLS_CA_FILE" >&2
  exit 2
fi

sudo kubectl get namespace "$K8S_NAMESPACE" >/dev/null 2>&1 \
  || sudo kubectl create namespace "$K8S_NAMESPACE"

rm -rf "$CONFIG_WORKDIR"
mkdir -p "$CONFIG_WORKDIR"
cp "$RUNNER_TLS_CA_FILE" "$CONFIG_WORKDIR/glemu.local.crt"

sudo gitlab-runner register --non-interactive \
  --config "$CONFIG_WORKDIR/config.toml" \
  --url "$RUNNER_URL" \
  --registration-token "$RUNNER_TOKEN" \
  --tls-ca-file "$RUNNER_TLS_CA_FILE" \
  --name "$RUNNER_NAME" \
  --executor kubernetes \
  --kubernetes-namespace "$K8S_NAMESPACE" \
  --kubernetes-image "$RUNNER_IMAGE" \
  --tag-list "$RUNNER_TAGS" \
  --run-untagged="$RUNNER_RUN_UNTAGGED" \
  --locked=false

sudo sed -i \
  -e 's|tls-ca-file = ".*"|tls-ca-file = "/etc/gitlab-runner/certs/glemu.local.crt"|' \
  -e '/^[[:space:]]*host = /d' \
  -e '/^[[:space:]]*ca_file = /d' \
  -e '/^[[:space:]]*cert_file = /d' \
  -e '/^[[:space:]]*key_file = /d' \
  -e '/^[[:space:]]*poll_timeout = /d' \
  "$CONFIG_WORKDIR/config.toml"
sudo sed -i "/\\[runners.kubernetes\\]/a\\    poll_timeout = 180" "$CONFIG_WORKDIR/config.toml"
sudo tee -a "$CONFIG_WORKDIR/config.toml" >/dev/null <<EOF
    [[runners.kubernetes.host_aliases]]
      ip = "$GLEMU_HOST_ALIAS_IP"
      hostnames = ["glemu.local"]
EOF

sudo kubectl -n "$K8S_NAMESPACE" delete deployment glemu-k8s-incluster-runner --ignore-not-found
sudo kubectl -n "$K8S_NAMESPACE" delete configmap glemu-k8s-incluster-runner-config --ignore-not-found
sudo kubectl -n "$K8S_NAMESPACE" delete secret glemu-k8s-incluster-runner-ca --ignore-not-found
sudo kubectl -n "$K8S_NAMESPACE" create configmap glemu-k8s-incluster-runner-config \
  --from-file=config.toml="$CONFIG_WORKDIR/config.toml"
sudo kubectl -n "$K8S_NAMESPACE" create secret generic glemu-k8s-incluster-runner-ca \
  --from-file=glemu.local.crt="$CONFIG_WORKDIR/glemu.local.crt"

sudo kubectl apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: glemu-k8s-incluster-runner
  namespace: ${K8S_NAMESPACE}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: glemu-k8s-incluster-runner
  namespace: ${K8S_NAMESPACE}
rules:
  - apiGroups: [""]
    resources: ["pods", "pods/exec", "pods/attach", "pods/log", "secrets", "configmaps", "services"]
    verbs: ["get", "list", "watch", "create", "patch", "update", "delete"]
  - apiGroups: [""]
    resources: ["events"]
    verbs: ["get", "list", "watch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: glemu-k8s-incluster-runner
  namespace: ${K8S_NAMESPACE}
subjects:
  - kind: ServiceAccount
    name: glemu-k8s-incluster-runner
    namespace: ${K8S_NAMESPACE}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: glemu-k8s-incluster-runner
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: glemu-k8s-incluster-runner
  namespace: ${K8S_NAMESPACE}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: glemu-k8s-incluster-runner
  template:
    metadata:
      labels:
        app: glemu-k8s-incluster-runner
    spec:
      serviceAccountName: glemu-k8s-incluster-runner
      hostAliases:
        - ip: "${GLEMU_HOST_ALIAS_IP}"
          hostnames:
            - glemu.local
      containers:
        - name: gitlab-runner
          image: ${RUNNER_MANAGER_IMAGE}
          imagePullPolicy: IfNotPresent
          command: ["gitlab-runner"]
          args: ["run", "--working-directory", "/home/gitlab-runner", "--config", "/etc/gitlab-runner/config.toml", "--service", "gitlab-runner", "--user", "gitlab-runner"]
          volumeMounts:
            - name: runner-config
              mountPath: /etc/gitlab-runner/config.toml
              subPath: config.toml
              readOnly: true
            - name: runner-ca
              mountPath: /etc/gitlab-runner/certs/glemu.local.crt
              subPath: glemu.local.crt
              readOnly: true
      volumes:
        - name: runner-config
          configMap:
            name: glemu-k8s-incluster-runner-config
        - name: runner-ca
          secret:
            secretName: glemu-k8s-incluster-runner-ca
EOF

sudo kubectl -n "$K8S_NAMESPACE" rollout status deployment/glemu-k8s-incluster-runner --timeout=180s
sudo kubectl -n "$K8S_NAMESPACE" get pods -o wide
