param(
    [string]$OutputDirectory = "artifacts/contract-server",
    [string]$ReleaseId = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$outputRoot = if ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
    $OutputDirectory
} else {
    Join-Path $repoRoot $OutputDirectory
}

New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null

$archiveName = "dranswer-agent-contract-$ReleaseId.tar.gz"
$archivePath = Join-Path $outputRoot $archiveName
$checksumPath = "$archivePath.sha256"
$installerPath = Join-Path $outputRoot "install_contract_release.sh"

Push-Location $repoRoot
try {
    tar `
        --exclude="__pycache__" `
        --exclude="*.pyc" `
        --exclude="*.pyo" `
        --exclude=".pytest_cache" `
        --exclude=".ruff_cache" `
        -czf $archivePath `
        contract_test_server `
        shared/__init__.py `
        shared/async_v13_contracts.py `
        shared/backend_v13_contracts.py `
        shared/chat_contracts.py `
        shared/contract_boundary.py `
        shared/db.py `
        shared/public_ids.py `
        shared/schemas.py `
        docs/AI_V13_CHAT_OPENAPI.json `
        docs/AI_V13_ASYNC_MEDICATION_OPENAPI.json `
        docs/BACKEND_V13_ASYNC_CALLBACK_OPENAPI.json `
        deploy/ec2-contract-server
    if ($LASTEXITCODE -ne 0) {
        throw "tar failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

$entries = tar -tzf $archivePath
if ($LASTEXITCODE -ne 0) {
    throw "cannot inspect generated archive"
}

$forbidden = $entries | Where-Object {
    (
        ($_ -match '(^|/)\.env($|\.)') -and
        ($_ -notmatch '\.example$')
    ) -or ($_ -match '\.(pem|key|p12|pfx)$')
}
if ($forbidden) {
    throw "archive contains a secret-like file: $($forbidden -join ', ')"
}

$hash = (
    Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath
).Hash.ToLowerInvariant()
[System.IO.File]::WriteAllText(
    $checksumPath,
    "$hash  $archiveName`n",
    [System.Text.UTF8Encoding]::new($false)
)
Copy-Item `
    -LiteralPath (Join-Path $PSScriptRoot "install_release.sh") `
    -Destination $installerPath `
    -Force

$archive = Get-Item -LiteralPath $archivePath
[pscustomobject]@{
    ReleaseId = $ReleaseId
    Archive = $archive.FullName
    Bytes = $archive.Length
    Sha256 = $hash
    ChecksumFile = $checksumPath
    Installer = $installerPath
}
