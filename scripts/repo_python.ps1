$ErrorActionPreference = "Stop"

$script:RepoRoot = Split-Path -Parent $PSScriptRoot
$script:BundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

function Test-PythonCandidate {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [string[]]$PrefixArgs = @()
    )

    try {
        $output = & $Executable @PrefixArgs -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $output) {
            return [pscustomobject]@{
                Executable = $Executable
                PrefixArgs = $PrefixArgs
                Resolved = ($output | Select-Object -First 1).Trim()
            }
        }
    } catch {
        return $null
    }

    return $null
}

function Get-RepoPython {
    $candidates = @()

    if ($env:CHATBOT_PYTHON) {
        $candidates += ,@($env:CHATBOT_PYTHON, @())
    }

    $venvPython = Join-Path $script:RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython) {
        $candidates += ,@($venvPython, @())
    }

    $candidates += ,@("python", @())
    $candidates += ,@("py", @("-3"))

    if (Test-Path -LiteralPath $script:BundledPython) {
        $candidates += ,@($script:BundledPython, @())
    }

    foreach ($candidate in $candidates) {
        $resolved = Test-PythonCandidate -Executable $candidate[0] -PrefixArgs $candidate[1]
        if ($resolved) {
            return $resolved
        }
    }

    throw "No usable Python interpreter found. Set CHATBOT_PYTHON or install Python."
}

$python = Get-RepoPython

$rawArguments = @($args)
$printPath = $false
$healthCheck = $false

while ($rawArguments.Count -gt 0) {
    $head = [string]$rawArguments[0]
    if ($head -eq "-PrintPath") {
        $printPath = $true
        if ($rawArguments.Count -eq 1) {
            $rawArguments = @()
        } else {
            $rawArguments = @($rawArguments[1..($rawArguments.Count - 1)])
        }
        continue
    }
    if ($head -eq "-HealthCheck" -or $head -eq "--health-check") {
        $healthCheck = $true
        if ($rawArguments.Count -eq 1) {
            $rawArguments = @()
        } else {
            $rawArguments = @($rawArguments[1..($rawArguments.Count - 1)])
        }
        continue
    }
    break
}

if ($healthCheck) {
    Write-Host "OK $($python.Resolved)"
    exit 0
}

if ($printPath) {
    Write-Output $python.Resolved
    exit 0
}

if (-not $rawArguments -or $rawArguments.Count -eq 0) {
    Write-Error "No Python arguments were provided."
    exit 2
}

& $python.Executable @($python.PrefixArgs) @rawArguments
exit $LASTEXITCODE
