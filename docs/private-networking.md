# Private networking (forward-looking)

Notes for the future case where this is deployed into an internal-only
Azure Container Apps environment instead of the open evaluation topology.
This is **not** required to run the POC; it is a planning aid for the
official deployment path described in
[`turning-it-off.md`](turning-it-off.md).

## What the app needs at runtime

Steady-state, the analyzer is a **single container** that:

- Accepts inbound HTTPS on the Container Apps ingress port.
- Runs LibreOffice headless and Tesseract inside the container; both are
  installed at build time and have no outbound dependency at runtime.
- Imports Python packages installed at build time; no runtime package
  pulls.
- Does **not** call any external SaaS in steady state.

So the steady-state runtime connectivity surface is small.

## Build-time vs runtime

| Phase | Egress needed | Notes |
|---|---|---|
| Build (in ACR with `az acr build`) | Yes: PyPI, Debian package mirrors, Microsoft base image registry (mcr.microsoft.com). | Performed by ACR, not by the running app. |
| Pull image to Container App  | ACR endpoint reachable from the Container Apps environment. | Use private endpoint on ACR if private only. |
| Runtime analyze / remediate  | None to the public internet. | Inbound only. |

## Required connectivity for a private deployment

Front to back, an internal-only deployment typically needs:

1. **Inbound** to the Container App
   - From an internal source (APIM, App Gateway, internal load balancer,
     or VNet peer).
   - Over HTTPS to the ingress port.
   - Behind your normal authentication layer (Entra ID, APIM JWT policy,
     App Service Auth, etc.).

2. **Container Apps environment**
   - Deployed with **internal-only** ingress.
   - Bound to a workload subnet inside your VNet.

3. **Egress to ACR (image pull)**
   - Private endpoint on ACR exposed in (or peered into) the same VNet.
   - Required DNS resolution for `*.azurecr.io` mapped to the private IP.

4. **Egress to Microsoft platform services**
   - Only required for image pull and platform operations.
   - Service tags or private link to keep this off the public internet.

5. **Observability**
   - Log Analytics workspace reachable from the Container App. Use
     `Microsoft.OperationalInsights` via private link if your standard
     requires it.

6. **No outbound to the public internet at runtime**
   - The app does not need it. Block egress and you'll see no functional
     change in analyze/remediate behavior.

## Minimal architecture sketch (internal-only)

```
User
 │   (corporate network)
 ▼
APIM / App Gateway (WAF, auth)
 │   private
 ▼
Container Apps environment  ── pulls image ──► ACR (private endpoint)
 │
 └── stdout/stderr ──► Log Analytics (private link, optional)
```

## What to add on top of the POC code before a private deployment

Code-side gaps the POC does not fill (deliberately):

- Authentication and authorization (rely on the layer in front).
- Rate limiting (rely on APIM or App Gateway).
- Per-tenant isolation (decide if you need multi-tenant logical separation
  beyond network isolation).
- Audit logging beyond stdout (route through Azure Monitor).
- Image hardening and SBOM (rebuild from your golden base image, run image
  scanning).

## Bottom line

The application's connectivity needs are intentionally minimal: inbound
HTTPS from an internal source; image pull from a registry you control;
optional log shipping. Everything else is your organization's standard
landing-zone work.
