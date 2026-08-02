param(
    [ValidateSet("start", "stop", "status", "verify", "restart")]
    [string]$Action = "start",
    [string]$EnvFile = ".env.9000",
    [string]$AgentEnvFile = ".env.agent_app.secret",
    [int]$SystemPort = 9000,
    [int]$AgentPort = 9001,
    [int]$TimeoutSeconds = 45,
    [switch]$AllowStopUnknown
)

$ErrorActionPreference = "Stop"

function Normalize-Process-PathEnvironment {
    $pathKeys = @(
        [System.Environment]::GetEnvironmentVariables().Keys |
            Where-Object { "$_" -ieq "PATH" }
    )
    if ($pathKeys.Count -le 1) {
        return
    }
    $pathValue = [System.Environment]::GetEnvironmentVariable(
        "Path",
        [System.EnvironmentVariableTarget]::Process
    )
    [System.Environment]::SetEnvironmentVariable(
        "PATH",
        $null,
        [System.EnvironmentVariableTarget]::Process
    )
    [System.Environment]::SetEnvironmentVariable(
        "Path",
        $pathValue,
        [System.EnvironmentVariableTarget]::Process
    )
}

Normalize-Process-PathEnvironment

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RunDir = Join-Path $Root ".run\9000-stack"
$LogDir = Join-Path $Root "outputs\runtime\9000"
$VerifyPath = Join-Path $LogDir "verify.json"
$RuntimeDir = Join-Path $Root "runtime"
$SystemRuntimeDir = Join-Path $RuntimeDir "system"
$AgentRuntimeDir = Join-Path $RuntimeDir "agent"
$Ports = @($SystemPort, $AgentPort)
$BedrockBearerEnvKey = "AWS_BEARER_TOKEN_BEDROCK"

function Ensure-Dirs {
    New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
    New-Item -ItemType Directory -Force -Path $SystemRuntimeDir | Out-Null
    New-Item -ItemType Directory -Force -Path $AgentRuntimeDir | Out-Null
}

function Single-Quote([string]$Value) {
    return "'" + ($Value -replace "'", "''") + "'"
}

function Resolve-Python {
    $venvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        return $venvPython
    }
    return "python"
}

function Read-EnvMap([string]$PathValue) {
    $map = @{}
    foreach ($part in ($PathValue -replace ";", ",").Split(",")) {
        $trimmed = $part.Trim()
        if (-not $trimmed) {
            continue
        }
        $envPath = $trimmed
        if (-not [System.IO.Path]::IsPathRooted($envPath)) {
            $envPath = Join-Path $Root $envPath
        }
        if (-not (Test-Path -LiteralPath $envPath)) {
            continue
        }
        foreach ($line in Get-Content -LiteralPath $envPath) {
            $raw = $line.Trim()
            if (-not $raw -or $raw.StartsWith("#") -or -not $raw.Contains("=")) {
                continue
            }
            $idx = $raw.IndexOf("=")
            $key = $raw.Substring(0, $idx).Trim()
            $value = $raw.Substring($idx + 1).Trim()
            if (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                $value = $value.Substring(1, $value.Length - 2)
            }
            $map[$key] = $value
        }
    }
    return $map
}

function Assert-CommonEnvCredentialBoundary {
    $commonEnv = Read-EnvMap $EnvFile
    if ($commonEnv.ContainsKey($BedrockBearerEnvKey)) {
        throw (
            "$BedrockBearerEnvKey must not be present in the common env file. " +
            "Move it to the Agent-only credential overlay."
        )
    }
}

function Service-Specs {
    $agentRuntimeEnvFiles = "$EnvFile,$AgentEnvFile"
    return @(
        [PSCustomObject]@{ Name = "system"; Service = "system_app"; Kind = "uvicorn"; Module = "system_app.main:app"; Port = $SystemPort; EnvFiles = $EnvFile },
        [PSCustomObject]@{ Name = "agent"; Service = "agent_app"; Kind = "uvicorn"; Module = "agent_app.main:app"; Port = $AgentPort; EnvFiles = $agentRuntimeEnvFiles },
        [PSCustomObject]@{ Name = "agent-worker"; Service = "agent_app"; Kind = "worker"; Module = "agent_app.worker_main"; Port = 0; EnvFiles = $agentRuntimeEnvFiles }
    )
}

function Pid-Path($Spec) {
    return Join-Path $RunDir "$($Spec.Name).pid"
}

function Launcher-Pid-Path($Spec) {
    return Join-Path $RunDir "$($Spec.Name).launcher.pid"
}

function Worker-Runtime-Pid-Path($Spec) {
    return Join-Path $RunDir "$($Spec.Name).runtime.pid"
}

function Is-Process-Running([int]$PidValue) {
    try {
        $null = Get-Process -Id $PidValue -ErrorAction Stop
        return $true
    }
    catch {
        return $false
    }
}

function Child-Pids([int]$ParentPid) {
    try {
        return @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $ParentPid" -ErrorAction Stop | ForEach-Object { [int]$_.ProcessId })
    }
    catch {
        return @()
    }
}

function Stop-ProcessTree([int]$RootPid) {
    $taskkill = Get-Command taskkill.exe -ErrorAction SilentlyContinue
    if ($taskkill -and (Is-Process-Running $RootPid)) {
        $previousErrorAction = $ErrorActionPreference
        try {
            $ErrorActionPreference = "SilentlyContinue"
            & $taskkill.Source /PID $RootPid /T /F 2>$null | Out-Null
        }
        finally {
            $ErrorActionPreference = $previousErrorAction
        }
        if (-not (Is-Process-Running $RootPid)) {
            return
        }
    }
    foreach ($childPid in Child-Pids $RootPid) {
        Stop-ProcessTree $childPid
    }
    if (Is-Process-Running $RootPid) {
        Stop-Process -Id $RootPid -Force -ErrorAction SilentlyContinue
    }
}

function Existing-Pid($Spec) {
    $path = Pid-Path $Spec
    if (-not (Test-Path -LiteralPath $path)) {
        return $null
    }
    $pidText = (Get-Content -LiteralPath $path -Raw).Trim()
    if (-not $pidText) {
        return $null
    }
    $pidValue = [int]$pidText
    if (Is-Process-Running $pidValue) {
        return $pidValue
    }
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    return $null
}

function Wait-Port-Pid([int]$Port) {
    for ($i = 0; $i -lt 40; $i++) {
        $pidValue = Port-Pid $Port
        if ($pidValue) {
            return $pidValue
        }
        Start-Sleep -Milliseconds 250
    }
    return $null
}

function Wait-Child-Pid([int]$ParentPid) {
    for ($i = 0; $i -lt 40; $i++) {
        $children = Child-Pids $ParentPid
        if ($children.Count -gt 0) {
            return $children[0]
        }
        Start-Sleep -Milliseconds 250
    }
    return $null
}

function Wait-Pid-File([string]$Path) {
    for ($i = 0; $i -lt 40; $i++) {
        if (Test-Path -LiteralPath $Path) {
            $value = (Get-Content -LiteralPath $Path -Raw).Trim()
            if ($value) {
                return [int]$value
            }
        }
        Start-Sleep -Milliseconds 250
    }
    return $null
}

function Port-Pid([int]$Port) {
    if ($Port -le 0) {
        return $null
    }
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
        if ($conn) {
            return [int]$conn.OwningProcess
        }
    }
    catch {
        # Get-NetTCPConnection can be denied in managed Windows sessions.
        # netstat still gives us the exact listener PID without requiring CIM.
        $pattern = "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
        foreach ($line in @(& netstat.exe -ano -p tcp 2>$null)) {
            if ("$line" -match $pattern) {
                return [int]$Matches[1]
            }
        }
    }
    return $null
}

function Invoke-AgentMigration {
    $python = Resolve-Python
    $previousService = $env:DA_DRUG_SERVICE
    $previousEnvFile = $env:DA_DRUG_ENV_FILE
    $previousBedrockBearer = $env:AWS_BEARER_TOKEN_BEDROCK
    try {
        $env:DA_DRUG_SERVICE = "agent_app"
        $env:DA_DRUG_ENV_FILE = $EnvFile
        $env:AWS_BEARER_TOKEN_BEDROCK = $null
        & $python -m agent_app.migrate
        if ($LASTEXITCODE -ne 0) {
            throw "Agent database migration failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        $env:DA_DRUG_SERVICE = $previousService
        $env:DA_DRUG_ENV_FILE = $previousEnvFile
        $env:AWS_BEARER_TOKEN_BEDROCK = $previousBedrockBearer
    }
}

function Invoke-SystemMigration {
    $python = Resolve-Python
    $previousService = $env:DA_DRUG_SERVICE
    $previousEnvFile = $env:DA_DRUG_ENV_FILE
    $previousBedrockBearer = $env:AWS_BEARER_TOKEN_BEDROCK
    try {
        $env:DA_DRUG_SERVICE = "system_app"
        $env:DA_DRUG_ENV_FILE = $EnvFile
        $env:AWS_BEARER_TOKEN_BEDROCK = $null
        & $python -m system_app.migrate
        if ($LASTEXITCODE -ne 0) {
            throw "System database migration failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        $env:DA_DRUG_SERVICE = $previousService
        $env:DA_DRUG_ENV_FILE = $previousEnvFile
        $env:AWS_BEARER_TOKEN_BEDROCK = $previousBedrockBearer
    }
}

function Invoke-LocalPostgres([string]$PostgresAction) {
    $scriptPath = Join-Path $Root "tools\local_postgres.ps1"
    $previousBedrockBearer = $env:AWS_BEARER_TOKEN_BEDROCK
    try {
        $env:AWS_BEARER_TOKEN_BEDROCK = $null
        & powershell.exe `
            -NoProfile `
            -ExecutionPolicy Bypass `
            -File $scriptPath `
            -Action $PostgresAction
        if ($LASTEXITCODE -ne 0) {
            throw (
                "Local PostgreSQL action '$PostgresAction' failed with " +
                "exit code $LASTEXITCODE"
            )
        }
    }
    finally {
        $env:AWS_BEARER_TOKEN_BEDROCK = $previousBedrockBearer
    }
}

function Prepare-PostgresqlRuntime {
    Invoke-LocalPostgres "start"
    Invoke-SystemMigration
    Invoke-AgentMigration
    Invoke-LocalPostgres "grant"
}

function Build-ReactFrontend {
    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npm) {
        throw "npm.cmd is required to build the React frontend"
    }
    $previousBedrockBearer = $env:AWS_BEARER_TOKEN_BEDROCK
    try {
        $env:AWS_BEARER_TOKEN_BEDROCK = $null
        & $npm.Source --prefix (Join-Path $Root "frontend") run build
        if ($LASTEXITCODE -ne 0) {
            throw "React frontend build failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        $env:AWS_BEARER_TOKEN_BEDROCK = $previousBedrockBearer
    }
}

function Start-One($Spec) {
    $existing = Existing-Pid $Spec
    if ($existing) {
        Write-Host "$($Spec.Name) already running pid=$existing"
        return
    }
    $portPid = Port-Pid $Spec.Port
    if ($portPid) {
        Write-Host "$($Spec.Name) port $($Spec.Port) already occupied pid=$portPid; use stop -AllowStopUnknown if this is the 9000 stack"
        return
    }
    $python = Resolve-Python
    $stdout = Join-Path $LogDir "$($Spec.Name).out.log"
    $stderr = Join-Path $LogDir "$($Spec.Name).err.log"
    $arguments = @("-m")
    if ($Spec.Kind -eq "worker") {
        $arguments += @($Spec.Module)
    }
    else {
        $arguments += @(
            "uvicorn",
            $Spec.Module,
            "--host",
            "127.0.0.1",
            "--port",
            "$($Spec.Port)"
        )
    }
    $previousService = $env:DA_DRUG_SERVICE
    $previousEnvFile = $env:DA_DRUG_ENV_FILE
    $previousRuntimePidFile = $env:DA_DRUG_RUNTIME_PID_FILE
    $previousBedrockBearer = $env:AWS_BEARER_TOKEN_BEDROCK
    $previousPythonUnbuffered = $env:PYTHONUNBUFFERED
    $runtimePidPath = Worker-Runtime-Pid-Path $Spec
    try {
        $env:DA_DRUG_SERVICE = $Spec.Service
        $env:DA_DRUG_ENV_FILE = $Spec.EnvFiles
        $env:AWS_BEARER_TOKEN_BEDROCK = $null
        $env:PYTHONUNBUFFERED = "1"
        if ($Spec.Kind -eq "worker") {
            Remove-Item -LiteralPath $runtimePidPath -Force -ErrorAction SilentlyContinue
            $env:DA_DRUG_RUNTIME_PID_FILE = $runtimePidPath
        }
        else {
            $env:DA_DRUG_RUNTIME_PID_FILE = $null
        }
        $proc = Start-Process -FilePath $python `
            -ArgumentList $arguments `
            -WorkingDirectory $Root `
            -PassThru `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdout `
            -RedirectStandardError $stderr
    }
    finally {
        $env:DA_DRUG_SERVICE = $previousService
        $env:DA_DRUG_ENV_FILE = $previousEnvFile
        $env:DA_DRUG_RUNTIME_PID_FILE = $previousRuntimePidFile
        $env:AWS_BEARER_TOKEN_BEDROCK = $previousBedrockBearer
        $env:PYTHONUNBUFFERED = $previousPythonUnbuffered
    }
    $servicePid = $proc.Id
    Set-Content -LiteralPath (Launcher-Pid-Path $Spec) -Value $proc.Id -Encoding ascii
    if ($Spec.Kind -eq "uvicorn") {
        $portPid = Wait-Port-Pid $Spec.Port
        if ($portPid) {
            $servicePid = $portPid
        }
    }
    else {
        $runtimePid = Wait-Pid-File $runtimePidPath
        if ($runtimePid) {
            $servicePid = $runtimePid
        }
    }
    Set-Content -LiteralPath (Pid-Path $Spec) -Value $servicePid -Encoding ascii
    Write-Host "started $($Spec.Name) pid=$servicePid launcher_pid=$($proc.Id) log=$stdout"
}

function Stop-One($Spec) {
    $pidValue = Existing-Pid $Spec
    if ($pidValue) {
        Stop-ProcessTree $pidValue
        Write-Host "stopped $($Spec.Name) pid=$pidValue"
    }
    else {
        Write-Host "$($Spec.Name) pid file not active"
    }
    $launcherPath = Launcher-Pid-Path $Spec
    if (Test-Path -LiteralPath $launcherPath) {
        $launcherText = (Get-Content -LiteralPath $launcherPath -Raw).Trim()
        if ($launcherText) {
            $launcherPid = [int]$launcherText
            if ($launcherPid -ne $pidValue -and (Is-Process-Running $launcherPid)) {
                Stop-ProcessTree $launcherPid
            }
        }
    }
    Remove-Item -LiteralPath (Pid-Path $Spec) -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $launcherPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath (Worker-Runtime-Pid-Path $Spec) -Force -ErrorAction SilentlyContinue
}

function Stop-Unknown-Ports {
    if (-not $AllowStopUnknown) {
        return
    }
    foreach ($port in $Ports) {
        $pidValue = Port-Pid $port
        if ($pidValue -and (Is-Process-Running $pidValue)) {
            Stop-ProcessTree $pidValue
            Write-Host "stopped unknown 9000-stack listener port=$port pid=$pidValue"
        }
    }
    $venvPython = Join-Path $Root ".venv\Scripts\python.exe"
    try {
        $workers = Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $_.ExecutablePath -eq $venvPython -and $_.CommandLine -like "*agent_app.worker_main*"
        }
        foreach ($worker in $workers) {
            Stop-ProcessTree ([int]$worker.ProcessId)
            Write-Host "stopped unknown 9000-stack worker pid=$($worker.ProcessId)"
        }
    }
    catch {
    }
}

function Stack-Status {
    foreach ($spec in Service-Specs) {
        $pidValue = Existing-Pid $spec
        $portPid = Port-Pid $spec.Port
        [PSCustomObject]@{
            name = $spec.Name
            pid = $pidValue
            port = if ($spec.Port -gt 0) { $spec.Port } else { $null }
            port_pid = $portPid
            running = [bool]$pidValue
            log = Join-Path $LogDir "$($spec.Name).out.log"
        }
    }
}

function Request-Json([string]$Url, [hashtable]$Headers = @{}) {
    try {
        return Invoke-RestMethod -Method Get -Uri $Url -Headers $Headers -TimeoutSec 3
    }
    catch {
        return [PSCustomObject]@{ error = $_.Exception.Message }
    }
}

function Wait-Service-Ready($Spec) {
    if ($Spec.Kind -ne "uvicorn") {
        return
    }
    $healthPath = if ($Spec.Name -eq "agent") { "/health/ready" } else { "/health" }
    $expectedStatus = if ($Spec.Name -eq "agent") { "ready" } else { "ok" }
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        $health = Request-Json "http://127.0.0.1:$($Spec.Port)$healthPath"
        if ($health.status -eq $expectedStatus) {
            return
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw "$($Spec.Name) did not become healthy on port $($Spec.Port). See $(Join-Path $LogDir "$($Spec.Name).err.log")"
}

function Invoke-BoundaryContractVerification {
    $python = Resolve-Python
    $scriptPath = Join-Path $Root "tools\verify_testbed_contract.py"
    $previousBedrockBearer = $env:AWS_BEARER_TOKEN_BEDROCK
    try {
        $env:AWS_BEARER_TOKEN_BEDROCK = $null
        $output = @(
            & $python $scriptPath `
                --scope all `
                --env-file $EnvFile `
                --runtime `
                --json 2>&1
        )
        $exitCode = $LASTEXITCODE
    }
    finally {
        $env:AWS_BEARER_TOKEN_BEDROCK = $previousBedrockBearer
    }
    $raw = ($output | ForEach-Object { "$_" }) -join "`n"
    try {
        $result = $raw | ConvertFrom-Json
    }
    catch {
        return [PSCustomObject]@{
            ok = $false
            exit_code = $exitCode
            violations = @("boundary verifier did not return valid JSON")
            output = $raw
        }
    }
    if ($exitCode -ne 0) {
        $result.ok = $false
    }
    return $result
}

function Test-System-Details([object]$Details) {
    return $Details.status -eq "ok"
}

function Verify-Stack {
    Assert-CommonEnvCredentialBoundary
    $envMap = Read-EnvMap $EnvFile
    $token = $envMap["INTERNAL_API_TOKEN"]
    $headers = @{}
    if ($token) {
        $headers["X-Internal-Api-Token"] = $token
    }
    $generationHeaders = @{}
    $agentSyncToken = $envMap["AGENT_SYNC_API_TOKEN"]
    if ($agentSyncToken) {
        $generationHeaders["Authorization"] = "Bearer $agentSyncToken"
    }
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $last = $null
    do {
        $systemHealth = Request-Json "http://127.0.0.1:$SystemPort/health"
        $agentHealth = Request-Json `
            "http://127.0.0.1:$AgentPort/health/generation/ready" `
            $generationHeaders
        $agentAsync = Request-Json "http://127.0.0.1:$AgentPort/agent/async/tasks/status" $headers
        $systemDetails = Request-Json "http://127.0.0.1:$SystemPort/health/details"
        $runningWorkers = @(
            @($agentAsync.workers) |
                Where-Object { $_.status -eq "running" }
        )
        $systemDetailsAccepted = Test-System-Details $systemDetails
        $ok = (
            $systemHealth.status -eq "ok" -and
            $agentHealth.status -eq "ready" -and
            $agentAsync.status -eq "ok" -and
            $runningWorkers.Count -gt 0 -and
            $systemDetailsAccepted
        )
        $last = [PSCustomObject]@{
            ok = [bool]$ok
            generated_at = (Get-Date).ToUniversalTime().ToString("o")
            ports = [PSCustomObject]@{ system = $SystemPort; agent = $AgentPort }
            env_file = $EnvFile
            system_health = $systemHealth
            agent_health = $agentHealth
            agent_async = $agentAsync
            system_details = $systemDetails
            system_details_accepted = [bool]$systemDetailsAccepted
            boundary_contract = $null
        }
        if ($ok) {
            break
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    if ($last.ok) {
        $last.boundary_contract = Invoke-BoundaryContractVerification
        $last.ok = [bool]($last.ok -and $last.boundary_contract.ok)
    }
    $last | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $VerifyPath -Encoding utf8
    $last | ConvertTo-Json -Depth 12
    if (-not $last.ok) {
        exit 1
    }
}

Ensure-Dirs

switch ($Action) {
    "start" {
        Assert-CommonEnvCredentialBoundary
        Build-ReactFrontend
        Prepare-PostgresqlRuntime
        foreach ($spec in Service-Specs) {
            Start-One $spec
            Wait-Service-Ready $spec
        }
        Verify-Stack
    }
    "stop" {
        foreach ($spec in (Service-Specs | Sort-Object Name -Descending)) {
            Stop-One $spec
        }
        Stop-Unknown-Ports
    }
    "status" {
        Stack-Status | Format-Table -AutoSize
    }
    "verify" {
        Verify-Stack
    }
    "restart" {
        Assert-CommonEnvCredentialBoundary
        foreach ($spec in (Service-Specs | Sort-Object Name -Descending)) {
            Stop-One $spec
        }
        Stop-Unknown-Ports
        Start-Sleep -Seconds 1
        Build-ReactFrontend
        Prepare-PostgresqlRuntime
        foreach ($spec in Service-Specs) {
            Start-One $spec
            Wait-Service-Ready $spec
        }
        Verify-Stack
    }
}
