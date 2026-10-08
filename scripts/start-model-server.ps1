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
    Context window size in tokens (default 65536).

    This was raised from 32768 because the research agent's prompts regularly
    exceeded the old window and llama.cpp rejected them with HTTP 400
    ("request (NNNNN tokens) exceeds the available context size"). The
    Qwen3.8-27B GGUF advertises a native context of 262144 tokens, so it can be
    raised much further — but the KV cache grows with the context and competes
    with the model weights for VRAM. If llama-server fails to load (out of
    memory), lower this value or reduce -GpuLayers.

.PARAMETER GpuLayers
    Layers offloaded to the GPU via -ngl (default 99 = all; lower if VRAM is tight).

.PARAMETER MmprojPath
    Path to multimodal projector GGUF file. When empty, auto-located in HfCacheDir
    or LlamaDir. Enables vision capabilities for screenshot analysis.

.PARAMETER Device
    GPU device to use (default: Vulkan1 = Intel Arc Pro B70). Use --list-devices to see available devices.

.PARAMETER ServerLog
    File that receives the llama-server console output (default:
    logs/llama-server.log, relative to the repo root). This is where you look
    for load failures and, most importantly, the HTTP 400 context-size errors
    that the pipeline reports. The file is truncated on every launch.
#>
param(
    # Directory containing llama-server.exe (and the ggml-*.dll runtime).
    [string]$LlamaDir = "C:\Users\skran\.unsloth\llama.cpp\build\bin\Release",

    # Full path to a .gguf model. Empty = auto-locate the largest
    # Qwen3.8-27B GGUF under -HfCacheDir.
    [string]$ModelPath = "",

    # Hugging Face cache root used for model auto-discovery.
    [string]$HfCacheDir = "C:\Users\skran\.cache\huggingface",

    # Model name exposed through the OpenAI-compatible API (must match
    # MODEL_NAME in .env).
    [string]$ModelAlias = "qwen3.8-27b",

    # API port (must match MODEL_BASE_URL in .env).
    [int]$Port = 8080,

    # Context window in tokens. Raised from 32768 to 65536 because research
    # prompts exceeded the old window (llama.cpp HTTP 400). Lower it if VRAM
    # is tight; raise it (model supports 262144) for longer research prompts.
    [int]$CtxSize = 65536,

    # Layers offloaded to the GPU via -ngl (99 = all; lower if VRAM is tight).
    [int]$GpuLayers = 99,

    # Multimodal projector GGUF for vision. Empty = auto-locate in HfCacheDir
    # or LlamaDir; vision is disabled when none is found.
    [string]$MmprojPath = "",

    # GPU device (default Vulkan1 = Intel Arc Pro B70). Use --list-devices.
    [string]$Device = "Vulkan1",

    # File receiving the server console output (default logs/llama-server.log).
    # Truncated on each launch; check it for load failures and HTTP 400 errors.
    [string]$ServerLog = ""
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

# Route the server console output to a log file so problems can be reviewed
# after the fact. The most useful entries are load failures and the HTTP 400
# "exceeds the available context size" rejections reported by the pipeline.
if ([string]::IsNullOrWhiteSpace($ServerLog)) {
    $repoRoot = Split-Path -Parent $PSScriptRoot
    $ServerLog = Join-Path $repoRoot "logs\llama-server.log"
}
$logDir = Split-Path -Parent $ServerLog
if ($logDir -and -not (Test-Path -LiteralPath $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}
# Truncate the previous log so each launch starts clean.
if (Test-Path -LiteralPath $ServerLog) {
    Remove-Item -LiteralPath $ServerLog -Force -ErrorAction SilentlyContinue
}

Write-Host "Starting llama-server in new window (port $Port, ctx $CtxSize, ngl $GpuLayers, alias $ModelAlias)"
Write-Host "Device: $Device (Intel Arc Pro B70)"
Write-Host "GPU layers: $GpuLayers (99 = offload all layers to GPU)"
Write-Host "Server log: $ServerLog"

# Build argument list
$argList = @("--model", $ModelPath, "--alias", $ModelAlias, "--port", "$Port", "-c", "$CtxSize", "-ngl", "$GpuLayers", "--device", $Device, "--log-file", $ServerLog)

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
Write-Host "Server output is also being written to:"
Write-Host "  $ServerLog"
Write-Host ""
exit 0
