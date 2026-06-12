param(
    [string[]]$Task = @("t1-fs-quick-note"),
    [int]$Runs = 1,
    [int]$Port = 8129,
    [string]$Python = "",
    [string]$Model = "chatbot-current-agent",
    [switch]$AllPublic,
    [switch]$NoCapabilityGating,
    [switch]$NoStartService
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
if (-not $Python) {
    $venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
    $Python = if (Test-Path $venvPython) { $venvPython } else { "python" }
}

$normalizedTasks = @()
foreach ($taskItem in $Task) {
    foreach ($taskId in (($taskItem -split ",") | ForEach-Object { $_.Trim() })) {
        if ($taskId) {
            $normalizedTasks += $taskId
        }
    }
}

$argsList = @(
    "stress_tools\run_clawbench_current_agent_scored.py",
    "--runs", "$Runs",
    "--port", "$Port",
    "--model", $Model
)
foreach ($taskId in $normalizedTasks) {
    $argsList += @("--task", $taskId)
}
if ($AllPublic) {
    $argsList += "--all-public"
}
if ($NoStartService) {
    $argsList += "--no-start-service"
}
if ($NoCapabilityGating) {
    $argsList += "--no-capability-gating"
}

Push-Location $repoRoot
try {
    & $Python @argsList
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
