$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not $env:MODULAR_ONTOLOGY_ROOT) {
  $env:MODULAR_ONTOLOGY_ROOT = $Root
}

$HostName = if ($env:MODULAR_ONTOLOGY_MCP_HOST) { $env:MODULAR_ONTOLOGY_MCP_HOST } else { "127.0.0.1" }
$Port = if ($env:MODULAR_ONTOLOGY_MCP_PORT) { $env:MODULAR_ONTOLOGY_MCP_PORT } else { "8011" }
$Path = if ($env:MODULAR_ONTOLOGY_MCP_PATH) { $env:MODULAR_ONTOLOGY_MCP_PATH } else { "/mcp/{mcp_token}" }
$AllowedHosts = if ($env:MODULAR_ONTOLOGY_MCP_ALLOWED_HOSTS) { $env:MODULAR_ONTOLOGY_MCP_ALLOWED_HOSTS } else { "127.0.0.1:*,localhost:*,[::1]:*" }
$AllowedOrigins = if ($env:MODULAR_ONTOLOGY_MCP_ALLOWED_ORIGINS) { $env:MODULAR_ONTOLOGY_MCP_ALLOWED_ORIGINS } else { "http://127.0.0.1:*,http://localhost:*,http://[::1]:*" }

python -m pip install -e .
python -m modular_ontology.mcp_server `
  --transport streamable-http `
  --host $HostName `
  --port $Port `
  --path $Path `
  --allowed-host $AllowedHosts `
  --allowed-origin $AllowedOrigins
