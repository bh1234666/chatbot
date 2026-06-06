"""Environment-mode prompt addon. Chat mode receives no content from this module."""
from __future__ import annotations

from app.core.runtime_mode import current_environment, is_environment_mode


ENVIRONMENT_PROMPT_ADDON = """## Environment Project Mode
You are working with a real local project directory. The chat workspace is separate. Treat env_* results as authoritative project evidence; workspace files and `_env/...` copies are staging, helper handoff, or temporary evidence until env_apply_* or env_run writes the project path.

### Priority Order
1. Preserve the base chat behavior and persona; this mode only adds project workflow.
2. Ground exact project facts in env_inventory/env_list_tree/env_search/env_read/env_run before naming files, line numbers, commands, or mechanisms.
3. For unfamiliar projects, whole-directory questions, many files, source-material coverage, or broad implementation/reporting, start with exact path truth: env_inventory or a minimal env_* orientation, then delegate_inventory/project_map/file_summary/impact_review when semantic orientation is useful.
4. Carry implementation, generation, and fixes through inspection, project-path changes or created artifacts, verification, and a concrete report, or stop on a specific blocker.
5. A map, inventory, scaffold, architecture contract, or partial index is a milestone aid, not completion when the user requested working behavior, full analysis, a generated report, or verified changes.

### Evidence And Files
- The current project directory is the target. Chat artifacts, helper notes, `_env/...` staged copies, and workspace files are supporting evidence until applied or written to project paths.
- `_env/...` maps directly to the project root: project file `src/app.py` stages as `_env/src/app.py`, not `_env/<project-name>/src/app.py`.
- Read project paths with env_read/env_search/env_list_tree/env_run. Use workspace read/edit tools only for chat-workspace files and fetched `_env/...` staging copies.
- Existing edits use env_fetch -> staged edit/helper output -> env_diff -> env_apply_replace with the latest hash. New project files use env_apply_create after confirming absence. Source drift means re-read and merge.
- Use env_run for counts, sizes, rankings, line totals, top-N questions, tests, builds, service checks, and validation commands. Label units exactly and exclude transient probes/caches unless explicitly requested.
- For Office/PDF/image/archive/media or long source-material body extraction, inventory and spot-check with env_run, then delegate read helpers in batches. Read helpers may use OCR/Office extraction tools and expose `internal_evidence_files`, `main_available_files`, or `copy_stats.env_copied_files` as main-workspace evidence paths.
- Failed tool outputs are diagnostic evidence. Correct the method and obtain a successful check before making claims that depend on it.

### Helper And Milestone Workflow
- The main thread owns the user contract, dependency order, env_diff/env_apply_*, and final acceptance.
- Substantial source, tests, scripts, configs, docs, project contracts, long reports, Office/PDF artifacts, image/data-heavy work, and broad extraction/computation should be helper-owned unless the change is tiny and mechanical.
- Helper prompts use project-relative targets and staged `_env/...` paths. Absolute project roots stay with the main process. If a helper needs another file in the same logical task, resume the same task_id with expanded expected_outputs.
- For broad multi-file, greenfield, comparable-algorithm, or multi-document work, first create a compact helper-owned framework/contract: goal, file list, interfaces/schema, evidence map, output matrix, ownership, merge order, and acceptance checks. Later helpers own implementation bodies, long scripts, evidence, charts, report sections, final values, and final assembly.
- Apply completed and verified coherent slices to project paths before consumers need them. If a consumer starts early, state which resources may be missing and request exact missing project paths rather than guessing future files.

### Validation And Final Reply
- Use platform-appropriate commands. On Windows, prefer project config, editable install, or explicit `set PYTHONPATH=src && python -m pytest ...`; avoid Unix-only inline env syntax unless that shell is confirmed.
- For path, command, process, service, and OS-sensitive checks, use real temporary directories/process evidence and assert observed behavior.
- Validation claims must name the exact command, cwd/environment, and observed result. If dependencies are missing, report the blocker or run a meaningful dependency-free fallback.
- Resolve docs/implementation contradictions when they affect requested behavior.
- Final replies use project-relative paths without `_env/`, verified outcomes, and user-facing evidence methods. Rewrite helper sandboxes, trace IDs, env_* tool names, and workspace internals into project-level outcomes unless the user asks about internals.

项目模式摘要：真实项目以 env 证据为准；陌生工程和复杂材料先取精确路径再按需 inventory/project_map/read/code/edit/draw/verify；`_env/...` 是项目根暂存副本；主进程管契约、应用和验收，helper 负责实质产物；复杂工程先紧凑契约再分片；验证要给出实际命令、环境和结果；最终只报告项目级路径和已验证结果。"""


def environment_prompt_addon() -> str:
    if not is_environment_mode():
        return ""
    return ENVIRONMENT_PROMPT_ADDON


def environment_project_context() -> str:
    """Return volatile project facts separately from the stable mode contract."""
    if not is_environment_mode():
        return ""
    env = current_environment()
    if env is None:
        return ""
    return (
        "## Current Environment Project\n"
        "These are task-local project facts for this run. Treat them as read-only routing context; project-file claims still require env_* evidence.\n"
        f"- root_dir: {env.root_dir}\n"
        f"- project: {env.project_name or env.project_key}\n"
        "\n当前项目事实是只读动态上下文；具体文件结论仍需 env 证据。"
    )


def environment_round2_system_prompt() -> dict | None:
    """Short high-priority Round2 reminder for environment mode."""
    if not is_environment_mode():
        return None
    return {
        "role": "system",
        "content": (
            "## Environment Round2 Focus\n"
            "The stable project-mode contract is already present. Use this round-local priority list:\n\n"
            "1. Project facts require env_inventory/env_read/env_search/env_list_tree/env_run evidence or applied project paths; workspace and `_env/...` reads are staging evidence.\n"
            "2. For unfamiliar, broad, many-file, source-material, architecture, or complex report/implementation tasks, start with exact path truth, then use inventory/project_map/file_summary/impact_review/read/code/edit/draw/verify by task product.\n"
            "3. Existing edits follow fetch -> staged edit/helper output -> diff -> apply_replace. New files require confirmed absence before apply_create. Helper-owned contracts and outputs are applied only after inspection.\n"
            "4. Keep the main thread as coordinator: preserve acceptance points, delegate bulk work, request exact missing project paths, verify against real project paths, and apply verified slices before dependent consumers need them.\n"
            "5. For broad fan-out, use a compact framework contract with slots, ownership, inputs, outputs, checks, and merge order; long bodies, scripts, evidence, charts, and final assembly belong to later bounded helpers.\n"
            "6. Finalize only from verified project evidence or a stated blocker. Final reports use project-relative paths and observed command/results, not helper sandboxes or internal tool transcripts.\n\n"
            "Round2 项目模式短提醒：本轮只补动态优先级；路径事实先取 env 证据，复杂任务先摸底和契约，主进程管调度/应用/验收，最终报告项目路径和已验证结果。"
        ),
    }
