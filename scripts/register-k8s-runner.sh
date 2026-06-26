#!/usr/bin/env bash
set -euo pipefail

RUNNER_TOKEN="${RUNNER_TOKEN:-}"
RUNNER_URL="${RUNNER_URL:-https://glemu.local}"
RUNNER_NAME="${RUNNER_NAME:-glemu-k8s-runner}"
RUNNER_TAGS="${RUNNER_TAGS:-k8s}"
RUNNER_IMAGE="${RUNNER_IMAGE:-alpine:3.20}"
RUNNER_TLS_CA_FILE="${RUNNER_TLS_CA_FILE:-/usr/local/share/ca-certificates/glemu-root.crt}"
RUNNER_RUN_UNTAGGED="${RUNNER_RUN_UNTAGGED:-false}"
RUNNER_REPLACE_EXISTING="${RUNNER_REPLACE_EXISTING:-true}"
K8S_NAMESPACE="${K8S_NAMESPACE:-gitlab-runner}"
K8S_HOST="${K8S_HOST:-https://127.0.0.1:6443}"
K8S_CERT_DIR="${K8S_CERT_DIR:-/etc/gitlab-runner/k3s}"
GLEMU_HOST_ALIAS_IP="${GLEMU_HOST_ALIAS_IP:-192.168.124.10}"

if [ -z "$RUNNER_TOKEN" ]; then
  echo "RUNNER_TOKEN is required" >&2
  exit 2
fi

extract_kubeconfig_value() {
  local key="$1"
  awk -v key="$key" '$1 == key ":" { print $2; exit }' /etc/rancher/k3s/k3s.yaml
}

sudo install -d -m 0755 "$K8S_CERT_DIR"
extract_kubeconfig_value certificate-authority-data | base64 -d | sudo tee "$K8S_CERT_DIR/ca.crt" >/dev/null
extract_kubeconfig_value client-certificate-data | base64 -d | sudo tee "$K8S_CERT_DIR/client.crt" >/dev/null
extract_kubeconfig_value client-key-data | base64 -d | sudo tee "$K8S_CERT_DIR/client.key" >/dev/null
sudo chmod 0644 "$K8S_CERT_DIR/ca.crt" "$K8S_CERT_DIR/client.crt"
sudo chmod 0600 "$K8S_CERT_DIR/client.key"

sudo kubectl get namespace "$K8S_NAMESPACE" >/dev/null 2>&1 \
  || sudo kubectl create namespace "$K8S_NAMESPACE"

tls_args=()
if [ -f "$RUNNER_TLS_CA_FILE" ]; then
  tls_args=(--tls-ca-file "$RUNNER_TLS_CA_FILE")
fi

if [ "$RUNNER_REPLACE_EXISTING" = "true" ]; then
  sudo systemctl stop gitlab-runner || true
  sudo rm -f /etc/gitlab-runner/config.toml
fi

sudo gitlab-runner register --non-interactive \
  --url "$RUNNER_URL" \
  --registration-token "$RUNNER_TOKEN" \
  "${tls_args[@]}" \
  --name "$RUNNER_NAME" \
  --executor kubernetes \
  --kubernetes-namespace "$K8S_NAMESPACE" \
  --kubernetes-image "$RUNNER_IMAGE" \
  --tag-list "$RUNNER_TAGS" \
  --run-untagged="$RUNNER_RUN_UNTAGGED" \
  --locked=false

sudo sed -i \
  -e "0,/^[[:space:]]*host = .*/s|^[[:space:]]*host = .*|    host = \"$K8S_HOST\"|" \
  -e '/^[[:space:]]*ca_file = /d' \
  -e '/^[[:space:]]*cert_file = /d' \
  -e '/^[[:space:]]*key_file = /d' \
  -e '/^[[:space:]]*poll_timeout = /d' \
  /etc/gitlab-runner/config.toml
sudo sed -i "/^[[:space:]]*host = /a\\    ca_file = \"$K8S_CERT_DIR/ca.crt\"\\n    cert_file = \"$K8S_CERT_DIR/client.crt\"\\n    key_file = \"$K8S_CERT_DIR/client.key\"\\n    poll_timeout = 180" /etc/gitlab-runner/config.toml
sudo tee -a /etc/gitlab-runner/config.toml >/dev/null <<EOF
    [[runners.kubernetes.host_aliases]]
      ip = "$GLEMU_HOST_ALIAS_IP"
      hostnames = ["glemu.local"]
EOF

sudo gitlab-runner restart
sudo gitlab-runner list
sudo kubectl get nodes -o wide
