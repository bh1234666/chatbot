"""Mixed DeepSeek + GPT-5.5 model pool.

Round 1, easy Round 2, Round 3, and auto-continue checks stay on DeepSeek;
all other named tasks route to GPT-5.5 through the shared facade.

Round 1、简单 Round 2、Round 3 与 auto-continue 检查保留在 DeepSeek;其余具名任务经由共享门面路由到 GPT-5.5。
"""

from __future__ import annotations

from app.config import settings
from app.llm.model_pool_common import (
    ModelSpec,
    ProviderConfig,
    chat_json,
    chat_stream,
    chat_with_tools_loop,
)

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

THINK: dict[str, tuple[str, ProviderConfig]] = {
    "low": ("gpt-5.5", GPT55),
    "mid": ("gpt-5.5", GPT55),
    "high": ("gpt-5.5", GPT55),
}

NONTHINK: dict[str, tuple[str, ProviderConfig]] = {
    "low": ("gpt-5.5", GPT55),
    "mid": ("gpt-5.5", GPT55),
    "high": ("gpt-5.5", GPT55),
}

REASONING: dict[str, str] = {
    "low": "disabled",
    "mid": "disabled",
    "high": "disabled",
}

TASK_TIER: dict[str, tuple[bool, str]] = {
    "round1_intent": (False, "low"),
    "round2_medium": (False, "low"),
    "round2_medium_coding": (False, "mid"),
    "round2_hard": (True, "low"),
    "round2_veryhard": (True, "mid"),
    "round3_easy": (False, "low"),
    "round3_normal": (False, "mid"),
    "helper_lite": (False, "low"),
    "helper_full_coding": (False, "mid"),
    "helper_full_coding_think": (True, "low"),
    "helper_full_edit": (False, "mid"),
    "helper_full_legacy_hard": (True, "high"),
    "helper_full_verify": (False, "mid"),
    "helper_full_verify_think": (True, "low"),
    "helper_full_read_hard": (True, "mid"),
    "helper_full_edit_hard": (True, "mid"),
    "helper_full_draw_hard": (True, "mid"),
    "helper_full_project_analysis_hard": (True, "mid"),
    "helper_full_tts_hard": (False, "mid"),
    "progress_message": (False, "low"),
    "self_check_plan": (False, "low"),
    "upgrade_assess": (False, "low"),
    "plan_intent_assess": (False, "low"),
    "user_profile": (False, "low"),
    "office_tail_downgrade": (False, "low"),
    "auto_continue_check": (False, "low"),
}

TASK_MODEL_OVERRIDES: dict[str, ModelSpec] = {
    "round1_intent": ModelSpec("deepseek-v4-flash", "disabled", DEEPSEEK),
    "round2_medium": ModelSpec("deepseek-v4-flash", "disabled", DEEPSEEK),
    "round3_easy": ModelSpec("deepseek-v4-flash", "disabled", DEEPSEEK),
    "round3_normal": ModelSpec("deepseek-v4-pro", "disabled", DEEPSEEK),
    "auto_continue_check": ModelSpec("deepseek-v4-flash", "disabled", DEEPSEEK),
}


def resolve(think: bool, tier: str) -> ModelSpec:
    model, provider = THINK[tier] if think else NONTHINK[tier]
    reasoning = REASONING[tier] if think else "disabled"
    return ModelSpec(model=model, reasoning=reasoning, provider=provider)


def resolve_task(task: str) -> ModelSpec:
    if task in TASK_MODEL_OVERRIDES:
        return TASK_MODEL_OVERRIDES[task]
    think, tier = TASK_TIER[task]
    return resolve(think, tier)

