#!/usr/bin/env bash
# Validate CI rules job selection through the official GitLab Runner.
set -uo pipefail

API="${API:-https://glemu.local/api/v4}"
REPO_NAME="${REPO_NAME:-runner-rules-probe}"
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

section "Setup"

TOKEN=$(curl -sk "$API/admin/tokens" \
    -X POST -H "Content-Type: application/json" \
    -d '{"login":"admin","name":"runner-rules-validation","scopes":["repo","user","admin:org"]}' \
    | json_get .token)

if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
    echo "FATAL: could not create token"
    exit 1
fi
AUTH_HEADER="Authorization: token ${TOKEN}"
pass "Token created"

git config --global http.sslVerify false
git config --global user.name "Runner Rules Validation"
git config --global user.email "runner-rules-validation@example.com"
git config --global commit.gpgsign false

curl -sk -X DELETE -H "$AUTH_HEADER" "$API/repos/$REPO_FULL" > /dev/null 2>&1 || true

CREATE_OUTPUT=$(curl -sk -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
    -d "{\"name\":\"${REPO_NAME}\",\"description\":\"Runner rules validation\",\"auto_init\":false}" \
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
mkdir -p docs src
printf "print('hello')\n" > src/app.py
printf "# docs\n" > docs/readme.md

cat > .gitlab-ci.yml <<YAML
variables:
  DEPLOY_TARGET: prod

rules_run:
  image: alpine:3.20
  tags:
    - ${RUNNER_TAG}
  script:
    - echo "rules_run executed"
    - test "\${DEPLOY_TARGET}" = "prod"
  rules:
    - if: '\$DEPLOY_TARGET == "prod" && \$CI_COMMIT_REF_NAME == "main"'

exists_run:
  image: alpine:3.20
  tags:
    - ${RUNNER_TAG}
  script:
    - echo "exists_run executed"
  rules:
    - exists:
        - src/*.py

changes_run:
  image: alpine:3.20
  tags:
    - ${RUNNER_TAG}
  script:
    - echo "changes_run executed"
  rules:
    - changes:
        - docs/**

manual_gate:
  image: alpine:3.20
  tags:
    - ${RUNNER_TAG}
  script:
    - echo "manual should not run"
  rules:
    - when: manual

never_job:
  image: alpine:3.20
  tags:
    - ${RUNNER_TAG}
  script:
    - echo "never should not exist"
  rules:
    - if: '\$DEPLOY_TARGET == "prod"'
      when: never
YAML

git add .gitlab-ci.yml docs/readme.md src/app.py
git commit -m "Add rules validation pipeline" > /dev/null 2>&1
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

section "Inspect selected jobs"

JOBS=$(curl -sk -H "$AUTH_HEADER" "$API/projects/$PROJECT_ID/pipelines/$PIPELINE_ID/jobs")
for name in rules_run exists_run changes_run manual_gate; do
    if echo "$JOBS" | jq -e ".[] | select(.name == \"$name\")" > /dev/null; then
        pass "$name was created"
    else
        fail "$name was not created"
    fi
done

if echo "$JOBS" | jq -e '.[] | select(.name == "never_job")' > /dev/null; then
    fail "never_job was created"
else
    pass "never_job was not created"
fi

MANUAL_STATUS=$(echo "$JOBS" | jq -r '.[] | select(.name == "manual_gate") | .status' | head -n1)
if [ "$MANUAL_STATUS" = "manual" ]; then
    pass "manual_gate persisted as manual"
else
    fail "manual_gate status was $MANUAL_STATUS"
fi

section "Wait for runnable jobs"

for name in rules_run exists_run changes_run; do
    JOB_ID=$(wait_for_job "$PROJECT_ID" "$PIPELINE_ID" "$name")
    if [ -n "$JOB_ID" ]; then
        pass "$name succeeded: $JOB_ID"
    else
        fail "$name did not succeed"
        exit 1
    fi
done

section "Summary"

TOTAL=$((PASS + FAIL))
printf "\n  %d checks: \033[32m%d passed\033[0m" "$TOTAL" "$PASS"
if [ "$FAIL" -gt 0 ]; then
    printf ", \033[31m%d failed\033[0m" "$FAIL"
    printf "\n\n  Failures:%b\n" "$ERRORS"
fi
printf "\n"

exit "$FAIL"
