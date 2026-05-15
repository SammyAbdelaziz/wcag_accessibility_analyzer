# GitHub Setup Checklist For Safe ALM

This checklist turns the repo-side ALM baseline into enforced GitHub controls.

Apply these settings only to the canonical product repo.

## 1. Protect `main`

If your repo shows the newer GitHub UI:

- Go to `Settings` -> `Rules` -> `Rulesets`
- Create a new branch ruleset
- Target branch pattern: `main`

If your repo shows the classic UI:

- Go to `Settings` -> `Branches`
- Add a branch protection rule for `main`

Use these settings:

- Require a pull request before merging: `On`
- Required approvals: `1`
- Dismiss stale pull request approvals when new commits are pushed: `On`
- Require approval of the most recent reviewable push: `On` if available
- Require status checks to pass before merging: `On`
- Require branches to be up to date before merging: `On`
- Required status check: select the check created by the `CI` workflow after its first run
- Require conversation resolution before merging: `On`
- Block force pushes: `On`
- Block deletions: `On`
- Include administrators: `On`
- Do not allow bypassing the above settings: `On` if available

Do not add a wildcard rule here. Protect `main` only.

## 2. Create the `production` Environment

- Go to `Settings` -> `Environments`
- Create environment: `production`

Set these controls:

- Required reviewers: at least `1`
- Wait timer: `0` unless your team wants a delay before deploy
- Deployment branches: restrict to `main` if that option is shown

This environment is used by the `Deploy Production` workflow so that deploys require an explicit human approval step.

## 3. Add Repository Variables

- Go to `Settings` -> `Secrets and variables` -> `Actions` -> `Variables`

Create these repository variables:

- `AZURE_ACR_NAME` = `acrwcag1965`
- `AZURE_CONTAINER_APP` = `ca-wcag-1965`
- `AZURE_RESOURCE_GROUP` = `rg-wcag-analyzer`
- `AZURE_SUBSCRIPTION_ID` = `ec1b7fdf-25ae-466f-ad14-49935ca19236`
- `AZURE_TENANT_ID` = `8f4b2458-706c-4396-a1fe-4182a53cc1f9`
- `AZURE_CLIENT_ID` = `<client id of the GitHub OIDC Azure app or user-assigned managed identity>`

Notes:

- `AZURE_CLIENT_ID` should belong to the principal you use for GitHub OIDC login.
- Do not store the client secret in GitHub for this flow. The workflows are designed for OIDC, not secret-based login.
- The current Azure CLI context used for this setup was subscription `ME-M365CPI77194953-sabdelaziz-1` under tenant `8f4b2458-706c-4396-a1fe-4182a53cc1f9`.

## 4. Configure Azure OIDC Trust

Create or reuse an Azure app registration or user-assigned managed identity for GitHub Actions.

Then add a federated credential with these values:

- Issuer: `https://token.actions.githubusercontent.com`
- Subject: `repo:SammyAbdelaziz/wcag_accessibility_analyzer:environment:production`
- Audience: `api://AzureADTokenExchange`

Recommended minimum Azure access:

- On the resource group containing the Container App: `Contributor`
- On the ACR used for builds: a role sufficient for ACR build and repository read operations

If you want tighter scope later, split build and deploy into separate principals.

## 5. First-Time Activation Order

Follow this order to avoid confusing GitHub with a required status check that does not exist yet.

1. Push the ALM branch and open a PR.
2. Let the `CI` workflow run once.
3. After the first `CI` run appears in GitHub, return to branch protection and select its required check.
4. Merge the PR only after protection is enabled.
5. Create the `production` environment.
6. Add the repository variables.
7. Configure the Azure federated credential.
8. Run `Build Release Candidate` from `main`.
9. Review the digest produced by the workflow summary.
10. Run `Deploy Production` using that exact digest.

## 6. Normal Day-to-Day Flow

Use this operating model:

1. Branch from `main` using `feature/<name>` or `chore/<name>`.
2. Open a PR into `main`.
3. Wait for `CI` to pass.
4. Merge by PR only.
5. Rebuild the image from merged `main` with `Build Release Candidate`.
6. Deploy the exact digest with `Deploy Production`.

This keeps the deployed artifact tied to a specific Git commit and a specific review trail.

## 7. What Not To Do

- Do not build from the sandbox `wwwroot` repo for production deploys.
- Do not deploy mutable tags to production when a digest is available.
- Do not push directly to `main` once branch protection is enabled.
- Do not skip the release-candidate rebuild step before production deploy.