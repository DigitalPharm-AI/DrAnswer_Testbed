param(
  [int]$SystemPort = 0,
  [int]$AgentPort = 0,
  [int]$PhrPort = 0,
  [string]$ChromePath = "C:/Program Files/Google/Chrome/Application/chrome.exe"
)

$ErrorActionPreference = "Stop"
$pathValue = $env:Path
[System.Environment]::SetEnvironmentVariable("PATH", $null, "Process")
[System.Environment]::SetEnvironmentVariable("Path", $pathValue, "Process")

$root = (Resolve-Path ".").Path
$runId = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds().ToString()
$outDir = Join-Path $root "outputs\playwright\real-llm-demo-$runId"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
  $python = "python"
}
$powershellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

if ($SystemPort -eq 0 -or $AgentPort -eq 0 -or $PhrPort -eq 0) {
  $basePort = Get-Random -Minimum 19000 -Maximum 24000
  $SystemPort = $basePort
  $AgentPort = $basePort + 1
  $PhrPort = $basePort + 2
}

$systemUrl = "http://127.0.0.1:$SystemPort"
$agentUrl = "http://127.0.0.1:$AgentPort"
$phrUrl = "http://127.0.0.1:$PhrPort"
$internalToken = "playwright-internal-token"

function DbUrl([string]$name) {
  $path = (Join-Path $outDir $name).Replace("\", "/")
  return "sqlite:///$path"
}

function New-ServiceCommand(
  [string]$EnvFile,
  [string]$DatabaseEnv,
  [string]$DatabaseUrl,
  [string]$Module,
  [int]$Port
) {
  @"
`$env:APP_ENV = 'development'
`$env:DA_DRUG_ENV_FILE = '.env,$EnvFile'
`$env:INTERNAL_API_TOKEN = '$internalToken'
`$env:SYSTEM_BASE_URL = '$systemUrl'
`$env:AGENT_BASE_URL = '$agentUrl'
`$env:PHR_BASE_URL = '$phrUrl'
`$env:AGENT_EMBEDDED_WORKER_ENABLED = 'false'
`$env:$DatabaseEnv = '$DatabaseUrl'
& '$python' -m uvicorn $Module --host 127.0.0.1 --port $Port
"@
}

function New-AgentWorkerCommand() {
  @"
`$env:APP_ENV = 'development'
`$env:DA_DRUG_ENV_FILE = '.env,.env.agent_app'
`$env:INTERNAL_API_TOKEN = '$internalToken'
`$env:SYSTEM_BASE_URL = '$systemUrl'
`$env:AGENT_BASE_URL = '$agentUrl'
`$env:PHR_BASE_URL = '$phrUrl'
`$env:AGENT_DATABASE_URL = '$(DbUrl "agent.db")'
& '$python' -m agent_app.worker_main
"@
}

function Start-DaDrugService(
  [string]$Name,
  [string]$EnvFile,
  [string]$DatabaseEnv,
  [string]$DatabaseUrl,
  [string]$Module,
  [int]$Port
) {
  $stdout = Join-Path $outDir "$Name.stdout.log"
  $stderr = Join-Path $outDir "$Name.stderr.log"
  $command = New-ServiceCommand $EnvFile $DatabaseEnv $DatabaseUrl $Module $Port
  Start-Process `
    -FilePath $powershellExe `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $command) `
    -WorkingDirectory $root `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -WindowStyle Hidden `
    -PassThru
}

function Start-AgentWorker() {
  $stdout = Join-Path $outDir "agent-worker.stdout.log"
  $stderr = Join-Path $outDir "agent-worker.stderr.log"
  $command = New-AgentWorkerCommand
  Start-Process `
    -FilePath $powershellExe `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $command) `
    -WorkingDirectory $root `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -WindowStyle Hidden `
    -PassThru
}

function Wait-Http([string]$Url, [string]$Name) {
  for ($i = 0; $i -lt 90; $i++) {
    try {
      $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 2
      if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
        return
      }
    } catch {
      Start-Sleep -Seconds 1
    }
  }
  throw "$Name did not become ready at $Url"
}

function Stop-PortProcesses([int[]]$Ports) {
  foreach ($port in $Ports) {
    $connections = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
    $processIds = $connections | Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($processId in $processIds) {
      if ($processId -and $processId -ne $PID) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
      }
    }
  }
}

$processes = @()
try {
  $processes += Start-DaDrugService "phr" ".env.phr_app" "PHR_DATABASE_URL" (DbUrl "phr.db") "phr_app.main:app" $PhrPort
  $processes += Start-DaDrugService "agent" ".env.agent_app" "AGENT_DATABASE_URL" (DbUrl "agent.db") "agent_app.main:app" $AgentPort
  $processes += Start-DaDrugService "system" ".env.system_app" "SYSTEM_DATABASE_URL" (DbUrl "system.db") "system_app.main:app" $SystemPort

  Wait-Http "$phrUrl/health" "phr_app"
  Wait-Http "$agentUrl/health" "agent_app"
  Wait-Http "$systemUrl/health" "system_app"
  $processes += Start-AgentWorker

  $env:BASE_URL = $systemUrl
  $env:CHROME_PATH = $ChromePath
  $env:DEMO_RUN_ID = $runId
  $env:INTERNAL_API_TOKEN = $internalToken

  Write-Host "Running real LLM demo video scenario..."
  Write-Host "Output directory: $outDir"
  & node tools/playwright_real_llm_demo_video.js
  if ($LASTEXITCODE -ne 0) {
    throw "Playwright demo failed with exit code $LASTEXITCODE"
  }
} finally {
  foreach ($process in $processes) {
    if ($process -and -not $process.HasExited) {
      Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
  }
  Stop-PortProcesses @($SystemPort, $AgentPort, $PhrPort)
}
