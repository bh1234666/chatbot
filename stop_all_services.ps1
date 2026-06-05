param(
    [switch]$DryRun,
    [switch]$NoPause,
    [switch]$Check
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$Root = (Resolve-Path $PSScriptRoot).Path.TrimEnd('\')
$KnownPorts = @(8000, 8031, 8062, 8074, 8075, 8090, 8765, 51111)
$MineruStateFiles = @(
    "mineru\.mineru_api_pid",
    "mineru\.mineru_api_port",
    "mineru\.mineru_api_config.json"
)

if ($Check) {
    Write-Host "stop_all_services.ps1 OK"
    exit 0
}

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host $Text -ForegroundColor Cyan
}

function Short-Text([string]$Text, [int]$Max = 180) {
    if ([string]::IsNullOrWhiteSpace($Text)) { return "" }
    $oneLine = ($Text -replace "\s+", " ").Trim()
    if ($oneLine.Length -le $Max) { return $oneLine }
    return $oneLine.Substring(0, $Max) + "..."
}

function Invoke-KillProcessTree([int]$TargetPid, [string]$Reason) {
    if ($TargetPid -le 0 -or $TargetPid -eq $PID) { return }
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$TargetPid" -ErrorAction SilentlyContinue
    if (-not $proc) { return }

    Write-Host ("  kill PID {0,-6} {1,-14} {2}" -f $TargetPid, $proc.Name, $Reason)
    Write-Host ("       " + (Short-Text $proc.CommandLine 220)) -ForegroundColor DarkGray
    if ($DryRun) { return }
    & taskkill /PID $TargetPid /T /F *> $null
}

function Test-RelatedCommand([object]$Proc) {
    $cmd = [string]$Proc.CommandLine
    $exe = [string]$Proc.ExecutablePath
    $name = [string]$Proc.Name
    if ([string]::IsNullOrWhiteSpace($cmd) -and [string]::IsNullOrWhiteSpace($exe)) { return $false }
    if ([int]$Proc.ProcessId -eq $PID -or [int]$Proc.ParentProcessId -eq $PID) { return $false }

    $lcCmd = $cmd.ToLowerInvariant()
    $lcExe = $exe.ToLowerInvariant()
    $lcRoot = $Root.ToLowerInvariant()

    if ($lcCmd.Contains("\openai\codex\") -or $lcCmd.Contains("\codex\bin\") -or $lcExe.Contains("\openai\codex\") -or $lcExe.Contains("\microsoft vs code\")) {
        return $false
    }

    $serviceHostNames = @(
        "python.exe", "pythonw.exe", "node.exe", "nodejs.exe",
        "cmd.exe", "conhost.exe"
    )
    $batchNames = @(
        "start_backend.bat",
        "start_agent.bat",
        "start_qqbot.bat",
        "startbot.bat",
        "napcat.bat",
        "napcat.quick.bat"
    )

    if (($name -notin $serviceHostNames) -and -not ($name -like "NapCat*")) {
        $isProjectBatch = $false
        foreach ($m in $batchNames) {
            if ($lcCmd.Contains($m)) { $isProjectBatch = $true; break }
        }
        if (-not $isProjectBatch) { return $false }
    }

    $markers = @(
        "app.main:app",
        "agent_frontend\serve_frontend.py",
        "agent_frontend/serve_frontend.py",
        "napcat_bridge.py",
        "stress_tools.run_agent_longtest_no_group",
        "stress_tools.run_capability_regression",
        "stress_tools.run_focused_capability_regression",
        "stress_tools.run_three_project_longtests",
        "stress_tools.run_direct_complex_stress",
        "stress_tools.run_complex_long_stress",
        "stress_tools.run_environment_maintenance",
        "run_agent_longtest_no_group.py",
        "run_capability_regression.py",
        "run_focused_capability_regression.py",
        "run_three_project_longtests.py",
        "run_direct_complex_stress.py",
        "run_complex_long_stress.py",
        "run_environment_maintenance.py",
        "mineru.cli.fast_api",
        "mineru\bg_service.py",
        "mineru/bg_service.py",
        "ocr_headless.py",
        "ocr_worker.py",
        "tts_headless.py",
        "botctl_helper.py",
        "start_backend.bat",
        "start_agent.bat",
        "start_qqbot.bat",
        "startbot.bat",
        "napcat.bat",
        "napcat.quick.bat",
        "chatbot api",
        "chatbot backend api",
        "chatbot agent frontend",
        "chatbot qq bot",
        "napcat bridge",
        "napcat qq"
    )
    foreach ($m in $markers) {
        if ($lcCmd.Contains($m)) { return $true }
    }

    $projectRuntimeMarkers = @(
        "\mineru\py310\python.exe",
        "\umi-ocr\",
        "\ominvioce\",
        "\napcat\"
    )
    foreach ($m in $projectRuntimeMarkers) {
        if (($lcCmd.Contains($lcRoot) -or $lcExe.Contains($lcRoot)) -and ($lcCmd.Contains($m) -or $lcExe.Contains($m))) {
            return $true
        }
    }

    if (($name -in @("python.exe", "pythonw.exe")) -and $lcCmd.Contains("-m uvicorn") -and $lcCmd.Contains("app.main:app")) {
        return $true
    }
    return $false
}

function Get-ListeningPids([int[]]$Ports) {
    $pids = New-Object System.Collections.Generic.HashSet[int]
    foreach ($port in $Ports) {
        try {
            $rows = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop
            foreach ($row in $rows) {
                [void]$pids.Add([int]$row.OwningProcess)
            }
        } catch {
            $netstat = & netstat -ano -p tcp 2>$null | Select-String -Pattern (":$port\s+.*LISTENING\s+(\d+)")
            foreach ($line in $netstat) {
                $parts = ($line.Line -split "\s+") | Where-Object { $_ }
                if ($parts.Count -gt 0) {
                    [int]$pidValue = 0
                    if ([int]::TryParse($parts[-1], [ref]$pidValue)) {
                        [void]$pids.Add($pidValue)
                    }
                }
            }
        }
    }
    return @($pids)
}

function Stop-RecordedMineru {
    $mineruPy = Join-Path $Root "mineru\py310\python.exe"
    $venvPy = Join-Path $Root ".venv\Scripts\python.exe"
    $py = $null
    if (Test-Path $mineruPy) { $py = $mineruPy }
    elseif (Test-Path $venvPy) { $py = $venvPy }
    if (-not $py) { return }

    if ($DryRun) {
        Write-Host "  would call mineru.bg_service.stop_recorded_service()"
        return
    }
    $tmpOut = [System.IO.Path]::GetTempFileName()
    $tmpErr = [System.IO.Path]::GetTempFileName()
    try {
        $code = "import sys; sys.path.insert(0, r'$Root'); from mineru.bg_service import stop_recorded_service; print(stop_recorded_service())"
        $proc = Start-Process -FilePath $py -ArgumentList @("-c", $code) -WindowStyle Hidden -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr -PassThru
        if (-not $proc.WaitForExit(20 * 1000)) {
            Write-Host "  MinerU recorded-service stop timed out; killing stop helper" -ForegroundColor Yellow
            Invoke-KillProcessTree ([int]$proc.Id) "mineru stop helper timeout"
        }
    } catch {
        Write-Host ("  MinerU recorded-service stop failed: " + (Short-Text $_.Exception.Message 220)) -ForegroundColor Yellow
    } finally {
        Remove-Item -LiteralPath $tmpOut, $tmpErr -Force -ErrorAction SilentlyContinue
    }
}

function Remove-MineruState {
    foreach ($rel in $MineruStateFiles) {
        $path = Join-Path $Root $rel
        if (Test-Path $path) {
            Write-Host "  remove $rel"
            if (-not $DryRun) {
                Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

Write-Host ""
Write-Host "=============================================="
Write-Host "     Chatbot One-Click Stop / Cleanup"
Write-Host "=============================================="
Write-Host "Root: $Root"
if ($DryRun) { Write-Host "Mode: dry run, no process will be killed" -ForegroundColor Yellow }

Write-Step "[1/6] Stop recorded MinerU service tree"
Stop-RecordedMineru

Write-Step "[2/6] Stop known project processes by command line"
$all = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
$targets = New-Object System.Collections.Generic.HashSet[int]
foreach ($proc in $all) {
    if (Test-RelatedCommand $proc) {
        if ($proc.ProcessId -ne $PID) {
            [void]$targets.Add([int]$proc.ProcessId)
        }
    }
}
foreach ($pidValue in @($targets | Sort-Object)) {
    Invoke-KillProcessTree $pidValue "known chatbot service"
}

Write-Step "[3/6] Stop listeners on service ports"
$portPids = Get-ListeningPids $KnownPorts
foreach ($pidValue in $portPids) {
    $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$pidValue" -ErrorAction SilentlyContinue
    if (-not $proc) { continue }
    if ((Test-RelatedCommand $proc) -or ($pidValue -in $targets)) {
        Invoke-KillProcessTree $pidValue ("service port " + (($KnownPorts | Where-Object {
            try {
                (Get-NetTCPConnection -LocalPort $_ -State Listen -ErrorAction Stop | Where-Object OwningProcess -eq $pidValue)
            } catch { $false }
        }) -join ","))
    } else {
        Write-Host ("  keep PID {0,-6} {1,-14} listening on a known port but not recognized as this project" -f $pidValue, $proc.Name) -ForegroundColor Yellow
        Write-Host ("       " + (Short-Text $proc.CommandLine 220)) -ForegroundColor DarkGray
    }
}

Write-Step "[4/6] Stop bundled OCR/TTS runtimes"
$runtimeMarkers = @(
    (Join-Path $Root "mineru\py310"),
    (Join-Path $Root "umi-ocr"),
    (Join-Path $Root "ominvioce")
) | ForEach-Object { $_.ToLowerInvariant() }

$gpuMarkers = @("mineru.cli.fast_api", "ocr_headless.py", "ocr_worker.py", "tts_headless.py", "omnivoice")
foreach ($proc in (Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)) {
    $cmd = ([string]$proc.CommandLine).ToLowerInvariant()
    $exe = ([string]$proc.ExecutablePath).ToLowerInvariant()
    $matched = $false
    foreach ($m in $runtimeMarkers) {
        if ($cmd.Contains($m) -or $exe.Contains($m)) { $matched = $true; break }
    }
    foreach ($m in $gpuMarkers) {
        if ($cmd.Contains($m)) { $matched = $true; break }
    }
    if ($matched -and $proc.ProcessId -ne $PID) {
        Invoke-KillProcessTree ([int]$proc.ProcessId) "OCR/TTS runtime"
    }
}

Write-Step "[5/6] Remove service state files"
Remove-MineruState

Write-Step "[6/6] Remaining related processes"
$remaining = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { Test-RelatedCommand $_ }
if ($remaining) {
    $remaining | Select-Object ProcessId, Name, CommandLine | Format-Table -Wrap -AutoSize
} else {
    Write-Host "  none"
}

Write-Host ""
Write-Host "Known port listeners after cleanup:"
foreach ($port in $KnownPorts) {
    $rows = @()
    try { $rows = @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction Stop) } catch { $rows = @() }
    if ($rows.Count -eq 0) {
        Write-Host ("  {0}: free" -f $port)
    } else {
        foreach ($row in $rows) {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($row.OwningProcess)" -ErrorAction SilentlyContinue
            Write-Host ("  {0}: PID {1} {2}" -f $port, $row.OwningProcess, $proc.Name) -ForegroundColor Yellow
        }
    }
}

Write-Host ""
Write-Host "Cleanup complete."

if (-not $NoPause -and -not $env:CI) {
    Write-Host ""
    Write-Host "Press any key to continue . . ."
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
}
