# -*- mode: ruby -*-
# vi: set ft=ruby :

Vagrant.configure("2") do |config|

  # --- Server VM: runs the GitLab emulator in Docker ---
  config.vm.define "server", primary: true do |server|
    server.vm.box = "debian/bookworm64"
    server.vm.hostname = "glemu"

    server.vm.network "private_network",
      ip: "192.168.124.10",
      libvirt__network_name: "glemu124_net",
      libvirt__dhcp_enabled: false,
      libvirt__forward_mode: "none"

    server.vm.synced_folder ".", "/vagrant", disabled: true

    server.vm.provider :libvirt do |lv|
      lv.uri = "qemu:///system"
      lv.cpus = 2
      lv.memory = 2048
    end

    server.vm.provision "shell", inline: <<-SHELL
      set -eux
      export DEBIAN_FRONTEND=noninteractive

      apt-get update
      apt-get install -y ca-certificates curl gnupg rsync

      # Docker CE from official repo
      install -m 0755 -d /etc/apt/keyrings
      curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
      chmod a+r /etc/apt/keyrings/docker.gpg

      echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/debian \
        $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list

      apt-get update
      apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

      usermod -aG docker vagrant
      mkdir -p /srv/gitlab_emulator
      chown vagrant:vagrant /srv/gitlab_emulator

      echo "Docker provisioning complete."
      docker --version
      docker compose version
    SHELL
  end

  # --- Client VM: clean environment for testing with the glab CLI ---
  config.vm.define "client" do |client|
    client.vm.box = "debian/bookworm64"
    client.vm.hostname = "glemu-client"

    client.vm.network "private_network",
      ip: "192.168.124.11",
      libvirt__network_name: "glemu124_net",
      libvirt__dhcp_enabled: false,
      libvirt__forward_mode: "none"

    client.vm.synced_folder ".", "/vagrant", disabled: true

    client.vm.provider :libvirt do |lv|
      lv.uri = "qemu:///system"
      lv.cpus = 1
      lv.memory = 512
    end

    client.vm.provision "shell", inline: <<-SHELL
      set -eux
      export DEBIAN_FRONTEND=noninteractive

      apt-get update
      apt-get install -y ca-certificates curl git jq rsync

      # Point glemu.local at the server VM
      echo "192.168.124.10 glemu.local" >> /etc/hosts

      mkdir -p /srv/bin /srv/scripts
      chown -R vagrant:vagrant /srv

      echo "Client provisioning complete."
    SHELL
  end

  # --- Runner VM: official GitLab Runner for validating CI execution ---
  config.vm.define "runner" do |runner|
    runner.vm.box = "debian/bookworm64"
    runner.vm.hostname = "glemu-runner"

    runner.vm.network "private_network",
      ip: "192.168.124.12",
      libvirt__network_name: "glemu124_net",
      libvirt__dhcp_enabled: false,
      libvirt__forward_mode: "none"

    runner.vm.synced_folder ".", "/vagrant", disabled: true

    runner.vm.provider :libvirt do |lv|
      lv.uri = "qemu:///system"
      lv.cpus = 2
      lv.memory = 4096
    end

    runner.vm.provision "shell", inline: <<-SHELL
      set -eux
      export DEBIAN_FRONTEND=noninteractive

      apt-get update
      apt-get install -y ca-certificates curl gnupg git jq rsync

      # Point glemu.local at the server VM.
      grep -q '^192\\.168\\.124\\.10 glemu\\.local$' /etc/hosts \
        || echo "192.168.124.10 glemu.local" >> /etc/hosts

      # Docker CE from official repo. The runner uses the Docker executor with
      # privileged containers so the agentic CI job image can run nested Podman.
      install -m 0755 -d /etc/apt/keyrings
      if [ ! -f /etc/apt/keyrings/docker.gpg ]; then
        curl -fsSL https://download.docker.com/linux/debian/gpg \
          | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
      fi
      chmod a+r /etc/apt/keyrings/docker.gpg

      echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/debian \
        $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list

      apt-get update
      apt-get install -y docker-ce docker-ce-cli containerd.io
      usermod -aG docker vagrant

      # Official GitLab Runner package repository.
      curl -fsSL https://packages.gitlab.com/install/repositories/runner/gitlab-runner/script.deb.sh \
        | bash
      apt-get install -y gitlab-runner
      usermod -aG docker gitlab-runner

      mkdir -p /srv/gitlab-runner /srv/scripts
      chown -R vagrant:vagrant /srv

      cat > /srv/scripts/register-runner.sh <<'EOF'
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
EOF
      chmod +x /srv/scripts/register-runner.sh

      echo "Runner provisioning complete."
      docker --version
      gitlab-runner --version
    SHELL
  end

end
