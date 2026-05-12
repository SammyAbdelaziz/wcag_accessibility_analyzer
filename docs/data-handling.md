# Data handling

What happens to the documents you (or your testers) upload.

## Short answer

The POC code itself does **not** persist uploaded documents.

- Files are received in memory, analyzed, and discarded when the request
  ends.
- Findings are returned in the HTTP response. No database, no blob storage,
  no file system writes by the analyzer code.
- The `/api/remediate` endpoint returns the patched file in the response
  body (base64). It does not store the patched output.

## What can still retain data

Even though the application code does not persist, your **hosting
environment** can. This is not a hidden trick of this app — it is just how
Azure works — but it matters for security review:

| Source | Default behavior | How to control |
|---|---|---|
| Azure Container Apps platform logs | Capture stdout/stderr. Findings are logged at info level; filenames are logged. | Adjust log level. Disable Log Analytics integration if not required. |
| Application Insights | If you connect it, it captures request metadata. Request bodies are not captured by default. | Don't connect it, or disable request-body sampling. |
| Ingress / front-door / WAF logs | Capture request URLs and headers. | Configured by your network team. |
| Cloud Shell history (if you used `curl` with a file path) | Captures the command line. | Clear shell history if needed. |
| Tesseract / LibreOffice temp files | Written to `/tmp` inside the container during OCR. Wiped when the replica restarts. | Use ephemeral storage only (the default). |

## What we recommend telling users

A short, plain-English upload notice for any front end built on top of this
API:

> **Do not upload regulated, restricted, or highly confidential content
> (clinical data, patient identifiers, controlled financial information,
> legal hold material) unless your organization has formally approved this
> deployment for that data class.** Use synthetic or already-public
> documents for evaluation.

## What this POC does NOT implement

These are deliberately out of scope. Add them in your deployment, not in
this codebase:

- Authentication / authorization on the HTTP endpoint.
- Per-tenant or per-user isolation.
- Rate limiting.
- Encryption at rest (the app does not persist anything to begin with).
- Customer-managed-keys integration.
- Data residency controls beyond what the Azure region provides.
- Formal audit trails (only standard application logs are produced).

If any of the above are required for your scenario, that is a strong
signal that the POC should be re-platformed through your standard
engineering channels rather than promoted in place.
