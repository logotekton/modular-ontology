param(
  [int]$Port = 8787,
  [string]$Token = "",
  [string]$OllamaUrl = "http://127.0.0.1:11434"
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$DataDir = Join-Path $Root "data"
$StatusFile = Join-Path $DataDir "local_ai_proxy.json"
$ProxyOut = Join-Path $Root "local-ai-proxy.log"
$ProxyErr = Join-Path $Root "local-ai-proxy.err.log"

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

if (-not $Token) {
  $Bytes = New-Object byte[] 32
  $Generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try {
    $Generator.GetBytes($Bytes)
  } finally {
    $Generator.Dispose()
  }
  $Token = [Convert]::ToBase64String($Bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

$Existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
foreach ($Connection in $Existing) {
  Stop-Process -Id $Connection.OwningProcess -Force -ErrorAction SilentlyContinue
}

Remove-Item $ProxyOut, $ProxyErr -Force -ErrorAction SilentlyContinue

$Command = @"
`$env:MODDULAR_GRAPH_LOCAL_AI_TOKEN = '$Token'
`$env:MODDULAR_GRAPH_LOCAL_AI_OLLAMA_URL = '$OllamaUrl'
python -m uvicorn moddular_graph.local_ai_proxy:app --host 127.0.0.1 --port $Port
"@

$ProxyProcess = Start-Process `
  -FilePath powershell.exe `
  -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $Command) `
  -WorkingDirectory $Root `
  -WindowStyle Hidden `
  -RedirectStandardOutput $ProxyOut `
  -RedirectStandardError $ProxyErr `
  -PassThru

$Payload = [ordered]@{
  localUrl = "http://127.0.0.1:$Port"
  ollamaUrl = $OllamaUrl
  token = $Token
  tokenHeader = "X-Modular-AI-Token"
  createdAt = (Get-Date).ToString("o")
  proxyProcessId = $ProxyProcess.Id
}
$Payload | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 $StatusFile

Write-Host "Local AI proxy URL: http://127.0.0.1:$Port"
Write-Host "Proxy PID: $($ProxyProcess.Id)"
Write-Host "Token: $Token"
