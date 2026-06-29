# CI Variables, Secrets, and Pipeline Security Plan

## Purpose

Add enough GitLab-compatible CI variable, secret, and pipeline security behavior
to run realistic jobs against the emulator without accidentally training tests
to depend on unsafe or GitLab-incompatible semantics.

This plan is scoped to the emulator's MVP runner coordinator and official
GitLab Runner validation path. It does not try to implement all GitLab UI,
roles, audit, protected refs, or external secret-provider behavior at once.

## GitLab Behavior to Preserve

CI/CD variables are environment variables used to control jobs, avoid
hard-coded reusable values, and pass values to scripts. GitLab supports
predefined variables, YAML top-level variables, job variables, project/group
/instance variables, pipeline variables, trigger variables, scheduled variables,
manual variables, file variables, masked/hidden/protected visibility flags,
environment scopes, and variable expansion.

Important compatibility points:

- YAML top-level variables are defaults for all jobs.
- YAML job variables override top-level YAML variables.
- Pipeline variables have higher precedence than project/group/instance and
  YAML variables.
- Project variables override group variables.
- Group variables override instance variables.
- YAML-defined variables cannot be file variables.
- File variables are injected as file paths whose contents are the variable
  value.
- Protected variables are only exposed to pipelines on protected refs.
- Masked variables must be redacted from job logs, but masking is not a hard
  security boundary against malicious jobs.

GitLab Secrets Manager is separate from CI/CD variables. Secrets are not
available to jobs by default; jobs must explicitly request secrets with the
`secrets:` keyword. By default, a secret is injected as a temporary file and
the environment variable contains the file path. `file: false` injects the
secret as an environment variable. Secret values are masked in logs.

Pipeline security behavior is mostly guardrail behavior around secrets and
inputs:

- prefer secrets managers for sensitive data
- mask, hide, and protect variable values when variables must store sensitive
  values
- restrict pipeline variables because they can override predefined variables
  and share permission scope with sensitive secrets
- treat untrusted pipeline configuration as dangerous
- prefer pinned container image digests and pinned includes
- warn or block unsafe external includes and mutable image tags when operating
  in strict mode

## Current Emulator State

Already implemented:

- YAML top-level and job-level variables.
- Pipeline-level variables for trigger/schedule/API/manual contexts.
- Minimal metadata preservation for raw, masked/public, and file variables in
  runner payloads.
- Official runner VM validation for variable delivery.
- Trigger token and schedule paths that create persisted pipelines.
- Runner trace append APIs where log redaction can be enforced centrally.

Known gaps:

- No persisted project/group/instance variable API or admin UI.
- No full precedence merge across instance, group, project, pipeline, YAML,
  dotenv, and predefined variables.
- No environment-scoped variable matching.
- No protected-ref variable filtering.
- No variable expansion policy.
- No hidden variable write-only behavior.
- No log redaction engine shared by variables and secrets.
- No native secrets manager data model or `secrets:` YAML support.
- No pipeline-security policy/config model.
- No strict-mode warnings or blocks for mutable images, unsafe includes, or
  dangerous variable override behavior.

## Data Model

### CI Variables

Add a `ci_variables` table:

- `id`
- `scope_type`: `instance`, `group`, `project`
- `scope_id`: nullable for instance variables
- `key`
- `value_encrypted` or MVP `value`
- `variable_type`: `env_var` or `file`
- `visibility`: `visible`, `masked`, `masked_and_hidden`
- `protected`: boolean
- `raw`: boolean, matching API naming for expansion disabled
- `environment_scope`: default `*`
- `description`
- `created_at`, `updated_at`

Unique key:

- `scope_type`, `scope_id`, `key`, `environment_scope`

MVP can store values plaintext in SQLite with explicit documentation. The API
and UI should hide values for `masked_and_hidden` after creation to match the
write-only expectation.

### CI Secrets

Add a `ci_secrets` table:

- `id`
- `scope_type`: `group`, `project`
- `scope_id`
- `name`
- `value_encrypted` or MVP `value`
- `description`
- `environment_scope`: default `*`
- `branch_scope`: default `*`
- `protected`: boolean
- `rotation_reminder_days`: nullable
- `status`: `healthy`, `missing`, `inaccessible`, `rotating`, default
  `healthy`
- `last_accessed_at`: nullable
- `last_accessed_by_job_id`: nullable
- `created_at`, `updated_at`

Unique key:

- `scope_type`, `scope_id`, `name`, `environment_scope`, `branch_scope`

MVP can skip provisioning state and model GitLab Secrets Manager as enabled by
default for emulator-created projects/groups. A later slice can add
`secrets_manager_enabled` and provisioning/error states.

Add a `ci_secret_access_events` table after the first working secret delivery:

- `id`
- `secret_id`
- `project_id`
- `pipeline_id`
- `job_id`
- `ref`
- `environment`
- `accessed_at`

This matches the important product behavior shown in the reference UI: every
job read should become auditable, even if the emulator starts with a simple
table instead of a full audit event stream.

### Pipeline Security Settings

Add project-level CI security settings, either as columns on `repositories` or
a `project_ci_settings` table:

- `ci_pipeline_variables_minimum_override_role`: MVP enum
  `developer`, `maintainer`, `owner`, `no_one_allowed`
- `ci_allow_untrusted_remote_includes`: boolean, default false
- `ci_strict_security_mode`: boolean, default false
- `ci_warn_on_unpinned_images`: boolean, default true
- `ci_warn_on_unpinned_includes`: boolean, default true

Roles can map to the emulator's current coarse owner/admin permissions until a
real member role model exists.

## API Surface

### Project Variables

Implement GitLab-shaped project variable endpoints first:

- `GET /api/v4/projects/:id/variables`
- `POST /api/v4/projects/:id/variables`
- `GET /api/v4/projects/:id/variables/:key`
- `PUT /api/v4/projects/:id/variables/:key`
- `DELETE /api/v4/projects/:id/variables/:key`

Support `filter[environment_scope]` where useful.

Fields:

- `key`
- `value` on create/update/read unless hidden
- `variable_type`
- `protected`
- `masked`
- `hidden`
- `raw`
- `environment_scope`
- `description`

### Group Variables

Status: implemented for the MVP. Group variable CRUD is available through the
GitLab-shaped API and through the Admin > Groups edit page.

- `GET /api/v4/groups/:id/variables`
- `POST /api/v4/groups/:id/variables`
- `GET /api/v4/groups/:id/variables/:key`
- `PUT /api/v4/groups/:id/variables/:key`
- `DELETE /api/v4/groups/:id/variables/:key`

Group inheritance traverses parent group paths in the resolver. UI management
is admin-oriented for now because there is no dedicated group frontend shell.

### Instance Variables

Status: implemented for the MVP. Instance variables are managed through
Admin > Instance variables and participate in runner payload resolution below
group and project variables.

- Add REST endpoints later if client workflows need direct instance-variable
  API access.

### Secrets Manager

GitLab's public REST shape for the new native Secrets Manager may still be
moving while it is beta. MVP should prioritize pipeline YAML compatibility and
admin/API seedability over pretending the full product API is stable.

Implement emulator-native APIs first:

- `GET /api/v4/projects/:id/secrets`
- `POST /api/v4/projects/:id/secrets`
- `GET /api/v4/projects/:id/secrets/:name`
- `PUT /api/v4/projects/:id/secrets/:name`
- `DELETE /api/v4/projects/:id/secrets/:name`
- matching group endpoints after project secrets work

Document these as emulator extension endpoints until confirmed against the
current GitLab Secrets Manager API surface.

## Variable Resolution Engine

Create a shared `app/services/ci_variables.py` resolver:

Input:

- project/repo
- pipeline
- job
- ref
- environment name, if known
- pipeline source
- requested pipeline variables
- YAML top-level variables
- YAML job variables
- dotenv variables, later

Output:

- runner payload variables list with:
  - `key`
  - `value`
  - `public`
  - `masked`
  - `raw`
  - `file`
  - `source`

MVP precedence, highest to lowest:

1. Pipeline variables: API, trigger, schedule, manual, manual job variables.
2. Project variables.
3. Group variables.
4. Instance variables.
5. Dotenv report variables, once implemented.
6. YAML job variables.
7. YAML top-level variables.
8. Deployment variables, later.
9. Predefined variables.

This matches GitLab closely enough for practical integration tests while
leaving policy variables and scan execution policy variables for later.

Filtering:

- Drop protected variables unless the ref is protected.
- Match environment scope with exact, wildcard, then `*`.
- Do not include hidden values in API responses after creation, but do include
  them in runner payloads when eligible.
- Reject variable keys with spaces or invalid characters.
- Enforce rough value limits.

File variables:

- Preserve `file: true` in the runner payload.
- Let the official GitLab Runner create temp files where it already supports
  file variables.
- For any emulator-side execution path, create temp files and pass paths.

Expansion:

- MVP: implement one-pass `$VAR`/`${VAR}` expansion only for variables with
  `raw=false` and not masked/hidden.
- Later: match GitLab's expansion edge cases more exactly.

## Secrets Resolution Engine

Extend CI YAML parsing for job `secrets:`.

Supported MVP syntax:

```yaml
job:
  secrets:
    GCP_SERVICE_ACCOUNT_KEY:
      gitlab_secrets_manager:
        name: gcp_service_account_key
      file: true
    DEPLOY_SECRET:
      gitlab_secrets_manager:
        name: deploy-credentials
      file: false
```

Also support group source:

```yaml
job:
  secrets:
    KUBE_CA_PEM:
      gitlab_secrets_manager:
        name: kube-cert
        source: group/my-group/my-subgroup
```

Rules:

- Secrets are never injected unless requested by the job.
- Project secrets are only available to pipelines in that project.
- Group secrets are available to projects in that group/subgroup hierarchy.
- Match `environment_scope` and `branch_scope`.
- Drop protected secrets unless the ref is protected.
- Default injection is file mode.
- `file: false` injects the secret value as an env var.
- Missing/ineligible secrets should fail pipeline creation or mark the job
  failed before runner assignment with a clear diagnostic.
- Job scripts should receive file-mode secrets as a variable containing a path
  to a temporary file. The runner or executor should discard the file when the
  job ends.
- Access events should be written when a job receives a secret, tied to the
  pipeline/job/ref/environment that requested it.

Runner payload:

- Treat resolved secrets as variables with `masked=true`.
- Use `file=true` unless `file: false`.
- Add source metadata internally for diagnostics, but avoid leaking value or
  path decisions in public UI.

## Log Redaction

Create a shared redaction service:

- Build a per-job redaction set from masked variables and resolved secrets.
- Replace exact values in traces with `[MASKED]`.
- Ignore empty values and values shorter than the minimum maskable threshold.
- Redact on trace append, not only display, so API responses and UI agree.
- Include a redaction regression suite for:
  - plain masked variable
  - file variable value
  - secret value
  - multiline secret
  - modified/escaped value not fully redacted, documented as GitLab-compatible
    limitation

## Pipeline Security Guardrails

Add a `app/services/ci_security.py` analyzer that runs during pipeline
creation after includes are resolved and before jobs are persisted.

MVP diagnostics:

- warn if job image uses `:latest`
- warn if job image has a tag but no digest
- warn if image uses variable interpolation
- warn or block `include:remote` depending on project setting
- warn if `include:project` lacks a pinned ref
- warn if pipeline variables override predefined `CI_*` variables
- block pipeline variables when
  `ci_pipeline_variables_minimum_override_role=no_one_allowed`

Persist diagnostics:

- Add JSON `security_warnings` to pipeline metadata, or a separate
  `pipeline_diagnostics` table if broader diagnostics need querying.
- Show warnings in CI Lab and repo pipeline detail.

Strict mode:

- `warn` mode is default.
- `strict` mode converts unsafe includes and unsafe image refs to pipeline
  creation errors.

## UI Work

### Admin UI

Status: implemented for MVP CI variable management.

- Instance variables table with create/update/delete.
- Group variables and secrets are available from Admin > Groups edit pages.
- Project variables and secrets are available from project `Secure` pages.
- Pipeline security defaults.

### Project Settings UI

Status: implemented for project-scoped CI/CD variables and secrets. The project
sidebar exposes `Secure > CI/CD variables` and `Secure > Secrets`; repository
settings continue to expose pipeline variable policy and strict CI security
mode.

- Variables table with add/edit/delete.
- Visibility, protected, file type, raw/expand, environment scope fields.
- Hide values for hidden variables.
- Secrets table with add/edit/delete.
- Security settings panel for pipeline variable restriction and strict mode.

### Secrets Manager UI

Status: implemented for MVP project workflows as `Secure > Secrets`.

- Route: `/ui/:owner/:repo/-/secrets`
- Left nav group: `Secure`
- Nav item: `Secrets`
- Heading: `Secrets`
- Description: secrets can be API tokens, database credentials, or private
  keys; unlike CI/CD variables, they must be explicitly requested by a job.
- Stored secrets table:
  - `Name`
  - `Status`
  - environment scope
  - branch/ref scope
  - protected flag
  - last access metadata
- Inline create/update/delete forms support value replacement without showing
  stored secret values after creation.

### CI Lab

Add fast validation controls:

- Inject pipeline variables when creating a pipeline.
- Show effective variables by key/source without secret values.
- Show resolved secrets by key/source without values.
- Show security warnings.
- Add sample YAML snippets for variables and secrets.

### Repo Pipeline UI

Extend pipeline detail when secret-consuming jobs exist:

- Jobs that requested secrets should display normally in the pipeline graph.
- Pipeline/job detail should not reveal secret values.
- Job detail may show a non-sensitive note that secrets were requested, with
  secret names and whether each was injected as file or env var.
- Trace output must show masked values if a job prints a secret.
- The pipeline graph does not need special secret icons for MVP, but tests
  should prove a job named like `verify-db-password` can run with a requested
  secret.

## Validation Plan

### Unit/API Tests

- Project variable CRUD.
- Group variable CRUD.
- Hidden value cannot be read back.
- Protected variables filtered on unprotected refs.
- Environment scope matching.
- Precedence resolution.
- File variable payload shape.
- Secret CRUD.
- `secrets:` YAML parse and resolution.
- Secret file mode and `file: false`.
- Missing secret failure.
- Log redaction.
- Security warning generation.

### Official Runner VM Tests

Add Make targets:

- `make vm-runner-project-variable-test`
- `make vm-runner-group-variable-test`
- `make vm-runner-file-variable-test`
- `make vm-runner-secret-file-test`
- `make vm-runner-secret-env-test`
- `make vm-runner-redaction-test`
- `make vm-k8s-runner-secret-file-test`
- `make vm-k8s-runner-secret-env-test`
- `make vm-k8s-runner-redaction-test`
- `make vm-k8s-runner-secret-validate`
- `make vm-k8s-incluster-secret-file-test`
- `make vm-runner-security-diagnostics-test`

Scenarios:

- A Docker runner job receives project variable and prints non-secret value.
- A masked variable printed in job output appears as `[MASKED]` in trace.
- A file variable is usable as a path inside the job.
- A job with `secrets:` receives a file path and can read the secret file.
- A job with `file: false` receives the secret as an env var.
- A job on an unprotected ref cannot access protected variable/secret.
- A pipeline with unsafe image/include stores warnings.

### Kubernetes Runner Tests

After Docker runner validation:

- Repeat variable, file variable, secret, and redaction tests with the
  Kubernetes executor VM-service runner.
- Repeat at least secret file mode with the in-cluster runner.

## Implementation Slices

### Slice 1: Variable Data Model and Project Variable API

Status: implemented for project-scoped variables.

Deliver:

- `ci_variables` model.
- Project variable CRUD endpoints.
- Tests for CRUD, hidden read behavior, validation.

### Slice 2: Variable Resolver and Runner Payload Integration

Status: implemented for default-scope project variables.

Deliver:

- Shared variable resolver.
- Project variables merged into persisted runner payload.
- Precedence tests.
- Official Docker runner project variable validation.

### Slice 3: File, Masked, Protected, and Scoped Variables

Status: implemented for project variables in the MVP. File/masked/raw metadata
reaches runner payloads, protected variables are filtered by protected refs,
environment scopes match job environments, and masked values are redacted from
stored traces.

Deliver:

- File variable support for persisted variables.
- Protected ref filtering.
- Environment scope matching.
- Trace redaction for masked values.
- Runner validation for file variable and redaction.

### Slice 4: Group and Instance Variables

Status: implemented for the MVP. Group variable CRUD and instance admin
variable UI are available, and runner payload resolution applies instance,
parent group, child group, and project variable precedence.

Deliver:

- Group variable CRUD.
- Direct group inheritance.
- Instance variable admin UI.
- Precedence tests across instance/group/project.

### Slice 5: Secrets Data Model and Emulator APIs

Status: implemented for the MVP. Project and group secrets have emulator API
CRUD, secret values are write-only on read responses, and jobs that resolve
secrets record non-sensitive `ci_secret_access_events` plus last-access fields
on the underlying secret. Project secrets are managed from the project
`Secure > Secrets` page, and group secrets are managed from Admin > Groups edit
pages.

Deliver:

- `ci_secrets` model.
- `ci_secret_access_events` model.
- Project secret CRUD.
- Group secret CRUD if group hierarchy support is ready.
- UI/admin seed controls.
- Project `Secure > Secrets Manager` list and `New secret` pages.

### Slice 6: `secrets:` YAML Resolution

Status: implemented for the MVP. Job-level `secrets:` entries using
`gitlab_secrets_manager` are parsed, resolved from eligible project/group
secrets, injected into the runner variable payload as masked values, and logged
to `ci_secret_access_events` when jobs are created. Pipeline and job API/UI
surfaces show non-sensitive secret metadata such as variable key, backing secret
name, scope, and file/env delivery mode without exposing secret values.

Deliver:

- Parser support for `gitlab_secrets_manager`.
- Project and group source resolution.
- File mode default and `file: false`.
- Missing/ineligible secret diagnostics.
- Runner payload integration.
- Secret access event creation.
- Pipeline/job UI avoids value leaks while showing non-sensitive secret names.

### Slice 7: Secret Redaction and Runner Validation

Status: implemented for the MVP. Trace redaction now uses a shared service fed
by masked job variables, including resolved secrets. Local API tests cover
masked variables and `secrets:` values. Official Docker runner validation is
available through `make vm-runner-secret-file-test`,
`make vm-runner-secret-env-test`, and `make vm-runner-redaction-test`; the
redaction target validates both file-mode and env-mode secret delivery plus
stored trace masking. Kubernetes executor validation reuses the same secret
validation script with the `k8s` and `k8s-incluster` runner tags through
`make vm-k8s-runner-secret-validate` and
`make vm-k8s-incluster-secret-file-test`.

Deliver:

- Shared redaction engine for masked variables and secrets.
- Official runner Docker validation.
- Kubernetes runner validation.

### Slice 8: Pipeline Security Settings and Diagnostics

Status: implemented for the MVP. Projects expose emulator CI security settings
for pipeline variable override policy and strict security mode. Pipeline
creation stores warning diagnostics for mutable image refs, variable image refs,
remote include risks, and predefined `CI_*`/`GITLAB_*` pipeline variable
overrides. Warnings are returned in pipeline JSON, pipeline diagnostics, the CI
Lab, and repository pipeline detail views. Repository settings now include UI
controls for pipeline variable policy and strict CI security mode.

Deliver:

- Project CI security settings.
- Analyzer warnings for mutable images, variable image refs, unsafe includes,
  unpinned includes, and pipeline-variable overrides.
- CI Lab and repo pipeline detail warning display.
- Strict-mode blocking and role-gated pipeline variable rejection are covered
  by Slice 9.

### Slice 9: Strict Mode and Permission Gates

Status: implemented for the MVP. Strict security mode blocks pipeline creation
for mutable image refs, variable image refs, unsafe remote includes, and
unpinned remote includes. Pipeline variable override policy is enforced against
the emulator's GitLab-shaped access levels:

- `developer`: Developer and higher.
- `maintainer`: Maintainer and higher.
- `owner`: Owner/site admin.
- `no_one_allowed`: nobody.

Project and group CI variables and secrets now require Maintainer or higher.
Trigger-token and schedule-created pipelines keep their credentialed variable
flows, while direct project pipeline creation checks the requesting user's
project access level.

Deliver:

- Shared GitLab access-level helper for Guest, Reporter, Developer,
  Maintainer, and Owner.
- Pipeline variable minimum role enforcement using project membership,
  inherited group membership, project ownership, and site-admin status.
- Maintainer-plus gates for project/group CI variables and secrets.
- Strict-mode blocks for unsafe includes/images.
- Tests for blocked pipeline creation, diagnostics, and Maintainer vs
  Developer vs Guest CI variable/secret access.

Deferred from this slice:

- Full GitLab authorization parity across every non-CI project/group endpoint.

## Open Questions

- Should emulator-native secrets APIs intentionally differ from GitLab while
  GitLab Secrets Manager remains beta, or should we wait for a stable REST
  contract?
- Should protected refs be modeled minimally as exact branch/tag names first,
  or should this depend on the protected branches API work?
- Do we want security diagnostics to be warnings-only by default in every
  environment, or should CI Lab expose a strict-mode toggle first?
