<#
.SYNOPSIS
    RealEstateMagnet - Local Windows Development Setup
    Installs Docker Desktop, VS Code, Git, and required extensions via winget.

.DESCRIPTION
    Run this script as Administrator from the project root:

        Set-ExecutionPolicy Bypass -Scope Process -Force
        .\Installation\scripts\setup-local-windows.ps1

    What it installs:
        - Git for Windows
        - Docker Desktop (WSL 2 backend)
        - Visual Studio Code
        - VS Code extensions: Dev Containers, Python, SQLTools, Docker
        - Enables WSL2 (if not already enabled)

    Requirements:
        - Windows 10 21H2+ or Windows 11
        - winget (comes with Windows 11; install via App Installer on Win 10)
        - Run as Administrator
#>

#Requires -RunAsAdministrator

$ErrorActionPreference = "Stop"

# -- Colour helpers ------------------------------------------------------------
function Write-Step  { param($msg) Write-Host "`n>> $msg" -ForegroundColor Cyan }
function Write-OK    { param($msg) Write-Host "   [OK]   $msg" -ForegroundColor Green }
function Write-Warn  { param($msg) Write-Host "   [WARN] $msg" -ForegroundColor Yellow }
function Write-Fail  { param($msg) Write-Host "   [FAIL] $msg" -ForegroundColor Red }

# -- Check winget --------------------------------------------------------------
Write-Step "Checking winget availability"
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Fail "winget not found."
    Write-Warn "Install 'App Installer' from the Microsoft Store, then re-run this script."
    Write-Warn "  https://apps.microsoft.com/store/detail/app-installer/9NBLGGH4NNS1"
    exit 1
}
$wingetVer = (winget --version)
Write-OK "winget $wingetVer"

# -- Helper: install via winget if not already installed ----------------------
function Install-WingetPackage {
    param(
        [string]$PackageId,
        [string]$PackageName,
        [string]$CheckCommand = ""
    )

    if ($CheckCommand -and (Get-Command $CheckCommand -ErrorAction SilentlyContinue)) {
        Write-OK "$PackageName already installed -- skipping"
        return
    }

    Write-Host "   Installing $PackageName ..." -ForegroundColor White
    $null = winget install --id $PackageId --silent --accept-package-agreements --accept-source-agreements 2>&1
    if ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq -1978335189) {
        # -1978335189 = APPINSTALLER_ERROR_ALREADY_INSTALLED
        Write-OK "$PackageName installed"
    } else {
        Write-Warn "$PackageName install returned exit code $LASTEXITCODE"
        Write-Warn "Check manually: winget install --id $PackageId"
    }
}

# -- 1. Git -------------------------------------------------------------------
Write-Step "Installing Git for Windows"
Install-WingetPackage "Git.Git" "Git for Windows" "git"

# -- 2. WSL 2 -----------------------------------------------------------------
Write-Step "Enabling WSL 2 (required for Docker Desktop)"
$wslStatus = (wsl --status 2>&1)
if ($wslStatus -match "Default Version: 2") {
    Write-OK "WSL 2 already the default"
} else {
    Write-Host "   Enabling WSL feature..." -ForegroundColor White
    try {
        $null = wsl --install --no-distribution 2>&1
        wsl --set-default-version 2
        Write-OK "WSL 2 enabled"
        Write-Warn "A reboot may be required before Docker Desktop works correctly."
    } catch {
        Write-Warn "WSL 2 setup may need to be completed manually."
        Write-Warn "Run:  wsl --install && wsl --set-default-version 2"
    }
}

# -- 3. Docker Desktop --------------------------------------------------------
Write-Step "Installing Docker Desktop"
Install-WingetPackage "Docker.DockerDesktop" "Docker Desktop" "docker"

# -- 4. Visual Studio Code ----------------------------------------------------
Write-Step "Installing Visual Studio Code"
Install-WingetPackage "Microsoft.VisualStudioCode" "VS Code" "code"

# -- 5. VS Code Extensions ----------------------------------------------------
Write-Step "Installing VS Code extensions"
$extensions = @(
    @{ id = "ms-vscode-remote.remote-containers"; name = "Dev Containers" },
    @{ id = "ms-vscode-remote.remote-ssh";        name = "Remote - SSH" },
    @{ id = "ms-python.python";                   name = "Python" },
    @{ id = "ms-python.vscode-pylance";           name = "Pylance" },
    @{ id = "ms-python.black-formatter";          name = "Black Formatter" },
    @{ id = "ms-azuretools.vscode-docker";        name = "Docker" },
    @{ id = "mtxr.sqltools";                      name = "SQLTools" },
    @{ id = "mtxr.sqltools-driver-pg";            name = "SQLTools PostgreSQL driver" },
    @{ id = "eamodio.gitlens";                    name = "GitLens" }
)

# Refresh PATH so 'code' is available after fresh install
$env:PATH = [System.Environment]::GetEnvironmentVariable("PATH", "Machine") + ";" +
            [System.Environment]::GetEnvironmentVariable("PATH", "User")

foreach ($ext in $extensions) {
    Write-Host "   Installing $($ext.name)..." -ForegroundColor White
    $out = code --install-extension $ext.id --force 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-OK $ext.name
    } else {
        Write-Warn "$($ext.name): $out"
    }
}

# -- 6. Docker Desktop resource guidance --------------------------------------
Write-Step "Docker Desktop resource configuration"
Write-Warn "Please set Docker Desktop resources for optimal performance:"
Write-Host ""
Write-Host "   Docker Desktop -> Settings -> Resources:" -ForegroundColor Gray
Write-Host "     CPUs   : 4  (minimum 2)"               -ForegroundColor Gray
Write-Host "     Memory : 8 GB  (minimum 4 GB)"         -ForegroundColor Gray
Write-Host "     Disk   : 60 GB  (minimum 20 GB)"       -ForegroundColor Gray
Write-Host ""
Write-Host "   Then restart Docker Desktop."            -ForegroundColor Gray

# -- 7. Clone prompt ----------------------------------------------------------
Write-Step "Project setup"
$repoDir = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

if (Test-Path "$repoDir\.devcontainer") {
    Write-OK "Repository already present at: $repoDir"
} else {
    $clone = Read-Host "   Paste your git repository URL (or press Enter to skip)"
    if ($clone) {
        $dest = Read-Host "   Destination directory (e.g. C:\Projects)"
        git clone $clone "$dest\RealEstateMagnet"
        Write-OK "Cloned to $dest\RealEstateMagnet"
        $repoDir = "$dest\RealEstateMagnet"
    }
}

# Copy .env if it doesn't exist
if ((Test-Path "$repoDir\.env.example") -and -not (Test-Path "$repoDir\.env")) {
    Copy-Item "$repoDir\.env.example" "$repoDir\.env"
    Write-OK "Created .env from .env.example"
}

# -- Summary ------------------------------------------------------------------
Write-Host ""
Write-Host "--------------------------------------------------" -ForegroundColor Cyan
Write-Host " Setup complete!  Next steps:"                      -ForegroundColor Cyan
Write-Host "--------------------------------------------------"  -ForegroundColor Cyan
Write-Host ""
Write-Host "  1. Restart Docker Desktop (if just installed)"    -ForegroundColor White
Write-Host "  2. Open the project:  code $repoDir"              -ForegroundColor White
Write-Host "  3. VS Code will prompt 'Reopen in Container' -- click it" -ForegroundColor White
Write-Host "  4. First build ~5 min.  Then run tests inside the container:" -ForegroundColor White
Write-Host "       python -m pytest tests/ -v"                  -ForegroundColor Yellow
Write-Host ""
