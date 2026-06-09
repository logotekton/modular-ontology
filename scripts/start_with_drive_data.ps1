param(
  [Parameter(Mandatory=$true)]
  [string]$DriveDataDir,
  [int]$Port = 8010
)

$ErrorActionPreference = "Stop"

$env:MODULAR_ONTOLOGY_DATA_DIR = $DriveDataDir
$env:MODULAR_ONTOLOGY_STRUCTURED_DATA_DIR = "1"

python -m uvicorn modular_ontology.app:app --host 0.0.0.0 --port $Port
