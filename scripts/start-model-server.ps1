<#
.SYNOPSIS
    Starts llama.cpp llama-server serving a Qwen GGUF for Workstream B (community research).

.DESCRIPTION
    Uses the local llama.cpp Vulkan build in -LlamaDir and a local GGUF from the
    Hugging Face cache (auto-located when -ModelPath is not given). No download
    happens: the model must already be cached.

    On success the OpenAI-compatible API is at http://localhost:<Port>/v1 with
    model name <ModelAlias>, matching MODEL_BASE_URL / MODEL_NAME in .env.example.

    Automatically loads the multimodal projector (mmproj) if found, enabling
    vision capabilities for screenshot analysis.

.PARAMETER LlamaDir
    Directory containing llama-server.exe (default: local unsloth build).

.PARAMETER ModelPath
    Full path to a .gguf file. When empty, the largest Qwen3.8-27B GGUF is
    auto-located under -HfCacheDir.

.PARAMETER HfCacheDir
    Hugging Face cache root used for model auto-discovery.

.PARAMETER ModelAlias
    Model name exposed by the OpenAI-compatible API (default qwen3.8-27b).

.PARAMETER Port
    API port (default 8080).

.PARAMETER CtxSize
    Context size (default 32768).

.PARAMETER GpuLayers
    Layers offloaded to the GPU via -ngl (default 99 = all; lower if VRAM is tight).

.PARAMETER MmprojPath
    Path to multimodal projector GGUF file. When empty, auto-located in HfCacheDir
    or LlamaDir. Enables vision capabilities for screenshot analysis.

.PARAMETER Device
    GPU device to use (default: Vulkan1 = Intel Arc Pro B70). Use --list-devices to see available devices.
#>
param(
    [string]$LlamaDir = "C:\Users\skran\.unsloth\llama.cpp\build\bin\Release",
    [string]$ModelPath = "",
    [string]$HfCacheDir = "C:\Users\skran\.cache\huggingface",
    [string]$ModelAlias = "qwen3.8-27b",
    [int]$Port = 8080,
    [int]$CtxSize = 32768,
    [int]$GpuLayers = 99,
    [string]$MmprojPath = "",
    [string]$Device = "Vulkan1"
)

$ErrorActionPreference = "Stop"

# Check if a model server is already running on the target port
Write-Host "Checking for existing model server on port $Port..."
$serverDetected = $false

# Method 1: Check if the port is responding
try {
    $existingServer = Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 "http://localhost:$Port/health"
    if ($existingServer.StatusCode -eq 200) {
        $serverDetected = $true
        Write-Host "  [OK] Server responding on port $Port" -ForegroundColor Green
    }
} catch {
    Write-Host "  [INFO] Port $Port not responding: $($_.Exception.Message)" -ForegroundColor Gray
}

# Method 2: Check for existing llama-server processes
$existingProcesses = Get-Process -Name "llama-server" -ErrorAction SilentlyContinue
if ($existingProcesses -and $existingProcesses.Count -gt 0) {
    Write-Host "  [INFO] Found $($existingProcesses.Count) llama-server process(es): $($existingProcesses.Id -join ', ')" -ForegroundColor Gray
    
    if (-not $serverDetected) {
        Write-Host "WARNING: Found llama-server process(es) but port $Port is not responding!" -ForegroundColor Yellow
        Write-Host "  - Server may still be loading the model"
        Write-Host "  - Starting a second instance would waste memory and cause conflicts"
        Write-Host ""
        Write-Host "Options:" -ForegroundColor Cyan
        Write-Host "  1. Wait for the existing server to finish loading (recommended)"
        Write-Host "  2. Stop existing servers and restart: Get-Process llama-server | Stop-Process -Force"
        Write-Host "  3. Use a different port: .\scripts\start-model-server.ps1 -Port <new_port>"
        Write-Host ""
        exit 0
    }
}

if ($serverDetected) {
    Write-Host "WARNING: A model server is already running on port $Port!" -ForegroundColor Yellow
    Write-Host "  - Server is healthy and responding"
    Write-Host "  - Starting a second instance would waste memory and cause conflicts"
    Write-Host ""
    Write-Host "Options:" -ForegroundColor Cyan
    Write-Host "  1. Use the existing server (recommended)"
    Write-Host "  2. Stop the existing server and restart with different parameters"
    Write-Host "  3. Use a different port: .\scripts\start-model-server.ps1 -Port <new_port>"
    Write-Host ""
    Write-Host "To stop the existing server, find and kill the llama-server.exe process:"
    Write-Host "  Get-Process llama-server | Stop-Process -Force"
    Write-Host ""
    exit 0
}

$exe = Join-Path $LlamaDir "llama-server.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    Write-Error "llama-server.exe not found at '$exe'. Set -LlamaDir to the directory containing llama-server.exe."
}

if ([string]::IsNullOrWhiteSpace($ModelPath)) {
    $candidate = Get-ChildItem -Path $HfCacheDir -Recurse -Filter "*.gguf" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -like "Qwen3.8-27B*" -and $_.Name -notlike "mmproj*" } |
        Sort-Object Length -Descending | Select-Object -First 1
    if (-not $candidate) {
        Write-Error "No Qwen3.8-27B GGUF found under '$HfCacheDir'. Pass -ModelPath explicitly."
    }
    $ModelPath = $candidate.FullName
}
if (-not (Test-Path -LiteralPath $ModelPath)) {
    Write-Error "Model file not found: '$ModelPath'"
}

# Auto-locate multimodal projector if not specified
if ([string]::IsNullOrWhiteSpace($MmprojPath)) {
    # First check in HfCacheDir alongside the model
    $mmprojCandidate = Get-ChildItem -Path $HfCacheDir -Recurse -Filter "mmproj*.gguf" -ErrorAction SilentlyContinue |
        Sort-Object Length -Descending | Select-Object -First 1

    # If not found, check in LlamaDir
    if (-not $mmprojCandidate) {
        $mmprojCandidate = Get-ChildItem -Path $LlamaDir -Filter "mmproj*.gguf" -ErrorAction SilentlyContinue |
            Select-Object -First 1
    }

    if ($mmprojCandidate) {
        $MmprojPath = $mmprojCandidate.FullName
    }
}

# Validate mmproj path if specified
if (-not [string]::IsNullOrWhiteSpace($MmprojPath) -and -not (Test-Path -LiteralPath $MmprojPath)) {
    Write-Warning "Multimodal projector file not found: '$MmprojPath'. Vision capabilities will be disabled."
    $MmprojPath = ""
}

Write-Host "Model:   $ModelPath"
if (-not [string]::IsNullOrWhiteSpace($MmprojPath)) {
    Write-Host "Mmproj:  $MmprojPath (vision enabled)"
} else {
    Write-Host "Mmproj:  not found (vision disabled)"
}

# Check for Vulkan DLL to verify GPU support
$vulkanDll = Join-Path $LlamaDir "ggml-vulkan.dll"
if (Test-Path $vulkanDll) {
    Write-Host "Vulkan:  detected (ggml-vulkan.dll present)" -ForegroundColor Green
} else {
    Write-Warning "Vulkan DLL not found - GPU acceleration may not work"
}

Write-Host "Starting llama-server in new window (port $Port, ctx $CtxSize, ngl $GpuLayers, alias $ModelAlias)"
Write-Host "Device: $Device (Intel Arc Pro B70)"
Write-Host "GPU layers: $GpuLayers (99 = offload all layers to GPU)"

# Build argument list
$argList = @("--model", $ModelPath, "--alias", $ModelAlias, "--port", "$Port", "-c", "$CtxSize", "-ngl", "$GpuLayers", "--device", $Device)

# Add mmproj if available
if (-not [string]::IsNullOrWhiteSpace($MmprojPath)) {
    $argList += "--mmproj"
    $argList += $MmprojPath
}

# Start in a new window so server output doesn't mix with discovery output
Write-Host "Launching llama-server..."
$proc = Start-Process -FilePath $exe `
    -ArgumentList $argList `
    -PassThru `
    -WindowStyle Normal

if (-not $proc) {
    Write-Error "Failed to start llama-server process."
    exit 1
}

Write-Host ""
Write-Host "Model server launched successfully" -ForegroundColor Green
Write-Host "  PID: $($proc.Id)"
Write-Host "  Port: $Port"
Write-Host "  Model: $ModelAlias"
Write-Host ""
Write-Host "The server is loading the model (this may take a few minutes)."
Write-Host "Other scripts will wait automatically for the server to be ready."
Write-Host ""
Write-Host "To monitor progress, check the new console window or visit:"
Write-Host ("  http://localhost:" + $Port + "/health")
Write-Host ""
exit 0
