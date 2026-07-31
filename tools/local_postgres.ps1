param(
    [ValidateSet("start", "stop", "status", "bootstrap", "grant")]
    [string]$Action = "start",
    [int]$Port = 55432,
    [string]$PostgresBin = $env:POSTGRES_BIN
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$PostgresRuntime = Join-Path $Root "runtime\postgresql"
$DataDir = Join-Path $PostgresRuntime "data"
$LogPath = Join-Path $PostgresRuntime "postgresql.log"

if (-not $PostgresBin) {
    $PostgresBin = "C:\Program Files\PostgreSQL\18\bin"
}

function Tool-Path([string]$Name) {
    $path = Join-Path $PostgresBin "$Name.exe"
    if (-not (Test-Path -LiteralPath $path)) {
        throw "PostgreSQL tool was not found: $path"
    }
    return $path
}

function Assert-Safe-Data-Path {
    $runtimeFull = [System.IO.Path]::GetFullPath($PostgresRuntime).TrimEnd(
        [System.IO.Path]::DirectorySeparatorChar
    )
    $dataFull = [System.IO.Path]::GetFullPath($DataDir)
    $expectedPrefix = $runtimeFull + [System.IO.Path]::DirectorySeparatorChar
    if (-not $dataFull.StartsWith(
        $expectedPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "Refusing to manage a PostgreSQL data directory outside runtime/postgresql."
    }
}

function Test-Ready {
    $pgIsReady = Tool-Path "pg_isready"
    & $pgIsReady -h 127.0.0.1 -p $Port 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
}

function Ensure-Cluster {
    Assert-Safe-Data-Path
    if (Test-Path -LiteralPath (Join-Path $DataDir "PG_VERSION")) {
        return
    }
    New-Item -ItemType Directory -Path $PostgresRuntime -Force | Out-Null
    $initdb = Tool-Path "initdb"
    # This cluster is bound to loopback and is strictly for the local
    # testbed. Production and shared environments must use password/TLS
    # authentication supplied by infrastructure.
    & $initdb `
        -D $DataDir `
        -U postgres `
        -A trust `
        --encoding=UTF8 `
        --locale=C
    if ($LASTEXITCODE -ne 0) {
        throw "Local PostgreSQL cluster initialization failed."
    }
}

function Start-Cluster {
    Ensure-Cluster
    if (Test-Ready) {
        return
    }
    $pgCtl = Tool-Path "pg_ctl"
    New-Item -ItemType Directory -Path $PostgresRuntime -Force | Out-Null
    & $pgCtl `
        -D $DataDir `
        -l $LogPath `
        -o "`"-p $Port -h 127.0.0.1`"" `
        start
    if ($LASTEXITCODE -ne 0) {
        throw "Local PostgreSQL start failed. See $LogPath"
    }
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (Test-Ready) {
            return
        }
        Start-Sleep -Milliseconds 500
    }
    throw "Local PostgreSQL did not become ready on port $Port."
}

function Invoke-Psql(
    [string]$Database,
    [string]$User,
    [string]$Sql
) {
    $psql = Tool-Path "psql"
    & $psql `
        -h 127.0.0.1 `
        -p $Port `
        -U $User `
        -d $Database `
        -v ON_ERROR_STOP=1 `
        -c $Sql
    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL command failed for database $Database."
    }
}

function Test-Database([string]$Database) {
    $psql = Tool-Path "psql"
    $result = & $psql `
        -h 127.0.0.1 `
        -p $Port `
        -U postgres `
        -d postgres `
        -Atc "SELECT 1 FROM pg_database WHERE datname = '$Database'"
    return "$result".Trim() -eq "1"
}

function Ensure-Local-Databases {
    Start-Cluster
    $roleSql = @"
DO `$`$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'system_migrator') THEN
        CREATE ROLE system_migrator LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'system_app_rw') THEN
        CREATE ROLE system_app_rw LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_migrator') THEN
        CREATE ROLE agent_migrator LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_app_rw') THEN
        CREATE ROLE agent_app_rw LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'contract_migration') THEN
        CREATE ROLE contract_migration LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'contract_app_rw') THEN
        CREATE ROLE contract_app_rw LOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ai_backend_reader') THEN
        CREATE ROLE ai_backend_reader LOGIN;
    END IF;
END
`$`$;
"@
    Invoke-Psql "postgres" "postgres" $roleSql
    Invoke-Psql "postgres" "postgres" @"
ALTER ROLE ai_backend_reader
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
ALTER ROLE ai_backend_reader SET default_transaction_read_only = on;
ALTER ROLE ai_backend_reader SET search_path = public;
ALTER ROLE contract_migration
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
ALTER ROLE contract_app_rw
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
"@

    $createdb = Tool-Path "createdb"
    if (-not (Test-Database "dranswer_system")) {
        & $createdb `
            -h 127.0.0.1 `
            -p $Port `
            -U postgres `
            -O system_migrator `
            dranswer_system
        if ($LASTEXITCODE -ne 0) {
            throw "Could not create dranswer_system."
        }
    }
    if (-not (Test-Database "dranswer_agent")) {
        & $createdb `
            -h 127.0.0.1 `
            -p $Port `
            -U postgres `
            -O agent_migrator `
            dranswer_agent
        if ($LASTEXITCODE -ne 0) {
            throw "Could not create dranswer_agent."
        }
    }
    if (-not (Test-Database "dranswer_contract")) {
        & $createdb `
            -h 127.0.0.1 `
            -p $Port `
            -U postgres `
            -O contract_migration `
            dranswer_contract
        if ($LASTEXITCODE -ne 0) {
            throw "Could not create dranswer_contract."
        }
    }
}

function Grant-Runtime-Roles {
    Ensure-Local-Databases
    Invoke-Psql "dranswer_system" "system_migrator" @"
GRANT CONNECT ON DATABASE dranswer_system TO system_app_rw;
GRANT USAGE ON SCHEMA public TO system_app_rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO system_app_rw;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO system_app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE system_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO system_app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE system_migrator IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO system_app_rw;
GRANT CONNECT ON DATABASE dranswer_system TO ai_backend_reader;
GRANT USAGE ON SCHEMA public TO ai_backend_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM ai_backend_reader;
DO `$`$
DECLARE
    view_name text;
BEGIN
    FOR view_name IN
        SELECT c.relname
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
          AND c.relkind IN ('v', 'm')
          AND c.relname = ANY(ARRAY[
              'ai_v13_active_medication_schedules',
              'ai_v13_chat_messages',
              'ai_v13_dose_events',
              'ai_v13_nutrition_food_ref',
              'ai_v13_nutrition_foods',
              'ai_v13_nutrition_meals',
              'ai_v13_nutrition_preferences',
              'ai_v13_patient_profiles',
              'ai_v13_reminder_policies',
              'ai_v13_side_effect_records'
          ])
    LOOP
        EXECUTE format(
            'GRANT SELECT ON TABLE public.%I TO ai_backend_reader',
            view_name
        );
    END LOOP;
END
`$`$;
"@
    Invoke-Psql "dranswer_agent" "agent_migrator" @"
GRANT CONNECT ON DATABASE dranswer_agent TO agent_app_rw;
GRANT USAGE ON SCHEMA public TO agent_app_rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO agent_app_rw;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO agent_app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE agent_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO agent_app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE agent_migrator IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO agent_app_rw;
"@
    Invoke-Psql "dranswer_contract" "contract_migration" @"
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT CONNECT ON DATABASE dranswer_contract TO contract_app_rw;
GRANT USAGE ON SCHEMA public TO contract_app_rw;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO contract_app_rw;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO contract_app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE contract_migration IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO contract_app_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE contract_migration IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO contract_app_rw;
"@
}

function Stop-Cluster {
    Assert-Safe-Data-Path
    if (-not (Test-Path -LiteralPath (Join-Path $DataDir "PG_VERSION"))) {
        return
    }
    if (-not (Test-Ready)) {
        return
    }
    $pgCtl = Tool-Path "pg_ctl"
    & $pgCtl -D $DataDir stop -m fast
    if ($LASTEXITCODE -ne 0) {
        throw "Local PostgreSQL stop failed."
    }
}

switch ($Action) {
    "start" {
        Ensure-Local-Databases
        [PSCustomObject]@{
            status = "ready"
            host = "127.0.0.1"
            port = $Port
            data_dir = $DataDir
        }
    }
    "stop" {
        Stop-Cluster
        [PSCustomObject]@{ status = "stopped"; port = $Port }
    }
    "status" {
        [PSCustomObject]@{
            status = if (Test-Ready) { "ready" } else { "stopped" }
            host = "127.0.0.1"
            port = $Port
            data_dir = $DataDir
        }
    }
    "bootstrap" {
        Ensure-Local-Databases
        [PSCustomObject]@{ status = "bootstrapped"; port = $Port }
    }
    "grant" {
        Grant-Runtime-Roles
        [PSCustomObject]@{ status = "granted"; port = $Port }
    }
}
