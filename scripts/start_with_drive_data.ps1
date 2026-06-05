param(
  [Parameter(Mandatory=$true)]
  [string]$DriveDataDir,
  [int]$Port = 8010
)

$ErrorActionPreference = "Stop"

$env:MODDULAR_GRAPH_DATA_DIR = $DriveDataDir
$env:MODDULAR_GRAPH_STRUCTURED_DATA_DIR = "1"

python -m uvicorn moddular_graph.app:app --host 0.0.0.0 --port $Port
