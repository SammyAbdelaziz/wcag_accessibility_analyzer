# Security and Responsible Use

This project is a **proof of concept (POC)**. It is provided as-is under the
[MIT license](LICENSE) with no warranty and no support obligation.

## Before deploying

Treat this repo the way you would treat any unreviewed third-party code:

1. **Read the source.** All analyzer logic is in `wcag/`.
2. **Run your own security review** (SCA scans, image scanning, secret
   scanning) before promoting any build of this image.
3. **Deploy into your own subscription/tenant** following your organization's
   landing-zone, networking, and identity standards.
4. **Do not** expose the HTTP endpoint to the public internet without an
   authentication layer (API Management, App Service Auth, front-end gateway,
   etc.). The provided ingress is anonymous for evaluation only.
5. **Validate data handling**. Uploaded documents are read into memory for
   analysis. They are not persisted by this code, but your hosting
   environment (logs, traces, request bodies) may retain copies depending on
   your configuration.

## What this code does not do

- It does not implement authentication or authorization.
- It does not implement rate limiting.
- It does not encrypt data at rest (it does not persist data at all — see
  [`docs/data-handling.md`](docs/data-handling.md)).
- It does not produce audit logs beyond standard application logs.

## Public endpoint risk

If you deploy this with public ingress and no authentication, **assume
the URL will be discovered**. Treat the endpoint as you would any
unauthenticated public service:

- Upload limit is 20 MB but there is **no rate limit** in this code. A
  bad actor can drive cost (compute, OCR time) and noise (log volume).
- The analyzer parses untrusted documents using third-party libraries
  (lxml, python-docx, python-pptx, openpyxl, pikepdf, Pillow,
  pdf2image, pytesseract, LibreOffice). Pin and patch them.
- Image scanning, SCA, and runtime monitoring are **your responsibility**
  in your subscription, not provided by this repo.

For anything beyond a short evaluation window, put the service behind an
authentication layer (APIM, App Gateway with WAF, App Service Auth, an
internal load balancer in a VNet, etc.) — see
[`docs/private-networking.md`](docs/private-networking.md).

## Turning it off

The POC is intended for time-boxed use. See
[`docs/turning-it-off.md`](docs/turning-it-off.md) for the four levels of
"off" (disable ingress, scale to zero, delete the app, delete the resource
group) and the recommendation to re-platform through your standard
engineering channels if the capability is wanted long-term.

## Reporting issues

For bugs, open a GitHub issue. For anything security-sensitive, open a
private security advisory on the repo instead of a public issue.
