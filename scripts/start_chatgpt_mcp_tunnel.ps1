$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$ToolsDir = Join-Path $Root "tools"
$Cloudflared = Join-Path $ToolsDir "cloudflared.exe"
$DataDir = Join-Path $Root "data"
$RemoteFile = Join-Path $DataDir "mcp_remote.json"
$TunnelOut = Join-Path $Root "cloudflared-tunnel.log"
$TunnelErr = Join-Path $Root "cloudflared-tunnel.err.log"
$McpOut = Join-Path $Root "remote-mcp.log"
$McpErr = Join-Path $Root "remote-mcp.err.log"

New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

if (-not (Test-Path $Cloudflared)) {
  $DownloadUrl = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
  Invoke-WebRequest -Uri $DownloadUrl -OutFile $Cloudflared
}

Get-Process cloudflared -ErrorAction SilentlyContinue | Stop-Process -Force

$ExistingMcp = Get-NetTCPConnection -LocalPort 8011 -State Listen -ErrorAction SilentlyContinue
foreach ($Connection in $ExistingMcp) {
  Stop-Process -Id $Connection.OwningProcess -Force -ErrorAction SilentlyContinue
}

Remove-Item $TunnelOut, $TunnelErr, $McpOut, $McpErr -Force -ErrorAction SilentlyContinue

$TunnelProcess = Start-Process `
  -FilePath $Cloudflared `
  -ArgumentList @("tunnel", "--url", "http://127.0.0.1:8011", "--no-autoupdate") `
  -WorkingDirectory $Root `
  -WindowStyle Hidden `
  -RedirectStandardOutput $TunnelOut `
  -RedirectStandardError $TunnelErr `
  -PassThru

$PublicBaseUrl = $null
for ($Attempt = 0; $Attempt -lt 80; $Attempt++) {
  Start-Sleep -Milliseconds 500
  $LogText = ""
  if (Test-Path $TunnelOut) { $LogText += Get-Content $TunnelOut -Raw -ErrorAction SilentlyContinue }
  if (Test-Path $TunnelErr) { $LogText += Get-Content $TunnelErr -Raw -ErrorAction SilentlyContinue }
  $Match = [regex]::Match($LogText, "https://[a-zA-Z0-9-]+\.trycloudflare\.com")
  if ($Match.Success) {
    $PublicBaseUrl = $Match.Value
    break
  }
}

if (-not $PublicBaseUrl) {
  Stop-Process -Id $TunnelProcess.Id -Force -ErrorAction SilentlyContinue
  throw "Cloudflare tunnel URL을 찾지 못했습니다. $TunnelErr 로그를 확인하세요."
}

$PublicMcpUrl = "$PublicBaseUrl/mcp"
$PublicUserUrlTemplate = "$PublicBaseUrl/mcp/{token}"
$PublicHost = ([Uri]$PublicBaseUrl).Host

$RemotePayload = [ordered]@{
  publicUrl = $PublicMcpUrl
  publicBaseUrl = $PublicBaseUrl
  localUrl = "http://127.0.0.1:8011/mcp"
  publicUserUrlTemplate = $PublicUserUrlTemplate
  localUserUrlTemplate = "http://127.0.0.1:8011/mcp/{token}"
  host = $PublicHost
  createdAt = (Get-Date).ToString("o")
  tunnelProcessId = $TunnelProcess.Id
}
$RemotePayload | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 $RemoteFile

$env:MODDULAR_GRAPH_ROOT = $Root
$env:MODULAR_GRAPH_PUBLIC_MCP_URL = $PublicMcpUrl

$AllowedHosts = "$PublicHost,127.0.0.1:*,localhost:*,[::1]:*"
$AllowedOrigins = "https://chatgpt.com,https://chat.openai.com,http://127.0.0.1:*,http://localhost:*"

$McpProcess = Start-Process `
  -FilePath python `
  -ArgumentList @(
    "-m", "moddular_graph.mcp_server",
    "--transport", "streamable-http",
    "--host", "127.0.0.1",
    "--port", "8011",
    "--path", "/mcp/{mcp_token}",
    "--allowed-host", $AllowedHosts,
    "--allowed-origin", $AllowedOrigins
  ) `
  -WorkingDirectory $Root `
  -WindowStyle Hidden `
  -RedirectStandardOutput $McpOut `
  -RedirectStandardError $McpErr `
  -PassThru

$RemotePayload["mcpProcessId"] = $McpProcess.Id
$RemotePayload | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 $RemoteFile

Write-Host "MCP public base URL: $PublicMcpUrl"
Write-Host "User MCP URL template: $PublicUserUrlTemplate"
Write-Host "Tunnel PID: $($TunnelProcess.Id)"
Write-Host "MCP PID: $($McpProcess.Id)"
