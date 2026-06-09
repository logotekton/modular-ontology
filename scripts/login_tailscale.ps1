param(
  [switch]$Install
)

$ErrorActionPreference = "Stop"

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

if ($Install) {
  winget install --id Tailscale.Tailscale --source winget --silent --accept-package-agreements --accept-source-agreements
}

$Tailscale = Resolve-Tailscale
if (-not $Tailscale) {
  throw "Tailscale is not installed. Run this script with -Install, or install Tailscale manually from https://tailscale.com/download/windows. If winget reports error 1603 with RebootPending, reboot Windows and run again."
}

$Service = Get-Service -Name Tailscale -ErrorAction SilentlyContinue
if ($Service -and $Service.Status -ne "Running") {
  Start-Service -Name Tailscale
}

Write-Host "Tailscale CLI: $Tailscale"
& $Tailscale version

try {
  $StatusJson = & $Tailscale status --json 2>$null
  $Status = $StatusJson | ConvertFrom-Json
  if ($Status.Self -and $Status.Self.Online) {
    $DnsName = [string]$Status.Self.DNSName
    Write-Host "Tailscale is logged in."
    if ($DnsName) {
      Write-Host "Device DNS name: $($DnsName.TrimEnd('.'))"
    }
    exit 0
  }
} catch {
}

Write-Host "Opening Tailscale login. Complete the browser login, then run scripts\start_tailscale_mcp_funnel.ps1."
& $Tailscale up
