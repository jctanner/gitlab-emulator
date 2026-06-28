#!/usr/bin/env bash
# Validate CI variable precedence through the official GitLab Runner.
set -uo pipefail

API="${API:-https://glemu.local/api/v4}"
REPO_NAME="${REPO_NAME:-runner-variable-probe}"
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
    -d '{"login":"admin","name":"runner-variable-validation","scopes":["repo","user","admin:org"]}' \
    | json_get .token)

if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
    echo "FATAL: could not create token"
    exit 1
fi
AUTH_HEADER="Authorization: token ${TOKEN}"
pass "Token created"

git config --global http.sslVerify false
git config --global user.name "Runner Variable Validation"
git config --global user.email "runner-variable-validation@example.com"
git config --global commit.gpgsign false

curl -sk -X DELETE -H "$AUTH_HEADER" "$API/repos/$REPO_FULL" > /dev/null 2>&1 || true

CREATE_OUTPUT=$(curl -sk -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
    -d "{\"name\":\"${REPO_NAME}\",\"description\":\"Runner variable validation\",\"auto_init\":false}" \
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
  FROM_YAML: yaml
  FROM_PIPELINE: yaml-override
  SHARED: yaml
  CI_COMMIT_REF_NAME: yaml-ref
  YAML_FILE:
    value: yaml-file-content
    variable_type: file
  RAW_LITERAL:
    value: "\$FROM_YAML-literal"
    expand: false

variable_probe:
  image: alpine:3.20
  tags:
    - ${RUNNER_TAG}
  variables:
    SHARED: job
    JOB_ONLY: job
    JOB_FILE:
      value: job-file-content
      file: true
  script:
    - echo "PIPELINE_ONLY=\${PIPELINE_ONLY}"
    - echo "FROM_PIPELINE=\${FROM_PIPELINE}"
    - echo "FROM_YAML=\${FROM_YAML}"
    - echo "JOB_ONLY=\${JOB_ONLY}"
    - echo "SHARED=\${SHARED}"
    - echo "CI_COMMIT_REF_NAME=\${CI_COMMIT_REF_NAME}"
    - echo "YAML_FILE_CONTENT=\$(cat "\${YAML_FILE}")"
    - echo "JOB_FILE_CONTENT=\$(cat "\${JOB_FILE}")"
    - echo "PIPELINE_FILE_CONTENT=\$(cat "\${PIPELINE_FILE}")"
    - echo "RAW_LITERAL=\${RAW_LITERAL}"
    - echo "PIPELINE_RAW=\${PIPELINE_RAW}"
    - test "\${PIPELINE_ONLY}" = "pipeline"
    - test "\${FROM_PIPELINE}" = "yaml-override"
    - test "\${FROM_YAML}" = "yaml"
    - test "\${JOB_ONLY}" = "job"
    - test "\${SHARED}" = "job"
    - test "\${CI_COMMIT_REF_NAME}" = "yaml-ref"
    - test "\$(cat "\${YAML_FILE}")" = "yaml-file-content"
    - test "\$(cat "\${JOB_FILE}")" = "job-file-content"
    - test "\$(cat "\${PIPELINE_FILE}")" = "pipeline-file-content"
    - test "\${RAW_LITERAL}" = '\$FROM_YAML-literal'
    - test "\${PIPELINE_RAW}" = '\$FROM_YAML-pipeline'
YAML

git add .gitlab-ci.yml
git commit -m "Add variable validation pipeline" > /dev/null 2>&1
git push -u origin main > /dev/null 2>&1
pass ".gitlab-ci.yml pushed"

section "Create pipeline"

PIPELINE_PAYLOAD=$(jq -n \
    '{ref:"main", variables:[
        {key:"PIPELINE_ONLY", value:"pipeline"},
        {key:"FROM_PIPELINE", value:"pipeline"},
        {key:"SHARED", value:"pipeline"},
        {key:"CI_COMMIT_REF_NAME", value:"pipeline-ref"},
        {key:"PIPELINE_FILE", value:"pipeline-file-content", variable_type:"file"},
        {key:"PIPELINE_RAW", value:"$FROM_YAML-pipeline", raw:true}
    ]}')
PIPELINE_OUTPUT=$(curl -sk -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
    -d "$PIPELINE_PAYLOAD" \
    "$API/projects/$PROJECT_ID/pipeline")
PIPELINE_ID=$(echo "$PIPELINE_OUTPUT" | json_get .id)

if [ -n "$PIPELINE_ID" ] && [ "$PIPELINE_ID" != "null" ]; then
    pass "Pipeline created: $PIPELINE_ID"
else
    fail "Pipeline creation: $PIPELINE_OUTPUT"
    exit 1
fi

section "Wait for job"

JOB_ID=$(wait_for_job "$PROJECT_ID" "$PIPELINE_ID" "variable_probe")
if [ -n "$JOB_ID" ]; then
    pass "variable_probe succeeded: $JOB_ID"
else
    fail "variable_probe did not succeed"
    exit 1
fi

section "Inspect trace"

TRACE=$(curl -sk -H "$AUTH_HEADER" "$API/projects/$PROJECT_ID/jobs/$JOB_ID/trace")

assert_trace_contains "pipeline variable present" "PIPELINE_ONLY=pipeline"
assert_trace_contains "YAML variable overrides pipeline variable" "FROM_PIPELINE=yaml-override"
assert_trace_contains "YAML-only variable present" "FROM_YAML=yaml"
assert_trace_contains "job-only variable present" "JOB_ONLY=job"
assert_trace_contains "job variable overrides YAML and pipeline variables" "SHARED=job"
assert_trace_contains "YAML variable can override predefined variable" "CI_COMMIT_REF_NAME=yaml-ref"
assert_trace_contains "YAML file variable content present" "YAML_FILE_CONTENT=yaml-file-content"
assert_trace_contains "job file variable content present" "JOB_FILE_CONTENT=job-file-content"
assert_trace_contains "pipeline file variable content present" "PIPELINE_FILE_CONTENT=pipeline-file-content"
assert_trace_contains "YAML raw variable did not expand" 'RAW_LITERAL=$FROM_YAML-literal'
assert_trace_contains "pipeline raw variable did not expand" 'PIPELINE_RAW=$FROM_YAML-pipeline'

section "Summary"

TOTAL=$((PASS + FAIL))
printf "\n  %d checks: \033[32m%d passed\033[0m" "$TOTAL" "$PASS"
if [ "$FAIL" -gt 0 ]; then
    printf ", \033[31m%d failed\033[0m" "$FAIL"
    printf "\n\n  Failures:%b\n" "$ERRORS"
fi
printf "\n"

exit "$FAIL"
