param(
    [string]$Distro = "Ubuntu",
    [string]$Model = "deepseek/deepseek-v4-pro",
    [string]$RepoRoot = "F:\chatbot"
)

$ErrorActionPreference = "Stop"

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$worker = Join-Path $RepoRoot "stress_tools\run_clawbench_original_agent_parallel_worker.ps1"
$runsRoot = Join-Path $RepoRoot ".benchmarks\clawbench_original_runs"
New-Item -ItemType Directory -Force -Path $runsRoot | Out-Null

$launches = @()
for ($i = 1; $i -le 5; $i++) {
    $process = Start-Process powershell -ArgumentList @(
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        $worker,
        "-Index",
        "$i",
        "-Stamp",
        $stamp,
        "-Distro",
        $Distro,
        "-Model",
        $Model,
        "-RepoRoot",
        $RepoRoot
    ) -WindowStyle Hidden -PassThru
    $launches += [ordered]@{
        index = $i
        pid = $process.Id
        meta = (Join-Path $runsRoot "stability_parallel_${i}_$stamp.meta.json")
        log = (Join-Path $runsRoot "stability_parallel_${i}_$stamp.log")
    }
    Start-Sleep -Seconds 3
}

$launcherMeta = Join-Path $runsRoot "stability_parallel5_$stamp.launcher.json"
[ordered]@{
    status = "running"
    started_at = (Get-Date).ToString("o")
    stamp = $stamp
    model = $Model
    runs = 5
    mode = "five isolated one-run processes"
    launches = $launches
} | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 $launcherMeta

[ordered]@{
    stamp = $stamp
    launcher_meta = $launcherMeta
    launches = $launches
} | ConvertTo-Json -Depth 6
