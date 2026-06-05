$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$commands = @(
    @("python", @("-m", "pytest", "tests/test_smoke.py", "tests/test_model_pool.py", "tests/test_stage_model_consistency.py", "tests/test_context_ordering.py", "tests/test_schema_defaults.py")),
    @("python", @("-m", "pytest", "tests/test_bg_tasks.py", "tests/test_memory_scope.py", "tests/test_migrations_startup.py", "tests/test_sql_translate_snapshot.py")),
    @("python", @("-m", "pytest", "tests/test_chat_sse_contract.py", "tests/test_file_policy.py", "tests/test_config.py", "tests/test_permissions.py")),
    @("python", @("-m", "pytest", "tests/test_tool_result_budget.py", "tests/test_tool_safety.py", "tests/test_workspace_command_risk.py", "tests/test_tool_meta.py")),
    @("python", @("-m", "pytest", "tests/test_group_files_sync.py", "tests/test_hooks.py", "tests/test_kb_claim.py", "tests/test_bridge_state_machine.py")),
    @("python", @("-m", "pytest", "tests/test_kb_placeholder_cleanup.py", "tests/test_kb_placeholder_cleanup_api.py")),
    @("python", @("tests/verify_imports.py")),
    @("python", @("tests/test_structure.py"))
)

foreach ($cmd in $commands) {
    $exe = $cmd[0]
    $args = $cmd[1]
    Write-Host "> $exe $($args -join ' ')"
    & $exe @args
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}

Write-Host "precheck passed"
