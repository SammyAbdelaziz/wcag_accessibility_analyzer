[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$AcrName,
    [string]$ImageRepository = "wcag-analyzer",
    [string]$ImageTag,
    [string]$OutputPath,
    [int]$DigestRetryCount = 6,
    [int]$DigestRetryDelaySeconds = 10
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Assert-Command {
    param([Parameter(Mandatory = $true)][string]$Name)

    if (-not (Get-Command -Name $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $Name"
    }
}

function Invoke-AzCli {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $output = & az @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "az $($Arguments -join ' ') failed.`n$output"
    }

    return ($output | Out-String).TrimEnd()
}

Assert-Command -Name "az"
Assert-Command -Name "git"

$repoRoot = Split-Path -Parent $PSScriptRoot
$shortSha = (& git -C $repoRoot rev-parse --short HEAD).Trim()

if ([string]::IsNullOrWhiteSpace($ImageTag)) {
    $ImageTag = "buildcheck-$shortSha-$(Get-Date -Format 'yyyyMMddHHmmss')"
}

$loginServer = (Invoke-AzCli -Arguments @(
    "acr",
    "show",
    "--name", $AcrName,
    "--query", "loginServer",
    "-o", "tsv"
)).Trim()

Push-Location $repoRoot
try {
    Invoke-AzCli -Arguments @(
        "acr",
        "build",
        "--registry", $AcrName,
        "--image", "$ImageRepository`:$ImageTag",
        "--no-logs",
        "."
    ) | Out-Null
}
finally {
    Pop-Location
}

$digest = $null
for ($attempt = 1; $attempt -le $DigestRetryCount; $attempt++) {
    $candidate = (Invoke-AzCli -Arguments @(
        "acr",
        "repository",
        "show-manifests",
        "--name", $AcrName,
        "--repository", $ImageRepository,
        "--query", "[?tags[?@=='$ImageTag']].digest | [0]",
        "-o", "tsv"
    )).Trim()

    if ($candidate -match 'sha256:[0-9a-f]{64}') {
        $digest = $matches[0]
        break
    }

    if ($attempt -lt $DigestRetryCount) {
        Start-Sleep -Seconds $DigestRetryDelaySeconds
    }
}

if ([string]::IsNullOrWhiteSpace($digest)) {
    throw "Unable to resolve image digest for $ImageRepository`:$ImageTag in ACR $AcrName"
}

$result = [pscustomobject]@{
    acrName = $AcrName
    imageRepository = $ImageRepository
    imageTag = $ImageTag
    shortSha = $shortSha
    digest = $digest
    imageRef = "$loginServer/$ImageRepository@$digest"
}

if (-not [string]::IsNullOrWhiteSpace($OutputPath)) {
    $result | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $OutputPath -Encoding utf8
}

$result