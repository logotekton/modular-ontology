param(
  [Parameter(Mandatory=$true)]
  [string]$DriveDataDir
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$sourceData = Join-Path $root "data"
$target = $DriveDataDir
$timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"

$paths = @(
  "00_Admin",
  "01_Database",
  "01_Database\snapshots\$timestamp",
  "02_Ontology_Packs",
  "02_Ontology_Packs\incoming",
  "02_Ontology_Packs\indexed",
  "02_Ontology_Packs\archive",
  "03_Projects",
  "03_Projects\Samcheok Building B\source-json",
  "03_Projects\Samcheok Building B\ontology-packs",
  "03_Projects\Samcheok Building B\reports",
  "03_Projects\Yeoju Modular Dormitory\source-json",
  "03_Projects\Yeoju Modular Dormitory\ontology-packs",
  "03_Projects\Yeoju Modular Dormitory\reports",
  "04_MCP",
  "04_MCP\server-config",
  "04_MCP\connection-guides",
  "04_MCP\tool-manifests",
  "05_Backups",
  "05_Backups\daily",
  "05_Backups\manual",
  "99_Archive"
)

foreach ($relative in $paths) {
  New-Item -ItemType Directory -Force -Path (Join-Path $target $relative) | Out-Null
  $readme = Join-Path $target (Join-Path $relative "README.md")
  if (-not (Test-Path $readme)) {
    Set-Content -LiteralPath $readme -Encoding UTF8 -Value "# $relative`n`nModular Ontology managed storage area."
  }
}

$db = Join-Path $sourceData "modular_ontology.sqlite3"
if (Test-Path $db) {
  Copy-Item -LiteralPath $db -Destination (Join-Path $target "01_Database\modular_ontology.sqlite3") -Force
  Copy-Item -LiteralPath $db -Destination (Join-Path $target "01_Database\snapshots\$timestamp\modular_ontology.sqlite3") -Force
}

$users = Join-Path $sourceData "users.json"
if (Test-Path $users) {
  Copy-Item -LiteralPath $users -Destination (Join-Path $target "00_Admin\users.json") -Force
}

$mcp = Join-Path $sourceData "mcp_remote.json"
if (Test-Path $mcp) {
  Copy-Item -LiteralPath $mcp -Destination (Join-Path $target "00_Admin\mcp_remote.json") -Force
}

$sourcePacks = Join-Path $sourceData "packs"
if (Test-Path $sourcePacks) {
  Copy-Item -LiteralPath (Join-Path $sourcePacks "*.zip") -Destination (Join-Path $target "02_Ontology_Packs\indexed") -Force -ErrorAction SilentlyContinue
}

Copy-Item -Path (Join-Path $root "*.zip") -Destination (Join-Path $target "02_Ontology_Packs\indexed") -Force -ErrorAction SilentlyContinue

Copy-Item -LiteralPath (Join-Path $root "docs\google_drive_storage_plan.md") -Destination (Join-Path $target "00_Admin\storage-plan.md") -Force

Write-Host "Drive data directory prepared:"
Write-Host $target
Write-Host ""
Write-Host "Set this before starting the server:"
Write-Host "`$env:MODULAR_ONTOLOGY_DATA_DIR='$target'"
Write-Host "`$env:MODULAR_ONTOLOGY_STRUCTURED_DATA_DIR='1'"
