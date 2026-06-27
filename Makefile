.PHONY: help build up down reset restart logs test test-full test-affected test-focused smoke clean

.DEFAULT_GOAL := help

# General

## Show this help
help:
	@echo "Usage: make <target>"
	@echo ""
	@awk 'BEGIN{section=""} \
		/^# -{10}/ { next } \
		/^# [A-Z]/ { if(section!="") print ""; printf "\033[1m%s\033[0m\n", substr($$0,3); section=$$0; next } \
		/^## /     { desc=substr($$0,4); next } \
		/^[a-zA-Z0-9_-]+:/{ if(desc!="") { target=$$1; sub(/:.*/, "", target); printf "  \033[36m%-15s\033[0m %s\n", target, desc; desc="" } }' $(MAKEFILE_LIST)
	@echo ""

# Docker (local)

## Build the container image
build:
	docker compose build

## Start the container, preserving volumes
up: build
	docker compose up -d --force-recreate
	@echo "Waiting for server to start..."
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
		curl -sf http://localhost:8000/api/v4 > /dev/null 2>&1 && break; \
		sleep 1; \
	done
	@echo "Server is up at http://localhost:8000"

## Reset local containers and volumes, then start fresh
reset: build
	docker compose down --volumes 2>/dev/null || true
	docker compose up -d
	@echo "Waiting for server to start..."
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
		curl -sf http://localhost:8000/api/v4 > /dev/null 2>&1 && break; \
		sleep 1; \
	done
	@echo "Server is up at http://localhost:8000"

## Stop and remove the container + volumes
down:
	docker compose down --volumes

## Rebuild and restart, preserving volumes
restart: up

## Tail container logs
logs:
	docker compose logs -f

## Run the pytest suite (local, not in container)
test: test-full

## Run the full local pytest suite with dev dependencies
test-full:
	timeout 600s env UV_CACHE_DIR=/tmp/glemu-uv-cache uv run --extra dev python -m pytest tests/ -v

## Run the affected GitLab API regression suite
test-affected:
	timeout 300s env UV_CACHE_DIR=/tmp/glemu-uv-cache uv run --extra dev python -m pytest \
		tests/test_admin.py \
		tests/test_pagination_api.py \
		tests/test_collaborators_api.py \
		tests/test_gitlab_commits_api.py \
		tests/test_groups_api.py \
		tests/test_merge_requests_api.py \
		tests/test_pipelines_api.py \
		tests/test_projects_api.py \
		tests/test_pulls_api.py \
		tests/test_releases_api.py \
		tests/test_repository_files_api.py \
		tests/test_search_api.py \
		tests/test_webhooks_api.py \
		-v

## Run focused route and data-model cleanup regressions
test-focused:
	timeout 300s env UV_CACHE_DIR=/tmp/glemu-uv-cache uv run --extra dev python -m pytest \
		tests/test_pipelines_api.py::test_pipeline_trigger_creates_trigger_source_pipeline \
		tests/test_projects_api.py::test_project_path_route_does_not_shadow_pipeline_routes \
		tests/test_merge_requests_api.py \
		tests/test_search_api.py::test_gitlab_global_search_merge_requests \
		-v

## Quick smoke test against the running container
smoke:
	@echo "=== Creating token ==="
	$(eval TOKEN := $(shell curl -sf -X POST 'http://localhost:8000/admin/tokens' \
		-H 'Content-Type: application/json' \
		-d '{"login":"admin","name":"smoke-token","scopes":["repo","user"]}' \
		| python3 -c "import sys,json; print(json.load(sys.stdin)['token'])"))
	@echo "Token: $(TOKEN)"
	@echo ""
	@echo "=== GET /user ==="
	@curl -sf -H "Authorization: token $(TOKEN)" http://localhost:8000/api/v4/user | python3 -m json.tool | head -5
	@echo ""
	@echo "=== Create repo ==="
	@curl -sf -X POST -H "Authorization: token $(TOKEN)" -H "Content-Type: application/json" \
		-d '{"name":"smoke-repo","description":"Smoke test"}' \
		http://localhost:8000/api/v4/user/repos | python3 -m json.tool | head -5
	@echo ""
	@echo "=== Create issue ==="
	@curl -sf -X POST -H "Authorization: token $(TOKEN)" -H "Content-Type: application/json" \
		-d '{"title":"Smoke test issue","body":"Testing"}' \
		http://localhost:8000/api/v4/repos/admin/smoke-repo/issues | python3 -m json.tool | head -5
	@echo ""
	@echo "=== List issues ==="
	@curl -sf http://localhost:8000/api/v4/repos/admin/smoke-repo/issues | python3 -m json.tool | head -5
	@echo ""
	@echo "=== Git clone + push ==="
	@rm -rf /tmp/smoke-clone
	@git clone http://localhost:8000/admin/smoke-repo.git /tmp/smoke-clone 2>&1 || true
	@cd /tmp/smoke-clone && git checkout -b main 2>/dev/null; \
		echo "# Smoke Test" > README.md; \
		git add README.md; \
		git -c commit.gpgsign=false -c user.name="Smoke" -c user.email="smoke@test.com" \
			commit -m "smoke test" 2>&1; \
		git -c commit.gpgsign=false push http://admin:$(TOKEN)@localhost:8000/admin/smoke-repo.git main 2>&1
	@echo ""
	@echo "=== Verify clone ==="
	@rm -rf /tmp/smoke-verify
	@git clone http://localhost:8000/admin/smoke-repo.git /tmp/smoke-verify 2>&1
	@cat /tmp/smoke-verify/README.md 2>/dev/null && echo "PASS: File content verified" || echo "FAIL: File not found"
	@rm -rf /tmp/smoke-clone /tmp/smoke-verify
	@echo ""
	@echo "=== Smoke test complete ==="

## Remove all build artifacts
clean: down
	docker rmi gitlab_emulator_gitlab-emulator 2>/dev/null || true
	rm -rf .venv __pycache__ .pytest_cache

# Vagrant VM (Debian 12 + Docker, via libvirt/KVM)

.PHONY: vm-net vm-up vm-sync vm-build vm-start vm-reset vm-stop vm-logs vm-deploy vm-deploy-reset vm-destroy vm-ssh vm-ip vm-test vm-git-test vm-glab vm-ci-lab-smoke vm-validate vm-validate-current vm-runner-validate vm-runner-variable-test vm-runner-secret-file-test vm-runner-secret-env-test vm-runner-redaction-test vm-runner-rules-test vm-runner-extends-test vm-runner-include-test vm-runner-cache-test vm-runner-artifact-needs-test vm-client-scripts-sync vm-client-install-glab vm-client-install-ca vm-client-sync vm-client-ssh vm-runner-sync vm-runner-ssh vm-runner-status vm-runner-cache-config vm-runner-ensure-ca vm-runner-install-ca vm-runner-register vm-k8s-runner-up vm-k8s-runner-sync vm-k8s-runner-ssh vm-k8s-runner-status vm-k8s-runner-logs vm-k8s-runner-pods vm-k8s-runner-install-ca vm-k8s-runner-register vm-k8s-runner-validate vm-k8s-runner-secret-validate vm-k8s-runner-secret-file-test vm-k8s-runner-secret-env-test vm-k8s-runner-redaction-test vm-k8s-incluster-sync vm-k8s-incluster-deploy vm-k8s-incluster-status vm-k8s-incluster-logs vm-k8s-incluster-pods vm-k8s-incluster-validate vm-k8s-incluster-secret-file-test

VM_IP := 192.168.124.10
RUNNER_IP := 192.168.124.12
K8S_RUNNER_IP := 192.168.124.13
VM_PROJECT_DIR := /srv/gitlab_emulator
GLAB_VERSION ?= 1.101.0
GLAB_SHA256 ?= f8f40309a622416b769a455a85509bae7800070cd023466f2d33d8ee82f3fc61
RUNNER_TOKEN ?= runner-registration-token

# (internal) ensure the libvirt network exists before booting
vm-net:
	@sudo virsh -c qemu:///system net-info glemu124_net >/dev/null 2>&1 \
		|| { echo '<network><name>glemu124_net</name><bridge name="virbr-glemu124" stp="on" delay="0"/><ip address="192.168.124.1" netmask="255.255.255.0"/></network>' \
		     | sudo virsh -c qemu:///system net-define /dev/stdin \
		     && sudo virsh -c qemu:///system net-start glemu124_net \
		     && sudo virsh -c qemu:///system net-autostart glemu124_net; }
	@sudo virsh -c qemu:///system net-start glemu124_net 2>/dev/null || true

## Boot the VMs (provisions on first run)
vm-up: vm-net
	vagrant up

## Rsync the codebase into the server VM
vm-sync:
	@echo "Syncing codebase to $(VM_PROJECT_DIR) ..."
	@vagrant ssh-config server > .vagrant-ssh-config
	rsync -avz --delete \
		--exclude '.venv' \
		--exclude '__pycache__' \
		--exclude '.git' \
		--exclude 'data/' \
		--exclude '.vagrant' \
		--exclude '.vagrant-ssh-config' \
		-e "ssh -F .vagrant-ssh-config" \
		. server:$(VM_PROJECT_DIR)/
	@rm -f .vagrant-ssh-config
	@echo "Sync complete."

## Build the container image inside the server VM
vm-build:
	vagrant ssh server -c "cd $(VM_PROJECT_DIR) && docker compose build"

## Start containers inside the server VM, preserving volumes
vm-start:
	vagrant ssh server -c "cd $(VM_PROJECT_DIR) && docker compose up -d --force-recreate"

## Reset server VM containers and volumes, then start fresh
vm-reset:
	vagrant ssh server -c "cd $(VM_PROJECT_DIR) && docker compose down --volumes 2>/dev/null; docker compose up -d"

## Stop containers inside the server VM
vm-stop:
	vagrant ssh server -c "cd $(VM_PROJECT_DIR) && docker compose down"

## Tail container logs inside the server VM
vm-logs:
	vagrant ssh server -c "cd $(VM_PROJECT_DIR) && docker compose logs -f"

## Sync, build, and start containers in server VM, preserving volumes
vm-deploy: vm-sync vm-build vm-start
	@echo "Deploy complete. Service should be reachable at https://$(VM_IP)"

## Sync, build, reset server VM volumes, and start containers fresh
vm-deploy-reset: vm-sync vm-build vm-reset
	@echo "Reset deploy complete. Service should be reachable at https://$(VM_IP)"

## Destroy all VMs
vm-destroy:
	vagrant destroy -f

## SSH into the server VM
vm-ssh:
	vagrant ssh server

## Print the VM IP for /etc/hosts
vm-ip:
	@echo "$(VM_IP)  glemu.local"
	@echo "$(RUNNER_IP)  glemu-runner"
	@echo "$(K8S_RUNNER_IP)  glemu-k8s-runner"

# Testing

## Run glab CLI integration tests from the client VM
vm-test: vm-client-sync vm-client-install-ca
	vagrant ssh client -c "bash /srv/scripts/glab-integration-test.sh"

## Run git CLI integration tests from the client VM
vm-git-test: vm-client-scripts-sync
	vagrant ssh client -c "bash /srv/scripts/git-integration-test.sh"

## Quick glab user API check from the client VM
vm-glab: vm-client-sync vm-client-install-ca
	@echo "Creating token and running glab API user check ..."
	@vagrant ssh client -c '\
		TOKEN=$$(curl -sk https://glemu.local/api/v4/admin/tokens \
			-X POST -H "Content-Type: application/json" \
			-d "{\"login\":\"admin\",\"name\":\"glab-test-$$$$\",\"scopes\":[\"repo\",\"user\"]}" \
			| jq -r .token) && \
		echo "Token: $$TOKEN" && \
		mkdir -p ~/.config/glab && \
			printf "glemu.local:\n  oauth_token: %s\n  user: admin\n" "$$TOKEN" > ~/.config/glab/hosts.yml && \
			GITLAB_INSECURE=1 GITLAB_HOST=glemu.local /srv/bin/glab api user'

## Validate CI Lab project/pipeline/job execution through the official runner
vm-ci-lab-smoke: vm-client-scripts-sync vm-client-install-ca vm-runner-ensure-ca
	vagrant ssh client -c "bash /srv/scripts/ci-lab-smoke.sh"

## Deploy and run the full VM validation path
vm-validate: vm-deploy vm-validate-current

## Validate currently deployed server with client and runner VMs
vm-validate-current: vm-test vm-runner-validate

## Validate official runner registration, rules, extends, variables, secrets, includes, cache, and needs:artifacts
vm-runner-validate: vm-runner-install-ca vm-runner-register vm-runner-variable-test vm-runner-secret-file-test vm-runner-secret-env-test vm-runner-redaction-test vm-runner-rules-test vm-runner-extends-test vm-runner-include-test vm-runner-cache-test vm-runner-artifact-needs-test

## Validate official runner CI variable precedence
vm-runner-variable-test: vm-client-scripts-sync
	vagrant ssh client -c "bash /srv/scripts/runner-variable-validation.sh"

## Validate official runner file-mode CI secrets
vm-runner-secret-file-test: vm-client-scripts-sync
	vagrant ssh client -c "SECRET_VALIDATION_MODE=file bash /srv/scripts/runner-secret-validation.sh"

## Validate official runner env-mode CI secrets
vm-runner-secret-env-test: vm-client-scripts-sync
	vagrant ssh client -c "SECRET_VALIDATION_MODE=env bash /srv/scripts/runner-secret-validation.sh"

## Validate official runner secret trace redaction
vm-runner-redaction-test: vm-client-scripts-sync
	vagrant ssh client -c "SECRET_VALIDATION_MODE=redaction bash /srv/scripts/runner-secret-validation.sh"

## Validate official runner CI rules job selection
vm-runner-rules-test: vm-client-scripts-sync
	vagrant ssh client -c "bash /srv/scripts/runner-rules-validation.sh"

## Validate official runner CI extends/default/inherit behavior
vm-runner-extends-test: vm-client-scripts-sync
	vagrant ssh client -c "bash /srv/scripts/runner-extends-validation.sh"

## Validate official runner nested and project CI includes
vm-runner-include-test: vm-client-scripts-sync
	vagrant ssh client -c "bash /srv/scripts/runner-include-validation.sh"

## Validate official runner cache upload/restore through MinIO
vm-runner-cache-test: vm-client-scripts-sync
	vagrant ssh client -c "bash /srv/scripts/runner-cache-validation.sh"

## Validate official runner needs:artifacts download behavior
vm-runner-artifact-needs-test: vm-client-scripts-sync
	vagrant ssh client -c "bash /srv/scripts/runner-artifact-needs-validation.sh"

## Rsync test scripts to client VM
vm-client-scripts-sync:
	@vagrant ssh-config client > .vagrant-ssh-config
	@rsync -avz \
		-e "ssh -F .vagrant-ssh-config" \
		$(CURDIR)/scripts/ client:/srv/scripts/
	@rm -f .vagrant-ssh-config

## Install the pinned GitLab CLI inside the client VM
vm-client-install-glab:
	vagrant ssh client -c 'set -euo pipefail; \
		if [ -x /srv/bin/glab ] && [ -f /srv/bin/.glab-version ] && [ "$$(cat /srv/bin/.glab-version)" = "$(GLAB_VERSION)" ]; then \
			echo "glab $(GLAB_VERSION) already installed at /srv/bin/glab"; \
		else \
			tmp=$$(mktemp -d); \
			trap "rm -rf $$tmp" EXIT; \
			arch=$$(dpkg --print-architecture); \
			case "$$arch" in \
				amd64) glab_arch=amd64 ;; \
				arm64) glab_arch=arm64 ;; \
				*) echo "Unsupported client VM architecture: $$arch" >&2; exit 2 ;; \
			esac; \
			url="https://gitlab.com/gitlab-org/cli/-/releases/v$(GLAB_VERSION)/downloads/glab_$(GLAB_VERSION)_linux_$${glab_arch}.tar.gz"; \
			echo "Installing glab $(GLAB_VERSION) from $$url"; \
			curl -fsSL -o "$$tmp/glab.tar.gz" "$$url"; \
			if [ "$$glab_arch" = "amd64" ]; then \
				echo "$(GLAB_SHA256)  $$tmp/glab.tar.gz" | sha256sum -c -; \
			else \
				echo "No pinned checksum configured for linux_$${glab_arch}; skipping checksum."; \
			fi; \
			tar -xzf "$$tmp/glab.tar.gz" -C "$$tmp"; \
			install -m 0755 "$$tmp/bin/glab" /srv/bin/glab; \
			echo "$(GLAB_VERSION)" > /srv/bin/.glab-version; \
			echo "glab $(GLAB_VERSION) installed at /srv/bin/glab"; \
		fi; \
		test -x /srv/bin/glab'

## Sync test scripts and ensure glab is installed in the client VM
vm-client-sync: vm-client-scripts-sync vm-client-install-glab

## Install the emulator Caddy root CA into the client VM
vm-client-install-ca:
	vagrant ssh server -c 'docker exec gitlab_emulator-gitlab-emulator-1 sh -c '"'"'for path in /data/caddy-data/caddy/pki/authorities/local/root.crt /root/.local/share/caddy/pki/authorities/local/root.crt; do test -f "$$path" && exec cat "$$path"; done; echo "Caddy root CA not found" >&2; exit 1'"'"'' > .glemu-root.crt
	@vagrant ssh-config client > .vagrant-ssh-config
	rsync -avz -e "ssh -F .vagrant-ssh-config" .glemu-root.crt client:/tmp/glemu-root.crt
	vagrant ssh client -c "sudo cp /tmp/glemu-root.crt /usr/local/share/ca-certificates/glemu-root.crt && sudo update-ca-certificates"
	@rm -f .glemu-root.crt .vagrant-ssh-config

## SSH into the client VM
vm-client-ssh:
	vagrant ssh client

## Rsync runner helper scripts to the runner VM
vm-runner-sync:
	@vagrant ssh-config runner > .vagrant-ssh-config
	@rsync -avz \
		-e "ssh -F .vagrant-ssh-config" \
		$(CURDIR)/scripts/register-runner.sh runner:/srv/scripts/register-runner.sh
	vagrant ssh runner -c "chmod +x /srv/scripts/register-runner.sh"
	@rm -f .vagrant-ssh-config

## SSH into the runner VM
vm-runner-ssh:
	vagrant ssh runner

## Show GitLab Runner status on the runner VM
vm-runner-status:
	vagrant ssh runner -c "sudo gitlab-runner status; sudo gitlab-runner list || true; docker ps"

## Show GitLab Runner distributed cache config on the runner VM
vm-runner-cache-config:
	vagrant ssh runner -c "sudo sed -n '/\\[runners.cache\\]/,/\\[runners.docker\\]/p' /etc/gitlab-runner/config.toml"

## Install runner CA only when runner TLS verification fails
vm-runner-ensure-ca:
	@if vagrant ssh runner -c "sudo curl -fsS --cacert /etc/gitlab-runner/certs/glemu.local.crt https://glemu.local/api/v4 >/dev/null"; then \
		echo "Runner CA is current."; \
	else \
		echo "Runner CA verification failed; installing current emulator CA."; \
		vagrant ssh server -c 'docker exec gitlab_emulator-gitlab-emulator-1 sh -c '"'"'for path in /data/caddy-data/caddy/pki/authorities/local/root.crt /root/.local/share/caddy/pki/authorities/local/root.crt; do test -f "$$path" && exec cat "$$path"; done; echo "Caddy root CA not found" >&2; exit 1'"'"'' > .glemu-root.crt; \
		vagrant ssh-config runner > .vagrant-ssh-config; \
		rsync -avz -e "ssh -F .vagrant-ssh-config" .glemu-root.crt runner:/tmp/glemu-root.crt; \
		vagrant ssh runner -c "sudo cp /tmp/glemu-root.crt /usr/local/share/ca-certificates/glemu-root.crt && sudo mkdir -p /etc/gitlab-runner/certs && sudo cp /tmp/glemu-root.crt /etc/gitlab-runner/certs/glemu.local.crt && sudo update-ca-certificates && (sudo grep -q 'extra_hosts = .*glemu.local:192.168.124.10' /etc/gitlab-runner/config.toml || sudo sed -i '/\\[runners.docker\\]/a\\    extra_hosts = [\"glemu.local:192.168.124.10\"]' /etc/gitlab-runner/config.toml) && sudo gitlab-runner restart"; \
		rm -f .glemu-root.crt .vagrant-ssh-config; \
	fi

## Install the emulator Caddy root CA into the runner VM
vm-runner-install-ca:
	vagrant ssh server -c 'docker exec gitlab_emulator-gitlab-emulator-1 sh -c '"'"'for path in /data/caddy-data/caddy/pki/authorities/local/root.crt /root/.local/share/caddy/pki/authorities/local/root.crt; do test -f "$$path" && exec cat "$$path"; done; echo "Caddy root CA not found" >&2; exit 1'"'"'' > .glemu-root.crt
	@vagrant ssh-config runner > .vagrant-ssh-config
	rsync -avz -e "ssh -F .vagrant-ssh-config" .glemu-root.crt runner:/tmp/glemu-root.crt
	vagrant ssh runner -c "sudo cp /tmp/glemu-root.crt /usr/local/share/ca-certificates/glemu-root.crt && sudo mkdir -p /etc/gitlab-runner/certs && sudo cp /tmp/glemu-root.crt /etc/gitlab-runner/certs/glemu.local.crt && sudo update-ca-certificates && (sudo grep -q 'extra_hosts = .*glemu.local:192.168.124.10' /etc/gitlab-runner/config.toml || sudo sed -i '/\\[runners.docker\\]/a\\    extra_hosts = [\"glemu.local:192.168.124.10\"]' /etc/gitlab-runner/config.toml) && sudo gitlab-runner restart"
	@rm -f .glemu-root.crt .vagrant-ssh-config

## Register the runner VM with the emulator
vm-runner-register: vm-runner-sync
	@test -n "$(RUNNER_TOKEN)" || { echo "RUNNER_TOKEN is required"; exit 2; }
	vagrant ssh runner -c 'RUNNER_TOKEN="$(RUNNER_TOKEN)" RUNNER_URL="$${RUNNER_URL:-https://glemu.local}" RUNNER_NAME="$${RUNNER_NAME:-glemu-runner}" RUNNER_TAGS="$${RUNNER_TAGS:-aipcc-small-x86_64,vm,docker,podman}" RUNNER_IMAGE="$${RUNNER_IMAGE:-quay.io/aipcc/agentic-ci/podman:latest}" RUNNER_RUN_UNTAGGED="$${RUNNER_RUN_UNTAGGED:-true}" RUNNER_CACHE_TYPE="$${RUNNER_CACHE_TYPE:-s3}" RUNNER_CACHE_PATH="$${RUNNER_CACHE_PATH:-gitlab-runner}" RUNNER_CACHE_SHARED="$${RUNNER_CACHE_SHARED:-true}" RUNNER_CACHE_S3_SERVER_ADDRESS="$${RUNNER_CACHE_S3_SERVER_ADDRESS:-glemu.local:9000}" RUNNER_CACHE_S3_ACCESS_KEY="$${RUNNER_CACHE_S3_ACCESS_KEY:-glemu}" RUNNER_CACHE_S3_SECRET_KEY="$${RUNNER_CACHE_S3_SECRET_KEY:-glemu-cache-secret}" RUNNER_CACHE_S3_BUCKET_NAME="$${RUNNER_CACHE_S3_BUCKET_NAME:-gitlab-runner-cache}" RUNNER_CACHE_S3_BUCKET_LOCATION="$${RUNNER_CACHE_S3_BUCKET_LOCATION:-us-east-1}" RUNNER_CACHE_S3_INSECURE="$${RUNNER_CACHE_S3_INSECURE:-true}" RUNNER_CACHE_S3_PATH_STYLE="$${RUNNER_CACHE_S3_PATH_STYLE:-true}" /srv/scripts/register-runner.sh'

## Boot only the k3s Kubernetes runner VM
vm-k8s-runner-up: vm-net
	vagrant up k8s-runner

## Rsync Kubernetes runner helper scripts to the k8s runner VM
vm-k8s-runner-sync:
	@vagrant ssh-config k8s-runner > .vagrant-ssh-config
	@rsync -avz \
		-e "ssh -F .vagrant-ssh-config" \
		$(CURDIR)/scripts/register-k8s-runner.sh \
		$(CURDIR)/scripts/deploy-incluster-k8s-runner.sh \
		k8s-runner:/srv/scripts/
	vagrant ssh k8s-runner -c "chmod +x /srv/scripts/register-k8s-runner.sh /srv/scripts/deploy-incluster-k8s-runner.sh"
	@rm -f .vagrant-ssh-config

## SSH into the k3s runner VM
vm-k8s-runner-ssh:
	vagrant ssh k8s-runner

## Show GitLab Runner and k3s status on the k8s runner VM
vm-k8s-runner-status:
	vagrant ssh k8s-runner -c "sudo gitlab-runner status; sudo gitlab-runner list || true; sudo kubectl get nodes -o wide; sudo kubectl get pods -A"

## Tail GitLab Runner service logs on the k8s runner VM
vm-k8s-runner-logs:
	vagrant ssh k8s-runner -c "sudo journalctl -u gitlab-runner -n 200 --no-pager"

## Show GitLab Runner job pods in k3s
vm-k8s-runner-pods:
	vagrant ssh k8s-runner -c "sudo kubectl get pods -n gitlab-runner -o wide"

## Install the emulator Caddy root CA into the k8s runner VM
vm-k8s-runner-install-ca:
	vagrant ssh server -c 'docker exec gitlab_emulator-gitlab-emulator-1 sh -c '"'"'for path in /data/caddy-data/caddy/pki/authorities/local/root.crt /root/.local/share/caddy/pki/authorities/local/root.crt; do test -f "$$path" && exec cat "$$path"; done; echo "Caddy root CA not found" >&2; exit 1'"'"'' > .glemu-root.crt
	@vagrant ssh-config k8s-runner > .vagrant-ssh-config
	rsync -avz -e "ssh -F .vagrant-ssh-config" .glemu-root.crt k8s-runner:/tmp/glemu-root.crt
	vagrant ssh k8s-runner -c "sudo cp /tmp/glemu-root.crt /usr/local/share/ca-certificates/glemu-root.crt && sudo mkdir -p /etc/gitlab-runner/certs && sudo cp /tmp/glemu-root.crt /etc/gitlab-runner/certs/glemu.local.crt && sudo update-ca-certificates && sudo gitlab-runner restart || true"
	@rm -f .glemu-root.crt .vagrant-ssh-config

## Register the k3s runner VM with the emulator using the Kubernetes executor
vm-k8s-runner-register: vm-k8s-runner-sync
	@test -n "$(RUNNER_TOKEN)" || { echo "RUNNER_TOKEN is required"; exit 2; }
	vagrant ssh k8s-runner -c 'RUNNER_TOKEN="$(RUNNER_TOKEN)" RUNNER_URL="$${RUNNER_URL:-https://glemu.local}" RUNNER_NAME="$${RUNNER_NAME:-glemu-k8s-runner}" RUNNER_TAGS="$${RUNNER_TAGS:-k8s}" RUNNER_IMAGE="$${RUNNER_IMAGE:-alpine:3.20}" RUNNER_RUN_UNTAGGED="$${RUNNER_RUN_UNTAGGED:-false}" K8S_NAMESPACE="$${K8S_NAMESPACE:-gitlab-runner}" /srv/scripts/register-k8s-runner.sh'

## Validate official GitLab Runner Kubernetes executor through k3s
vm-k8s-runner-validate: vm-client-scripts-sync vm-k8s-runner-install-ca vm-k8s-runner-register
	vagrant ssh client -c "bash /srv/scripts/k8s-runner-validation.sh"
	vagrant ssh k8s-runner -c "sudo kubectl get pods -n gitlab-runner -o wide"

## Validate Kubernetes executor file/env secrets and trace redaction
vm-k8s-runner-secret-validate: vm-k8s-runner-secret-file-test vm-k8s-runner-secret-env-test vm-k8s-runner-redaction-test

## Validate Kubernetes executor file-mode CI secrets
vm-k8s-runner-secret-file-test: vm-client-scripts-sync vm-k8s-runner-install-ca vm-k8s-runner-register
	vagrant ssh client -c "PROJECT_NAME=k8s-runner-secret-file RUNNER_TAG=k8s SECRET_VALIDATION_MODE=file bash /srv/scripts/runner-secret-validation.sh"

## Validate Kubernetes executor env-mode CI secrets
vm-k8s-runner-secret-env-test: vm-client-scripts-sync vm-k8s-runner-install-ca vm-k8s-runner-register
	vagrant ssh client -c "PROJECT_NAME=k8s-runner-secret-env RUNNER_TAG=k8s SECRET_VALIDATION_MODE=env bash /srv/scripts/runner-secret-validation.sh"

## Validate Kubernetes executor secret trace redaction
vm-k8s-runner-redaction-test: vm-client-scripts-sync vm-k8s-runner-install-ca vm-k8s-runner-register
	vagrant ssh client -c "PROJECT_NAME=k8s-runner-secret-redaction RUNNER_TAG=k8s SECRET_VALIDATION_MODE=redaction bash /srv/scripts/runner-secret-validation.sh"

## Rsync in-cluster Kubernetes runner deployment helper to the k8s runner VM
vm-k8s-incluster-sync: vm-k8s-runner-sync

## Deploy an official GitLab Runner manager pod inside k3s
vm-k8s-incluster-deploy: vm-k8s-incluster-sync vm-k8s-runner-install-ca
	@test -n "$(RUNNER_TOKEN)" || { echo "RUNNER_TOKEN is required"; exit 2; }
	vagrant ssh k8s-runner -c 'RUNNER_TOKEN="$(RUNNER_TOKEN)" RUNNER_URL="$${RUNNER_URL:-https://glemu.local}" RUNNER_NAME="$${RUNNER_NAME:-glemu-k8s-incluster-runner}" RUNNER_TAGS="$${RUNNER_TAGS:-k8s-incluster}" RUNNER_IMAGE="$${RUNNER_IMAGE:-alpine:3.20}" RUNNER_RUN_UNTAGGED="$${RUNNER_RUN_UNTAGGED:-false}" RUNNER_MANAGER_IMAGE="$${RUNNER_MANAGER_IMAGE:-gitlab/gitlab-runner:v19.1.1}" K8S_NAMESPACE="$${K8S_NAMESPACE:-gitlab-runner-incluster}" /srv/scripts/deploy-incluster-k8s-runner.sh'

## Show in-cluster GitLab Runner manager status
vm-k8s-incluster-status:
	vagrant ssh k8s-runner -c "sudo kubectl get deployment,pods -n gitlab-runner-incluster -o wide"

## Tail in-cluster GitLab Runner manager logs
vm-k8s-incluster-logs:
	vagrant ssh k8s-runner -c "sudo kubectl logs -n gitlab-runner-incluster deployment/glemu-k8s-incluster-runner --tail=200"

## Show in-cluster runner manager and job pods
vm-k8s-incluster-pods:
	vagrant ssh k8s-runner -c "sudo kubectl get pods -n gitlab-runner-incluster -o wide"

## Validate official GitLab Runner running as an in-cluster k3s Deployment
vm-k8s-incluster-validate: vm-client-scripts-sync vm-k8s-incluster-deploy
	vagrant ssh client -c "PROJECT_NAME=k8s-incluster-runner-probe RUNNER_TAG=k8s-incluster bash /srv/scripts/k8s-runner-validation.sh"
	vagrant ssh k8s-runner -c "sudo kubectl get pods -n gitlab-runner-incluster -o wide"

## Validate in-cluster Kubernetes executor file-mode CI secrets
vm-k8s-incluster-secret-file-test: vm-client-scripts-sync vm-k8s-incluster-deploy
	vagrant ssh client -c "PROJECT_NAME=k8s-incluster-secret-file RUNNER_TAG=k8s-incluster SECRET_VALIDATION_MODE=file bash /srv/scripts/runner-secret-validation.sh"
	vagrant ssh k8s-runner -c "sudo kubectl get pods -n gitlab-runner-incluster -o wide"
