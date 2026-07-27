<#
.SYNOPSIS
Build the external jaffle_shop dataset and ingest its metadata into local DataHub.

.DESCRIPTION
Creates isolated virtual environments for this project and dbt, optionally
starts DataHub Quickstart, builds dbt artifacts, ingests them, and verifies the
customers context plus field-level PII tags. Existing clones and environments
are reused, so the script is safe to run again.
#>
[CmdletBinding()]
param(
    [string]$PythonCommand = "python",
    [string]$DbtPythonCommand = "python",
    [string]$JaffleShopPath = (Join-Path $PSScriptRoot "..\..\jaffle_shop_duckdb"),
    [string]$JaffleShopCommit = "36bde6cba69d962b83be1d52fc65a0dce1cb4ebb",
    [string]$GmsUrl = "http://localhost:8080",
    [switch]$StartDataHub,
    [switch]$IncludeImpactDemo,
    [switch]$AllowUnpinnedJaffleShop,
    [switch]$SkipDependencyInstall,
    [switch]$SkipIngest
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$JaffleShopPath = [System.IO.Path]::GetFullPath($JaffleShopPath)
$ProjectVenv = Join-Path $RepoRoot ".venv"
$ProjectPython = Join-Path $ProjectVenv "Scripts\python.exe"
$ProjectDataHub = Join-Path $ProjectVenv "Scripts\datahub.exe"
$DbtVenv = Join-Path $JaffleShopPath ".venv"
$DbtPython = Join-Path $DbtVenv "Scripts\python.exe"
$DbtExecutable = Join-Path $DbtVenv "Scripts\dbt.exe"
$Recipe = Join-Path $RepoRoot "recipes\dbt_recipe.yml"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [string]$WorkingDirectory = $RepoRoot
    )
    Push-Location $WorkingDirectory
    try {
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) {
            throw "Command failed with exit code $LASTEXITCODE`: $FilePath $Arguments"
        }
    }
    finally {
        Pop-Location
    }
}

function Test-GmsHealth {
    try {
        $response = Invoke-WebRequest `
            -UseBasicParsing `
            -TimeoutSec 3 `
            -Uri "$($GmsUrl.TrimEnd('/'))/health"
        return $response.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

Write-Host "[1/7] Preparing the locked project environment"
Invoke-Checked $PythonCommand @(
    "-c",
    "import sys; assert sys.version_info >= (3, 12), 'Python 3.12+ is required'"
)
if (-not (Test-Path -LiteralPath $ProjectPython)) {
    Invoke-Checked $PythonCommand @("-m", "venv", $ProjectVenv)
}
if (-not $SkipDependencyInstall) {
    Invoke-Checked $ProjectPython @(
        "-m", "pip", "install", "--require-hashes", "-r",
        (Join-Path $RepoRoot "requirements.lock")
    )
}
if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot ".env"))) {
    Copy-Item `
        -LiteralPath (Join-Path $RepoRoot ".env.example") `
        -Destination (Join-Path $RepoRoot ".env")
    Write-Host "Created .env from .env.example; add NVIDIA_API_KEY before generation."
}
$env:PATH = "$(Join-Path $ProjectVenv 'Scripts');$env:PATH"
$env:DATAHUB_GMS_URL = $GmsUrl.TrimEnd("/")
$env:DATAHUB_TELEMETRY_ENABLED = "false"

Write-Host "[2/7] Checking local DataHub"
if (-not (Test-GmsHealth) -and $StartDataHub) {
    Invoke-Checked $ProjectDataHub @("docker", "quickstart")
    for ($attempt = 0; $attempt -lt 60 -and -not (Test-GmsHealth); $attempt++) {
        Start-Sleep -Seconds 2
    }
}
if (-not (Test-GmsHealth)) {
    throw "DataHub GMS is unavailable at $GmsUrl. Re-run with -StartDataHub or start it separately."
}

Write-Host "[3/7] Preparing dbt-labs/jaffle_shop_duckdb"
if (-not (Test-Path -LiteralPath (Join-Path $JaffleShopPath ".git"))) {
    if (Test-Path -LiteralPath $JaffleShopPath) {
        throw "JaffleShopPath exists but is not a Git checkout: $JaffleShopPath"
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $JaffleShopPath -Parent) | Out-Null
    Invoke-Checked "git" @(
        "clone", "--branch", "duckdb", "--single-branch",
        "https://github.com/dbt-labs/jaffle_shop_duckdb.git",
        $JaffleShopPath
    )
    Invoke-Checked "git" @(
        "-C", $JaffleShopPath, "checkout", "--detach", $JaffleShopCommit
    )
}
$ActualJaffleCommit = (& git -C $JaffleShopPath rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) {
    throw "Could not resolve the jaffle_shop_duckdb commit."
}
if ($ActualJaffleCommit -ne $JaffleShopCommit -and -not $AllowUnpinnedJaffleShop) {
    throw (
        "Expected jaffle_shop_duckdb commit $JaffleShopCommit but found " +
        "$ActualJaffleCommit. Use -AllowUnpinnedJaffleShop only if this is intentional."
    )
}
Invoke-Checked $DbtPythonCommand @(
    "-c",
    "import sys; assert sys.version_info >= (3, 12), 'Python 3.12+ is required'"
)
if (-not (Test-Path -LiteralPath $DbtPython)) {
    Invoke-Checked $DbtPythonCommand @("-m", "venv", $DbtVenv)
}
if (-not $SkipDependencyInstall) {
    Invoke-Checked $DbtPython @(
        "-m", "pip", "install", "-r",
        (Join-Path $JaffleShopPath "requirements.txt")
    )
}

Write-Host "[4/7] Building dbt models and documentation artifacts"
Invoke-Checked $DbtExecutable @("build") $JaffleShopPath
Invoke-Checked $DbtExecutable @("docs", "generate") $JaffleShopPath
$env:DBT_PROJECT_ROOT = $JaffleShopPath

Write-Host "[5/7] Ingesting dbt metadata into DataHub"
if (-not $SkipIngest) {
    Invoke-Checked $ProjectDataHub @("ingest", "run", "-c", $Recipe)
}

Write-Host "[6/7] Building and governing the DataHub context"
Invoke-Checked $ProjectPython @("src/context_builder.py", "customers")
Invoke-Checked $ProjectPython @("src/bootstrap_governance.py")

Write-Host "[7/7] Optional downstream impact graph"
if ($IncludeImpactDemo) {
    Invoke-Checked $ProjectPython @("src/bootstrap_impact_demo.py")
}

Write-Host "Bootstrap complete. Next:"
Write-Host "  $ProjectPython src/orchestrator.py run --require-clean-git"
Write-Host "Add --writeback only when you intentionally want to update DataHub."
