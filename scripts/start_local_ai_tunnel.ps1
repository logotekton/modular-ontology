param(
  [int]$Port = 8787
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$ToolsDir = Join-Path $Root "tools"
$Cloudflared = Join-Path $ToolsDir "cloudflared.exe"
$DataDir = Join-Path $Root "data"
$RemoteFile = Join-Path $DataDir "local_ai_remote.json"
$ProxyFile = Join-Path $DataDir "local_ai_proxy.json"
$TunnelOut = Join-Path $Root "local-ai-tunnel.log"
$TunnelErr = Join-Path $Root "local-ai-tunnel.err.log"

New-Item -ItemType Directory -Force -Path $ToolsDir | Out-Null
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

if (-not (Test-Path $Cloudflared)) {
  $DownloadUrl = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
  Invoke-WebRequest -Uri $DownloadUrl -OutFile $Cloudflared
}

if (Test-Path $RemoteFile) {
  try {
    $Previous = Get-Content $RemoteFile -Raw | ConvertFrom-Json
    if ($Previous.tunnelProcessId) {
      Stop-Process -Id ([int]$Previous.tunnelProcessId) -Force -ErrorAction SilentlyContinue
    }
  } catch {
    # Ignore invalid stale status files.
  }
}

Remove-Item $TunnelOut, $TunnelErr -Force -ErrorAction SilentlyContinue

$TunnelProcess = Start-Process `
  -FilePath $Cloudflared `
  -ArgumentList @("tunnel", "--url", "http://127.0.0.1:$Port", "--no-autoupdate") `
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
  throw "Cloudflare tunnel URL was not found. Check $TunnelErr."
}

$TokenPresent = $false
if (Test-Path $ProxyFile) {
  try {
    $ProxyStatus = Get-Content $ProxyFile -Raw | ConvertFrom-Json
    $TokenPresent = [bool]$ProxyStatus.token
  } catch {
    $TokenPresent = $false
  }
}

$RemotePayload = [ordered]@{
  publicBaseUrl = $PublicBaseUrl
  localUrl = "http://127.0.0.1:$Port"
  tokenRequired = $true
  tokenPresent = $TokenPresent
  createdAt = (Get-Date).ToString("o")
  tunnelProcessId = $TunnelProcess.Id
}
$RemotePayload | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 $RemoteFile

Write-Host "Local AI tunnel URL: $PublicBaseUrl"
Write-Host "Tunnel PID: $($TunnelProcess.Id)"
