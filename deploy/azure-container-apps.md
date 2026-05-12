# Deploying the POC to Azure Container Apps

> This is an evaluation walkthrough for the POC. **Do not** treat it as a
> production runbook. Follow your own organization's landing-zone and
> security standards.

You will:

1. Create an Azure Container Registry (ACR).
2. Build the container image from this repo using Cloud Shell.
3. Create an Azure Container Apps environment and app pointing at the image.
4. Smoke-test the public HTTPS endpoint.

Estimated time: 20–30 minutes the first time.

---

## Prerequisites

- An Azure subscription where you can create resources.
- Permissions to create ACR, Container Apps Environment, and Container App.
- Azure CLI available (Cloud Shell is fine).
- This repo cloned, either locally or in Cloud Shell.

---

## Step 1 — Create a resource group (Portal)

1. Sign in to <https://portal.azure.com>.
2. Search for **Resource groups** → **Create**.
3. Pick a subscription, name (e.g., `rg-wcag-poc`), and region.
4. Review + create.

## Step 2 — Create an Azure Container Registry (Portal)

1. Search for **Container registries** → **Create**.
2. Resource group: the one from Step 1.
3. Registry name: must be globally unique, e.g., `acrwcagpoc<initials>`.
4. SKU: **Basic** is fine for evaluation.
5. Review + create.
6. After creation, open the registry → **Settings → Access keys** → enable
   **Admin user** (for the simplest evaluation path). Note the **Login
   server**, e.g., `acrwcagpoc<initials>.azurecr.io`.

> For non-evaluation use, prefer a managed identity over admin keys.

## Step 3 — Build the image from this repo

Open **Cloud Shell** (top bar of the Portal) and run:

```bash
git clone https://github.com/SammyAbdelaziz/wcag_accessibility_analyzer.git
cd wcag_accessibility_analyzer

az acr build \
  --registry <YOUR_ACR_NAME> \
  --image wcag-analyzer:poc \
  .
```

Replace `<YOUR_ACR_NAME>` with the registry name (without `.azurecr.io`). The
build runs in ACR; you do not need Docker installed locally.

## Step 4 — Create a Container Apps environment (Portal)

1. Search for **Container Apps** → **Create**.
2. On the **Basics** tab:
   - Resource group: same as above.
   - Container app name: `ca-wcag-poc`.
   - Region: same as ACR.
3. On the **Container Apps environment** step, create a new environment
   (Consumption plan) in the same region.

## Step 5 — Configure the container image (Portal)

On the **Container** tab of the Container App create flow:

| Field | Value |
|---|---|
| Image source | **Azure Container Registry** |
| Registry | The ACR you created |
| Image | `wcag-analyzer` |
| Image tag | `poc` |
| CPU / Memory | 1.0 CPU / 2.0 Gi |
| Environment variable | `AzureWebJobsStorage` (leave empty for POC) |

## Step 6 — Configure ingress (Portal)

On the **Ingress** tab:

- **Enable ingress**: yes.
- **Ingress traffic**: Accepting traffic from anywhere (for evaluation only).
- **Target port**: `80`.
- Transport: HTTP.

> For anything beyond evaluation, restrict ingress and put an auth gateway
> (APIM, App Gateway with WAF, App Service Auth, etc.) in front.

## Step 7 — Review + create

Click **Review + create** → **Create**. Wait for provisioning.

## Step 8 — Capture the URL

Open the new Container App → **Overview** → copy the **Application URL**
(something like `https://ca-wcag-poc.<random>.<region>.azurecontainerapps.io`).

## Step 9 — Smoke test

Follow [`SMOKE_TEST.md`](SMOKE_TEST.md).

---

## Optional add-ons (skip for the smoke test)

- **Log Analytics**: enable it in the Container Apps environment to get
  request logs and stdout. Useful for collecting POC feedback signal.
- **Min replicas = 1**: avoid cold-start delay on idle traffic.
- **Managed identity + ACR pull role**: replace the admin keys for the
  registry pull.
- **Custom domain + TLS**: only if your evaluation requires a stable URL.

## Tear-down

Delete the resource group to remove everything in one step.
