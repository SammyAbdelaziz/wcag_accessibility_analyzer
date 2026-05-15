[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$ResourceGroup,
    [Parameter(Mandatory = $true)][string]$ContainerApp,
    [string]$ImageRef,
    [string[]]$SmokeFiles = @(
        (Join-Path (Split-Path -Parent $PSScriptRoot) "samples\sample.docx"),
        (Join-Path (Split-Path -Parent $PSScriptRoot) "samples\sample.pptx")
    ),
    [string]$ProbeOutputPath = (Join-Path $PSScriptRoot "tmp_live_smoke_probe.json"),
    [int]$RequestTimeoutSeconds = 300,
    [int]$RetryCount = 2,
    [int]$RetryDelaySeconds = 10,
    [switch]$SkipUpdate,
    [switch]$SkipSmoke
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Assert-Command {
    param([Parameter(Mandatory = $true)][string]$Name)

    if (-not (Get-Command -Name $Name -ErrorAction SilentlyContinue)) {
        throw "Required command not found: $Name"
    }
}

function Assert-FileExists {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Smoke file not found: $Path"
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

function Get-ContainerAppStatus {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Group
    )

    $statusJson = Invoke-AzCli -Arguments @(
        "containerapp",
        "show",
        "--name", $Name,
        "--resource-group", $Group,
        "--query", "{revision:properties.latestRevisionName,image:properties.template.containers[0].image,runningStatus:properties.runningStatus,fqdn:properties.configuration.ingress.fqdn}",
        "-o", "json"
    )

    return $statusJson | ConvertFrom-Json
}

function Get-SummaryCount {
    param(
        [Parameter(Mandatory = $true)]$Summary,
        [Parameter(Mandatory = $true)][string]$PropertyName
    )

    if ($null -eq $Summary) {
        return 0
    }

    $property = $Summary.PSObject.Properties[$PropertyName]
    if ($null -eq $property -or $null -eq $property.Value) {
        return 0
    }

    return [int]$property.Value
}

function Get-FindingsCount {
    param([Parameter(Mandatory = $true)]$Response)

    if ($Response.PSObject.Properties.Name -contains "findings") {
        return @($Response.findings).Count
    }

    $hasConfirmed = $Response.PSObject.Properties.Name -contains "confirmed_findings"
    $hasPossible = $Response.PSObject.Properties.Name -contains "possible_findings"

    if ($hasConfirmed -or $hasPossible) {
        return @($Response.confirmed_findings).Count + @($Response.possible_findings).Count
    }

    throw "Response did not include findings, confirmed_findings, or possible_findings."
}

function Test-IsRetryableMessage {
    param([Parameter(Mandatory = $true)][string]$Message)

    return $Message -match "502|503|504|timeout|timed out|connection was closed|temporarily unavailable"
}

function Invoke-SmokeUpload {
    param(
        [Parameter(Mandatory = $true)][string]$AnalyzeUrl,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        [Parameter(Mandatory = $true)][int]$MaxAttempts,
        [Parameter(Mandatory = $true)][int]$DelaySeconds
    )

    $expectedFileType = [System.IO.Path]::GetExtension($FilePath).TrimStart('.').ToLowerInvariant()
    $fileItem = Get-Item -LiteralPath $FilePath

    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        try {
            $response = Invoke-RestMethod -Method Post -Uri $AnalyzeUrl -Form @{ file = $fileItem } -TimeoutSec $TimeoutSeconds

            if (-not ($response.PSObject.Properties.Name -contains "file_type")) {
                throw "Response did not include a file_type property."
            }

            if ($response.file_type -ne $expectedFileType) {
                throw "Expected file_type '$expectedFileType' but received '$($response.file_type)'."
            }

            $findingsCount = Get-FindingsCount -Response $response

            return [pscustomobject]@{
                file = $fileItem.Name
                fileType = $response.file_type
                findingsCount = $findingsCount
                confirmedCount = Get-SummaryCount -Summary $response.summary -PropertyName "confirmed_count"
                possibleCount = Get-SummaryCount -Summary $response.summary -PropertyName "possible_count"
                status = "passed"
            }
        }
        catch {
            $message = $_.Exception.Message
            if ($attempt -ge $MaxAttempts -or -not (Test-IsRetryableMessage -Message $message)) {
                throw "Smoke upload failed for $($fileItem.Name): $message"
            }

            Write-Warning "Smoke upload attempt $attempt/$MaxAttempts failed for $($fileItem.Name): $message"
            Start-Sleep -Seconds $DelaySeconds
        }
    }

    throw "Smoke upload failed for $($fileItem.Name) after $MaxAttempts attempts."
}

Assert-Command -Name "az"

if (-not $SkipUpdate -and [string]::IsNullOrWhiteSpace($ImageRef)) {
    throw "ImageRef is required unless -SkipUpdate is specified."
}

foreach ($smokeFile in $SmokeFiles) {
    Assert-FileExists -Path $smokeFile
}

if (-not $SkipUpdate) {
    if ($PSCmdlet.ShouldProcess("$ContainerApp in $ResourceGroup", "Deploy image $ImageRef")) {
        Invoke-AzCli -Arguments @(
            "containerapp",
            "update",
            "--name", $ContainerApp,
            "--resource-group", $ResourceGroup,
            "--image", $ImageRef
        ) | Out-Null
    }
}

$containerStatus = Get-ContainerAppStatus -Name $ContainerApp -Group $ResourceGroup

if ($null -eq $containerStatus.fqdn -or [string]::IsNullOrWhiteSpace([string]$containerStatus.fqdn)) {
    throw "Container App FQDN is unavailable; cannot run smoke test."
}

$appUrl = "https://$($containerStatus.fqdn)"
$analyzeUrl = "$appUrl/api/analyze"
$smokeResults = @()

if (-not $SkipSmoke) {
    foreach ($smokeFile in $SmokeFiles) {
        $smokeResults += Invoke-SmokeUpload `
            -AnalyzeUrl $analyzeUrl `
            -FilePath $smokeFile `
            -TimeoutSeconds $RequestTimeoutSeconds `
            -MaxAttempts $RetryCount `
            -DelaySeconds $RetryDelaySeconds
    }
}

$probe = [pscustomobject]@{
    timestamp = (Get-Date).ToString("s")
    resourceGroup = $ResourceGroup
    containerApp = $ContainerApp
    imageRef = $ImageRef
    appUrl = $appUrl
    analyzeUrl = $analyzeUrl
    containerStatus = $containerStatus
    smokeResults = $smokeResults
}

$probe | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ProbeOutputPath -Encoding utf8

$probe