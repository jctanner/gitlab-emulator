#!/usr/bin/env bash
# Validate CI secrets through the official GitLab Runner.
set -uo pipefail

API="${API:-https://glemu.local/api/v4}"
RUNNER_TAG="${RUNNER_TAG:-aipcc-small-x86_64}"
SECRET_VALIDATION_MODE="${SECRET_VALIDATION_MODE:-all}"
REPO_NAME="${REPO_NAME:-runner-secret-probe-${SECRET_VALIDATION_MODE}}"
REPO_FULL="admin/${REPO_NAME}"
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

mode_includes() {
    local wanted="$1"
    [ "$SECRET_VALIDATION_MODE" = "all" ] || [ "$SECRET_VALIDATION_MODE" = "$wanted" ]
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

assert_trace_not_contains() {
    local name="$1"
    local unexpected="$2"
    if echo "$TRACE" | grep -Fq "$unexpected"; then
        fail "$name: found '$unexpected'"
    else
        pass "$name"
    fi
}

section "Setup"

TOKEN=$(curl -sk "$API/admin/tokens" \
    -X POST -H "Content-Type: application/json" \
    -d '{"login":"admin","name":"runner-secret-validation","scopes":["repo","user","admin:org"]}' \
    | json_get .token)

if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
    echo "FATAL: could not create token"
    exit 1
fi
AUTH_HEADER="Authorization: token ${TOKEN}"
pass "Token created"

git config --global http.sslVerify false
git config --global user.name "Runner Secret Validation"
git config --global user.email "runner-secret-validation@example.com"
git config --global commit.gpgsign false

curl -sk -X DELETE -H "$AUTH_HEADER" "$API/repos/$REPO_FULL" > /dev/null 2>&1 || true

CREATE_OUTPUT=$(curl -sk -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
    -d "{\"name\":\"${REPO_NAME}\",\"description\":\"Runner secret validation\",\"auto_init\":false}" \
    "$API/user/repos")

PROJECT_ID=$(echo "$CREATE_OUTPUT" | json_get .id)
if [ -n "$PROJECT_ID" ] && [ "$PROJECT_ID" != "null" ]; then
    pass "Repository created"
else
    fail "Repository creation: $CREATE_OUTPUT"
    exit 1
fi

section "Create secrets"

FILE_SECRET_OUTPUT=$(curl -sk -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
    -d '{"name":"DATABASE_PASSWORD","value":"database-file-secret"}' \
    "$API/projects/$PROJECT_ID/secrets")
if [ "$(echo "$FILE_SECRET_OUTPUT" | json_get .name)" = "DATABASE_PASSWORD" ]; then
    pass "File-mode secret created"
else
    fail "File-mode secret creation: $FILE_SECRET_OUTPUT"
    exit 1
fi

ENV_SECRET_OUTPUT=$(curl -sk -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
    -d '{"name":"API_TOKEN","value":"api-env-secret"}' \
    "$API/projects/$PROJECT_ID/secrets")
if [ "$(echo "$ENV_SECRET_OUTPUT" | json_get .name)" = "API_TOKEN" ]; then
    pass "Env-mode secret created"
else
    fail "Env-mode secret creation: $ENV_SECRET_OUTPUT"
    exit 1
fi

section "Commit CI config"

WORK=$(mktmp)
REPO_URL="https://admin:${TOKEN}@glemu.local/${REPO_FULL}.git"
git clone "$REPO_URL" "$WORK/repo" > /dev/null 2>&1
cd "$WORK/repo" || exit 1
git checkout -b main > /dev/null 2>&1 || true

cat > .gitlab-ci.yml <<YAML
secret_file_probe:
  image: alpine:3.20
  tags:
    - ${RUNNER_TAG}
  secrets:
    DB_PASSWORD:
      gitlab_secrets_manager:
        name: DATABASE_PASSWORD
  script:
    - test "\$(cat "\${DB_PASSWORD}")" = "database-file-secret"
    - echo "FILE_SECRET_READ=ok"
    - echo "FILE_SECRET_VALUE=\$(cat "\${DB_PASSWORD}")"

secret_env_probe:
  image: alpine:3.20
  tags:
    - ${RUNNER_TAG}
  secrets:
    API_TOKEN:
      gitlab_secrets_manager:
        name: API_TOKEN
      file: false
  script:
    - test "\${API_TOKEN}" = "api-env-secret"
    - echo "ENV_SECRET_READ=ok"
    - echo "ENV_SECRET_VALUE=\${API_TOKEN}"
YAML

git add .gitlab-ci.yml
git commit -m "Add secret validation pipeline" > /dev/null 2>&1
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

if mode_includes "file" || mode_includes "redaction"; then
    section "Validate file-mode secret"
    FILE_JOB_ID=$(wait_for_job "$PROJECT_ID" "$PIPELINE_ID" "secret_file_probe")
    if [ -n "$FILE_JOB_ID" ]; then
        pass "secret_file_probe succeeded: $FILE_JOB_ID"
    else
        fail "secret_file_probe did not succeed"
        exit 1
    fi
    TRACE=$(curl -sk -H "$AUTH_HEADER" "$API/projects/$PROJECT_ID/jobs/$FILE_JOB_ID/trace")
    assert_trace_contains "file secret was readable" "FILE_SECRET_READ=ok"
    assert_trace_contains "file secret trace was masked" "FILE_SECRET_VALUE=[MASKED]"
    assert_trace_not_contains "file secret value not leaked" "database-file-secret"
fi

if mode_includes "env" || mode_includes "redaction"; then
    section "Validate env-mode secret"
    ENV_JOB_ID=$(wait_for_job "$PROJECT_ID" "$PIPELINE_ID" "secret_env_probe")
    if [ -n "$ENV_JOB_ID" ]; then
        pass "secret_env_probe succeeded: $ENV_JOB_ID"
    else
        fail "secret_env_probe did not succeed"
        exit 1
    fi
    TRACE=$(curl -sk -H "$AUTH_HEADER" "$API/projects/$PROJECT_ID/jobs/$ENV_JOB_ID/trace")
    assert_trace_contains "env secret was readable" "ENV_SECRET_READ=ok"
    assert_trace_contains "env secret trace was masked" "ENV_SECRET_VALUE=[MASKED]"
    assert_trace_not_contains "env secret value not leaked" "api-env-secret"
fi

section "Summary"

TOTAL=$((PASS + FAIL))
printf "\n  %d checks: \033[32m%d passed\033[0m" "$TOTAL" "$PASS"
if [ "$FAIL" -gt 0 ]; then
    printf ", \033[31m%d failed\033[0m" "$FAIL"
    printf "\n\n  Failures:%b\n" "$ERRORS"
fi
printf "\n"

exit "$FAIL"
