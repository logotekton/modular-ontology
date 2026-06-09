param(
  [Parameter(Mandatory = $true)]
  [string]$Hostname,

  [string]$TunnelName = "modular-ontology-mcp",
  [int]$Port = 8011,
  [switch]$OverwriteDns
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$ToolsDir = Join-Path $Root "tools"
$Cloudflared = Join-Path $ToolsDir "cloudflared.exe"
$DataDir = Join-Path $Root "data\00_Admin"
$RemoteFile = Join-Path $DataDir "mcp_remote.json"
$TunnelOut = Join-Path $Root "cloudflared-named-mcp.log"
$TunnelErr = Join-Path $Root "cloudflared-named-mcp.err.log"
$McpOut = Join-Path $Root "remote-mcp.log"
$McpErr = Join-Path $Root "remote-mcp.err.log"
$CloudflaredDir = Join-Path $env:USERPROFILE ".cloudflared"
$OriginCert = Join-Path $CloudflaredDir "cert.pem"

New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

if (-not (Test-Path $Cloudflared)) {
  $DownloadUrl = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
  Invoke-WebRequest -Uri $DownloadUrl -OutFile $Cloudflared
}

if (-not (Test-Path $OriginCert)) {
  throw "Cloudflare origin certificate not found. Run first: `"$Cloudflared`" tunnel login"
}

function Stop-ProcessIdIfAlive([object]$ProcessId) {
  if (-not $ProcessId) { return }
  try {
    $Process = Get-Process -Id ([int]$ProcessId) -ErrorAction Stop
    Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
  } catch {
  }
}

$PreviousRemote = $null
if (Test-Path $RemoteFile) {
  try {
    $PreviousRemote = Get-Content $RemoteFile -Raw -Encoding UTF8 | ConvertFrom-Json
    Stop-ProcessIdIfAlive $PreviousRemote.tunnelProcessId
    Stop-ProcessIdIfAlive $PreviousRemote.mcpProcessId
  } catch {
  }
}

$ExistingMcp = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($Connection in $ExistingMcp) {
  Stop-Process -Id $Connection.OwningProcess -Force -ErrorAction SilentlyContinue
}

Remove-Item $TunnelOut, $TunnelErr, $McpOut, $McpErr -Force -ErrorAction SilentlyContinue

$TunnelListRaw = & $Cloudflared tunnel list --output json 2>$null
$TunnelList = @()
if ($TunnelListRaw) {
  $TunnelList = @($TunnelListRaw | ConvertFrom-Json)
}
$Tunnel = $TunnelList | Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1

if (-not $Tunnel) {
  & $Cloudflared tunnel create $TunnelName | Write-Host
  $TunnelListRaw = & $Cloudflared tunnel list --output json
  $TunnelList = @($TunnelListRaw | ConvertFrom-Json)
  $Tunnel = $TunnelList | Where-Object { $_.name -eq $TunnelName } | Select-Object -First 1
}

if (-not $Tunnel) {
  throw "Named tunnel을 만들거나 찾지 못했습니다: $TunnelName"
}

$RouteArgs = @("tunnel", "route", "dns")
if ($OverwriteDns) {
  $RouteArgs += "--overwrite-dns"
}
$RouteArgs += @($TunnelName, $Hostname)
& $Cloudflared @RouteArgs | Write-Host

$PublicBaseUrl = "https://$Hostname"
$PublicMcpUrl = "$PublicBaseUrl/mcp"
$PublicUserUrlTemplate = "$PublicBaseUrl/mcp/{token}"

$RemotePayload = [ordered]@{
  publicUrl = $PublicMcpUrl
  publicBaseUrl = $PublicBaseUrl
  localUrl = "http://127.0.0.1:$Port/mcp"
  publicUserUrlTemplate = $PublicUserUrlTemplate
  localUserUrlTemplate = "http://127.0.0.1:$Port/mcp/{token}"
  host = $Hostname
  tunnelName = $TunnelName
  tunnelId = $Tunnel.id
  tunnelMode = "named"
  createdAt = (Get-Date).ToString("o")
}
$RemotePayload | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $RemoteFile

$env:MODULAR_ONTOLOGY_ROOT = $Root
$env:MODULAR_ONTOLOGY_PUBLIC_MCP_URL = $PublicMcpUrl

$AllowedHosts = "$Hostname,127.0.0.1:*,localhost:*,[::1]:*"
$AllowedOrigins = "https://chatgpt.com,https://chat.openai.com,http://127.0.0.1:*,http://localhost:*"

$McpProcess = Start-Process `
  -FilePath python `
  -ArgumentList @(
    "-m", "modular_ontology.mcp_server",
    "--transport", "streamable-http",
    "--host", "127.0.0.1",
    "--port", "$Port",
    "--path", "/mcp/{mcp_token}",
    "--allowed-host", $AllowedHosts,
    "--allowed-origin", $AllowedOrigins
  ) `
  -WorkingDirectory $Root `
  -WindowStyle Hidden `
  -RedirectStandardOutput $McpOut `
  -RedirectStandardError $McpErr `
  -PassThru

$TunnelProcess = Start-Process `
  -FilePath $Cloudflared `
  -ArgumentList @("tunnel", "--no-autoupdate", "run", "--url", "http://127.0.0.1:$Port", $TunnelName) `
  -WorkingDirectory $Root `
  -WindowStyle Hidden `
  -RedirectStandardOutput $TunnelOut `
  -RedirectStandardError $TunnelErr `
  -PassThru

Start-Sleep -Seconds 3

$RemotePayload["tunnelProcessId"] = $TunnelProcess.Id
$RemotePayload["mcpProcessId"] = $McpProcess.Id
$RemotePayload | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $RemoteFile

Write-Host "MCP fixed public URL: $PublicMcpUrl"
Write-Host "User MCP URL template: $PublicUserUrlTemplate"
Write-Host "Tunnel name: $TunnelName"
Write-Host "Tunnel PID: $($TunnelProcess.Id)"
Write-Host "MCP PID: $($McpProcess.Id)"
