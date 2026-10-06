<#
.SYNOPSIS
    Researches each target community one at a time, then updates the SQLite
    database and the Markdown document after each one.

.EXAMPLE
    .\scripts\gather-all.ps1
    .\scripts\gather-all.ps1 -Limit 5
#>
param(
    [int]$Limit = 0,
    [switch]$VerboseLog
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

Push-Location $ProjectRoot
try {
    $targets = Get-Content data\communities\target_list.json -Raw | ConvertFrom-Json
    $names = $targets.communities | Select-Object -ExpandProperty name
    if ($Limit -gt 0) { $names = $names | Select-Object -First $Limit }

    $i = 0
    foreach ($name in $names) {
        $i++
        Write-Host "`n===== [$i/$($names.Count)] $name =====" -ForegroundColor Cyan
        & $Python -m modules.community.research_engine --community $name --log-level INFO
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "Research failed for $name (exit $LASTEXITCODE) - continuing"
        }
        # Pull latest store data into SQLite, then refresh the Markdown doc
        & $Python -c 'from modules.community.database import CommunityDatabase; db=CommunityDatabase(); db.import_from_store(); print("imported")'
        & $Python -m modules.community.export --format markdown --output Documents\communities.md
    }
    Write-Host "`nDone. $i communities processed." -ForegroundColor Green
} finally {
    Pop-Location
}
