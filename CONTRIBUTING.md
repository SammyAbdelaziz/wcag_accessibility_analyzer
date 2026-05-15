# Contributing

This is a small POC. Contributions are welcome but there is no SLA.

## Ground rules

- Open an issue describing the change before sending a large PR.
- Keep PRs focused; one concern per PR.
- Use a short-lived feature branch and open a PR into `main`; do not push product changes directly to `main`.
- Run the test suite (`pytest`) and ensure it passes before requesting review.
- Do not add customer-specific names, logos, or branding to the codebase.

Release and deployment guidance lives in `deploy/ALM_BASELINE.md`.

## Local setup

```bash
python -m venv .venv
. .venv/Scripts/activate   # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install pytest           # test runner is not pinned in requirements.txt
pytest
```

## Style

- Python: PEP 8, prefer small focused functions, type hints where helpful.
- Tests live under `tests/` and follow the existing naming.
