param(
    [string]$Distro = "Ubuntu",
    [string]$Model = "deepseek/deepseek-chat",
    [string]$Task = "t1-fs-quick-note",
    [switch]$AllTasks,
    [int]$Runs = 1,
    [int]$Concurrency = 1,
    [int]$GatewayPort = 18789,
    [switch]$Browser,
    [switch]$Build,
    [switch]$PrepareOnly
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

function Convert-FromWslPath {
    param([string]$Path)
    if ($Path -match '^/mnt/([a-zA-Z])/(.*)$') {
        $drive = $matches[1].ToUpperInvariant()
        $rest = $matches[2] -replace '/', '\'
        return "${drive}:\$rest"
    }
    return $Path
}

function Quote-Bash {
    param([string]$Value)
    return "'" + ($Value -replace "'", "'\''") + "'"
}

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
$wslRepoRoot = Convert-ToWslPath $repoRoot
$skipBuild = if ($Build) { "0" } else { "1" }
$prepare = if ($PrepareOnly) { "1" } else { "0" }
$effectiveTask = if ($AllTasks) { "" } else { $Task }

$envParts = @(
    "REPO_ROOT=$(Quote-Bash $wslRepoRoot)",
    "MODEL=$(Quote-Bash $Model)",
    "TASK=$(Quote-Bash $effectiveTask)",
    "RUNS=$Runs",
    "CONCURRENCY=$Concurrency",
    "GATEWAY_PORT=$GatewayPort",
    "BROWSER_ENABLED=$(if ($Browser) { 'true' } else { 'false' })",
    "SKIP_BUILD=$skipBuild",
    "PREPARE_ONLY=$prepare"
)

$bashCommand = "cd $(Quote-Bash $wslRepoRoot) && " + ($envParts -join " ") + " ./stress_tools/run_clawbench_original_agent_wsl.sh"

Write-Host "Running ClawBench original OpenClaw agent baseline via WSL native Docker..."
Write-Host "WSL distro: $Distro"
Write-Host "Model: $Model"
Write-Host "Task: $(if ($effectiveTask) { $effectiveTask } else { '(all public tasks)' })"

& wsl -d $Distro -u root -- bash -lc $bashCommand
$exitCode = $LASTEXITCODE

$runsRoot = Join-Path $repoRoot ".benchmarks\clawbench_original_runs"
$latestSummary = Get-ChildItem -Path $runsRoot -Filter summary.json -Recurse -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

if ($latestSummary) {
    $summary = Get-Content -Path $latestSummary.FullName -Raw | ConvertFrom-Json
    $resultPath = Convert-FromWslPath ([string]$summary.result_json)
    $runRoot = Convert-FromWslPath ([string]$summary.run_root)
    $singleCallUsagePath = $null
    $maxSingleInputTokens = $null
    $maxSingleTotalTokens = $null
    $maxSingleInputTask = $null
    $maxSingleTotalTask = $null
    $analyzer = Join-Path $repoRoot "stress_tools\analyze_clawbench_single_call_tokens.py"
    if ($runRoot -and (Test-Path $runRoot) -and (Test-Path $analyzer)) {
        $singleCallUsagePath = Join-Path $runRoot "data\single_call_token_usage.json"
        try {
            & python $analyzer $runRoot --output $singleCallUsagePath | Out-Null
            if (Test-Path $singleCallUsagePath) {
                $singleCallUsage = Get-Content -Path $singleCallUsagePath -Raw | ConvertFrom-Json
                if ($singleCallUsage.max_single_input_call) {
                    $maxSingleInputTokens = $singleCallUsage.max_single_input_call.input_tokens
                    $maxSingleInputTask = $singleCallUsage.max_single_input_call.task_id
                }
                if ($singleCallUsage.max_single_total_call) {
                    $maxSingleTotalTokens = $singleCallUsage.max_single_total_call.total_tokens
                    $maxSingleTotalTask = $singleCallUsage.max_single_total_call.task_id
                }
            }
        } catch {
            Write-Warning "Single-call token analysis failed: $($_.Exception.Message)"
        }
    }
    if ($resultPath -and (Test-Path $resultPath)) {
        $result = Get-Content -Path $resultPath -Raw | ConvertFrom-Json
        [ordered]@{
            status = $summary.status
            exit_code = $exitCode
            run_root = $runRoot
            result_json = $resultPath
            single_call_token_usage_json = $singleCallUsagePath
            model = $result.model
            task = $(if ($effectiveTask) { $effectiveTask } else { "(all public tasks)" })
            overall_score = $result.overall_score
            overall_completion = $result.overall_completion
            overall_trajectory = $result.overall_trajectory
            overall_behavior = $result.overall_behavior
            overall_reliability = $result.overall_reliability
            median_latency_ms = $result.overall_median_latency_ms
            tokens_per_pass = $result.overall_tokens_per_pass
            max_single_input_tokens = $maxSingleInputTokens
            max_single_input_task = $maxSingleInputTask
            max_single_total_tokens = $maxSingleTotalTokens
            max_single_total_task = $maxSingleTotalTask
            cost_per_pass_usd = $result.overall_cost_per_pass
        } | ConvertTo-Json -Depth 5
    } else {
        Get-Content -Path $latestSummary.FullName -Raw
    }
}

exit $exitCode
