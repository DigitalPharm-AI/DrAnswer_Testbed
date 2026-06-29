param(
  [int]$SystemPort = 8100,
  [int]$AgentPort = 8101,
  [int]$PhrPort = 8102,
  [string]$ChromePath = "C:/Program Files/Google/Chrome/Application/chrome.exe",
  [switch]$SkipAdherenceMatrix,
  [switch]$SkipCurrentScenarioAdvanced,
  [switch]$SkipAsyncAgentFlow,
  [switch]$SkipComplexScenarios,
  [switch]$SkipHardMissedDoseSuite,
  [switch]$SkipPersonaPolicyLab,
  [switch]$SkipBugHunt,
  [switch]$SkipWeeklyMatrix,
  [switch]$SkipMcpToolContract,
  [switch]$SkipAgentAsyncLogContract,
  [switch]$RunPersonaChallengeLab,
  [switch]$RunP1ProductionScenario,
  [switch]$RunRealisticStressScenario
)

$ErrorActionPreference = "Stop"
$pathValue = $env:Path
[System.Environment]::SetEnvironmentVariable("PATH", $null, "Process")
[System.Environment]::SetEnvironmentVariable("Path", $pathValue, "Process")

$root = (Resolve-Path ".").Path
$runId = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds().ToString()
$outDir = Join-Path $root "outputs\playwright\isolated-$runId"
$tempDir = Join-Path $outDir "db"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
New-Item -ItemType Directory -Force -Path $tempDir | Out-Null

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
  $python = "python"
}
$powershellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"

$systemUrl = "http://127.0.0.1:$SystemPort"
$agentUrl = "http://127.0.0.1:$AgentPort"
$phrUrl = "http://127.0.0.1:$PhrPort"
$internalToken = "playwright-internal-token"

function DbUrl([string]$name) {
  $path = (Join-Path $tempDir $name).Replace("\", "/")
  return "sqlite:///$path"
}

$systemDbUrl = DbUrl "system.db"
$agentDbUrl = DbUrl "agent.db"
$phrDbUrl = DbUrl "phr.db"

function New-ServiceCommand(
  [string]$ServiceName,
  [string]$EnvFile,
  [string]$DatabaseEnv,
  [string]$DatabaseUrl,
  [string]$Module,
  [int]$Port
) {
  @"
`$env:APP_ENV = 'development'
`$env:DA_DRUG_SERVICE = '$ServiceName'
`$env:DA_DRUG_ENV_FILE = '.env,$EnvFile'
`$env:INTERNAL_API_TOKEN = '$internalToken'
`$env:SYSTEM_BASE_URL = '$systemUrl'
`$env:AGENT_BASE_URL = '$agentUrl'
`$env:PHR_BASE_URL = '$phrUrl'
`$env:LLM_PROVIDER = 'rule_based'
`$env:LLM_MODEL_TIER = 'fast'
`$env:AGENT_EMBEDDED_WORKER_ENABLED = 'false'
`$env:$DatabaseEnv = '$DatabaseUrl'
& '$python' -m uvicorn $Module --host 127.0.0.1 --port $Port
"@
}

function New-AgentWorkerCommand() {
  @"
`$env:APP_ENV = 'development'
`$env:DA_DRUG_SERVICE = 'agent_app'
`$env:DA_DRUG_ENV_FILE = '.env,.env.agent_app'
`$env:INTERNAL_API_TOKEN = '$internalToken'
`$env:SYSTEM_BASE_URL = '$systemUrl'
`$env:AGENT_BASE_URL = '$agentUrl'
`$env:PHR_BASE_URL = '$phrUrl'
`$env:LLM_PROVIDER = 'rule_based'
`$env:LLM_MODEL_TIER = 'fast'
`$env:AGENT_DATABASE_URL = '$agentDbUrl'
& '$python' -m agent_app.worker_main
"@
}

function Start-DaDrugService(
  [string]$Name,
  [string]$ServiceName,
  [string]$EnvFile,
  [string]$DatabaseEnv,
  [string]$DatabaseUrl,
  [string]$Module,
  [int]$Port
) {
  $stdout = Join-Path $outDir "$Name.stdout.log"
  $stderr = Join-Path $outDir "$Name.stderr.log"
  $command = New-ServiceCommand $ServiceName $EnvFile $DatabaseEnv $DatabaseUrl $Module $Port
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
    try {
      $connections = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue
      $processIds = $connections | Select-Object -ExpandProperty OwningProcess -Unique
      foreach ($processId in $processIds) {
        if ($processId -and $processId -ne $PID) {
          Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }
      }
    } catch {
      Write-Host "Could not inspect or stop process on port ${port}: $($_.Exception.Message)"
    }
  }
}

function Invoke-NodeScenario([string]$Name, [string]$ScriptPath) {
  Write-Host "Running $Name..."
  $logName = ($Name -replace "[^A-Za-z0-9_-]+", "-").Trim("-")
  $nodeOut = Join-Path $outDir "$logName.node.stdout.log"
  $nodeErr = Join-Path $outDir "$logName.node.stderr.log"
  $nodeProcess = Start-Process `
    -FilePath "node" `
    -ArgumentList @($ScriptPath) `
    -WorkingDirectory $root `
    -RedirectStandardOutput $nodeOut `
    -RedirectStandardError $nodeErr `
    -WindowStyle Hidden `
    -Wait `
    -PassThru
  $exitCode = $nodeProcess.ExitCode
  if (Test-Path $nodeOut) {
    Get-Content -Encoding utf8 $nodeOut | ForEach-Object { Write-Host $_ }
  }
  if (Test-Path $nodeErr) {
    Get-Content -Encoding utf8 $nodeErr | ForEach-Object { Write-Host $_ }
  }
  if ($exitCode -ne 0) {
    Write-Host "$Name failed with exit code $exitCode"
  }
  return $exitCode
}

function Assert-AgentLogPattern([string]$LogText, [string]$Pattern, [string]$Description) {
  if ($LogText -notmatch $Pattern) {
    throw "Agent log contract failed. Missing ${Description}. Pattern: $Pattern"
  }
}

function Assert-AgentLogMissing([string]$LogText, [string]$Pattern, [string]$Description) {
  if ($LogText -match $Pattern) {
    throw "Agent log contract failed. Forbidden ${Description}. Pattern: $Pattern"
  }
}

function Test-AgentAsyncLogContract([string]$OutDir) {
  $agentStdout = Join-Path $OutDir "agent.stdout.log"
  $agentStderr = Join-Path $OutDir "agent.stderr.log"
  $logText = ""
  if (Test-Path $agentStdout) {
    $logText += [Environment]::NewLine + (Get-Content -Raw -Encoding utf8 $agentStdout)
  }
  if (Test-Path $agentStderr) {
    $logText += [Environment]::NewLine + (Get-Content -Raw -Encoding utf8 $agentStderr)
  }
  Assert-AgentLogMissing $logText 'agent_api_call .*"path":\s*"/agent/daily-patterns".*"mode":\s*"legacy_sync"' "legacy sync /agent/daily-patterns call"
  Assert-AgentLogMissing $logText 'agent_api_call .*"path":\s*"/agent/missed-dose-events".*"mode":\s*"legacy_sync"' "legacy sync /agent/missed-dose-events call"
  Assert-AgentLogPattern $logText 'agent_api_call .*"path":\s*"/agent/async/daily-patterns".*"mode":\s*"async_submit"' "async submit /agent/async/daily-patterns call"
  Assert-AgentLogPattern $logText 'agent_api_call .*"path":\s*"/agent/async/missed-dose-events".*"mode":\s*"async_submit"' "async submit /agent/async/missed-dose-events call"
  Assert-AgentLogPattern $logText 'agent_api_call .*"path":\s*"/agent/async/chat-continuations".*"mode":\s*"async_submit"' "async submit /agent/async/chat-continuations call"
  Write-Host "PASS agent async log contract: async daily/missed/chat-continuation used, legacy sync daily/missed unused"
}

$processes = @()
$exitCodes = @()
try {
  Stop-PortProcesses @($SystemPort, $AgentPort, $PhrPort)
  $processes += Start-DaDrugService "phr" "phr_app" ".env.phr_app" "PHR_DATABASE_URL" $phrDbUrl "phr_app.main:app" $PhrPort
  $processes += Start-DaDrugService "agent" "agent_app" ".env.agent_app" "AGENT_DATABASE_URL" $agentDbUrl "agent_app.main:app" $AgentPort
  $processes += Start-DaDrugService "system" "system_app" ".env.system_app" "SYSTEM_DATABASE_URL" $systemDbUrl "system_app.main:app" $SystemPort

  Wait-Http "$phrUrl/health" "phr_app"
  Wait-Http "$agentUrl/health" "agent_app"
  Wait-Http "$systemUrl/health" "system_app"
  $processes += Start-AgentWorker

  $env:BASE_URL = $systemUrl
  $env:CHROME_PATH = $ChromePath
  $env:PYTHON_BIN = $python
  $env:INTERNAL_API_TOKEN = $internalToken
  $env:SYSTEM_BASE_URL = $systemUrl
  $env:AGENT_BASE_URL = $agentUrl
  $env:PHR_BASE_URL = $phrUrl
  $env:LLM_PROVIDER = "rule_based"
  $env:LLM_MODEL_TIER = "fast"
  $env:SYSTEM_DATABASE_URL = $systemDbUrl
  $env:AGENT_DATABASE_URL = $agentDbUrl
  $env:PHR_DATABASE_URL = $phrDbUrl

  Write-Host "Isolated Playwright run"
  Write-Host "System: $systemUrl"
  Write-Host "Agent:  $agentUrl"
  Write-Host "PHR:    $phrUrl"
  Write-Host "DB dir: $tempDir"
  Write-Host "Logs:   $outDir"

  if (-not $SkipAdherenceMatrix) {
    $exitCodes += Invoke-NodeScenario "adherence pattern matrix" "tools/playwright_adherence_pattern_matrix.js"
  }
  if (-not $SkipCurrentScenarioAdvanced) {
    $exitCodes += Invoke-NodeScenario "current scenario advanced" "tools/playwright_current_scenario_advanced.js"
  }
  if (-not $SkipAsyncAgentFlow) {
    $exitCodes += Invoke-NodeScenario "async agent flow" "tools/playwright_async_agent_flow.js"
  }
  if (-not $SkipComplexScenarios) {
    $exitCodes += Invoke-NodeScenario "complex adherence scenarios" "tools/playwright_complex_adherence_scenarios.js"
  }
  if (-not $SkipHardMissedDoseSuite) {
    $exitCodes += Invoke-NodeScenario "hard missed dose suite" "tools/playwright_hard_missed_dose_suite.js"
  }
  if (-not $SkipPersonaPolicyLab) {
    $exitCodes += Invoke-NodeScenario "persona policy lab" "tools/playwright_persona_policy_lab.js"
  }
  if (-not $SkipBugHunt) {
    $exitCodes += Invoke-NodeScenario "bug hunt" "tools/playwright_bug_hunt.js"
  }
  if (-not $SkipWeeklyMatrix) {
    $exitCodes += Invoke-NodeScenario "weekly pattern matrix" "tools/playwright_weekly_pattern_matrix.js"
  }
  if ($RunPersonaChallengeLab) {
    $exitCodes += Invoke-NodeScenario "persona policy challenge lab" "tools/playwright_persona_policy_challenge_lab.js"
  }
  if ($RunP1ProductionScenario) {
    $exitCodes += Invoke-NodeScenario "p1 production readiness scenario" "tools/playwright_p1_production_readiness_scenario.js"
  }
  if ($RunRealisticStressScenario) {
    $exitCodes += Invoke-NodeScenario "realistic stress scenario" "tools/playwright_realistic_stress_scenario.js"
  }
  if (-not $SkipMcpToolContract) {
    $exitCodes += Invoke-NodeScenario "mcp tool contract" "tools/playwright_mcp_tool_contract.js"
  }

  if (-not $SkipAgentAsyncLogContract -and -not $SkipAsyncAgentFlow) {
    Test-AgentAsyncLogContract $outDir
  }

  $failed = @($exitCodes | Where-Object { $_ -ne 0 })
  if ($failed.Count -gt 0) {
    throw "One or more Playwright scenarios failed: $($exitCodes -join ', ')"
  }
} finally {
  foreach ($process in $processes) {
    if ($process -and -not $process.HasExited) {
      Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
  }
  Stop-PortProcesses @($SystemPort, $AgentPort, $PhrPort)
}
