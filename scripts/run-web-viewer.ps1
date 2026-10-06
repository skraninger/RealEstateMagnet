#Requires -Version 5.1
<#
.SYNOPSIS
    Start the RealEstateMagnet web viewer

.DESCRIPTION
    Starts a local web server to view database contents.
    Opens http://localhost:8000 in your browser.

.PARAMETER Port
    Port to run the server on (default: 8000)

.PARAMETER NoBrowser
    Don't automatically open the browser

.EXAMPLE
    .\scripts\run-web-viewer.ps1
    
    Start the viewer and open browser

.EXAMPLE
    .\scripts\run-web-viewer.ps1 -Port 8080
    
    Start the viewer on port 8080

.EXAMPLE
    .\scripts\run-web-viewer.ps1 -NoBrowser
    
    Start the viewer without opening browser
#>

param(
    [int]$Port = 8000,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

# Navigate to project root
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Split-Path -Parent $ScriptDir
Set-Location $ProjectRoot

Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host "  RealEstateMagnet - Web Viewer" -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Starting web server on port $Port..." -ForegroundColor Green
Write-Host "URL: http://localhost:$Port" -ForegroundColor Green
Write-Host ""
Write-Host "Press Ctrl+C to stop the server" -ForegroundColor Yellow
Write-Host ""

# Open browser if not disabled
if (-not $NoBrowser) {
    Start-Sleep -Seconds 2
    Start-Process "http://localhost:$Port"
}

# Run the viewer
python run_viewer.py --port $Port
