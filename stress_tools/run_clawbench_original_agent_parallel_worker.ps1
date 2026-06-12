param(
    [Parameter(Mandatory = $true)][int]$Index,
    [Parameter(Mandatory = $true)][string]$Stamp,
    [string]$Distro = "Ubuntu",
    [string]$Model = "deepseek/deepseek-v4-pro",
    [string]$RepoRoot = "F:\chatbot"
)

$ErrorActionPreference = "Stop"

function Convert-ToWslPath {
    param([string]$Path)
    $resolved = (Resolve-Path $Path).Path
    if ($resolved -match '^([A-Za-z]):\\(.*)$') {
        $drive = $matches[1].ToLowerInvariant()
        $rest = $matches[2] -replace '\\', '/'
        return "/mnt/$drive/$rest"
    }
    throw "Cannot convert path to WSL form: $resolved"
}

function Quote-Bash {
    param([string]$Value)
    return "'" + ($Value -replace "'", "'\''") + "'"
}

$runsRoot = Join-Path $RepoRoot ".benchmarks\clawbench_original_runs"
New-Item -ItemType Directory -Force -Path $runsRoot | Out-Null

$runId = "stability_parallel_${Index}_$Stamp"
$log = Join-Path $runsRoot "$runId.log"
$meta = Join-Path $runsRoot "$runId.meta.json"
$wslRepoRoot = Convert-ToWslPath $RepoRoot
$stateDir = "/root/clawbench_openclaw_state_$runId"
$emptyStateCacheDir = "/root/clawbench_empty_state_cache_$runId"
$resultName = "original_agent_deepseek_v4_pro_all-public_$runId.json"

[ordered]@{
    status = "running"
    launch_index = $Index
    started_at = (Get-Date).ToString("o")
    model = $Model
    runs = 1
    concurrency = 1
    state_dir = $stateDir
    log = $log
    result_name = $resultName
} | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $meta

$bashParts = @(
    "rm -rf $(Quote-Bash $stateDir)",
    "mkdir -p $(Quote-Bash $stateDir) $(Quote-Bash $emptyStateCacheDir)",
    "chmod 777 $(Quote-Bash $stateDir)",
    "export REPO_ROOT=$(Quote-Bash $wslRepoRoot)",
    "export MODEL=$(Quote-Bash $Model)",
    "export TASK=''",
    "export RUNS=1",
    "export CONCURRENCY=1",
    "export BROWSER_ENABLED=true",
    "export SKIP_BUILD=1",
    "export WSL_STATE_DIR=$(Quote-Bash $stateDir)",
    "export STATE_CACHE_DIR=$(Quote-Bash $emptyStateCacheDir)",
    "export RESULT_NAME=$(Quote-Bash $resultName)",
    "./stress_tools/run_clawbench_original_agent_wsl.sh"
)
$bashCommand = "cd $(Quote-Bash $wslRepoRoot) && " + ($bashParts -join " && ")

$exitCode = 1
try {
    & wsl -d $Distro -u root -- bash -lc $bashCommand *> $log
    $exitCode = $LASTEXITCODE
} catch {
    Add-Content -Path $log -Encoding UTF8 -Value $_.Exception.ToString()
    $exitCode = 1
}

$latestSummary = Get-ChildItem -Path $runsRoot -Filter summary.json -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -like "*$Stamp*" -or (Get-Content $_.FullName -Raw -ErrorAction SilentlyContinue) -like "*$resultName*" } |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

[ordered]@{
    status = $(if ($exitCode -eq 0) { "finished" } else { "failed" })
    exit_code = $exitCode
    launch_index = $Index
    started_at = (Get-Content $meta -Raw | ConvertFrom-Json).started_at
    finished_at = (Get-Date).ToString("o")
    model = $Model
    runs = 1
    concurrency = 1
    state_dir = $stateDir
    log = $log
    result_name = $resultName
    summary_json = $(if ($latestSummary) { $latestSummary.FullName } else { $null })
} | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $meta

exit $exitCode
