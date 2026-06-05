param(
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..")).Path
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$DestRoot = Join-Path $Root "del\cleanup_$Stamp"

$GeneratedRootItems = @(
    ".benchmarks",
    ".pytest_cache",
    "__pycache__",
    # Backend state: archives, memory, environment-project mapping, uploads,
    # workspaces, logs, caches, and SQLite files. Secrets such as .env are kept.
    "data",
    "logs",
    "output",
    "tmp",
    "chatbot.db",
    "chatbot.db-shm",
    "chatbot.db-wal",
    "app.zip"
)

$GeneratedFrontendItems = @(
    "agent_frontend\dist",
    "agent_frontend\node_modules\.vite",
    "agent_frontend\.vite",
    "agent_frontend\.cache",
    "agent_frontend\__pycache__"
)

$GeneratedStressItems = @(
    "stress_tools\runs",
    "stress_tools\codex_bg",
    "stress_tools\__pycache__"
)

$GeneratedToolRuntimeItems = @(
    "mineru\output",
    "mineru\temp",
    "mineru\test_images",
    "mineru\__pycache__",
    "mineru\.mineru_api_config.json",
    "mineru\.mineru_api_pid",
    "mineru\.mineru_api_port",
    "mineru\.mineru_gpu.lock",
    "mineru\.mineru_service_start.lock",
    "ominvioce\_cache",
    "ominvioce\__pycache__",
    "ominvioce\_sample_cn_female.wav",
    "ominvioce\_sample_en_male.wav",
    "umi-ocr\__pycache__"
)

function Assert-InRoot {
    param([Parameter(Mandatory)][string]$Path)

    $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
    $rootWithSep = $Root.TrimEnd("\") + "\"
    if ($resolved -ne $Root -and -not $resolved.StartsWith($rootWithSep, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing path outside workspace: $resolved"
    }
    return $resolved
}

function Move-GeneratedPath {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Bucket
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return
    }

    $resolved = Assert-InRoot -Path $Path
    $rel = $resolved.Substring($Root.Length).TrimStart("\")
    if ($rel -eq "del" -or $rel.StartsWith("del\", [System.StringComparison]::OrdinalIgnoreCase)) {
        return
    }

    $targetDir = Join-Path $DestRoot $Bucket
    $safeName = ($rel -replace "[:\\/]+", "__")
    $target = Join-Path $targetDir $safeName
    $i = 1
    while (Test-Path -LiteralPath $target) {
        $target = Join-Path $targetDir "$safeName.$i"
        $i += 1
    }

    if ($DryRun) {
        Write-Host "DRYRUN`t$rel`t=>`t$Bucket\$(Split-Path -Leaf $target)"
        return
    }

    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
    Move-Item -LiteralPath $resolved -Destination $target
    Write-Host "MOVED`t$rel`t=>`t$Bucket\$(Split-Path -Leaf $target)"
}

Write-Host "Workspace: $Root"
if ($DryRun) {
    Write-Host "Mode: dry run"
} else {
    New-Item -ItemType Directory -Force -Path $DestRoot | Out-Null
    Write-Host "Destination: $DestRoot"
}

foreach ($item in $GeneratedRootItems) {
    Move-GeneratedPath -Path (Join-Path $Root $item) -Bucket "root"
}

foreach ($item in $GeneratedFrontendItems) {
    Move-GeneratedPath -Path (Join-Path $Root $item) -Bucket "agent_frontend"
}

foreach ($item in $GeneratedStressItems) {
    Move-GeneratedPath -Path (Join-Path $Root $item) -Bucket "stress_tools"
}

foreach ($item in $GeneratedToolRuntimeItems) {
    Move-GeneratedPath -Path (Join-Path $Root $item) -Bucket "tool_runtime_artifacts"
}

$PycacheRoots = @(
    "app",
    "scripts",
    "tests",
    "stress_tools"
)

foreach ($relRoot in $PycacheRoots) {
    $scanRoot = Join-Path $Root $relRoot
    if (-not (Test-Path -LiteralPath $scanRoot)) { continue }
    Get-ChildItem -LiteralPath $scanRoot -Directory -Recurse -Force -Filter "__pycache__" -ErrorAction SilentlyContinue |
        ForEach-Object {
            Move-GeneratedPath -Path $_.FullName -Bucket "pycache"
        }
    }

if (-not $DryRun) {
    $markerPath = Join-Path $Root "agent_frontend\.cleanup_state.json"
    $marker = [ordered]@{
        cleanup_id = $Stamp
        cleaned_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $markerPath) | Out-Null
    ($marker | ConvertTo-Json -Compress) | Set-Content -LiteralPath $markerPath -Encoding UTF8
    Write-Host "WROTE`tagent_frontend\.cleanup_state.json"

    $remaining = @()
    foreach ($item in $GeneratedRootItems + $GeneratedFrontendItems + $GeneratedStressItems + $GeneratedToolRuntimeItems) {
        $p = Join-Path $Root $item
        if (Test-Path -LiteralPath $p) {
            $remaining += $item
        }
    }
    if ($remaining.Count -gt 0) {
        Write-Host "Remaining generated paths:"
        $remaining | ForEach-Object { Write-Host "  $_" }
        exit 2
    }
}
