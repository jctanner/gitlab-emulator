#!/usr/bin/env bash
# Validate CI extends/default/inherit behavior through the official GitLab Runner.
set -uo pipefail

API="${API:-https://glemu.local/api/v4}"
REPO_NAME="${REPO_NAME:-runner-extends-probe}"
REPO_FULL="admin/${REPO_NAME}"
RUNNER_TAG="${RUNNER_TAG:-aipcc-small-x86_64}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-240}"

PASS=0
FAIL=0
ERRORS=""
TMPDIRS=()

pass() { PASS=$((PASS + 1)); printf "  \033[32mPASS\033[0m  %s\n" "$1"; }
fail() { FAIL=$((FAIL + 1)); ERRORS="${ERRORS}\n  - $1"; printf "  \033[31mFAIL\033[0m  %s\n" "$1"; }
section() { printf "\n\033[1m-- %s --\033[0m\n" "$1"; }

mktmp() {
    local d
    d=$(mktemp -d)
    TMPDIRS+=("$d")
    echo "$d"
}

cleanup() {
    for d in "${TMPDIRS[@]}"; do
        rm -rf "$d"
    done
}
trap cleanup EXIT

json_get() {
    jq -r "$1"
}

wait_for_job() {
    local project_id="$1"
    local pipeline_id="$2"
    local job_name="$3"
    local deadline=$((SECONDS + TIMEOUT_SECONDS))
    local jobs status

    while [ "$SECONDS" -lt "$deadline" ]; do
        jobs=$(curl -sk -H "$AUTH_HEADER" "$API/projects/$project_id/pipelines/$pipeline_id/jobs")
        status=$(echo "$jobs" | jq -r ".[] | select(.name == \"$job_name\") | .status" | head -n1)
        case "$status" in
            success)
                echo "$jobs" | jq -r ".[] | select(.name == \"$job_name\") | .id" | head -n1
                return 0
                ;;
            failed|skipped)
                echo "job $job_name ended with status $status" >&2
                return 1
                ;;
        esac
        sleep 3
    done

    echo "timed out waiting for $job_name" >&2
    return 1
}

assert_trace_contains() {
    local name="$1"
    local expected="$2"
    if echo "$TRACE" | grep -Fq "$expected"; then
        pass "$name"
    else
        fail "$name: missing '$expected'"
    fi
}

section "Setup"

TOKEN=$(curl -sk "$API/admin/tokens" \
    -X POST -u "${ADMIN_USERNAME:-admin}:${ADMIN_PASSWORD:-admin}" \
    -H "Content-Type: application/json" \
    -d '{"login":"admin","name":"runner-extends-validation","scopes":["repo","user","admin:org"]}' \
    | json_get .token)

if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
    echo "FATAL: could not create token"
    exit 1
fi
AUTH_HEADER="Authorization: token ${TOKEN}"
pass "Token created"

git config --global http.sslVerify false
git config --global user.name "Runner Extends Validation"
git config --global user.email "runner-extends-validation@example.com"
git config --global commit.gpgsign false

curl -sk -X DELETE -H "$AUTH_HEADER" "$API/repos/$REPO_FULL" > /dev/null 2>&1 || true

CREATE_OUTPUT=$(curl -sk -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
    -d "{\"name\":\"${REPO_NAME}\",\"description\":\"Runner extends validation\",\"auto_init\":false}" \
    "$API/user/repos")

PROJECT_ID=$(echo "$CREATE_OUTPUT" | json_get .id)
if [ -n "$PROJECT_ID" ] && [ "$PROJECT_ID" != "null" ]; then
    pass "Repository created"
else
    fail "Repository creation: $CREATE_OUTPUT"
    exit 1
fi

section "Commit CI config"

WORK=$(mktmp)
REPO_URL="https://admin:${TOKEN}@glemu.local/${REPO_FULL}.git"
git clone "$REPO_URL" "$WORK/repo" > /dev/null 2>&1
cd "$WORK/repo" || exit 1
git checkout -b main > /dev/null 2>&1 || true

cat > .gitlab-ci.yml <<YAML
variables:
  GLOBAL_KEEP: keep
  GLOBAL_DROP: drop

default:
  image: alpine:3.20
  before_script:
    - echo "DEFAULT_BEFORE=ran"
  tags:
    - ${RUNNER_TAG}

.base:
  variables:
    BASE: base

extends_probe:
  extends: .base
  inherit:
    variables:
      - GLOBAL_KEEP
  variables:
    LOCAL: local
  script:
    - echo "GLOBAL_KEEP=\${GLOBAL_KEEP}"
    - echo "GLOBAL_DROP=\${GLOBAL_DROP:-}"
    - echo "BASE=\${BASE}"
    - echo "LOCAL=\${LOCAL}"
    - test "\${GLOBAL_KEEP}" = "keep"
    - test -z "\${GLOBAL_DROP:-}"
    - test "\${BASE}" = "base"
    - test "\${LOCAL}" = "local"
YAML

git add .gitlab-ci.yml
git commit -m "Add extends validation pipeline" > /dev/null 2>&1
git push -u origin main > /dev/null 2>&1
pass ".gitlab-ci.yml pushed"

section "Create pipeline"

PIPELINE_OUTPUT=$(curl -sk -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
    -d '{"ref":"main"}' \
    "$API/projects/$PROJECT_ID/pipeline")
PIPELINE_ID=$(echo "$PIPELINE_OUTPUT" | json_get .id)

if [ -n "$PIPELINE_ID" ] && [ "$PIPELINE_ID" != "null" ]; then
    pass "Pipeline created: $PIPELINE_ID"
else
    fail "Pipeline creation: $PIPELINE_OUTPUT"
    exit 1
fi

section "Wait for job"

JOB_ID=$(wait_for_job "$PROJECT_ID" "$PIPELINE_ID" "extends_probe")
if [ -n "$JOB_ID" ]; then
    pass "extends_probe succeeded: $JOB_ID"
else
    fail "extends_probe did not succeed"
    exit 1
fi

section "Inspect trace"

TRACE=$(curl -sk -H "$AUTH_HEADER" "$API/projects/$PROJECT_ID/jobs/$JOB_ID/trace")

assert_trace_contains "default before_script ran" "DEFAULT_BEFORE=ran"
assert_trace_contains "selected global variable inherited" "GLOBAL_KEEP=keep"
assert_trace_contains "unselected global variable omitted" "GLOBAL_DROP="
assert_trace_contains "extends parent variable present" "BASE=base"
assert_trace_contains "job variable present" "LOCAL=local"

section "Summary"

TOTAL=$((PASS + FAIL))
printf "\n  %d checks: \033[32m%d passed\033[0m" "$TOTAL" "$PASS"
if [ "$FAIL" -gt 0 ]; then
    printf ", \033[31m%d failed\033[0m" "$FAIL"
    printf "\n\n  Failures:%b\n" "$ERRORS"
fi
printf "\n"

exit "$FAIL"
