#!/usr/bin/env bash
# Validate official GitLab Runner Kubernetes executor against the emulator.
#
# Intended execution environment: the Vagrant "client" VM.
set -euo pipefail

API="${API:-https://glemu.local/api/v4}"
HOST="${GITLAB_HOST:-glemu.local}"
PROJECT_NAME="${PROJECT_NAME:-k8s-runner-probe}"
PROJECT_REF="admin%2F${PROJECT_NAME}"
RUNNER_TAG="${RUNNER_TAG:-k8s}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-240}"

pass() { printf "  \033[32mPASS\033[0m  %s\n" "$1"; }
section() { printf "\n\033[1m-- %s --\033[0m\n" "$1"; }
json_get() { jq -r "$1"; }

require_cmd() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "FATAL: $1 is required" >&2
        exit 2
    fi
}

wait_for_job_success() {
    local project_id="$1"
    local pipeline_id="$2"
    local job_name="$3"
    local deadline=$((SECONDS + TIMEOUT_SECONDS))
    local jobs status job_id

    while [ "$SECONDS" -lt "$deadline" ]; do
        jobs=$(curl -sk -H "$AUTH_HEADER" "$API/projects/$project_id/pipelines/$pipeline_id/jobs")
        status=$(echo "$jobs" | jq -r ".[] | select(.name == \"$job_name\") | .status" | head -n1)
        job_id=$(echo "$jobs" | jq -r ".[] | select(.name == \"$job_name\") | .id" | head -n1)
        case "$status" in
            success)
                echo "$job_id"
                return 0
                ;;
            failed|canceled|skipped)
                echo "job $job_name ended with status $status" >&2
                echo "$jobs" | jq . >&2
                return 1
                ;;
            pending|running)
                printf "  waiting for %s: %s\n" "$job_name" "$status" >&2
                ;;
            "")
                printf "  waiting for %s to appear\n" "$job_name" >&2
                ;;
            *)
                printf "  waiting for %s: %s\n" "$job_name" "$status" >&2
                ;;
        esac
        sleep 3
    done

    echo "timed out waiting for $job_name after ${TIMEOUT_SECONDS}s" >&2
    echo "Check /admin/runners, /admin/ci-lab, and k8s runner logs." >&2
    return 1
}

section "Setup"

require_cmd curl
require_cmd jq

TOKEN=$(curl -sk "$API/admin/tokens" \
    -X POST \
    -u "${ADMIN_USERNAME:-admin}:${ADMIN_PASSWORD:-admin}" \
    -H "Content-Type: application/json" \
    -d '{"login":"admin","name":"k8s-runner-validation","scopes":["repo","user","admin:org"]}' \
    | json_get .token)

if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
    echo "FATAL: could not create admin token" >&2
    exit 1
fi
AUTH_HEADER="PRIVATE-TOKEN: ${TOKEN}"
pass "admin token created"

RUNNERS=$(curl -sk -H "$AUTH_HEADER" "$API/runners")
if echo "$RUNNERS" | jq -e '.[] | select(.executor == "kubernetes" or (.tag_list // [] | index("k8s")))' >/dev/null; then
    pass "kubernetes runner visible in runner API"
else
    echo "FATAL: no k8s runner visible in /api/v4/runners" >&2
    echo "$RUNNERS" | jq . >&2
    exit 1
fi

PROJECT_JSON=$(curl -sk -H "$AUTH_HEADER" "$API/projects/$PROJECT_REF")
PROJECT_ID=$(echo "$PROJECT_JSON" | jq -r '.id // empty')

if [ -n "$PROJECT_ID" ]; then
    pass "project reused: $PROJECT_ID"
else
    PROJECT_JSON=$(curl -sk -X POST \
        -H "$AUTH_HEADER" \
        -H "Content-Type: application/json" \
        -d "{\"name\":\"$PROJECT_NAME\",\"path\":\"$PROJECT_NAME\",\"visibility\":\"public\",\"initialize_with_readme\":true}" \
        "$API/projects")
    PROJECT_ID=$(echo "$PROJECT_JSON" | jq -r '.id // empty')
    if [ -z "$PROJECT_ID" ]; then
        echo "FATAL: project create failed: $PROJECT_JSON" >&2
        exit 1
    fi
    pass "project created: $PROJECT_ID"
fi

section "Save CI config"

RUN_MARKER="k8s-runner-validation-$(date +%s)"
CI_YAML=$(cat <<YAML
stages:
  - validate

k8s_probe:
  image: alpine:3.20
  stage: validate
  tags:
    - ${RUNNER_TAG}
  script:
    - echo "K8S_RUNNER_MARKER=${RUN_MARKER}"
    - echo "kubernetes executor validation"
    - uname -a
    - mkdir -p out
    - echo "${RUN_MARKER}" > out/result.txt
  artifacts:
    paths:
      - out/result.txt
YAML
)

FILE_PAYLOAD=$(jq -n \
    --arg branch "main" \
    --arg content "$CI_YAML" \
    --arg message "Update k8s runner validation pipeline" \
    '{branch:$branch, content:$content, commit_message:$message}')

SAVE_OUTPUT=$(curl -sk -X PUT \
    -H "$AUTH_HEADER" \
    -H "Content-Type: application/json" \
    -d "$FILE_PAYLOAD" \
    "$API/projects/$PROJECT_ID/repository/files/.gitlab-ci.yml")

if ! echo "$SAVE_OUTPUT" | jq -e '.file_path == ".gitlab-ci.yml"' >/dev/null 2>&1; then
    SAVE_OUTPUT=$(curl -sk -X POST \
        -H "$AUTH_HEADER" \
        -H "Content-Type: application/json" \
        -d "$FILE_PAYLOAD" \
        "$API/projects/$PROJECT_ID/repository/files/.gitlab-ci.yml")
fi

if echo "$SAVE_OUTPUT" | jq -e '.file_path == ".gitlab-ci.yml"' >/dev/null 2>&1; then
    pass ".gitlab-ci.yml saved"
else
    echo "FATAL: could not save CI config: $SAVE_OUTPUT" >&2
    exit 1
fi

section "Create pipeline"

PIPELINE_OUTPUT=$(curl -sk -X POST \
    -H "$AUTH_HEADER" \
    -H "Content-Type: application/json" \
    -d '{"ref":"main"}' \
    "$API/projects/$PROJECT_ID/pipeline")
PIPELINE_ID=$(echo "$PIPELINE_OUTPUT" | jq -r '.id // empty')

if [ -z "$PIPELINE_ID" ]; then
    echo "FATAL: pipeline create failed: $PIPELINE_OUTPUT" >&2
    exit 1
fi
pass "pipeline created: $PIPELINE_ID"

section "Wait for Kubernetes executor"

JOB_ID=$(wait_for_job_success "$PROJECT_ID" "$PIPELINE_ID" "k8s_probe")
pass "k8s_probe succeeded: $JOB_ID"

TRACE=$(curl -sk -H "$AUTH_HEADER" "$API/projects/$PROJECT_ID/jobs/$JOB_ID/trace")
if echo "$TRACE" | grep -Fq "K8S_RUNNER_MARKER=${RUN_MARKER}" \
    && echo "$TRACE" | grep -Fq "kubernetes executor validation"; then
    pass "job trace contains k8s markers"
else
    echo "FATAL: trace did not contain expected k8s markers" >&2
    echo "$TRACE" >&2
    exit 1
fi

JOB_JSON=$(curl -sk -H "$AUTH_HEADER" "$API/projects/$PROJECT_ID/jobs/$JOB_ID")
RUNNER_DESCRIPTION=$(echo "$JOB_JSON" | jq -r '.runner.description // empty')
HAS_K8S_TAG=$(echo "$JOB_JSON" | jq -r --arg tag "$RUNNER_TAG" '(.tag_list // []) | index($tag) != null')
ARTIFACT_COUNT=$(echo "$JOB_JSON" | jq -r '.artifacts | length')

if [ "$HAS_K8S_TAG" = "true" ]; then
    pass "job kept k8s runner tag; runner reported as ${RUNNER_DESCRIPTION:-unknown}"
else
    echo "FATAL: completed job did not preserve expected k8s tag" >&2
    echo "$JOB_JSON" | jq . >&2
    exit 1
fi

if [ "$ARTIFACT_COUNT" -gt 0 ]; then
    pass "artifact metadata recorded"
else
    echo "FATAL: no artifacts recorded for job $JOB_ID" >&2
    echo "$JOB_JSON" | jq . >&2
    exit 1
fi

section "URLs"

printf "  Admin runners: https://%s/admin/runners\n" "$HOST"
printf "  CI Lab:        https://%s/admin/ci-lab?project_id=%s&pipeline_id=%s&job_id=%s\n" "$HOST" "$PROJECT_ID" "$PIPELINE_ID" "$JOB_ID"
printf "  Job API:       %s/projects/%s/jobs/%s\n" "$API" "$PROJECT_ID" "$JOB_ID"

section "Summary"
pass "Kubernetes runner validation completed"
