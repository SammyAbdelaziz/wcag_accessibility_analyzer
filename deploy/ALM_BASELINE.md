# ALM Baseline For This Repo

This repo currently has one live Azure Container App and one canonical GitHub-tracked codebase. The immediate goal is to make changes auditable and safer without changing the runtime architecture first.

## Current State

- Product code source of truth: canonical GitHub repo on `main`
- Live runtime: one Azure Container App in single-revision mode
- Current risk: manual build and manual production deployment, with no GitHub-native CI gate or release approval gate
- Local sandbox content should stay separate from this product repo

## Recommended Baseline

Use a simple trunk-based flow.

1. Create a short-lived `feature/<name>` branch from `main`.
2. Open a pull request into `main`.
3. Require CI to pass before merge.
4. Merge to `main` only through a pull request.
5. Run `Build Release Candidate` to rebuild the image from the merged commit and capture the exact digest.
6. Review the build record, then run `Deploy Production` with that exact digest-based image reference.
7. Let the production workflow smoke-test the deployed app with the bundled DOCX and PPTX samples.

This keeps the artifact immutable between build and deploy, which is the key auditability improvement.

## Why Not Add `develop` Right Now

With one live environment and a small change surface, a permanent `develop` branch adds process overhead faster than it adds control. The safer next step is PR-gated `main` plus explicit build and deploy workflows. If a real staging environment is added later, a separate release branch or environment promotion flow becomes more justified.

## One-Time GitHub Setup

### Branch protection for `main`

Configure `main` so that:

- pull requests are required before merge
- direct pushes are blocked
- the `CI` workflow is required
- stale approvals are dismissed when new commits arrive
- administrators are included if you want the process enforced consistently

### GitHub environment

Create a `production` environment and require at least one reviewer before the `Deploy Production` workflow can run its deploy job.

### Repository variables

Add these repository variables:

- `AZURE_ACR_NAME`
- `AZURE_CLIENT_ID`
- `AZURE_CONTAINER_APP`
- `AZURE_RESOURCE_GROUP`
- `AZURE_SUBSCRIPTION_ID`
- `AZURE_TENANT_ID`

Use an Azure service principal or user-assigned managed identity configured for GitHub OIDC. The minimum role set should allow ACR build operations and Container App updates in the target resource group.

## Workflows Added

- `CI`: validates syntax, runs the stable repo-safe test gate, and verifies the Docker image builds
- `Build Release Candidate`: rebuilds the image in ACR from a chosen git ref and records the exact digest
- `Deploy Production`: deploys an exact digest-based image reference to the Container App and runs live smoke tests

## Local Fallback Scripts

Use these when you need the same flow from a terminal:

- `deploy/build_acr_image.ps1`
- `deploy/deploy_containerapp_digest.ps1`

The scripts are useful while GitHub environment settings are still being configured, and they mirror the same build-then-deploy discipline.

## Safe Rollout Order

1. Commit the workflow and documentation changes.
2. Configure branch protection and GitHub environment approval.
3. Configure Azure OIDC repository variables.
4. Run `CI` on a small PR.
5. Run `Build Release Candidate` from `main` and capture the digest.
6. Deploy only that digest with `Deploy Production`.

## Future Hardening

When you are ready for the next level, add:

- a separate staging Container App
- a `staging` GitHub environment with automatic deploys from `main`
- production deploys only by promotion of a staging-validated digest
- Azure Monitor alerts and Log Analytics-based release evidence