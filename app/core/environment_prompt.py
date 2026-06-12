"""Environment-mode prompt addon. Chat mode receives no content from this module."""
from __future__ import annotations

from app.core.runtime_mode import current_environment, is_environment_mode


ENVIRONMENT_PROMPT_ADDON = """## Environment Project Mode
maintaining a real local project. Facts:

### Facts
- env_inventory/env_list_tree/env_search/env_read/env_run are authoritative project facts. For coding/debugging, route code helpers from discovery facts; helpers own source/tests.
- The chat workspace is separate from the project. Workspace files and `_env/...` are staging, handoff, or evidence until env_apply_* or env_run writes a project path.
- `_env/...` mirrors the project root only after staging: `src/app.py` stages as `_env/src/app.py`.
- Read project paths with env_* tools. read_file/search_in_file/code_index/edit_file/multi_edit work on chat-workspace or staged `_env/...` copies.
- workspace.mkdir creates chat-workspace folders only; env_apply_create creates project parent directories for confirmed new files.
- Existing edits use env_fetch -> staged helper output -> env_diff -> env_apply_replace with latest hash. New files use env_apply_create after confirmed absence.
- A helper output with a bare filename is a chat-workspace artifact; project-visible outputs use `_env/...` or env_apply_*.
- Failed tool results are diagnostic evidence; use facts, then verify claims.

### Work
- For unfamiliar projects, whole-directory questions, many-file/source/report/implementation work, start with orientation, then project_map/file_summary/impact_review/read/code/edit/draw/verify. `delegate_inventory` is only a shortcut tool that spawns an `inventory` helper; not a kind.
- Maps, inventories, scaffolds, contracts, and indexes are milestones, not completion, when behavior, full analysis, artifacts, or verified changes were requested.
- Main owns contract, order, env_diff/env_apply_*, and compact acceptance summaries; helper `_env` edits do not affect a local URL before env_apply_*. Helpers own source edits, tests, docs, reports, Office/PDF/media artifacts, extraction, computation, charts, citations, final tables, assembly, and self-checks. Pass paths, facts, checks.
- If helper outputs must satisfy project checks, declare `_env/...` targets in `expected_outputs`; bare filenames are chat-workspace evidence/artifacts. For `kind=read`, `.txt` evidence is helper-local, not `_env/...`, unless downstream work intentionally turns it into a project artifact.
- Bulk source-material extraction belongs to helpers; env_run may spot-check.
- Broad multi-file/greenfield/comparable-algorithm/multi-document work first creates a compact framework contract: goal, files, schema, evidence map, output matrix, ownership, merge order, checks. It defines bounded slots, not final content; prose, claims, citations, values, and assembly belong to producer helpers.
- Reusable written deliverables (reports, explainers, guides, briefs, cited syntheses) may need verifier-visible project files. Treat as project-state fact, not an automatic decision; if durable state is needed, create with env_apply_* and verify.
- Apply helper-completed coherent slices to project paths after env_diff/env_apply facts; use producer or verify-helper evidence for content quality.
- Artifacts preserve literal anchors: names, phrases, dates, amounts, labels, paths, expected terms.

### Validate
- env_run executes in the real project directory. Use it for inventory, counts, locating, services, spot-checks, narrow main-owned checks, and explicit main-thread checks. For coding/debugging, pass checks to code/verify helpers and consume compact results. Python inspection scripts go in env_run python_code outside the project tree; inspection scripts are not project files, and transient inspection scripts are excluded from deliverables. Label units exactly: characters, bytes, file size, line count, file count.
- Use platform-appropriate commands. Prefer `env_run` `python_code` for portable capture; on Windows, prefer project config, editable install, or `set PYTHONPATH=src && python -m pytest ...`.
- Validation claims must name the exact command, cwd/environment, and observed result; missing dependencies are blockers or fallbacks.
- For new or substantially rewritten source, tests, scripts, configs, docs, contracts, outlines, or frameworks, delegate authoring and keep long file bodies out of main-thread tool calls.
- Final replies use project-relative paths without `_env/`, verified outcomes, commands, and evidence methods. Treat `_env/...` as internal staging path. Hide helper sandboxes, trace IDs, and internals unless asked.

项目事实以 env 证据为准；`_env/...` 暂存。"""


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
            "Project file paths are evidence, not guesses. Use env_* for facts and workspace/_env only for existing staged copies/handoff. Tool failures may add path/schema facts dynamically; use those facts.\n\n"
            "Priorities:\n"
            "1. Resolve paths/checks; choose project_map/file_summary/impact_review/read/code/edit/draw/verify. `delegate_inventory` is only a shortcut tool that spawns an `inventory` helper; env_run uses the real project tree.\n"
            "2. Keep the main thread light: preserve acceptance checklist/routing, delegate bulk authoring/extraction/computation and edits/tests, inspect outputs, apply verified slices.\n"
            "3. Narrow repairs: delegate code after likely paths/checks; a batch of main-thread source/test reads usually duplicates helper work. Pass failures as facts.\n"
            "4. Broad fan-out uses a compact framework contract: slots, ownership, inputs, outputs, checks, merge order.\n"
            "5. Written deliverables compare inline-chat sufficiency against project/verifier visibility; if durable state is needed, use env_apply_* and verify.\n"
            "6. Real project deliverables use env_apply_create or env_apply_replace; workspace.write is chat-workspace scratch. Bare helper filenames and workspace.write are chat-workspace artifacts, not project-verifier-visible files. Project outputs use `_env/...`; read-helper `.txt` evidence stays helper-local unless converted.\n"
            "7. Use env_run python_code for inspection scripts outside the project tree; inspection scripts are not project files. Label units exactly, exclude transient inspection scripts, and run project pytest from the project's own root.\n"
            "8. Finalize only from verified project evidence or a blocker. Final reports use project-relative paths without `_env/` plus observed commands/results.\n\n"
            "摘要：env 证据；helper；验收。"
        ),
    }
