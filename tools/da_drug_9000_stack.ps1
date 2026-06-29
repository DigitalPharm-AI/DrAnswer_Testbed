param(
    [ValidateSet("start", "stop", "status", "verify", "restart")]
    [string]$Action = "start",
    [string]$EnvFile = ".env.9000.rule_based.example",
    [int]$SystemPort = 9000,
    [int]$AgentPort = 9001,
    [int]$PhrPort = 9002,
    [int]$TimeoutSeconds = 45,
    [switch]$AllowStopUnknown
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RunDir = Join-Path $Root ".run\9000-stack"
$LogDir = Join-Path $Root "outputs\runtime\9000"
$VerifyPath = Join-Path $LogDir "verify.json"
$Ports = @($SystemPort, $AgentPort, $PhrPort)

function Ensure-Dirs {
    New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
    New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
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

function Service-Specs {
    return @(
        [PSCustomObject]@{ Name = "phr"; Service = "phr_app"; Kind = "uvicorn"; Module = "phr_app.main:app"; Port = $PhrPort },
        [PSCustomObject]@{ Name = "agent"; Service = "agent_app"; Kind = "uvicorn"; Module = "agent_app.main:app"; Port = $AgentPort },
        [PSCustomObject]@{ Name = "agent-worker"; Service = "agent_app"; Kind = "worker"; Module = "agent_app.worker_main"; Port = 0 },
        [PSCustomObject]@{ Name = "system"; Service = "system_app"; Kind = "uvicorn"; Module = "system_app.main:app"; Port = $SystemPort }
    )
}

function Pid-Path($Spec) {
    return Join-Path $RunDir "$($Spec.Name).pid"
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
        return $null
    }
    return $null
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
    $rootQ = Single-Quote $Root
    $pythonQ = Single-Quote $python
    $envFileQ = Single-Quote $EnvFile
    $serviceQ = Single-Quote $Spec.Service
    if ($Spec.Kind -eq "worker") {
        $command = "`$env:DA_DRUG_SERVICE=$serviceQ; `$env:DA_DRUG_ENV_FILE=$envFileQ; Set-Location $rootQ; & $pythonQ -m $($Spec.Module)"
    }
    else {
        $command = "`$env:DA_DRUG_SERVICE=$serviceQ; `$env:DA_DRUG_ENV_FILE=$envFileQ; Set-Location $rootQ; & $pythonQ -m uvicorn $($Spec.Module) --host 127.0.0.1 --port $($Spec.Port)"
    }
    $proc = Start-Process -FilePath "powershell.exe" `
        -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $command) `
        -PassThru `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr
    $servicePid = $proc.Id
    if ($Spec.Kind -eq "uvicorn") {
        $portPid = Wait-Port-Pid $Spec.Port
        if ($portPid) {
            $servicePid = $portPid
        }
    }
    else {
        $childPid = Wait-Child-Pid $proc.Id
        if ($childPid) {
            $servicePid = $childPid
        }
    }
    Set-Content -LiteralPath (Pid-Path $Spec) -Value $servicePid -Encoding ascii
    Write-Host "started $($Spec.Name) pid=$servicePid wrapper_pid=$($proc.Id) log=$stdout"
}

function Stop-One($Spec) {
    $pidValue = Existing-Pid $Spec
    if ($pidValue) {
        Stop-ProcessTree $pidValue
        Remove-Item -LiteralPath (Pid-Path $Spec) -Force -ErrorAction SilentlyContinue
        Write-Host "stopped $($Spec.Name) pid=$pidValue"
        return
    }
    Write-Host "$($Spec.Name) pid file not active"
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

function Verify-Stack {
    $envMap = Read-EnvMap $EnvFile
    $token = $envMap["INTERNAL_API_TOKEN"]
    $headers = @{}
    if ($token) {
        $headers["X-Internal-Api-Token"] = $token
    }
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $last = $null
    do {
        $systemHealth = Request-Json "http://127.0.0.1:$SystemPort/health"
        $agentHealth = Request-Json "http://127.0.0.1:$AgentPort/health"
        $phrHealth = Request-Json "http://127.0.0.1:$PhrPort/health"
        $agentAsync = Request-Json "http://127.0.0.1:$AgentPort/agent/async/tasks/status" $headers
        $systemDetails = Request-Json "http://127.0.0.1:$SystemPort/health/details"
        $ok = (
            $systemHealth.status -eq "ok" -and
            $agentHealth.status -eq "ok" -and
            $phrHealth.status -eq "ok" -and
            $agentAsync.status -eq "ok" -and
            $agentAsync.workers.Count -gt 0 -and
            $systemDetails.status -eq "ok"
        )
        $last = [PSCustomObject]@{
            ok = [bool]$ok
            generated_at = (Get-Date).ToUniversalTime().ToString("o")
            ports = [PSCustomObject]@{ system = $SystemPort; agent = $AgentPort; phr = $PhrPort }
            env_file = $EnvFile
            system_health = $systemHealth
            agent_health = $agentHealth
            phr_health = $phrHealth
            agent_async = $agentAsync
            system_details = $systemDetails
        }
        if ($ok) {
            break
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)

    $last | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $VerifyPath -Encoding utf8
    $last | ConvertTo-Json -Depth 12
    if (-not $last.ok) {
        exit 1
    }
}

Ensure-Dirs

switch ($Action) {
    "start" {
        foreach ($spec in Service-Specs) {
            Start-One $spec
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
        foreach ($spec in (Service-Specs | Sort-Object Name -Descending)) {
            Stop-One $spec
        }
        Stop-Unknown-Ports
        Start-Sleep -Seconds 1
        foreach ($spec in Service-Specs) {
            Start-One $spec
        }
        Verify-Stack
    }
}
