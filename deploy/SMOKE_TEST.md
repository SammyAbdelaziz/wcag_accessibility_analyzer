# Smoke test

Validates the container API is reachable and returns a structured response.
No Copilot Studio (or other front end) required.

## What you need

- The Container App URL (e.g., `https://ca-wcag-poc.<random>.<region>.azurecontainerapps.io`).
- A sample document. Two are bundled in [`../samples/`](../samples/):
  - `samples/sample.docx`
  - `samples/sample.pptx`
- `curl` (or PowerShell `Invoke-RestMethod`).

## 1. Health check

```bash
curl -i https://<your-app-url>/
```

You should get an HTTP 200 (or a Functions default page) confirming the
container is up.

## 2. Analyze a document

### bash / Cloud Shell

```bash
curl -sS -X POST \
  -F "file=@samples/sample.docx" \
  https://<your-app-url>/api/analyze \
  | jq '.'
```

### PowerShell

```powershell
$uri = "https://<your-app-url>/api/analyze"
$form = @{ file = Get-Item .\samples\sample.docx }
Invoke-RestMethod -Method Post -Uri $uri -Form $form | ConvertTo-Json -Depth 8
```

## 3. Expected response shape

A successful response is a JSON object that looks roughly like:

```json
{
  "filename": "sample.docx",
  "file_type": "docx",
  "summary": {
    "confirmed_count": 3,
    "possible_count": 1
  },
  "findings": [
    {
      "id": "F-0001",
      "criterion": "1.3.1",
      "confidence": "confirmed",
      "title": "Heading level skipped",
      "evidence": "...",
      "remediation_ref": "..."
    }
  ]
}
```

The exact fields evolve with the analyzer; what matters for the smoke test:

- HTTP 200 response.
- `findings` array is present.
- `file_type` matches the uploaded file.

## 4. Common failures

| Symptom | Likely cause |
|---|---|
| 404 on `/api/analyze` | Container not finished provisioning, or wrong URL. |
| 500 with "no analyzer for type" | Unsupported file extension. |
| 502 / cold-start timeout | First request after idle. Retry once. |
| Slow first response (~30s+) on DOCX/PPTX | OCR auto-mode ran. Subsequent requests are faster. |

## 5. Next steps after a green smoke test

- Try the other sample files in `samples/`.
- Upload your own non-sensitive document (read [`../SECURITY.md`](../SECURITY.md) first).
- Wire the endpoint into whatever front end you want to evaluate.
