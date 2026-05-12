# Capabilities

What you actually get when you deploy this POC. No marketing — just the
analyzers, what each one inspects, and what each one does **not** cover.

## Supported formats and analyzers

| Format | Module | Analyze | Remediate |
|---|---|---|---|
| DOCX  | `wcag/analyzers/docx_analyzer.py` | Yes | Yes (subset) |
| PPTX  | `wcag/analyzers/pptx_analyzer.py` | Yes | Yes (subset) |
| HTML  | `wcag/analyzers/html_analyzer.py` | Yes | No |
| PDF   | `wcag/analyzers/pdf_analyzer.py`  | Yes | No |
| XLSX  | `wcag/analyzers/xlsx_analyzer.py` | Yes | No |
| OCR   | `wcag/analyzers/ocr_analyzer.py`  | Optional layer for DOCX/PPTX | No |

Remediators live under `wcag/remediators/` and are intentionally limited
to deterministic fixes only (`docx_remediator.py`, `pptx_remediator.py`).

## What each analyzer looks at

### DOCX (`docx_analyzer.py`)
- Document language and metadata.
- Heading structure and reading order.
- Image presence and alternative text (presence + obvious quality boundaries).
- Table structure (header rows, merged cells, layout vs data tables).
- Hyperlink text quality (generic "click here", URL-as-text).
- Color contrast on directly styled runs (via theme resolver).
- Lists vs visually-styled pseudo-lists.

### PPTX (`pptx_analyzer.py`)
- Slide titles and reading order.
- Alt text on pictures, charts, grouped shapes.
- Color contrast against slide background and theme.
- Table structure inside slides.
- Decorative vs informative image classification (best-effort).

### HTML (`html_analyzer.py`)
- Landmark and heading structure.
- `alt` attributes on `img`.
- Label/`for`/`aria-label` association on form controls.
- Link text quality.
- Tabular structure (`th`, `scope`, `caption`).
- Contrast on inline-styled and stylesheet-derived colors (best-effort).
- Language attribute on the root element.

### PDF (`pdf_analyzer.py`)
- Tagged-PDF presence and structure tree.
- Document language metadata.
- Alt text in marked content.
- Title metadata.
- (Heuristic) heading order from the structure tree.

### XLSX (`xlsx_analyzer.py`)
- Sheet titles (default `Sheet1` flagging).
- Merged cells in data ranges.
- Table objects vs free ranges.
- Alt text on embedded images.
- Hyperlink text quality.
- (Heuristic) header-row detection.

### OCR layer (`ocr_analyzer.py`) — DOCX / PPTX only
Runs LibreOffice headless to render the document, then Tesseract on the
rendered pages. Used for:
- Detecting **images of text** (WCAG 1.4.5 confirmation).
- Detecting **visual tables** that aren't real tables (1.3.1 signal).
- Sampling **rendered low contrast** that the structural layer can't see.

Modes (`includeOCR` query parameter on `/api/analyze`):
- `auto` (default) — run only when the structural layer suggests OCR is
  worthwhile. Caps at 3 pages.
- `true` — always run, up to 20 pages.
- `false` — never run.

## Output model

Every analyzer returns a `FactSheet` JSON containing:

- `filename`, `file_type`.
- `summary` with confirmed/possible counts.
- `findings[]` — each with:
  - `criterion` (e.g., `1.3.1`).
  - `confidence` — `confirmed` or `possible`.
  - `title`, `evidence`, `remediation_ref`.

The confidence split is the single most important contract:

- **Confirmed** = direct evidence (XML/DOM/object model). Fix immediately.
- **Possible** = heuristic or OCR-derived. Review first.

## WCAG 2.2 coverage at a glance

This is a **subset**, not full coverage:

- ~45 criteria implemented across DOCX/PPTX/HTML/PDF/XLSX (mix of A/AA/AAA).
- Strongest where checks are structural and machine-verifiable
  (headings, alt presence, table structure, language, contrast on styled
  runs).
- Weakest where intent matters (semantic alt quality, complex visual
  layouts, time-based media).

See [`architecture.md`](architecture.md) for the layered model that produces
these findings.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/analyze`   | Analyze one uploaded file. Returns FactSheet JSON. |
| POST | `/api/remediate` | Apply a list of remediations to a DOCX or PPTX. Returns the patched file (base64) plus an applied/skipped/errors report. |

Limits:

- Max upload size: **20 MB** (`WCAG_MAX_FILE_SIZE_MB` env var).
- Auth: **none** — see [`../SECURITY.md`](../SECURITY.md). Put your own
  authentication layer in front before anything beyond local evaluation.
