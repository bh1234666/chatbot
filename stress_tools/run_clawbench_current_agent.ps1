param(
    [double]$DurationMin = 1.0,
    [int]$Port = 8125,
    [string]$Python = "python",
    [switch]$NoStartService,
    [switch]$NoAutoContinue,
    [int]$MaxAutoContinueTurns = 1,
    [double]$MaxAutoContinueSec = 180.0,
    [double]$HealthTimeoutSec = 120.0
)

$ErrorActionPreference = "Stop"

function Get-ProjectFingerprint {
    param([string]$RepoRoot)
    $targets = @("app", "stress_tools")
    $files = foreach ($target in $targets) {
        $path = Join-Path $RepoRoot $target
        if (Test-Path $path) {
            Get-ChildItem -Path $path -Recurse -File |
                Where-Object { $_.FullName -notmatch '\\(__pycache__|runs)\\' -and $_.Extension -notin @(".pyc", ".log") }
        }
    }
    $hashInput = $files |
        Sort-Object FullName |
        ForEach-Object {
            $rel = $_.FullName.Substring($RepoRoot.Length).TrimStart("\")
            "$rel|$($_.Length)|$($_.LastWriteTimeUtc.Ticks)"
        }
    $bytes = [System.Text.Encoding]::UTF8.GetBytes(($hashInput -join "`n"))
    $hash = [System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)
    -join ($hash | ForEach-Object { $_.ToString("x2") })
}

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$resultDir = Join-Path $repoRoot "stress_tools\runs\current_agent_interface"
New-Item -ItemType Directory -Force -Path $resultDir | Out-Null
$resultJson = Join-Path $resultDir "current_agent_$stamp.json"
$sourceFingerprint = Get-ProjectFingerprint -RepoRoot $repoRoot

$argsList = @(
    "stress_tools\clawbench_chatbot_interface.py",
    "run",
    "--duration-min", "$DurationMin",
    "--port", "$Port",
    "--health-timeout-sec", "$HealthTimeoutSec",
    "--max-auto-continue-turns", "$MaxAutoContinueTurns",
    "--max-auto-continue-sec", "$MaxAutoContinueSec",
    "--result-json", $resultJson
)

if ($NoStartService) {
    $argsList += "--no-start-service"
}
if ($NoAutoContinue) {
    $argsList += "--no-auto-continue"
}

Write-Host "Running current project agent benchmark interface..."
Write-Host "Project: $repoRoot"
Write-Host "Source fingerprint: $sourceFingerprint"
Write-Host "Result JSON: $resultJson"

Push-Location $repoRoot
try {
    & $Python @argsList
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

if (Test-Path $resultJson) {
    $result = Get-Content -Path $resultJson -Raw | ConvertFrom-Json
    $result | Add-Member -NotePropertyName result_json -NotePropertyValue $resultJson -Force
    $result | Add-Member -NotePropertyName source_fingerprint -NotePropertyValue $sourceFingerprint -Force
    $result | Add-Member -NotePropertyName project_root -NotePropertyValue $repoRoot -Force
    $result | ConvertTo-Json -Depth 20 | Set-Content -Path $resultJson -Encoding UTF8
    [ordered]@{
        status = $(if ($exitCode -eq 0) { "finished" } else { "failed" })
        exit_code = $exitCode
        source_fingerprint = $sourceFingerprint
        result_json = $resultJson
        run_dir = $result.run_dir
        calls = $result.calls
        errors = $result.errors
        quality_issue_count = $result.quality_issue_count
        trace_path = $result.trace_path
        validation = $result.validation
    } | ConvertTo-Json -Depth 10
}

exit $exitCode
