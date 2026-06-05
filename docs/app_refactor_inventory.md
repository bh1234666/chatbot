# App Refactor Inventory

This document records the current `app/` structure, prompt ownership, and safe
next split points. It is intentionally operational: each item should either be
done, testable, or a concrete candidate for a later batch.

## Completed In This Batch

- Removed generated `__pycache__` directories under `app/`.
- Moved helper model-visible system prompts and shared helper prompt fragments
  from `app/llm/tools/delegate.py` to `app/llm/tools/helper_prompt_catalog.py`.
- Kept compatibility re-exports in `delegate.py` for existing tests and runtime
  imports.
- Moved final English-first tool schema descriptions from
  `app/llm/tools/tool_schemas.py` to
  `app/llm/tools/tool_schema_descriptions.py`.
- Removed exact duplicate test files from `app/tests/` when the same file exists
  under root `tests/`.

## Prompt Ownership

- Helper system prompts:
  `app/llm/tools/helper_prompt_catalog.py`
- Tool schema descriptions:
  `app/llm/tools/tool_schema_descriptions.py`
- Tool schema objects:
  `app/llm/tools/tool_schemas.py`
- Environment mode prompt:
  `app/core/environment_prompt.py`
- Main orchestrator prompt fragments:
  `app/core/orchestrator_prompts.py`, with remaining inline fragments in
  `app/core/context.py` and orchestrator modules.
- Persona files:
  `personas/*.md`

All model-visible prompts should remain English-first with concise Chinese
summaries where useful.

## Largest Remaining Files

Approximate largest `app/` Python files after this batch:

- `app/llm/tools/delegate_actions.py` ~3476 lines
- `app/llm/tools/workspace.py` ~3352 lines
- `app/llm/client_tools_loop.py` ~3220 lines
- `app/llm/tools/office.py` ~3165 lines
- `app/llm/tools/registry.py` ~2945 lines
- `app/core/orchestrator_entry.py` ~2894 lines
- `app/core/orchestrator.py` ~2781 lines
- `app/llm/tools/delegate.py` ~2484 lines
- `app/llm/tools/tool_schemas.py` ~2306 lines
- `app/llm/tools/delegate_runner.py` ~2252 lines

## Safe Next Split Points

- Split `tool_schemas.py` by schema domain while preserving a re-export module:
  memory, workspace, file-read/edit, delegate/helper, office, media, and main
  process tools.
- Split `delegate_actions.py` by action family:
  spawn/collect/poll/status, validation/preflight, result assembly, and resource
  request handling.
- Split `workspace.py` into a compatibility facade plus action-specific modules.
  Much of this is already partly separated into `workspace_file_ops.py`,
  `workspace_run.py`, `workspace_text.py`, and utilities.
- Split `office.py` into a facade that dispatches to existing document, sheet,
  presentation, render, and verification modules.
- Move remaining main-process prompt literals from context/orchestrator files
  into a small prompt catalog once tests cover their exact injection behavior.

## Items Not Deleted Yet

- `app/tests/` still contains unique or divergent legacy tests. These should be
  migrated to root `tests/` or intentionally retired in a separate batch.
- API modules may look unreferenced to static grep because FastAPI imports and
  router registration are dynamic. Do not delete them without tracing
  `app/main.py` route includes and endpoint tests.
- Model pool variant files are runtime-switchable configuration variants; do not
  delete them as unused without checking the pool switching scripts.
