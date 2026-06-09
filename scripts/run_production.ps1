$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

python -m pip install -e .
npm install
npm run build
python -m uvicorn modular_ontology.app:app --host 127.0.0.1 --port 8010
