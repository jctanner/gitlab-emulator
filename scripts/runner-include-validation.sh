#!/usr/bin/env bash
# Validate nested local and project CI includes through the official GitLab Runner.
set -uo pipefail

API="${API:-https://glemu.local/api/v4}"
REPO_NAME="${REPO_NAME:-runner-include-probe}"
TEMPLATE_REPO_NAME="${TEMPLATE_REPO_NAME:-runner-include-template}"
REPO_FULL="admin/${REPO_NAME}"
TEMPLATE_REPO_FULL="admin/${TEMPLATE_REPO_NAME}"
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

create_repo() {
    local name="$1"
    local output id
    curl -sk -X DELETE -H "$AUTH_HEADER" "$API/repos/admin/$name" > /dev/null 2>&1 || true
    output=$(curl -sk -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
        -d "{\"name\":\"${name}\",\"description\":\"Runner include validation\",\"auto_init\":false}" \
        "$API/user/repos")
    id=$(echo "$output" | json_get .id)
    if [ -z "$id" ] || [ "$id" = "null" ]; then
        echo "$output"
        return 1
    fi
    echo "$id"
}

section "Setup"

TOKEN=$(curl -sk "$API/admin/tokens" \
    -X POST -u "${ADMIN_USERNAME:-admin}:${ADMIN_PASSWORD:-admin}" \
    -H "Content-Type: application/json" \
    -d '{"login":"admin","name":"runner-include-validation","scopes":["repo","user","admin:org"]}' \
    | json_get .token)

if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
    echo "FATAL: could not create token"
    exit 1
fi
AUTH_HEADER="Authorization: token ${TOKEN}"
pass "Token created"

git config --global http.sslVerify false
git config --global user.name "Runner Include Validation"
git config --global user.email "runner-include-validation@example.com"
git config --global commit.gpgsign false

TEMPLATE_PROJECT_ID=$(create_repo "$TEMPLATE_REPO_NAME")
if [[ "$TEMPLATE_PROJECT_ID" =~ ^[0-9]+$ ]]; then
    pass "Template repository created"
else
    fail "Template repository creation: $TEMPLATE_PROJECT_ID"
    exit 1
fi

PROJECT_ID=$(create_repo "$REPO_NAME")
if [[ "$PROJECT_ID" =~ ^[0-9]+$ ]]; then
    pass "Repository created"
else
    fail "Repository creation: $PROJECT_ID"
    exit 1
fi

section "Commit template CI config"

WORK=$(mktmp)
TEMPLATE_REPO_URL="https://admin:${TOKEN}@glemu.local/${TEMPLATE_REPO_FULL}.git"
git clone "$TEMPLATE_REPO_URL" "$WORK/template" > /dev/null 2>&1
cd "$WORK/template" || exit 1
git checkout -b main > /dev/null 2>&1 || true
mkdir -p templates

cat > base.yml <<'YAML'
.base:
  image: alpine:3.20
  variables:
    FROM_NESTED: nested
  before_script:
    - echo nested local include before
YAML

cat > templates/python.yml <<'YAML'
include:
  local: base.yml

.template:
  extends: .base
  variables:
    FROM_PROJECT: project
YAML

cat > templates/remote.yml <<'YAML'
.remote:
  variables:
    FROM_REMOTE: remote
YAML

git add base.yml templates/python.yml templates/remote.yml
git commit -m "Add project include templates" > /dev/null 2>&1
git push -u origin main > /dev/null 2>&1
pass "Template CI files pushed"

section "Commit root CI config"

ROOT_REPO_URL="https://admin:${TOKEN}@glemu.local/${REPO_FULL}.git"
git clone "$ROOT_REPO_URL" "$WORK/root" > /dev/null 2>&1
cd "$WORK/root" || exit 1
git checkout -b main > /dev/null 2>&1 || true

cat > .gitlab-ci.yml <<YAML
include:
  - project: ${TEMPLATE_REPO_FULL}
    ref: main
    file: templates/python.yml
  - remote: http://localhost:8000/ui/${TEMPLATE_REPO_FULL}/raw/main/templates/remote.yml
  - template: Bash.gitlab-ci.yml

include_probe:
  extends:
    - .template
    - .remote
  tags:
    - ${RUNNER_TAG}
  script:
    - echo "FROM_NESTED=\${FROM_NESTED}"
    - echo "FROM_PROJECT=\${FROM_PROJECT}"
    - echo "FROM_REMOTE=\${FROM_REMOTE}"
    - test "\${FROM_NESTED}" = "nested"
    - test "\${FROM_PROJECT}" = "project"
    - test "\${FROM_REMOTE}" = "remote"

template_probe:
  extends: .bash-template
  tags:
    - ${RUNNER_TAG}
  script:
    - echo template probe
YAML

git add .gitlab-ci.yml
git commit -m "Add project include validation pipeline" > /dev/null 2>&1
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

JOB_ID=$(wait_for_job "$PROJECT_ID" "$PIPELINE_ID" "include_probe")
if [ -n "$JOB_ID" ]; then
    pass "include_probe succeeded: $JOB_ID"
else
    fail "include_probe did not succeed"
    exit 1
fi

TEMPLATE_JOB_ID=$(wait_for_job "$PROJECT_ID" "$PIPELINE_ID" "template_probe")
if [ -n "$TEMPLATE_JOB_ID" ]; then
    pass "template_probe succeeded: $TEMPLATE_JOB_ID"
else
    fail "template_probe did not succeed"
    exit 1
fi

section "Inspect trace"

TRACE=$(curl -sk -H "$AUTH_HEADER" "$API/projects/$PROJECT_ID/jobs/$JOB_ID/trace")
TEMPLATE_TRACE=$(curl -sk -H "$AUTH_HEADER" "$API/projects/$PROJECT_ID/jobs/$TEMPLATE_JOB_ID/trace")

assert_trace_contains "nested local include before_script ran" "nested local include before"
assert_trace_contains "nested include variable present" "FROM_NESTED=nested"
assert_trace_contains "project include variable present" "FROM_PROJECT=project"
assert_trace_contains "remote include variable present" "FROM_REMOTE=remote"

TRACE="$TEMPLATE_TRACE"
assert_trace_contains "template include before_script ran" "bash template before"

section "Summary"

TOTAL=$((PASS + FAIL))
printf "\n  %d checks: \033[32m%d passed\033[0m" "$TOTAL" "$PASS"
if [ "$FAIL" -gt 0 ]; then
    printf ", \033[31m%d failed\033[0m" "$FAIL"
    printf "\n\n  Failures:%b\n" "$ERRORS"
fi
printf "\n"

exit "$FAIL"
