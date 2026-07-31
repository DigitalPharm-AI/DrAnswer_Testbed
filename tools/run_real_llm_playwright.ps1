param(
  [Parameter(Mandatory = $true)]
  [string]$Script,
  [int]$SystemPort = 9000,
  [int]$AgentPort = 9001,
  [string]$ChromePath = "C:/Program Files/Google/Chrome/Application/chrome.exe",
  [string]$OutputPrefix = "real-llm-run",
  [string]$EnvFile = ".env.9000",
  [string]$AgentEnvFile = ".env.agent_app.secret"
)

$ErrorActionPreference = "Stop"
$pathValue = $env:Path
[System.Environment]::SetEnvironmentVariable("PATH", $null, "Process")
[System.Environment]::SetEnvironmentVariable("Path", $pathValue, "Process")

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$scriptPath = Join-Path $root $Script
if (-not (Test-Path -LiteralPath $scriptPath)) {
  throw "Playwright script not found: $Script"
}

$runId = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds().ToString()
$outDir = Join-Path $root "output\playwright\$OutputPrefix-$runId"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

$stackScript = Join-Path $root "tools\da_drug_9000_stack.ps1"
$powershellExe = Join-Path `
  $env:SystemRoot `
  "System32\WindowsPowerShell\v1.0\powershell.exe"
$stackStarted = $false
$previousBedrockBearer = $env:AWS_BEARER_TOKEN_BEDROCK

try {
  $env:AWS_BEARER_TOKEN_BEDROCK = $null
  & $powershellExe `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File $stackScript `
    -Action start `
    -EnvFile $EnvFile `
    -AgentEnvFile $AgentEnvFile `
    -SystemPort $SystemPort `
    -AgentPort $AgentPort
  if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL-backed 9000 stack failed to start."
  }
  $stackStarted = $true

  $env:BASE_URL = "http://127.0.0.1:$SystemPort"
  $env:CHROME_PATH = $ChromePath
  $env:DEMO_RUN_ID = $runId
  $env:REAL_LLM_SERVICE_OUTPUT_DIR = $outDir

  Write-Host "Running real LLM Playwright script: $Script"
  Write-Host "Service output directory: $outDir"
  $nodeStdout = Join-Path $outDir "playwright.stdout.log"
  $nodeStderr = Join-Path $outDir "playwright.stderr.log"
  $nodeProcess = Start-Process `
    -FilePath "node" `
    -ArgumentList @($scriptPath) `
    -WorkingDirectory $root `
    -RedirectStandardOutput $nodeStdout `
    -RedirectStandardError $nodeStderr `
    -WindowStyle Hidden `
    -Wait `
    -PassThru
  if ($nodeProcess.ExitCode -ne 0) {
    throw (
      "Playwright script failed with exit code " +
      "$($nodeProcess.ExitCode). See $nodeStderr"
    )
  }
}
finally {
  try {
    if ($stackStarted) {
      & $powershellExe `
        -NoProfile `
        -ExecutionPolicy Bypass `
        -File $stackScript `
        -Action stop `
        -EnvFile $EnvFile `
        -AgentEnvFile $AgentEnvFile `
        -SystemPort $SystemPort `
        -AgentPort $AgentPort
    }
  }
  finally {
    $env:AWS_BEARER_TOKEN_BEDROCK = $previousBedrockBearer
  }
}
