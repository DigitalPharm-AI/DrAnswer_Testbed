param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$HostName,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$UserName,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$IdentityFile,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$TokenFile,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$')]
    [string]$ReleaseId,

    [ValidateRange(1, 65535)]
    [int]$Port = 22
)

$ErrorActionPreference = "Stop"
$remoteInstaller = (
    "/opt/dranswer-agent/releases/$ReleaseId/" +
    "deploy/ec2-agent/install_bedrock_token.sh"
)

if ($HostName -notmatch '^[A-Za-z0-9.-]+$') {
    throw "HostName contains unsupported characters."
}
if ($UserName -notmatch '^[A-Za-z0-9._-]+$') {
    throw "UserName contains unsupported characters."
}

$resolvedIdentity = (Resolve-Path -LiteralPath $IdentityFile).Path
$resolvedTokenFile = (Resolve-Path -LiteralPath $TokenFile).Path
if (-not (Test-Path -LiteralPath $resolvedIdentity -PathType Leaf)) {
    throw "IdentityFile must be a file."
}
if (-not (Test-Path -LiteralPath $resolvedTokenFile -PathType Leaf)) {
    throw "TokenFile must be a file."
}
$sshCommand = Get-Command ssh.exe -ErrorAction SilentlyContinue
if (-not $sshCommand) {
    throw "OpenSSH ssh.exe is required."
}

function Read-BedrockToken {
    param([Parameter(Mandatory = $true)][string]$Path)

    $content = [System.IO.File]::ReadAllText($Path)
    $assignments = @()
    foreach ($line in ($content -split "`r?`n")) {
        if ($line -match '^\s*(?:export\s+)?AWS_BEARER_TOKEN_BEDROCK\s*=(.*)$') {
            $assignments += $Matches[1]
        }
    }
    if ($assignments.Count -gt 1) {
        throw "TokenFile contains duplicate Bedrock token assignments."
    }
    if ($assignments.Count -eq 1) {
        $value = "$($assignments[0])".Trim()
        if (
            $value.Length -ge 2 -and
            (
                ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'"))
            )
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        return $value
    }

    $rawLines = @(
        $content -split "`r?`n" |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if ($rawLines.Count -ne 1) {
        throw (
            "TokenFile must contain one raw token or one " +
            "AWS_BEARER_TOKEN_BEDROCK assignment."
        )
    }
    return "$($rawLines[0])".Trim()
}

$token = Read-BedrockToken -Path $resolvedTokenFile
if ([string]::IsNullOrWhiteSpace($token)) {
    throw "TokenFile contains an empty Bedrock token."
}
if ($token -match '\s') {
    throw "TokenFile contains whitespace in the Bedrock token."
}
if (
    $token.Contains("CHANGE_ME") -or
    $token.Contains("<") -or
    $token.Contains(">")
) {
    throw "TokenFile still contains a placeholder."
}

$target = "$UserName@$HostName"
$sshArguments = @(
    "-o", "BatchMode=yes",
    "-o", "StrictHostKeyChecking=yes",
    "-o", "IdentitiesOnly=yes",
    "-p", "$Port",
    "-i", $resolvedIdentity,
    $target,
    "bash -- '$remoteInstaller'"
)

function ConvertTo-NativeArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    if ($Value -and $Value -notmatch '[\s"]') {
        return $Value
    }
    $builder = [System.Text.StringBuilder]::new()
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes += 1
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append(
                ((('\' * (($backslashes * 2) + 1))) -join "")
            )
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes) {
            [void]$builder.Append((('\' * $backslashes) -join ""))
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes) {
        [void]$builder.Append((('\' * ($backslashes * 2)) -join ""))
    }
    [void]$builder.Append('"')
    return $builder.ToString()
}

$startInfo = [System.Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $sshCommand.Source
$startInfo.Arguments = (
    $sshArguments |
        ForEach-Object { ConvertTo-NativeArgument -Value "$_" }
) -join " "
$startInfo.UseShellExecute = $false
$startInfo.RedirectStandardInput = $true
$process = [System.Diagnostics.Process]::new()
$process.StartInfo = $startInfo

try {
    if (-not $process.Start()) {
        throw "Could not start OpenSSH."
    }
    $process.StandardInput.Write($token)
    $process.StandardInput.Write("`n")
    $process.StandardInput.Close()
    $token = $null
    $process.WaitForExit()
    $sshExitCode = $process.ExitCode
}
finally {
    $token = $null
    $process.Dispose()
}

if ($sshExitCode -ne 0) {
    throw "Remote Bedrock token installation failed with exit code $sshExitCode."
}

Write-Output "Remote Bedrock credential installation completed."
