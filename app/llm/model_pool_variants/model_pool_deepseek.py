"""DeepSeek-only model pool.

All named tasks route to DeepSeek models through the shared model-pool facade.

所有具名任务经由 model_pool_common 共享门面路由到 DeepSeek 模型。
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

ACTIVE = ProviderConfig(
    name="deepseek",
    api_key=settings.deepseek_api_key,
    base_url=settings.deepseek_base_url,
)

THINK: dict[str, str] = {
    "low": "deepseek-v4-pro",
    "mid": "deepseek-v4-pro",
    "high": "deepseek-v4-pro",
}

NONTHINK: dict[str, str] = {
    "low": "deepseek-v4-flash",
    "mid": "deepseek-v4-pro",
    "high": "deepseek-v4-pro",
}

REASONING: dict[str, str] = {
    "low": "low",
    "mid": "high",
    "high": "max",
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

def resolve(think: bool, tier: str) -> ModelSpec:
    model = THINK[tier] if think else NONTHINK[tier]
    reasoning = REASONING[tier] if think else "disabled"
    return ModelSpec(model=model, reasoning=reasoning, provider=ACTIVE)


def resolve_task(task: str) -> ModelSpec:
    think, tier = TASK_TIER[task]
    return resolve(think, tier)

