# Copilot instructions

## Project structure

- The repository root contains the Python automation app. Main runtime code is under `module/`; campaign data and maps are under `campaign/`; GUI entry points include `alas.py` and `gui.py`.
- `webapp/` is a separate Electron/Vue application with its own dependencies and scripts. Follow `webapp/contributing.md` for its setup and contribution workflow.
- `requirements.txt` is generated from `requirements-in.txt`; keep dependency changes in the source input and regenerate the lock file rather than editing pins by hand.

## Change guidance

- Keep changes scoped to the requested behavior and follow nearby implementation and test patterns.
- Preserve existing server-specific behavior and translations (CN, EN, JP, TW) when touching shared features.
- Avoid unrelated changes to campaign data, assets, generated files, or dependency pins.

## Validation

- For Python changes, run the narrow relevant tests with `python -m pytest tests/<area>/<test_file>.py`. Pytest is not declared in the root requirements, so use an environment where it is installed.
- For `webapp/` changes, run commands from that directory: `npm test` (which builds first), `npm run lint`, and `npm run typecheck` as relevant.
- Do not claim unrun checks passed; report environment limitations when they prevent validation.