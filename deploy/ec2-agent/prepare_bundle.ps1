param(
    [string]$OutputDirectory = "artifacts",
    [string]$ReleaseId = ""
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$outputRoot = if ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
    $OutputDirectory
} else {
    Join-Path $repoRoot $OutputDirectory
}
$builder = Join-Path $PSScriptRoot "build_release_bundle.py"
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    $venvPython
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw "Python 3.11+ is required to build the Agent release."
    }
    $pythonCommand.Source
}

$arguments = @(
    $builder,
    "--repo-root", $repoRoot,
    "--output-directory", $outputRoot
)
if (-not [string]::IsNullOrWhiteSpace($ReleaseId)) {
    $arguments += @("--release-id", $ReleaseId)
}

& $python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Agent release bundle creation failed with exit code $LASTEXITCODE."
}
