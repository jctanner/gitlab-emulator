#!/usr/bin/env bash
set -euo pipefail

RUNNER_TOKEN="${RUNNER_TOKEN:-}"
RUNNER_URL="${RUNNER_URL:-https://glemu.local}"
RUNNER_NAME="${RUNNER_NAME:-glemu-runner}"
RUNNER_TAGS="${RUNNER_TAGS:-aipcc-small-x86_64,vm,docker,podman}"
RUNNER_IMAGE="${RUNNER_IMAGE:-quay.io/aipcc/agentic-ci/podman:latest}"
RUNNER_TLS_CA_FILE="${RUNNER_TLS_CA_FILE:-/usr/local/share/ca-certificates/glemu-root.crt}"
RUNNER_RUN_UNTAGGED="${RUNNER_RUN_UNTAGGED:-true}"
RUNNER_REPLACE_EXISTING="${RUNNER_REPLACE_EXISTING:-true}"
RUNNER_CACHE_TYPE="${RUNNER_CACHE_TYPE:-s3}"
RUNNER_CACHE_PATH="${RUNNER_CACHE_PATH:-gitlab-runner}"
RUNNER_CACHE_SHARED="${RUNNER_CACHE_SHARED:-true}"
RUNNER_CACHE_S3_SERVER_ADDRESS="${RUNNER_CACHE_S3_SERVER_ADDRESS:-glemu.local:9000}"
RUNNER_CACHE_S3_ACCESS_KEY="${RUNNER_CACHE_S3_ACCESS_KEY:-glemu}"
RUNNER_CACHE_S3_SECRET_KEY="${RUNNER_CACHE_S3_SECRET_KEY:-glemu-cache-secret}"
RUNNER_CACHE_S3_BUCKET_NAME="${RUNNER_CACHE_S3_BUCKET_NAME:-gitlab-runner-cache}"
RUNNER_CACHE_S3_BUCKET_LOCATION="${RUNNER_CACHE_S3_BUCKET_LOCATION:-us-east-1}"
RUNNER_CACHE_S3_INSECURE="${RUNNER_CACHE_S3_INSECURE:-true}"
RUNNER_CACHE_S3_PATH_STYLE="${RUNNER_CACHE_S3_PATH_STYLE:-true}"

if [ -z "$RUNNER_TOKEN" ]; then
  echo "RUNNER_TOKEN is required" >&2
  exit 2
fi

tls_args=()
if [ -f "$RUNNER_TLS_CA_FILE" ]; then
  tls_args=(--tls-ca-file "$RUNNER_TLS_CA_FILE")
fi

if [ "$RUNNER_REPLACE_EXISTING" = "true" ]; then
  sudo systemctl stop gitlab-runner || true
  sudo rm -f /etc/gitlab-runner/config.toml
fi

cache_args=()
if [ -n "$RUNNER_CACHE_TYPE" ]; then
  cache_args=(
    --cache-type "$RUNNER_CACHE_TYPE"
    --cache-path "$RUNNER_CACHE_PATH"
    --cache-shared="$RUNNER_CACHE_SHARED"
  )

  if [ "$RUNNER_CACHE_TYPE" = "s3" ]; then
    cache_args+=(
      --cache-s3-server-address "$RUNNER_CACHE_S3_SERVER_ADDRESS"
      --cache-s3-access-key "$RUNNER_CACHE_S3_ACCESS_KEY"
      --cache-s3-secret-key "$RUNNER_CACHE_S3_SECRET_KEY"
      --cache-s3-bucket-name "$RUNNER_CACHE_S3_BUCKET_NAME"
      --cache-s3-bucket-location "$RUNNER_CACHE_S3_BUCKET_LOCATION"
      --cache-s3-insecure="$RUNNER_CACHE_S3_INSECURE"
      --cache-s3-path-style="$RUNNER_CACHE_S3_PATH_STYLE"
    )
  fi
fi

sudo gitlab-runner register --non-interactive \
  --url "$RUNNER_URL" \
  --registration-token "$RUNNER_TOKEN" \
  "${tls_args[@]}" \
  "${cache_args[@]}" \
  --name "$RUNNER_NAME" \
  --executor docker \
  --docker-image "$RUNNER_IMAGE" \
  --docker-privileged \
  --docker-extra-hosts glemu.local:192.168.124.10 \
  --docker-volumes /cache \
  --tag-list "$RUNNER_TAGS" \
  --run-untagged="$RUNNER_RUN_UNTAGGED" \
  --locked=false

sudo gitlab-runner restart
sudo gitlab-runner list
