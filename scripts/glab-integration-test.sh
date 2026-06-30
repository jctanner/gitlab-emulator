#!/usr/bin/env bash
# glab CLI validation for the GitLab emulator.
#
# Intended execution environment: the Vagrant "client" VM.
# This script writes glab and git state only inside that VM.
set -uo pipefail

GLAB="${GLAB:-/srv/bin/glab}"
API="${API:-https://glemu.local/api/v4}"
HOST="${GITLAB_HOST:-glemu.local}"
export GITLAB_HOST="$HOST"
export GITLAB_INSECURE="${GITLAB_INSECURE:-1}"

RUN_ID="${RUN_ID:-$(date +%s)-$$}"
PROJECT_NAME="glab-smoke-${RUN_ID}"
PROJECT_PATH="$PROJECT_NAME"
PROJECT_REF="admin%2F${PROJECT_PATH}"
GROUP_PATH="glab-group-${RUN_ID}"
SUBGROUP_PATH="sub-${RUN_ID}"
GROUP_REF="$GROUP_PATH"
SUBGROUP_REF="${GROUP_PATH}%2F${SUBGROUP_PATH}"
GROUP_PROJECT_PATH="nested-project-${RUN_ID}"
GROUP_PROJECT_REF="${GROUP_PATH}%2F${SUBGROUP_PATH}%2F${GROUP_PROJECT_PATH}"
CLI_PROJECT_NAME="glab-repo-cli-${RUN_ID}"
CLI_PROJECT_PATH="$CLI_PROJECT_NAME"
CLI_PROJECT_REF="admin%2F${CLI_PROJECT_PATH}"

PASS=0
FAIL=0
ERRORS=""
TMPDIRS=()
ORIGINAL_HOME="${HOME:-/home/vagrant}"
TEST_HOME=""

pass() { PASS=$((PASS + 1)); printf "  \033[32mPASS\033[0m  %s\n" "$1"; }
fail() { FAIL=$((FAIL + 1)); ERRORS="${ERRORS}\n  - $1"; printf "  \033[31mFAIL\033[0m  %s\n" "$1"; }
section() { printf "\n\033[1m-- %s --\033[0m\n" "$1"; }

mktmp() {
    local dir
    dir=$(mktemp -d)
    TMPDIRS+=("$dir")
    echo "$dir"
}

cleanup() {
    if [ -n "${TOKEN:-}" ]; then
        curl -sk -X DELETE -H "PRIVATE-TOKEN: $TOKEN" \
            "$API/projects/$PROJECT_REF" >/dev/null 2>&1 || true
        curl -sk -X DELETE -H "PRIVATE-TOKEN: $TOKEN" \
            "$API/projects/$CLI_PROJECT_REF" >/dev/null 2>&1 || true
        curl -sk -X DELETE -H "PRIVATE-TOKEN: $TOKEN" \
            "$API/repos/admin/$PROJECT_PATH" >/dev/null 2>&1 || true
    fi
    for dir in "${TMPDIRS[@]}"; do
        rm -rf "$dir"
    done
}
trap cleanup EXIT

require_cmd() {
    local path="$1"
    local name="$2"
    if ! command -v "$path" >/dev/null 2>&1 && [ ! -x "$path" ]; then
        echo "FATAL: $name not found at $path" >&2
        echo "Run 'make vm-client-install-glab' or 'make vm-client-sync'." >&2
        exit 2
    fi
}

json_get() {
    jq -r "$1" 2>/dev/null
}

glab_api() {
    "$GLAB" api "$@" 2>&1
}

assert_json_field() {
    local name="$1"
    local json="$2"
    local filter="$3"
    if echo "$json" | jq -e "$filter" >/dev/null 2>&1; then
        pass "$name"
    else
        fail "$name: $json"
    fi
}

assert_contains() {
    local name="$1"
    local text="$2"
    local expected="$3"
    if echo "$text" | grep -q "$expected"; then
        pass "$name"
    else
        fail "$name: expected '$expected' in: $text"
    fi
}

section "Setup"

require_cmd "$GLAB" "glab"
require_cmd "jq" "jq"
require_cmd "git" "git"
require_cmd "curl" "curl"

echo "Using glab: $("$GLAB" --version 2>/dev/null | head -1)"
echo "Using API: $API"

TOKEN=$(curl -sk "$API/admin/tokens" \
    -X POST \
    -u "${ADMIN_USERNAME:-admin}:${ADMIN_PASSWORD:-admin}" \
    -H "Content-Type: application/json" \
    -d "{\"login\":\"admin\",\"name\":\"glab-smoke-${RUN_ID}\",\"scopes\":[\"repo\",\"user\",\"admin:org\"]}" \
    | jq -r .token)

if [ -z "$TOKEN" ] || [ "$TOKEN" = "null" ]; then
    echo "FATAL: could not create admin token" >&2
    exit 1
fi
pass "admin token created"

TEST_HOME=$(mktmp)
export HOME="$TEST_HOME"
export GITLAB_TOKEN="$TOKEN"
mkdir -p "$HOME/.config/glab-cli" "$HOME/.config/git"

login_output=$("$GLAB" auth login \
    --hostname "$HOST" \
    --api-protocol https \
    --git-protocol https \
    --token "$TOKEN" 2>&1)
if [ $? -eq 0 ]; then
    pass "isolated glab auth config written in client VM"
else
    fail "glab auth login: $login_output"
fi

git config --global http.sslVerify false
git config --global user.name "glab smoke"
git config --global user.email "glab-smoke@example.com"
git config --global commit.gpgsign false

section "glab auth"

auth_status=$("$GLAB" auth status 2>&1)
if [ $? -eq 0 ]; then
    pass "glab auth status"
else
    fail "glab auth status: $auth_status"
fi

api_user=$(glab_api user)
assert_json_field "glab api user" "$api_user" '.username == "admin" or .login == "admin"'

users_search=$(glab_api "users?search=admin&per_page=1")
assert_json_field "glab api users search" "$users_search" \
    'length >= 1 and .[0].username == "admin" and .[0].public_email != null'

section "Project API"

project_json=$(curl -sk -X POST \
    -H "PRIVATE-TOKEN: $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"$PROJECT_NAME\",\"path\":\"$PROJECT_PATH\",\"visibility\":\"public\",\"initialize_with_readme\":true}" \
    "$API/projects")

PROJECT_ID=$(echo "$project_json" | json_get '.id')
if [ -n "$PROJECT_ID" ] && [ "$PROJECT_ID" != "null" ]; then
    pass "project created via GitLab API"
else
    fail "project create: $project_json"
    section "Summary"
    echo "Cannot continue without a project"
    exit 1
fi

project_by_id=$(glab_api "projects/$PROJECT_ID")
assert_json_field "glab api project by id" "$project_by_id" ".id == $PROJECT_ID"

project_by_path=$(glab_api "projects/$PROJECT_REF")
assert_json_field "glab api project by encoded path" "$project_by_path" ".path_with_namespace == \"admin/$PROJECT_PATH\""

section "Project Members CLI via glab"

MEMBER_USERNAME="glab-member-${RUN_ID}"
member_user_json=$(curl -sk -X POST \
    -H "PRIVATE-TOKEN: $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"login\":\"$MEMBER_USERNAME\",\"password\":\"member-password\",\"name\":\"Glab Member\",\"email\":\"$MEMBER_USERNAME@example.com\"}" \
    "$API/admin/users")
MEMBER_USER_ID=$(echo "$member_user_json" | jq -r '.id // empty' 2>/dev/null)
if [ -n "$MEMBER_USER_ID" ]; then
    pass "member user created"
else
    fail "member user create: $member_user_json"
fi

member_add=$("$GLAB" repo members add \
    --repo "admin/$PROJECT_PATH" \
    --username "$MEMBER_USERNAME" \
    --role developer 2>&1)
if [ $? -eq 0 ]; then
    pass "glab repo members add"
else
    fail "glab repo members add: $member_add"
fi

project_members=$(glab_api "projects/$PROJECT_REF/members?query=$MEMBER_USERNAME")
assert_json_field "glab repo members add visible" "$project_members" \
    "map(select(.username == \"$MEMBER_USERNAME\" and .access_level == 30)) | length == 1"

member_remove=$("$GLAB" repo members remove \
    --repo "admin/$PROJECT_PATH" \
    --username "$MEMBER_USERNAME" 2>&1)
if [ $? -eq 0 ]; then
    pass "glab repo members remove"
else
    fail "glab repo members remove: $member_remove"
fi

project_members_after_remove=$(glab_api "projects/$PROJECT_REF/members?query=$MEMBER_USERNAME")
assert_json_field "glab repo members remove visible" "$project_members_after_remove" \
    "map(.username) | index(\"$MEMBER_USERNAME\") | not"

section "Groups API via glab"

group_json=$(curl -sk -X POST \
    -H "PRIVATE-TOKEN: $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"$GROUP_PATH\",\"path\":\"$GROUP_PATH\",\"description\":\"group smoke\"}" \
    "$API/groups")
GROUP_ID=$(echo "$group_json" | jq -r '.id // empty' 2>/dev/null)
if [ -n "$GROUP_ID" ]; then
    pass "group created via GitLab API"
else
    fail "group create: $group_json"
fi

subgroup_json=$(curl -sk -X POST \
    -H "PRIVATE-TOKEN: $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"$SUBGROUP_PATH\",\"path\":\"$SUBGROUP_PATH\",\"parent_id\":$GROUP_ID}" \
    "$API/groups")
SUBGROUP_ID=$(echo "$subgroup_json" | jq -r '.id // empty' 2>/dev/null)
if [ -n "$SUBGROUP_ID" ]; then
    pass "nested group created via GitLab API"
else
    fail "nested group create: $subgroup_json"
fi

group_by_path=$(glab_api "groups/$GROUP_REF")
assert_json_field "glab api group by path" "$group_by_path" ".full_path == \"$GROUP_PATH\" and ._links.projects != null"

subgroup_by_path=$(glab_api "groups/$SUBGROUP_REF")
assert_json_field "glab api nested group by encoded path" "$subgroup_by_path" ".full_path == \"$GROUP_PATH/$SUBGROUP_PATH\" and .parent_id == ($GROUP_ID | tonumber)"

groups_search=$(glab_api "groups?search=$GROUP_PATH&top_level_only=true")
assert_json_field "glab api groups search top-level" "$groups_search" "map(.full_path) | index(\"$GROUP_PATH\")"

group_member=$(curl -sk -X POST \
    -H "PRIVATE-TOKEN: $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"user_id\":1,\"access_level\":40}" \
    "$API/groups/$SUBGROUP_ID/members")
assert_json_field "group member add/existing membership" "$group_member" '.username == "admin" and .access_level >= 30'

group_members_all=$(glab_api "groups/$SUBGROUP_REF/members/all?query=admin")
assert_json_field "glab api group members all query" "$group_members_all" 'map(.username) | index("admin")'

group_project_json=$(curl -sk -X POST \
    -H "PRIVATE-TOKEN: $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"$GROUP_PROJECT_PATH\",\"path\":\"$GROUP_PROJECT_PATH\",\"namespace_path\":\"$GROUP_PATH/$SUBGROUP_PATH\",\"visibility\":\"public\",\"initialize_with_readme\":true}" \
    "$API/projects")
GROUP_PROJECT_ID=$(echo "$group_project_json" | jq -r '.id // empty' 2>/dev/null)
if [ -n "$GROUP_PROJECT_ID" ]; then
    pass "project created in nested group"
else
    fail "nested group project create: $group_project_json"
fi

group_projects=$(glab_api "groups/$SUBGROUP_REF/projects")
assert_json_field "glab api nested group projects" "$group_projects" "map(.path_with_namespace) | index(\"$GROUP_PATH/$SUBGROUP_PATH/$GROUP_PROJECT_PATH\")"

section "Repo CLI via glab"

REPO_CLI_WORK=$(mktmp)
repo_create=$(cd "$REPO_CLI_WORK" && "$GLAB" repo create "$CLI_PROJECT_PATH" \
    --public \
    --description "repo cli smoke" \
    --skipGitInit 2>&1)
if [ $? -eq 0 ]; then
    pass "glab repo create"
else
    fail "glab repo create: $repo_create"
fi

repo_view=$("$GLAB" repo view "admin/$CLI_PROJECT_PATH" --output json 2>&1)
if echo "$repo_view" | jq -e ".path_with_namespace == \"admin/$CLI_PROJECT_PATH\" and .default_branch == \"main\"" >/dev/null 2>&1; then
    pass "glab repo view json"
else
    fail "glab repo view json: $repo_view"
fi

CLI_PROJECT_ID=$(echo "$repo_view" | jq -r '.id // empty' 2>/dev/null)
if [ -n "$CLI_PROJECT_ID" ]; then
    repo_list=$("$GLAB" repo list --all --output json --per-page 100 2>&1)
    assert_json_field "glab repo list json" "$repo_list" "map(.path_with_namespace) | index(\"admin/$CLI_PROJECT_PATH\")"

    repo_search=$("$GLAB" repo search --search "$CLI_PROJECT_PATH" --output json --per-page 100 2>&1)
    assert_json_field "glab repo search json" "$repo_search" "map(.path_with_namespace) | index(\"admin/$CLI_PROJECT_PATH\")"

    repo_update=$("$GLAB" repo update "admin/$CLI_PROJECT_PATH" \
        --description "repo cli smoke updated" 2>&1)
    if [ $? -eq 0 ]; then
        pass "glab repo update"
    else
        fail "glab repo update: $repo_update"
    fi

    repo_updated=$("$GLAB" repo view "admin/$CLI_PROJECT_PATH" --output json 2>&1)
    assert_json_field "glab repo update visible" "$repo_updated" \
        '.description == "repo cli smoke updated"'

    cli_readme_payload=$(jq -n \
        --arg branch "main" \
        --arg message "seed cli repo" \
        --arg content "# $CLI_PROJECT_PATH" \
        '{branch:$branch, commit_message:$message, content:$content}')
    cli_readme=$(curl -sk -X POST \
        -H "PRIVATE-TOKEN: $TOKEN" \
        -H "Content-Type: application/json" \
        -d "$cli_readme_payload" \
        "$API/projects/$CLI_PROJECT_ID/repository/files/README.md")
    assert_json_field "repo cli seed README" "$cli_readme" '.file_path == "README.md"'

    repo_contributors=$("$GLAB" repo contributors \
        --repo "admin/$CLI_PROJECT_PATH" \
        --output json 2>&1)
    assert_json_field "glab repo contributors json" "$repo_contributors" \
        'map(.commits) | add >= 1'

    repo_clone=$(cd "$REPO_CLI_WORK" && "$GLAB" repo clone "admin/$CLI_PROJECT_PATH" cli-clone 2>&1)
    if [ -f "$REPO_CLI_WORK/cli-clone/README.md" ]; then
        pass "glab repo clone"
    else
        fail "glab repo clone: $repo_clone"
    fi
else
    fail "glab repo view did not return a project id: $repo_view"
fi

repo_delete=$("$GLAB" repo delete "admin/$CLI_PROJECT_PATH" --yes 2>&1)
if [ $? -eq 0 ]; then
    pass "glab repo delete"
else
    fail "glab repo delete: $repo_delete"
fi

section "Git Smart HTTP"

WORK=$(mktmp)
repo_url="https://admin:${TOKEN}@${HOST}/admin/${PROJECT_PATH}.git"
clone_output=$(git clone "$repo_url" "$WORK/repo" 2>&1)
if [ -d "$WORK/repo/.git" ]; then
    pass "git clone via client VM"
else
    fail "git clone: $clone_output"
fi

if [ -d "$WORK/repo/.git" ]; then
    (
        cd "$WORK/repo" || exit 1
        echo "hello from glab smoke" > smoke.txt
        git add smoke.txt
        git commit -m "add smoke file" >/dev/null
        git push origin HEAD:main >/dev/null
        git checkout -b feature >/dev/null
        echo "feature work" > feature.txt
        git add feature.txt
        git commit -m "add feature file" >/dev/null
        git push origin feature >/dev/null
    )
    if [ $? -eq 0 ]; then
        pass "git commit and push main/feature"
    else
        fail "git commit and push main/feature"
    fi
fi

VERIFY=$(mktmp)
verify_output=$(git clone "$repo_url" "$VERIFY/repo" 2>&1)
if [ -f "$VERIFY/repo/smoke.txt" ]; then
    pass "git clone verifies pushed file"
else
    fail "git clone verify: $verify_output"
fi

section "Repository Files API via glab"

file_json=$(glab_api "projects/$PROJECT_ID/repository/files/smoke.txt?ref=main")
assert_json_field "glab api repository file" "$file_json" '.file_path == "smoke.txt"'

file_head=$(curl -sk -I -H "PRIVATE-TOKEN: $TOKEN" \
    "$API/projects/$PROJECT_ID/repository/files/smoke.txt?ref=main")
assert_contains "repository file HEAD metadata" "$file_head" "x-gitlab-file-path: smoke.txt"

section "CI API via glab"

pipeline_payload=$(jq -n \
    --arg ref "main" \
    --arg name "glab_api_job" \
    --argjson script '["echo glab ci"]' \
    '{ref:$ref, job:{name:$name, script:$script}}')
pipeline_json=$(curl -sk -X POST \
    -H "PRIVATE-TOKEN: $TOKEN" \
    -H "Content-Type: application/json" \
    -d "$pipeline_payload" \
    "$API/projects/$PROJECT_ID/pipeline")
PIPELINE_ID=$(echo "$pipeline_json" | jq -r '.id // empty' 2>/dev/null)
if [ -n "$PIPELINE_ID" ]; then
    assert_json_field "pipeline created for glab verification" "$pipeline_json" \
        '.id != null and .ref == "main" and .status == "pending"'
else
    fail "pipeline create for glab verification: $pipeline_json"
fi

if [ -n "$PIPELINE_ID" ]; then
    pipelines_json=$(glab_api "projects/$PROJECT_ID/pipelines?per_page=5")
    assert_json_field "glab api pipelines list" "$pipelines_json" \
        "map(.id) | index($PIPELINE_ID)"

    pipeline_detail=$(glab_api "projects/$PROJECT_ID/pipelines/$PIPELINE_ID")
    assert_json_field "glab api pipeline detail" "$pipeline_detail" \
        ".id == $PIPELINE_ID and .ref == \"main\""

    pipeline_jobs=$(glab_api "projects/$PROJECT_ID/pipelines/$PIPELINE_ID/jobs")
    assert_json_field "glab api pipeline jobs" "$pipeline_jobs" \
        'map(.name) | index("glab_api_job")'
    JOB_ID=$(echo "$pipeline_jobs" | jq -r '.[] | select(.name == "glab_api_job") | .id' | head -1)

    if [ -n "$JOB_ID" ] && [ "$JOB_ID" != "null" ]; then
        job_detail=$(glab_api "projects/$PROJECT_ID/jobs/$JOB_ID")
        assert_json_field "glab api job detail" "$job_detail" \
            ".id == ($JOB_ID | tonumber) and .name == \"glab_api_job\" and .pipeline.id == ($PIPELINE_ID | tonumber)"

        job_trace=$(glab_api "projects/$PROJECT_ID/jobs/$JOB_ID/trace")
        if [ $? -eq 0 ]; then
            pass "glab api job trace"
        else
            fail "glab api job trace: $job_trace"
        fi
    else
        fail "glab api pipeline jobs did not return glab_api_job: $pipeline_jobs"
    fi
fi

section "Variable CLI via glab"

variable_set=$("$GLAB" variable set GLAB_SMOKE_VAR "initial-value" \
    --repo "admin/$PROJECT_PATH" \
    --description "glab variable smoke" \
    --scope review \
    --raw 2>&1)
if [ $? -eq 0 ]; then
    pass "glab variable set"
else
    fail "glab variable set: $variable_set"
fi

variable_get=$("$GLAB" variable get GLAB_SMOKE_VAR \
    --repo "admin/$PROJECT_PATH" \
    --scope review \
    --output json 2>&1)
assert_json_field "glab variable get json" "$variable_get" \
    '.key == "GLAB_SMOKE_VAR" and .value == "initial-value" and .environment_scope == "review" and .raw == true'

variable_list=$("$GLAB" variable list \
    --repo "admin/$PROJECT_PATH" \
    --output json \
    --per-page 100 2>&1)
assert_json_field "glab variable list json" "$variable_list" \
    'map(.key) | index("GLAB_SMOKE_VAR")'

variable_update=$("$GLAB" variable update GLAB_SMOKE_VAR "updated-value" \
    --repo "admin/$PROJECT_PATH" \
    --scope review \
    --description "updated glab variable smoke" 2>&1)
if [ $? -eq 0 ]; then
    pass "glab variable update"
else
    fail "glab variable update: $variable_update"
fi

variable_updated=$("$GLAB" variable get GLAB_SMOKE_VAR \
    --repo "admin/$PROJECT_PATH" \
    --scope review \
    --output json 2>&1)
assert_json_field "glab variable update visible" "$variable_updated" \
    '.key == "GLAB_SMOKE_VAR" and .value == "updated-value" and .description == "updated glab variable smoke"'

variable_delete=$(printf 'y\n' | "$GLAB" variable delete GLAB_SMOKE_VAR \
    --repo "admin/$PROJECT_PATH" \
    --scope review 2>&1)
if [ $? -eq 0 ]; then
    pass "glab variable delete"
else
    fail "glab variable delete: $variable_delete"
fi

variables_after_delete=$("$GLAB" variable list \
    --repo "admin/$PROJECT_PATH" \
    --output json \
    --per-page 100 2>&1)
assert_json_field "glab variable delete visible" "$variables_after_delete" \
    'map(.key) | index("GLAB_SMOKE_VAR") | not'

section "Issue CLI via glab"

issue_create=$("$GLAB" issue create \
    --repo "admin/$PROJECT_PATH" \
    --title "Issue smoke" \
    --description "created by glab issue smoke" \
    --label "cli-smoke" \
    --yes 2>&1)
if [ $? -eq 0 ]; then
    pass "glab issue create"
else
    fail "glab issue create: $issue_create"
fi

issue_list=$("$GLAB" issue list \
    --repo "admin/$PROJECT_PATH" \
    --all \
    --output json 2>&1)
assert_json_field "glab issue list json" "$issue_list" 'map(.title) | index("Issue smoke")'

ISSUE_IID=$(echo "$issue_list" | jq -r '.[] | select(.title == "Issue smoke") | .iid' | head -1)
if [ -n "$ISSUE_IID" ] && [ "$ISSUE_IID" != "null" ]; then
    issue_view=$("$GLAB" issue view "$ISSUE_IID" \
        --repo "admin/$PROJECT_PATH" \
        --output json 2>&1)
    assert_json_field "glab issue view json" "$issue_view" ".iid == ($ISSUE_IID | tonumber) and .description == \"created by glab issue smoke\""

    issue_update=$("$GLAB" issue update "$ISSUE_IID" \
        --repo "admin/$PROJECT_PATH" \
        --title "Issue smoke updated" \
        --description "updated by glab issue smoke" 2>&1)
    if [ $? -eq 0 ]; then
        pass "glab issue update"
    else
        fail "glab issue update: $issue_update"
    fi

    issue_updated=$("$GLAB" issue view "$ISSUE_IID" \
        --repo "admin/$PROJECT_PATH" \
        --output json 2>&1)
    assert_json_field "glab issue update visible" "$issue_updated" '.title == "Issue smoke updated" and .description == "updated by glab issue smoke"'

    issue_close=$("$GLAB" issue close "$ISSUE_IID" --repo "admin/$PROJECT_PATH" 2>&1)
    if [ $? -eq 0 ]; then
        pass "glab issue close"
    else
        fail "glab issue close: $issue_close"
    fi

    issue_closed=$("$GLAB" issue view "$ISSUE_IID" \
        --repo "admin/$PROJECT_PATH" \
        --output json 2>&1)
    assert_json_field "glab issue close visible" "$issue_closed" '.state == "closed"'

    issue_reopen=$("$GLAB" issue reopen "$ISSUE_IID" --repo "admin/$PROJECT_PATH" 2>&1)
    if [ $? -eq 0 ]; then
        pass "glab issue reopen"
    else
        fail "glab issue reopen: $issue_reopen"
    fi

    issue_reopened=$("$GLAB" issue view "$ISSUE_IID" \
        --repo "admin/$PROJECT_PATH" \
        --output json 2>&1)
    assert_json_field "glab issue reopen visible" "$issue_reopened" '.state == "opened"'
else
    fail "glab issue list did not return the created issue: $issue_list"
fi

section "Labels and Milestones API via glab"

label_create=$("$GLAB" label create \
    --repo "admin/$PROJECT_PATH" \
    --name "glab-label" \
    --color "#0052cc" \
    --description "glab label smoke" 2>&1)
if [ $? -eq 0 ]; then
    pass "glab label create"
else
    fail "glab label create: $label_create"
fi

labels_json=$("$GLAB" label list \
    --repo "admin/$PROJECT_PATH" \
    --output json \
    --per-page 100 2>&1)
assert_json_field "glab label list json" "$labels_json" \
    'map(.name) | index("glab-label")'

label_json=$(glab_api "projects/$PROJECT_REF/labels/glab-label?with_counts=true")
assert_json_field \
    "glab api project label get by path" \
    "$label_json" \
    '.name == "glab-label" and .is_project_label == true and .open_issues_count >= 0'
LABEL_ID=$(echo "$label_json" | jq -r '.id // empty' 2>/dev/null)

if [ -n "$LABEL_ID" ]; then
    label_get=$("$GLAB" label get "$LABEL_ID" \
        --repo "admin/$PROJECT_PATH" 2>&1)
    if [ $? -eq 0 ]; then
        pass "glab label get"
    else
        fail "glab label get: $label_get"
    fi
else
    fail "glab api project label get did not return an id: $label_json"
fi

label_update=$("$GLAB" label edit \
    --repo "admin/$PROJECT_PATH" \
    --label-id "$LABEL_ID" \
    --new-name "glab-label-updated" \
    --color "#ff0000" \
    --description "updated glab label smoke" 2>&1)
if [ $? -eq 0 ]; then
    pass "glab label edit"
else
    fail "glab label edit: $label_update"
fi

label_updated=$(glab_api "projects/$PROJECT_REF/labels/glab-label-updated?with_counts=true")
assert_json_field \
    "glab label edit visible" \
    "$label_updated" \
    '.name == "glab-label-updated" and .color == "#ff0000"'

label_delete=$(printf 'y\n' | "$GLAB" label delete "glab-label-updated" \
    --repo "admin/$PROJECT_PATH" 2>&1)
if [ $? -eq 0 ]; then
    pass "glab label delete"
else
    fail "glab label delete: $label_delete"
fi

labels_after_delete=$("$GLAB" label list \
    --repo "admin/$PROJECT_PATH" \
    --output json \
    --per-page 100 2>&1)
assert_json_field "glab label delete visible" "$labels_after_delete" \
    'map(.name) | index("glab-label-updated") | not'

milestone_create=$("$GLAB" milestone create \
    --project "admin/$PROJECT_PATH" \
    --title "glab milestone" \
    --description "glab milestone smoke" \
    --due-date "2026-07-01" 2>&1)
if [ $? -eq 0 ]; then
    pass "glab milestone create"
else
    fail "glab milestone create: $milestone_create"
fi

milestones_json=$("$GLAB" milestone list \
    --project "admin/$PROJECT_PATH" \
    --state active \
    --search glab \
    --output json 2>&1)
assert_json_field \
    "glab milestone list json" \
    "$milestones_json" \
    'map(.title) | index("glab milestone")'
MILESTONE_ID=$(echo "$milestones_json" | jq -r '.[] | select(.title == "glab milestone") | .id' 2>/dev/null | head -n1)

if [ -n "$MILESTONE_ID" ]; then
    milestone_json=$("$GLAB" milestone get "$MILESTONE_ID" \
        --project "admin/$PROJECT_PATH" \
        --output json 2>&1)
    assert_json_field \
        "glab milestone get json" \
        "$milestone_json" \
        '.title == "glab milestone" and .due_date == "2026-07-01"'
else
    fail "glab milestone list did not return an id for glab milestone: $milestones_json"
fi

milestone_update=$("$GLAB" milestone edit "$MILESTONE_ID" \
    --project "admin/$PROJECT_PATH" \
    --title "glab milestone closed" \
    --state close \
    --due-date "2026-07-15" 2>&1)
if [ $? -eq 0 ]; then
    pass "glab milestone edit"
else
    fail "glab milestone edit: $milestone_update"
fi

milestone_updated=$("$GLAB" milestone get "$MILESTONE_ID" \
    --project "admin/$PROJECT_PATH" \
    --output json 2>&1)
assert_json_field \
    "glab milestone edit visible" \
    "$milestone_updated" \
    '.title == "glab milestone closed" and .state == "closed" and .due_date == "2026-07-15"'

milestone_delete=$("$GLAB" milestone delete "$MILESTONE_ID" \
    --project "admin/$PROJECT_PATH" 2>&1)
if [ $? -eq 0 ]; then
    pass "glab milestone delete"
else
    fail "glab milestone delete: $milestone_delete"
fi

milestones_after_delete=$("$GLAB" milestone list \
    --project "admin/$PROJECT_PATH" \
    --state all \
    --output json 2>&1)
assert_json_field "glab milestone delete visible" "$milestones_after_delete" \
    "map(.id) | index($MILESTONE_ID) | not"

section "Branches API via glab"

branches_json=$(glab_api "projects/$PROJECT_ID/repository/branches")
assert_json_field "glab api branches list" "$branches_json" 'map(.name) | index("main") and index("feature")'

branch_json=$(glab_api "projects/$PROJECT_ID/repository/branches/feature")
assert_json_field "glab api branch get" "$branch_json" '.name == "feature"'

branch_create=$(curl -sk -X POST \
    -H "PRIVATE-TOKEN: $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"branch":"glab-created","ref":"main"}' \
    "$API/projects/$PROJECT_ID/repository/branches")
assert_json_field "branch created for glab verification" "$branch_create" '.name == "glab-created"'

branch_created=$(glab_api "projects/$PROJECT_ID/repository/branches/glab-created")
assert_json_field "glab api created branch get" "$branch_created" '.name == "glab-created" and .commit.web_url != null'

protected_branch=$(curl -sk -X POST \
    -H "PRIVATE-TOKEN: $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"name":"glab-created","push_access_level":40,"merge_access_level":40}' \
    "$API/projects/$PROJECT_ID/protected_branches")
assert_json_field "protected branch created" "$protected_branch" '.name == "glab-created" and .push_access_levels != null and .merge_access_levels != null'

branch_delete=$(curl -sk -X DELETE -H "PRIVATE-TOKEN: $TOKEN" \
    "$API/projects/$PROJECT_ID/repository/branches/glab-created")
assert_json_field "branch deleted" "$branch_delete" '.branch_name == "glab-created"'

section "Tags API via glab"

tag_create=$(curl -sk -X POST \
    -H "PRIVATE-TOKEN: $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"tag_name":"v1.0.0","ref":"main"}' \
    "$API/projects/$PROJECT_ID/repository/tags")
assert_json_field "tag created for glab verification" "$tag_create" '.name == "v1.0.0"'

tags_json=$(glab_api "projects/$PROJECT_ID/repository/tags")
assert_json_field "glab api tags list" "$tags_json" 'map(.name) | index("v1.0.0")'

tag_json=$(glab_api "projects/$PROJECT_ID/repository/tags/v1.0.0")
assert_json_field "glab api tag get" "$tag_json" '.name == "v1.0.0"'

tag_delete=$(curl -sk -X DELETE -H "PRIVATE-TOKEN: $TOKEN" \
    "$API/projects/$PROJECT_ID/repository/tags/v1.0.0")
assert_json_field "tag deleted" "$tag_delete" '.tag_name == "v1.0.0"'

section "Release CLI via glab"

release_tag="v-release-${RUN_ID}"
release_create=$("$GLAB" release create "$release_tag" \
    --repo "admin/$PROJECT_PATH" \
    --ref main \
    --name "Smoke Release" \
    --notes "Release created by glab smoke" \
    --no-update 2>&1)
if [ $? -eq 0 ]; then
    pass "glab release create"
else
    fail "glab release create: $release_create"
fi

release_json=$(glab_api "projects/$PROJECT_ID/releases/$release_tag")
assert_json_field "glab api release get" "$release_json" ".tag_name == \"$release_tag\" and .name == \"Smoke Release\""

release_upload_links=$("$GLAB" release upload "$release_tag" \
    --repo "admin/$PROJECT_PATH" \
    --assets-links "[{\"name\":\"smoke-upload-link\",\"url\":\"https://example.test/${RUN_ID}/upload-link.txt\",\"link_type\":\"other\",\"direct_asset_path\":\"upload-link.txt\"}]" 2>&1)
if [ $? -eq 0 ]; then
    pass "glab release upload assets-links"
else
    fail "glab release upload assets-links: $release_upload_links"
fi

release_upload_json=$(glab_api "projects/$PROJECT_ID/releases/$release_tag")
assert_json_field "glab release upload assets-links visible" "$release_upload_json" \
    '.assets.links | map(.name) | index("smoke-upload-link")'

package_asset_dir=$(mktmp)
package_asset="$package_asset_dir/smoke-package.txt"
printf "release package payload %s\n" "$RUN_ID" > "$package_asset"
release_upload_package=$("$GLAB" release upload "$release_tag" \
    "$package_asset#Smoke package#package" \
    --repo "admin/$PROJECT_PATH" \
    --use-package-registry \
    --package-name "release-assets-${RUN_ID}" 2>&1)
if [ $? -eq 0 ]; then
    pass "glab release upload package registry"
else
    fail "glab release upload package registry: $release_upload_package"
fi

release_package_json=$(glab_api "projects/$PROJECT_ID/releases/$release_tag")
assert_json_field "glab release upload package registry visible" "$release_package_json" \
    '.assets.links | map(.name) | index("Smoke package")'

asset_link_create=$(glab_api --method POST \
    "projects/$PROJECT_ID/releases/$release_tag/assets/links" \
    -f "name=smoke-runbook" \
    -f "url=https://example.test/${RUN_ID}/runbook.md" \
    -f "link_type=runbook")
assert_json_field "glab api release asset link create" "$asset_link_create" \
    '.name == "smoke-runbook" and .link_type == "runbook"'
ASSET_LINK_ID=$(echo "$asset_link_create" | jq -r '.id // empty')
if [ -n "$ASSET_LINK_ID" ]; then
    asset_links=$(glab_api "projects/$PROJECT_ID/releases/$release_tag/assets/links")
    assert_json_field "glab api release asset links list" "$asset_links" \
        'map(.name) | index("smoke-runbook")'

    asset_link_update=$(glab_api --method PUT \
        "projects/$PROJECT_ID/releases/$release_tag/assets/links/$ASSET_LINK_ID" \
        -f "name=smoke-binary" \
        -f "direct_asset_path=smoke-binary-linux-amd64" \
        -f "link_type=package")
    assert_json_field "glab api release asset link update" "$asset_link_update" \
        '.name == "smoke-binary" and .link_type == "package" and (.direct_asset_url | contains("/downloads/smoke-binary-linux-amd64"))'

    asset_link_delete=$(glab_api --method DELETE \
        "projects/$PROJECT_ID/releases/$release_tag/assets/links/$ASSET_LINK_ID")
    assert_json_field "glab api release asset link delete" "$asset_link_delete" \
        ".id == ($ASSET_LINK_ID | tonumber)"
else
    fail "glab api release asset link id missing: $asset_link_create"
fi

release_view=$("$GLAB" release view "$release_tag" --repo "admin/$PROJECT_PATH" 2>&1)
if [ $? -eq 0 ]; then
    assert_contains "glab release view" "$release_view" "Smoke Release"
else
    fail "glab release view: $release_view"
fi

release_delete=$("$GLAB" release delete "$release_tag" --repo "admin/$PROJECT_PATH" --yes 2>&1)
if [ $? -eq 0 ]; then
    pass "glab release delete"
else
    fail "glab release delete: $release_delete"
fi

section "Commits API via glab"

commits_json=$(glab_api "projects/$PROJECT_ID/repository/commits")
assert_json_field "glab api commits list" "$commits_json" 'length >= 2'

HEAD_SHA=$(echo "$commits_json" | jq -r '.[0].id')
commit_json=$(glab_api "projects/$PROJECT_ID/repository/commits/$HEAD_SHA")
assert_json_field "glab api commit get" "$commit_json" ".id == \"$HEAD_SHA\""

commit_stats=$(glab_api "projects/$PROJECT_ID/repository/commits/$HEAD_SHA?stats=true")
assert_json_field "glab api commit stats" "$commit_stats" '.stats.total >= 0'

commit_filtered=$(glab_api "projects/$PROJECT_ID/repository/commits?ref_name=main&path=smoke.txt&with_stats=true")
assert_json_field "glab api commit filters" "$commit_filtered" 'length >= 1 and .[0].stats.total >= 0'

previous_sha=$(echo "$commits_json" | jq -r '.[1].id // empty')
if [ -n "$previous_sha" ]; then
    compare_json=$(glab_api "projects/$PROJECT_ID/repository/compare?from=$previous_sha&to=$HEAD_SHA")
    assert_json_field "glab api repository compare" "$compare_json" ".commit.id == \"$HEAD_SHA\" and .compare_same_ref == false and (.commits | length) >= 1"
else
    fail "glab api repository compare: no previous commit available"
fi

status_create=$(curl -sk -X POST \
    -H "PRIVATE-TOKEN: $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"state":"running","name":"glab-status","description":"glab status smoke","target_url":"https://ci.example.test/glab-status"}' \
    "$API/projects/$PROJECT_ID/statuses/$HEAD_SHA")
assert_json_field "glab api commit status created" "$status_create" '.status == "running" and .name == "glab-status"'

status_list=$(glab_api "projects/$PROJECT_REF/repository/commits/$HEAD_SHA/statuses")
assert_json_field "glab api commit statuses list" "$status_list" 'map(.name) | index("glab-status")'

section "Merge Requests API via glab"

mr_create=$(curl -sk -X POST \
    -H "PRIVATE-TOKEN: $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"title":"Add feature","source_branch":"feature","target_branch":"main","description":"glab smoke MR"}' \
    "$API/projects/$PROJECT_ID/merge_requests")
MR_IID=$(echo "$mr_create" | jq -r '.iid')
if [ -n "$MR_IID" ] && [ "$MR_IID" != "null" ]; then
    pass "merge request created"
else
    fail "merge request create: $mr_create"
fi

mr_list=$(glab_api "projects/$PROJECT_ID/merge_requests")
assert_json_field "glab api merge requests list" "$mr_list" 'map(.title) | index("Add feature")'

mr_get=$(glab_api "projects/$PROJECT_ID/merge_requests/$MR_IID")
assert_json_field "glab api merge request get" "$mr_get" ".iid == ($MR_IID | tonumber)"

mr_changes=$(glab_api "projects/$PROJECT_ID/merge_requests/$MR_IID/changes")
assert_json_field "glab api merge request changes" "$mr_changes" '.changes_count != null and (.changes | length) >= 1'

mr_diffs=$(glab_api "projects/$PROJECT_ID/merge_requests/$MR_IID/diffs")
assert_json_field "glab api merge request diffs" "$mr_diffs" 'length >= 1 and .[0].new_path != null'

mr_merge=$(curl -sk -X PUT \
    -H "PRIVATE-TOKEN: $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{}' \
    "$API/projects/$PROJECT_ID/merge_requests/$MR_IID/merge")
assert_json_field "merge request merged" "$mr_merge" '.state == "merged"'

section "Merge Request CLI via glab"

if [ -d "$WORK/repo/.git" ]; then
    (
        cd "$WORK/repo" || exit 1
        git checkout main >/dev/null
        git pull origin main >/dev/null
        git checkout -b cli-mr >/dev/null
        echo "cli mr work" > cli-mr.txt
        git add cli-mr.txt
        git commit -m "add cli mr file" >/dev/null
        git push origin cli-mr >/dev/null
    )
    if [ $? -eq 0 ]; then
        pass "merge request cli source branch pushed"
    else
        fail "merge request cli source branch pushed"
    fi
fi

mr_cli_create=$(cd "$WORK/repo" && "$GLAB" mr create \
    --repo "admin/$PROJECT_PATH" \
    --source-branch cli-mr \
    --target-branch main \
    --title "CLI MR smoke" \
    --description "created by glab mr smoke" \
    --yes 2>&1)
if [ $? -eq 0 ]; then
    pass "glab mr create"
else
    fail "glab mr create: $mr_cli_create"
fi

mr_cli_list=$("$GLAB" mr list \
    --repo "admin/$PROJECT_PATH" \
    --all \
    --output json 2>&1)
assert_json_field "glab mr list json" "$mr_cli_list" 'map(.title) | index("CLI MR smoke")'

MR_CLI_IID=$(echo "$mr_cli_list" | jq -r '.[] | select(.title == "CLI MR smoke") | .iid' | head -1)
if [ -n "$MR_CLI_IID" ] && [ "$MR_CLI_IID" != "null" ]; then
    mr_cli_view=$("$GLAB" mr view "$MR_CLI_IID" \
        --repo "admin/$PROJECT_PATH" \
        --output json 2>&1)
    assert_json_field "glab mr view json" "$mr_cli_view" ".iid == ($MR_CLI_IID | tonumber) and .description == \"created by glab mr smoke\" and .user.can_merge == true"

    mr_cli_update=$("$GLAB" mr update "$MR_CLI_IID" \
        --repo "admin/$PROJECT_PATH" \
        --title "CLI MR smoke updated" \
        --description "updated by glab mr smoke" \
        --yes 2>&1)
    if [ $? -eq 0 ]; then
        pass "glab mr update"
    else
        fail "glab mr update: $mr_cli_update"
    fi

    mr_cli_updated=$("$GLAB" mr view "$MR_CLI_IID" \
        --repo "admin/$PROJECT_PATH" \
        --output json 2>&1)
    assert_json_field "glab mr update visible" "$mr_cli_updated" '.title == "CLI MR smoke updated" and .description == "updated by glab mr smoke"'

    mr_cli_merge=$("$GLAB" mr merge "$MR_CLI_IID" \
        --repo "admin/$PROJECT_PATH" \
        --yes \
        --auto-merge=false 2>&1)
    if [ $? -eq 0 ]; then
        pass "glab mr merge"
    else
        fail "glab mr merge: $mr_cli_merge"
    fi

    mr_cli_merged=$("$GLAB" mr view "$MR_CLI_IID" \
        --repo "admin/$PROJECT_PATH" \
        --output json 2>&1)
    assert_json_field "glab mr merge visible" "$mr_cli_merged" '.state == "merged"'
else
    fail "glab mr list did not return the created merge request: $mr_cli_list"
fi

section "Pipeline API via glab"

CI_RUNNER_TAG="glab-smoke-ci-${RUN_ID}"

ci_yaml=$(cat <<'EOF'
include:
  local: ci-include.yml
stages: [build]

smoke:
  extends: .base
  tags:
    - __CI_RUNNER_TAG__
  script:
    - echo smoke
    - mkdir -p out
    - echo artifact > out/result.txt
  artifacts:
    paths:
      - out/result.txt

manual_trigger:
  extends: .base
  tags:
    - __CI_RUNNER_TAG__
  script:
    - echo manual triggered
  rules:
    - when: manual
EOF
)
ci_yaml="${ci_yaml//__CI_RUNNER_TAG__/$CI_RUNNER_TAG}"
include_yaml=$(cat <<'EOF'
.base:
  image: alpine:3.20
  before_script:
    - echo included
EOF
)

ci_payload=$(jq -n \
    --arg branch "main" \
    --arg message "add glab ci" \
    --arg content "$ci_yaml" \
    '{branch:$branch, commit_message:$message, content:$content}')
include_payload=$(jq -n \
    --arg branch "main" \
    --arg message "add glab ci include" \
    --arg content "$include_yaml" \
    '{branch:$branch, commit_message:$message, content:$content}')

curl -sk -X POST -H "PRIVATE-TOKEN: $TOKEN" -H "Content-Type: application/json" \
    -d "$include_payload" \
    "$API/projects/$PROJECT_ID/repository/files/ci-include.yml" >/dev/null
curl -sk -X POST -H "PRIVATE-TOKEN: $TOKEN" -H "Content-Type: application/json" \
    -d "$ci_payload" \
    "$API/projects/$PROJECT_ID/repository/files/.gitlab-ci.yml" >/dev/null

pre_ci_pipelines=$(glab_api "projects/$PROJECT_ID/pipelines?ref=main&per_page=10")
pre_ci_pending_ids=$(echo "$pre_ci_pipelines" | jq -r '.[] | select(.source == "push" and (.status == "pending" or .status == "running")) | .id' 2>/dev/null)
if [ -n "$pre_ci_pending_ids" ]; then
    while IFS= read -r pending_pipeline_id; do
        [ -z "$pending_pipeline_id" ] && continue
        curl -sk -X POST \
            -H "PRIVATE-TOKEN: $TOKEN" \
            "$API/projects/$PROJECT_ID/pipelines/$pending_pipeline_id/cancel" >/dev/null
    done <<< "$pre_ci_pending_ids"
    pass "automatic push CI pipeline canceled before glab ci run"
fi

ci_run=$("$GLAB" ci run --repo "admin/$PROJECT_PATH" --branch main 2>&1)
if [ $? -eq 0 ]; then
    pass "glab ci run"
else
    fail "glab ci run: $ci_run"
fi

ci_list=$("$GLAB" ci list --repo "admin/$PROJECT_PATH" --output json 2>&1)
assert_json_field "glab ci list json" "$ci_list" 'map(.ref) | index("main")'

pipeline_alias_list=$("$GLAB" pipeline list --repo "admin/$PROJECT_PATH" --output json 2>&1)
assert_json_field "glab pipeline list alias json" "$pipeline_alias_list" 'map(.ref) | index("main")'

PIPELINE_ID=$(echo "$ci_list" | jq -r '.[0].id // empty' 2>/dev/null)
if [ -n "$PIPELINE_ID" ] && [ "$PIPELINE_ID" != "null" ]; then
    pass "pipeline created from local include"
else
    fail "pipeline id from glab ci list: $ci_list"
fi

ci_status=$("$GLAB" ci status --repo "admin/$PROJECT_PATH" --branch main --output json 2>&1)
assert_json_field "glab ci status json" "$ci_status" ".pipeline.id == ($PIPELINE_ID | tonumber) and (.jobs | map(.name) | index(\"smoke\"))"

ci_get=$("$GLAB" ci get --repo "admin/$PROJECT_PATH" --pipeline-id "$PIPELINE_ID" --output json --with-job-details 2>&1)
assert_json_field "glab ci get json" "$ci_get" ".id == ($PIPELINE_ID | tonumber) and (.jobs | map(.name) | index(\"smoke\"))"

pipelines_json=$(glab_api "projects/$PROJECT_ID/pipelines")
assert_json_field "glab api pipelines list" "$pipelines_json" "map(.id) | index($PIPELINE_ID)"

jobs_json=$(glab_api "projects/$PROJECT_ID/pipelines/$PIPELINE_ID/jobs")
assert_json_field "glab api pipeline jobs include smoke" "$jobs_json" 'map(.name) | index("smoke")'

JOB_ID=$(echo "$ci_get" | jq -r '.jobs[] | select(.name == "smoke") | .id' | head -1)
MANUAL_JOB_ID=$(echo "$ci_get" | jq -r '.jobs[] | select(.name == "manual_trigger") | .id' | head -1)
runner_payload=$(jq -n \
    --arg name "glab-smoke-client-runner-${RUN_ID}" \
    --arg tag "$CI_RUNNER_TAG" \
    '{token:"glrt-emulator-runner-token", info:{name:$name, config:{tag_list:$tag}}}')
runner_request=$(curl -sk -X POST \
    -H "RUNNER-TOKEN: glrt-emulator-runner-token" \
    -H "Content-Type: application/json" \
    -d "$runner_payload" \
    "$API/jobs/request")
RUNNER_JOB_ID=$(echo "$runner_request" | jq -r '.id // empty' 2>/dev/null)
RUNNER_JOB_TOKEN=$(echo "$runner_request" | jq -r '.token // empty' 2>/dev/null)
if [ "$RUNNER_JOB_ID" = "$JOB_ID" ] && [ -n "$RUNNER_JOB_TOKEN" ]; then
    pass "client claimed glab ci job"
else
    fail "client claimed glab ci job: $runner_request"
fi

trace_content="glab ci trace smoke"
curl -sk -X PATCH \
    -H "JOB-TOKEN: $RUNNER_JOB_TOKEN" \
    -H "Content-Range: 0-$(( ${#trace_content} - 1 ))" \
    --data-binary "$trace_content" \
    "$API/jobs/$JOB_ID/trace?debug_trace=false" >/dev/null

artifact_tmp=$(mktmp)
ARTIFACT_ZIP="$artifact_tmp/artifact.zip" python3 - <<'PY'
import os
import zipfile

with zipfile.ZipFile(os.environ["ARTIFACT_ZIP"], "w") as archive:
    archive.writestr("out/result.txt", "artifact\n")
PY
curl -sk -X POST \
    -H "JOB-TOKEN: $RUNNER_JOB_TOKEN" \
    -H "Content-Type: application/zip" \
    --data-binary @"$artifact_tmp/artifact.zip" \
    "$API/jobs/$JOB_ID/artifacts?artifact_format=zip&artifact_type=archive" >/dev/null

curl -sk -X PUT \
    -H "JOB-TOKEN: $RUNNER_JOB_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"token\":\"$RUNNER_JOB_TOKEN\",\"state\":\"success\",\"exit_code\":0}" \
    "$API/jobs/$JOB_ID" >/dev/null

ci_trace=$("$GLAB" ci trace "$JOB_ID" --repo "admin/$PROJECT_PATH" --pipeline-id "$PIPELINE_ID" 2>&1)
assert_contains "glab ci trace" "$ci_trace" "$trace_content"

job_artifacts=$("$GLAB" job artifact main smoke --repo "admin/$PROJECT_PATH" --list-paths 2>&1)
assert_contains "glab job artifact list paths" "$job_artifacts" "out/result.txt"

manual_before=$(glab_api "projects/$PROJECT_ID/jobs/$MANUAL_JOB_ID")
assert_json_field "manual job starts manual" "$manual_before" '.status == "manual"'

manual_trigger=$("$GLAB" ci trigger "$MANUAL_JOB_ID" \
    --repo "admin/$PROJECT_PATH" \
    --pipeline-id "$PIPELINE_ID" 2>&1)
if [ $? -eq 0 ]; then
    pass "glab ci trigger manual job"
else
    fail "glab ci trigger manual job: $manual_trigger"
fi

manual_after=$(glab_api "projects/$PROJECT_ID/jobs/$MANUAL_JOB_ID")
assert_json_field "glab ci trigger manual job visible" "$manual_after" '.status == "pending"'

manual_runner_payload=$(jq -n \
    --arg name "glab-smoke-manual-runner-${RUN_ID}" \
    --arg tag "$CI_RUNNER_TAG" \
    '{token:"glrt-emulator-runner-token", info:{name:$name, config:{tag_list:$tag}}}')
manual_request=$(curl -sk -X POST \
    -H "RUNNER-TOKEN: glrt-emulator-runner-token" \
    -H "Content-Type: application/json" \
    -d "$manual_runner_payload" \
    "$API/jobs/request")
MANUAL_RUNNER_JOB_ID=$(echo "$manual_request" | jq -r '.id // empty' 2>/dev/null)
MANUAL_RUNNER_JOB_TOKEN=$(echo "$manual_request" | jq -r '.token // empty' 2>/dev/null)
if [ "$MANUAL_RUNNER_JOB_ID" = "$MANUAL_JOB_ID" ] && [ -n "$MANUAL_RUNNER_JOB_TOKEN" ]; then
    pass "triggered manual job requeued for runner"
    curl -sk -X PUT \
        -H "JOB-TOKEN: $MANUAL_RUNNER_JOB_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"token\":\"$MANUAL_RUNNER_JOB_TOKEN\",\"state\":\"success\",\"exit_code\":0}" \
        "$API/jobs/$MANUAL_JOB_ID" >/dev/null
else
    fail "triggered manual job requeued for runner: $manual_request"
fi

section "CI Control CLI via glab"

cancel_pipeline_json=$(curl -sk -X POST \
    -H "PRIVATE-TOKEN: $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"ref":"main"}' \
    "$API/projects/$PROJECT_ID/pipeline")
CANCEL_PIPELINE_ID=$(echo "$cancel_pipeline_json" | jq -r '.id // empty' 2>/dev/null)
if [ -n "$CANCEL_PIPELINE_ID" ]; then
    cancel_pipeline=$("$GLAB" ci cancel pipeline "$CANCEL_PIPELINE_ID" --repo "admin/$PROJECT_PATH" 2>&1)
    if [ $? -eq 0 ]; then
        pass "glab ci cancel pipeline"
    else
        fail "glab ci cancel pipeline: $cancel_pipeline"
    fi

    canceled_pipeline=$(glab_api "projects/$PROJECT_ID/pipelines/$CANCEL_PIPELINE_ID")
    assert_json_field "glab ci cancel pipeline visible" "$canceled_pipeline" '.status == "canceled"'
else
    fail "create cancel pipeline: $cancel_pipeline_json"
fi

cancel_job_json=$(curl -sk -X POST \
    -H "PRIVATE-TOKEN: $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"ref":"main"}' \
    "$API/projects/$PROJECT_ID/pipeline")
CANCEL_JOB_PIPELINE_ID=$(echo "$cancel_job_json" | jq -r '.id // empty' 2>/dev/null)
CANCEL_JOB_ID=""
CANCEL_JOB_TOKEN=""
if [ -n "$CANCEL_JOB_PIPELINE_ID" ]; then
    cancel_runner_payload=$(jq -n \
        --arg name "glab-smoke-cancel-runner-${RUN_ID}" \
        --arg tag "$CI_RUNNER_TAG" \
        '{token:"glrt-emulator-runner-token", info:{name:$name, config:{tag_list:$tag}}}')
    cancel_job_request=$(curl -sk -X POST \
        -H "RUNNER-TOKEN: glrt-emulator-runner-token" \
        -H "Content-Type: application/json" \
        -d "$cancel_runner_payload" \
        "$API/jobs/request")
    CANCEL_JOB_ID=$(echo "$cancel_job_request" | jq -r '.id // empty' 2>/dev/null)
    CANCEL_JOB_TOKEN=$(echo "$cancel_job_request" | jq -r '.token // empty' 2>/dev/null)
fi
if [ -n "$CANCEL_JOB_ID" ] && [ -n "$CANCEL_JOB_TOKEN" ]; then
    cancel_job=$("$GLAB" ci cancel job "$CANCEL_JOB_ID" --repo "admin/$PROJECT_PATH" 2>&1)
    if [ $? -eq 0 ]; then
        pass "glab ci cancel job"
    else
        fail "glab ci cancel job: $cancel_job"
    fi

    canceled_job=$(glab_api "projects/$PROJECT_ID/jobs/$CANCEL_JOB_ID")
    assert_json_field "glab ci cancel job visible" "$canceled_job" '.status == "canceled"'
else
    fail "client claimed cancel job: ${cancel_job_request:-$cancel_job_json}"
fi

retry_pipeline_json=$(curl -sk -X POST \
    -H "PRIVATE-TOKEN: $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{"ref":"main"}' \
    "$API/projects/$PROJECT_ID/pipeline")
RETRY_PIPELINE_ID=$(echo "$retry_pipeline_json" | jq -r '.id // empty' 2>/dev/null)
RETRY_JOB_ID=""
RETRY_JOB_TOKEN=""
if [ -n "$RETRY_PIPELINE_ID" ]; then
    retry_runner_payload=$(jq -n \
        --arg name "glab-smoke-retry-runner-${RUN_ID}" \
        --arg tag "$CI_RUNNER_TAG" \
        '{token:"glrt-emulator-runner-token", info:{name:$name, config:{tag_list:$tag}}}')
    retry_job_request=$(curl -sk -X POST \
        -H "RUNNER-TOKEN: glrt-emulator-runner-token" \
        -H "Content-Type: application/json" \
        -d "$retry_runner_payload" \
        "$API/jobs/request")
    RETRY_JOB_ID=$(echo "$retry_job_request" | jq -r '.id // empty' 2>/dev/null)
    RETRY_JOB_TOKEN=$(echo "$retry_job_request" | jq -r '.token // empty' 2>/dev/null)
fi
if [ -n "$RETRY_JOB_ID" ] && [ -n "$RETRY_JOB_TOKEN" ]; then
    curl -sk -X PUT \
        -H "JOB-TOKEN: $RETRY_JOB_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"token\":\"$RETRY_JOB_TOKEN\",\"state\":\"failed\",\"exit_code\":1}" \
        "$API/jobs/$RETRY_JOB_ID" >/dev/null

    retry_job=$("$GLAB" ci retry "$RETRY_JOB_ID" \
        --repo "admin/$PROJECT_PATH" \
        --pipeline-id "$RETRY_PIPELINE_ID" 2>&1)
    if [ $? -eq 0 ]; then
        pass "glab ci retry job"
    else
        fail "glab ci retry job: $retry_job"
    fi

    retried_job=$(glab_api "projects/$PROJECT_ID/jobs/$RETRY_JOB_ID")
    assert_json_field "glab ci retry job visible" "$retried_job" '.status == "pending"'

    retry_claim=$(curl -sk -X POST \
        -H "RUNNER-TOKEN: glrt-emulator-runner-token" \
        -H "Content-Type: application/json" \
        -d "$retry_runner_payload" \
        "$API/jobs/request")
    RETRIED_JOB_ID=$(echo "$retry_claim" | jq -r '.id // empty' 2>/dev/null)
    RETRIED_JOB_TOKEN=$(echo "$retry_claim" | jq -r '.token // empty' 2>/dev/null)
    if [ "$RETRIED_JOB_ID" = "$RETRY_JOB_ID" ] && [ -n "$RETRIED_JOB_TOKEN" ] && [ "$RETRIED_JOB_TOKEN" != "$RETRY_JOB_TOKEN" ]; then
        pass "retried job requeued for runner"
    else
        fail "retried job requeued for runner: $retry_claim"
    fi
else
    fail "client claimed retry job: ${retry_job_request:-$retry_pipeline_json}"
fi

section "Summary"

TOTAL=$((PASS + FAIL))
printf "\n  %d checks: \033[32m%d passed\033[0m" "$TOTAL" "$PASS"
if [ "$FAIL" -gt 0 ]; then
    printf ", \033[31m%d failed\033[0m" "$FAIL"
    printf "\n\n  Failures:%b\n" "$ERRORS"
fi
printf "\n"

export HOME="$ORIGINAL_HOME"
exit "$FAIL"
