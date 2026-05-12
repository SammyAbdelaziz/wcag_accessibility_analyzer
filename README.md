# WCAG Accessibility Analyzer

> **Status: working proof point.** The end-to-end pipeline runs against
> real DOCX, PPTX, HTML, PDF, and XLSX documents and returns evidence-backed
> WCAG 2.2 findings. To move from "proof that this works" to a hardened,
> supported service, deploy it **behind an authentication layer**, add
> **private networking** where required, and put it through your
> organization's standard engineering channels for ownership and patching.
> Shared as-is under the MIT license.

An evidence-first WCAG 2.2 accessibility analyzer for office documents and
web content. Designed to run as a container behind a thin HTTP API so it can
be integrated with any front end (chat agent, web portal, CI step, etc.).

## Audience and packaging

- **What this is:** a containerized HTTP service. You build the Docker image,
  run the container, and POST documents to its endpoints.
- **What this is not:** a pip-installable Python library or a CLI tool. The
  analyzers under `wcag/` are organized as internal modules behind the HTTP
  API; they are not published to PyPI and there is no `wcag` command.
- **Who it's for:** engineers comfortable with Docker and Azure (or any
  container runtime). The deployment guide assumes Azure Container Apps,
  but the image runs anywhere Docker runs.

## What it does

This is a containerized HTTP service. You POST a document, you get a
structured WCAG fact-sheet back. Full breakdown in
[`docs/capabilities.md`](docs/capabilities.md).

- **Analyzers** (in `wcag/analyzers/`): `docx`, `pptx`, `html`, `pdf`,
  `xlsx`, plus an optional `ocr` layer for DOCX/PPTX.
- **Remediators** (in `wcag/remediators/`): `docx` and `pptx` only, for a
  deterministic subset of fixes.
- **Endpoints**:
  - `POST /api/analyze` — analyze one uploaded file (≤ 20 MB).
  - `POST /api/remediate` — apply a list of fixes to a DOCX/PPTX and return
    the patched file.
- **Output**: JSON `FactSheet` with `summary`, `findings[]`, and a
  confidence tier per finding (`confirmed` vs `possible`).

## What explicitly is NOT in scope

- It is **not** a full WCAG conformance certifier. Passing the analyzer
  does not by itself prove conformance.
- It does **not** evaluate time-based media (captions, transcripts, audio
  descriptions, sign-language).
- It does **not** perform full browser/runtime journey simulation for
  interactive web apps.
- It does **not** make legal, medical, or HR determinations.
- It does **not** implement authentication, authorization, or rate limiting
  on its HTTP endpoints. **Put your own auth layer in front before any use
  beyond local evaluation.**
- It does **not** persist uploaded documents (see
  [`docs/data-handling.md`](docs/data-handling.md)).

## Architecture (short version)

```
client ──HTTP──> Azure Container App (this repo)
                 │
                 ├─ Layer 1: Structural analysis (XML / DOM / object model)
                 ├─ Layer 2: Rendering metrics (contrast, theme resolution)
                 └─ Layer 3: Optional OCR (LibreOffice + Tesseract)
```

See [`docs/architecture.md`](docs/architecture.md) for the longer overview.

## Quick start

Pick one of the deployment paths below.

### Option A — Azure Container Apps (recommended for evaluation)

Step-by-step portal + Cloud Shell instructions are in
[`deploy/azure-container-apps.md`](deploy/azure-container-apps.md).

Summary:

```bash
git clone https://github.com/SammyAbdelaziz/wcag_accessibility_analyzer.git
cd wcag_accessibility_analyzer
az acr build --registry <YOUR_ACR_NAME> --image wcag-analyzer:poc .
# Then create the Container App from the Azure Portal pointing at this image.
```

### Option B — Local Docker (fastest sanity check)

```bash
git clone https://github.com/SammyAbdelaziz/wcag_accessibility_analyzer.git
cd wcag_accessibility_analyzer
docker build -t wcag-analyzer:local .
docker run --rm -p 8080:80 wcag-analyzer:local
```

In another shell, smoke-test the running container with the bundled sample:

```bash
curl -X POST "http://localhost:8080/api/analyze" \
  -F "file=@samples/sample.docx" \
  -o response.json
head -c 1500 response.json
```

You should see a JSON `FactSheet` with `summary` and `findings[]`. For more
examples (PowerShell, larger files, `/api/remediate`), see
[`deploy/SMOKE_TEST.md`](deploy/SMOKE_TEST.md).

## Smoke test

Once the container is reachable, follow
[`deploy/SMOKE_TEST.md`](deploy/SMOKE_TEST.md) to validate the API with a
sample document.

## Repo layout

```
.
├── function_app.py            # HTTP entry point
├── wcag/                      # Analyzer + remediator packages
├── Dockerfile                 # Container build
├── requirements.txt           # Python dependencies
├── host.json                  # Azure Functions host config
├── tests/                     # Regression tests
├── samples/                   # Small sample documents for smoke testing
├── deploy/
│   ├── azure-container-apps.md
│   └── SMOKE_TEST.md
└── docs/
    ├── architecture.md
    ├── capabilities.md
    ├── data-handling.md
    ├── private-networking.md
    └── turning-it-off.md
```

## Decommissioning

This is a time-boxed evaluation deployment by default. Before lighting it
up, agree on how it will be turned off — or what would need to be added
to keep it running. See [`docs/turning-it-off.md`](docs/turning-it-off.md)
for the four levels of "off" and the recommendation to re-platform
through your organization's standard engineering channels if the
capability is wanted long-term.

## License

[MIT](LICENSE). See also [`SECURITY.md`](SECURITY.md) for the responsible-use
disclaimer.

## Contributing

This is a small POC; issues and PRs are welcome but there is no SLA. See
[`CONTRIBUTING.md`](CONTRIBUTING.md).
