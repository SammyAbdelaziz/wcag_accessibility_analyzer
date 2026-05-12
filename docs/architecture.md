# Architecture

This document describes the POC architecture so reviewers can quickly judge
suitability before any deployment.

## Goals of the POC

- Provide an evidence-first WCAG 2.2 analyzer for office and web documents.
- Make outputs structured, auditable, and easy to triage.
- Keep the runtime portable: a single container with a thin HTTP surface.
- Keep cost predictable: no mandatory per-document paid APIs.

## High-level flow

```
┌──────────────┐    HTTPS     ┌────────────────────────────────────────┐
│ Any front    │ ───────────► │ Container App (this repo)              │
│ end          │              │                                        │
│ (chat agent, │              │  HTTP entry (function_app.py)          │
│ web portal,  │              │      │                                 │
│ CI step…)    │              │      ▼                                 │
└──────────────┘              │  Format router (file_types.py)         │
                              │      │                                 │
                              │      ▼                                 │
                              │  Analyzer for the detected type        │
                              │   ├─ Structural layer (XML / DOM)      │
                              │   ├─ Rendering layer (contrast, theme) │
                              │   └─ Optional OCR layer (LO + Tess.)   │
                              │      │                                 │
                              │      ▼                                 │
                              │  FactSheet JSON                        │
                              └────────────────────────────────────────┘
```

## Three analysis layers

1. **Structural** — parses the underlying package/DOM (OOXML, HTML, PDF
   objects, XLSX). Deterministic, fast (~0.5s typical), high confidence.
2. **Rendering metrics** — luminance, contrast ratios, theme resolution.
   Deterministic, low-latency.
3. **OCR (optional)** — LibreOffice headless + Tesseract. Catches
   images-of-text, visual tables, and rendered low-contrast cases.
   Heavier; runs only in `auto` or `deep` modes.

## Confidence tiers

- **Confirmed** — direct evidence in the file (XML/DOM/object model).
  Treat as fix-now.
- **Possible** — heuristic or OCR-derived signal. Treat as review-first.

This separation is the single most important design choice: it prevents the
false-fix loops that come with binary pass/fail tools.

## Supported formats

| Format | Analyze | Remediate |
|---|---|---|
| DOCX  | yes | yes (subset of criteria) |
| PPTX  | yes | yes (subset of criteria) |
| HTML  | yes | no  |
| PDF   | yes | no  |
| XLSX  | yes | no  |

## What this POC explicitly does not do

- It is not a full WCAG conformance certifier.
- It does not evaluate time-based media (captions, transcripts, audio
  descriptions, sign-language).
- It does not perform full browser/runtime journey simulation.
- It does not make legal, medical, or HR determinations.
- It does not provide authentication, authorization, or rate limiting.
- It does not encrypt or persist documents; the hosting environment's
  logging settings determine retention.

## Operational notes

- **Stateless**: scale horizontally with replicas; no shared session state.
- **Cold start**: ~10–15s on first request after idle (LibreOffice warmup).
  Set `min-replicas=1` if that matters for your evaluation.
- **OCR cost**: deep OCR can add tens of seconds per document. `auto` mode
  runs OCR only when the structural layer flags likely 1.4.5 candidates.

## Cost shape

- The code itself is MIT-licensed and free.
- Runtime cost = container compute + storage + egress + (optional) logs.
  Plan for normal Azure Container Apps pricing in your subscription.

## Suggested evaluation path

1. Deploy per [`../deploy/azure-container-apps.md`](../deploy/azure-container-apps.md).
2. Run the smoke test in [`../deploy/SMOKE_TEST.md`](../deploy/SMOKE_TEST.md).
3. Try a handful of your own non-sensitive documents.
4. Decide whether the structured-finding model is a fit before doing any
   integration work on your side.
