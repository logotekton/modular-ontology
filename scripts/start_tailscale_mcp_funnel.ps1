param(
  [int]$Port = 8011,
  [ValidateSet(443, 8443, 10000)]
  [int]$HttpsPort = 443,
  [switch]$ResetFunnel
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$DataDir = Join-Path $Root "data\00_Admin"
$RemoteFile = Join-Path $DataDir "mcp_remote.json"
$McpOut = Join-Path $Root "remote-mcp.log"
$McpErr = Join-Path $Root "remote-mcp.err.log"
$FunnelOut = Join-Path $Root "tailscale-funnel.log"
$FunnelErr = Join-Path $Root "tailscale-funnel.err.log"

function Resolve-Tailscale {
  $Command = Get-Command tailscale -ErrorAction SilentlyContinue
  if ($Command) { return $Command.Source }

  $Candidates = @(
    "C:\Program Files\Tailscale\tailscale.exe",
    "C:\Program Files (x86)\Tailscale\tailscale.exe"
  )
  foreach ($Candidate in $Candidates) {
    if (Test-Path $Candidate) { return $Candidate }
  }

  return $null
}

function Stop-ProcessIdIfAlive([object]$ProcessId) {
  if (-not $ProcessId) { return }
  try {
    $Process = Get-Process -Id ([int]$ProcessId) -ErrorAction Stop
    Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
  } catch {
  }
}

$Tailscale = Resolve-Tailscale
if (-not $Tailscale) {
  throw "Tailscale is not installed. Run scripts\login_tailscale.ps1 -Install first."
}

$Service = Get-Service -Name Tailscale -ErrorAction SilentlyContinue
if ($Service -and $Service.Status -ne "Running") {
  Start-Service -Name Tailscale
}

$StatusJson = & $Tailscale status --json
$Status = $StatusJson | ConvertFrom-Json
if (-not $Status.Self -or -not $Status.Self.Online) {
  throw "Tailscale is not logged in or this device is offline. Run scripts\login_tailscale.ps1 first."
}

$PublicHost = ([string]$Status.Self.DNSName).TrimEnd(".")
if (-not $PublicHost) {
  throw "Tailscale DNS name was not found. Enable MagicDNS and HTTPS certificates in the Tailscale admin console, then try again."
}

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

$PreviousRemote = $null
if (Test-Path $RemoteFile) {
  try {
    $PreviousRemote = Get-Content $RemoteFile -Raw -Encoding UTF8 | ConvertFrom-Json
    Stop-ProcessIdIfAlive $PreviousRemote.mcpProcessId
  } catch {
  }
}

$ExistingMcp = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($Connection in $ExistingMcp) {
  Stop-Process -Id $Connection.OwningProcess -Force -ErrorAction SilentlyContinue
}

Remove-Item $McpOut, $McpErr, $FunnelOut, $FunnelErr -Force -ErrorAction SilentlyContinue

if ($ResetFunnel) {
  & $Tailscale funnel reset | Tee-Object -FilePath $FunnelOut
}

$PublicBaseUrl = "https://$PublicHost"
if ($HttpsPort -ne 443) {
  $PublicBaseUrl = "${PublicBaseUrl}:$HttpsPort"
}
$PublicMcpUrl = "$PublicBaseUrl/mcp"
$PublicUserUrlTemplate = "$PublicBaseUrl/mcp/{token}"

$RemotePayload = [ordered]@{
  publicUrl = $PublicMcpUrl
  publicBaseUrl = $PublicBaseUrl
  localUrl = "http://127.0.0.1:$Port/mcp"
  publicUserUrlTemplate = $PublicUserUrlTemplate
  localUserUrlTemplate = "http://127.0.0.1:$Port/mcp/{token}"
  host = $PublicHost
  tunnelMode = "tailscale-funnel"
  httpsPort = $HttpsPort
  createdAt = (Get-Date).ToString("o")
}
$RemotePayload | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $RemoteFile

$env:MODULAR_ONTOLOGY_ROOT = $Root
$env:MODULAR_ONTOLOGY_PUBLIC_MCP_URL = $PublicMcpUrl

$AllowedHosts = "$PublicHost,$PublicHost`:$HttpsPort,127.0.0.1:*,localhost:*,[::1]:*"
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

Start-Sleep -Seconds 2
if ($McpProcess.HasExited) {
  throw "Remote MCP server exited early. Check $McpErr."
}

$FunnelArgs = @("funnel", "--bg", "--https=$HttpsPort", "--yes", "$Port")
$FunnelOutput = & $Tailscale @FunnelArgs 2>&1
$FunnelOutput | Set-Content -Encoding UTF8 $FunnelOut

if ($LASTEXITCODE -ne 0) {
  Stop-Process -Id $McpProcess.Id -Force -ErrorAction SilentlyContinue
  $FunnelOutput | Set-Content -Encoding UTF8 $FunnelErr
  throw "Tailscale Funnel failed. Check $FunnelErr. You may need to approve Funnel in the Tailscale admin browser prompt."
}

$StatusOutput = & $Tailscale funnel status 2>&1
$StatusOutput | Add-Content -Encoding UTF8 $FunnelOut

$RemotePayload["mcpProcessId"] = $McpProcess.Id
$RemotePayload | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $RemoteFile

Write-Host "MCP fixed public URL: $PublicMcpUrl"
Write-Host "User MCP URL template: $PublicUserUrlTemplate"
Write-Host "Tailscale host: $PublicHost"
Write-Host "MCP PID: $($McpProcess.Id)"
