"""
Model Pool — unified abstraction over LLM providers and models.

Replace this single file to switch provider / models / API keys.
All business code calls through this module, not directly through client.py.

Concepts:
  think    — reasoning-capable model (supports thinking/reasoning_effort)
  nonthink — fast model without reasoning (always reasoning="disabled")
  tier     — low / mid / high capability level within each type

Usage:
  from app.llm.model_pool import resolve_task, chat_json

  spec = resolve_task("round1_intent")  # → ModelSpec(model="gpt-5.5", reasoning="disabled", ...)
  result = await chat_json(msgs, model_spec=spec)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator

from app.config import settings

# ═══════════════════════════════════════════════════════════════
# PROVIDER CONFIG
# ═══════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class ProviderConfig:
    """API provider credentials and endpoint."""
    name: str          # "deepseek" | "openai" | "anthropic" | ...
    api_key: str
    base_url: str


@dataclass(frozen=True)
class ModelSpec:
    """Resolved model selection: which model, reasoning level, which provider."""
    model: str
    reasoning: str       # "disabled" | "low" | "high" | "max"
    provider: ProviderConfig


# ═══════════════════════════════════════════════════════════════
# PROVIDERS — add new providers here
# ═══════════════════════════════════════════════════════════════

DEEPSEEK = ProviderConfig(
    name="deepseek",
    api_key=settings.deepseek_api_key,
    base_url=settings.deepseek_base_url,
)

GPT55 = ProviderConfig(
    name="gpt55",
    api_key=settings.gpt55_api_key,
    base_url=settings.gpt55_base_url,
)

# ═══════════════════════════════════════════════════════════════
# MODEL MAPS
# ═══════════════════════════════════════════════════════════════

THINK: dict[str, tuple[str, ProviderConfig]] = {
    "low":  ("gpt-5.5", GPT55),
    "mid":  ("gpt-5.5", GPT55),
    "high": ("gpt-5.5", GPT55),
}

NONTHINK: dict[str, tuple[str, ProviderConfig]] = {
    "low":  ("gpt-5.5", GPT55),
    "mid":  ("gpt-5.5", GPT55),
    "high": ("gpt-5.5", GPT55),
}

# Reasoning effort — GPT-5.5 uses internal reasoning, no external effort knob.
# All tiers set to "disabled" (no DeepSeek-style thinking parameter sent).
REASONING: dict[str, str] = {
    "low":  "disabled",
    "mid":  "disabled",
    "high": "disabled",
}

# ═══════════════════════════════════════════════════════════════
# TASK → (think, tier) ASSIGNMENTS
# ═══════════════════════════════════════════════════════════════

TASK_TIER: dict[str, tuple[bool, str]] = {
    # ── Round 1: intent classification ──
    "round1_intent": (False, "low"),

    # ── Round 2 stages ──
    "round2_medium":        (False, "low"),
    "round2_medium_coding": (False, "mid"),
    "round2_hard":          (True,  "low"),
    "round2_veryhard":      (True,  "mid"),

    # ── Round 3: final reply ──
    "round3_easy":   (False, "low"),
    "round3_normal": (False, "mid"),

    # ── Helper tiers ──
    "helper_lite":              (False, "low"),
    "helper_full_coding":       (False, "mid"),
    "helper_full_coding_think": (True,  "low"),
    "helper_full_edit":         (False, "mid"),
    "helper_full_legacy_hard":  (True,  "high"),
    "helper_full_verify":       (False, "mid"),
    "helper_full_verify_think": (True,  "low"),
    "helper_full_read_hard":    (True,  "mid"),
    "helper_full_edit_hard":    (True,  "mid"),
    "helper_full_draw_hard":    (True,  "mid"),
    "helper_full_project_analysis_hard": (True, "mid"),
    "helper_full_tts_hard":     (False, "mid"),

    # ── Misc lightweight tasks ──
    "progress_message":      (False, "low"),
    "self_check_plan":       (False, "low"),
    "upgrade_assess":        (False, "low"),
    "plan_intent_assess":    (False, "low"),
    "user_profile":          (False, "low"),
    "office_tail_downgrade": (False, "low"),
}

# ═══════════════════════════════════════════════════════════════
# RESOLUTION
# ═══════════════════════════════════════════════════════════════

def resolve(think: bool, tier: str) -> ModelSpec:
    """Resolve (think, tier) → ModelSpec."""
    model, provider = THINK[tier] if think else NONTHINK[tier]
    reasoning = REASONING[tier] if think else "disabled"
    return ModelSpec(model=model, reasoning=reasoning, provider=provider)


def resolve_task(task: str) -> ModelSpec:
    """Resolve a named task → ModelSpec."""
    think, tier = TASK_TIER[task]
    return resolve(think, tier)


# ═══════════════════════════════════════════════════════════════
# UNIFIED API FACADE
# ═══════════════════════════════════════════════════════════════

async def chat_json(
    messages: list[dict],
    *,
    model_spec: ModelSpec | None = None,
    think: bool | None = None,
    tier: str | None = None,
    reasoning: str = "disabled",
    lite: bool = False,
    response_format_hint: str = "json",
    metrics_tag: str = "json",
) -> dict:
    """Ask model for structured JSON output.

    Accepts model_spec OR (think, tier) OR legacy (reasoning, lite).
    model_spec takes precedence; then think/tier; then legacy fallback.
    """
    from app.llm.client import chat_json as _chat_json

    if model_spec is None and think is not None and tier is not None:
        model_spec = resolve(think, tier)

    return await _chat_json(
        messages,
        reasoning=reasoning,
        lite=lite,
        model_spec=model_spec,
        response_format_hint=response_format_hint,
        metrics_tag=metrics_tag,
    )


async def chat_stream(
    messages: list[dict],
    *,
    model_spec: ModelSpec | None = None,
    think: bool | None = None,
    tier: str | None = None,
    reasoning: str = "disabled",
    lite: bool = False,
    abort_event=None,
) -> AsyncIterator[str]:
    """Stream tokens from model.

    Accepts model_spec OR (think, tier) OR legacy (reasoning, lite).
    """
    from app.llm.client import chat_stream as _chat_stream

    if model_spec is None and think is not None and tier is not None:
        model_spec = resolve(think, tier)

    async for tok in _chat_stream(
        messages,
        reasoning=reasoning,
        lite=lite,
        model_spec=model_spec,
        abort_event=abort_event,
    ):
        yield tok


async def chat_with_tools_loop(
    messages: list[dict],
    tools: list[dict],
    dispatcher,
    *,
    model_spec: ModelSpec | None = None,
    think: bool | None = None,
    tier: str | None = None,
    reasoning: str = "disabled",
    lite: bool = False,
    abort_event=None,
    progress_cb=None,
    tool_result_cb=None,
    finalize_kind: str = "json_plan",
    upgrade_signal: dict | None = None,
    max_iter: int | None = None,
    reasoning_callback=None,
    parallelizable: bool = True,
    task_id=None,
    helper_kind: str = "",
    chunk_callback=None,
) -> str:
    """Multi-turn tool-use loop with the model.

    Accepts model_spec OR (think, tier) OR legacy (reasoning, lite).
    """
    from app.llm.client import chat_with_tools_loop as _chat_with_tools_loop

    if model_spec is None and think is not None and tier is not None:
        model_spec = resolve(think, tier)

    content, _msgs = await _chat_with_tools_loop(
        messages,
        tools,
        dispatcher,
        reasoning=reasoning,
        lite=lite,
        model_spec=model_spec,
        abort_event=abort_event,
        progress_cb=progress_cb,
        tool_result_cb=tool_result_cb,
        finalize_kind=finalize_kind,
        upgrade_signal=upgrade_signal,
        max_iter=max_iter,
        reasoning_callback=reasoning_callback,
        parallelizable=parallelizable,
        task_id=task_id,
        helper_kind=helper_kind,
        chunk_callback=chunk_callback,
    )
    return content

