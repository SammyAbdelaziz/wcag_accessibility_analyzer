# Turning the POC off and choosing a permanent path

This POC is intended for **time-boxed evaluation only**. Once evaluation is
done, either turn it off or replatform it through your organization's
official channels.

## When to turn it off

Turn it off when **any** of these is true:

- The evaluation window is over.
- The endpoint is reachable on the public internet without an auth layer.
- A non-evaluator (anyone outside the agreed test group) is sending traffic.
- Real or potentially sensitive documents have been uploaded.
- You see unexpected traffic patterns in the Container App metrics.

## Fastest "off" (minutes)

Pick whichever level matches the risk:

### Level 1 — Stop traffic immediately (1 click)
Azure Portal → your Container App → **Ingress** → toggle **Ingress** off →
**Save**. The HTTPS endpoint will start returning a connection failure
within seconds.

### Level 2 — Scale to zero (1 minute)
Azure Portal → your Container App → **Scale and replicas** → set
**Min replicas = 0** and **Max replicas = 0** → **Save**. The app stops
serving and stops billing compute.

### Level 3 — Delete the app (5 minutes)
Azure Portal → your Container App → **Delete**. The image stays in ACR; the
runtime is gone.

### Level 4 — Delete everything (10 minutes)
Azure Portal → **Resource groups** → the resource group you created for the
POC → **Delete resource group**. Removes the Container App, environment,
ACR, Log Analytics, and any other resources you placed there.

> Tip: Use a dedicated resource group per POC. It makes Level 4 a single,
> reversible-by-redeploy action.

## What to keep, what to discard

After turning it off:

- **Keep**: any synthetic sample documents you created and the findings JSON
  you used for evaluation notes.
- **Discard**: any uploaded real documents that were used for testing.
- **Rotate**: any ACR admin keys you enabled for the evaluation, if you do
  not delete the registry.
- **Review**: Log Analytics workspace for filenames that should not have
  been uploaded, and purge if necessary.

## After the POC: choosing a permanent path

If the evaluation is successful and the capability is wanted long-term, do
**not** promote this POC into production as-is. Use your organization's
standard engineering channel. A typical path looks like:

1. **Capture requirements.** Bring the findings JSON, sample documents, and
   evaluation notes to your platform/security/engineering org.
2. **Re-platform through official channels.** Have the responsible team
   take ownership of:
   - Source-code review and supply-chain scanning.
   - Authentication / authorization (e.g., Entra ID, APIM, App Service Auth).
   - Network hardening (private networking, WAF — see
     [`private-networking.md`](private-networking.md)).
   - Logging, monitoring, alerting, on-call rotation.
   - Backup / DR posture.
   - Patch and dependency management.
   - Identity and key management.
3. **Decommission the POC** using Levels 1–4 above once the official
   deployment is in place.

This pattern keeps the POC honest about its role: it shows that the
capability works, then steps aside.

## Communicating "off" to evaluators

If others are using the endpoint, send a short note before you turn it off:

> The accessibility analyzer POC evaluation window ends on **YYYY-MM-DD**.
> After that date the endpoint will be disabled. If you want this
> capability longer-term, request it through the standard internal
> engineering intake so it can be reviewed and supported as a real service.
