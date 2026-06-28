#!/usr/bin/env bash
# Validate official GitLab Runner artifact download through needs:artifacts.
set -uo pipefail

API="${API:-https://glemu.local/api/v4}"
REPO_NAME="${REPO_NAME:-runner-artifact-needs-probe}"
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
    -X POST -u "${ADMIN_USERNAME:-admin}:${ADMIN_PASSWORD:-admin}" \
    -H "Content-Type: application/json" \
    -d '{"login":"admin","name":"runner-artifact-needs-validation","scopes":["repo","user","admin:org"]}' \
    | jq -r .token)

if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
    echo "FATAL: could not create token"
    exit 1
fi
AUTH_HEADER="Authorization: token ${TOKEN}"
pass "Token created"

git config --global http.sslVerify false
git config --global user.name "Runner Artifact Needs Validation"
git config --global user.email "runner-artifact-needs-validation@example.com"
git config --global commit.gpgsign false

curl -sk -X DELETE -H "$AUTH_HEADER" "$API/repos/$REPO_FULL" > /dev/null 2>&1 || true

CREATE_OUTPUT=$(curl -sk -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
    -d "{\"name\":\"${REPO_NAME}\",\"description\":\"Runner artifact needs validation\",\"auto_init\":false}" \
    "$API/user/repos")
PROJECT_ID=$(echo "$CREATE_OUTPUT" | jq -r .id)

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
stages:
  - build
  - test

build_artifact:
  stage: build
  image: alpine:3.20
  tags:
    - ${RUNNER_TAG}
  script:
    - mkdir -p out
    - echo from-build > out/result.txt
  artifacts:
    paths:
      - out/result.txt

build_extra:
  stage: build
  image: alpine:3.20
  tags:
    - ${RUNNER_TAG}
  script:
    - mkdir -p extra
    - echo from-extra > extra/extra.txt
  artifacts:
    paths:
      - extra/extra.txt

consume_artifact:
  stage: test
  image: alpine:3.20
  tags:
    - ${RUNNER_TAG}
  needs:
    - job: build_extra
      artifacts: true
    - job: build_artifact
      artifacts: true
  script:
    - test -f out/result.txt
    - test -f extra/extra.txt
    - grep from-build out/result.txt
    - grep from-extra extra/extra.txt
YAML

git add .gitlab-ci.yml
git commit -m "Add needs artifacts validation pipeline" > /dev/null 2>&1
git push -u origin main > /dev/null 2>&1
pass ".gitlab-ci.yml pushed"

section "Create pipeline"

PIPELINE_OUTPUT=$(curl -sk -X POST -H "$AUTH_HEADER" -H "Content-Type: application/json" \
    -d '{"ref":"main"}' \
    "$API/projects/$PROJECT_ID/pipeline")
PIPELINE_ID=$(echo "$PIPELINE_OUTPUT" | jq -r .id)

if [ -n "$PIPELINE_ID" ] && [ "$PIPELINE_ID" != "null" ]; then
    pass "Pipeline created: $PIPELINE_ID"
else
    fail "Pipeline creation: $PIPELINE_OUTPUT"
    exit 1
fi

section "Wait for jobs"

BUILD_JOB_ID=$(wait_for_job "$PROJECT_ID" "$PIPELINE_ID" "build_artifact")
if [ -n "$BUILD_JOB_ID" ]; then
    pass "build_artifact succeeded: $BUILD_JOB_ID"
else
    fail "build_artifact did not succeed"
    exit 1
fi

EXTRA_JOB_ID=$(wait_for_job "$PROJECT_ID" "$PIPELINE_ID" "build_extra")
if [ -n "$EXTRA_JOB_ID" ]; then
    pass "build_extra succeeded: $EXTRA_JOB_ID"
else
    fail "build_extra did not succeed"
    exit 1
fi

CONSUME_JOB_ID=$(wait_for_job "$PROJECT_ID" "$PIPELINE_ID" "consume_artifact")
if [ -n "$CONSUME_JOB_ID" ]; then
    pass "consume_artifact succeeded: $CONSUME_JOB_ID"
else
    fail "consume_artifact did not succeed"
    exit 1
fi

section "Inspect trace"

TRACE=$(curl -sk -H "$AUTH_HEADER" "$API/projects/$PROJECT_ID/jobs/$CONSUME_JOB_ID/trace")

if echo "$TRACE" | grep -q "Downloading artifacts for build_extra" \
    && echo "$TRACE" | grep -q "Downloading artifacts for build_artifact"; then
    pass "trace includes dependency artifact downloads"
else
    fail "trace missing dependency artifact download marker"
fi

EXTRA_LINE=$(echo "$TRACE" | grep -n "Downloading artifacts for build_extra" | head -n1 | cut -d: -f1)
BUILD_LINE=$(echo "$TRACE" | grep -n "Downloading artifacts for build_artifact" | head -n1 | cut -d: -f1)
if [ -n "$EXTRA_LINE" ] && [ -n "$BUILD_LINE" ] && [ "$EXTRA_LINE" -lt "$BUILD_LINE" ]; then
    pass "dependency artifact downloads follow needs order"
else
    fail "dependency artifact downloads did not follow needs order"
fi

if echo "$TRACE" | grep -q "from-build"; then
    pass "downstream job saw artifact contents"
else
    fail "downstream trace missing artifact contents"
fi

if echo "$TRACE" | grep -q "from-extra"; then
    pass "downstream job saw second artifact contents"
else
    fail "downstream trace missing second artifact contents"
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
