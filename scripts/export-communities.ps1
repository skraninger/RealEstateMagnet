<#
.SYNOPSIS
    Exports community data to a Markdown (or CSV) table document.

.EXAMPLE
    .\scripts\export-communities.ps1
    # Writes Documents/communities.md

.EXAMPLE
    .\scripts\export-communities.ps1 -Format csv -Output data\communities.csv
#>
param(
    [ValidateSet("markdown", "csv")]
    [string]$Format = "markdown",

    [string]$Output = "",

    [string]$DbPath = "data\communities.db",

    [switch]$Verbose
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

if (-not $Output) {
    $Output = if ($Format -eq "csv") { "data\communities.csv" } else { "Documents\communities.md" }
}

$exportArgs = @(
    "-m", "modules.community.export",
    "--format", $Format,
    "--output", $Output,
    "--db-path", $DbPath
)
if ($Verbose) { $exportArgs += "--verbose" }

Push-Location $ProjectRoot
try {
    & $Python @exportArgs
    if ($LASTEXITCODE -ne 0) { Write-Error "Export exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}
