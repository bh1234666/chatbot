from __future__ import annotations

from app.config import settings
from app.schemas.api import ResponsePlan, TendencyAnalysis


def fallback_plan_from_user(user_message: str, reason: str = "downstream LLM 失败") -> ResponsePlan:
    cleaned = (user_message or "").strip()
    marker = settings.memory_injection_marker
    if cleaned.startswith(marker) or marker in cleaned[:50]:
        cleaned = ""

    is_system_error = any(token in reason for token in (
        "round2 tool loop 异常", "downstream LLM 失败", "tool loop", "LLM 失败",
        "JSON", "解析失败", "parse failed",
    ))

    if is_system_error:
        return ResponsePlan(
            intent=(
                "This round did not reliably complete the request. Reply briefly in persona, "
                "state that the work was not completed, and invite retry or continuation.\n\n"
                "本轮未可靠完成，按人设简短说明并提示可重试或继续。"
            ),
            key_points=[
                "Keep the reply brief and evidence-based, using user-facing terms.\n用用户能理解的话简短说明。",
                "Offer retry or continuation when the user still needs the task.\n需要时提示可重试或继续。",
            ],
            tone="brief and honest",
            length_hint="short",
            avoid=[
                "Keep the content limited to the reliability status and next user action.\n只说明可靠性状态和下一步。",
                "Use neutral wording that does not claim completed review, active work, or hidden technical details.\n不声称已审阅、仍在工作或暴露内部细节。",
            ],
            callbacks=[],
            internal_note=f"Fallback: {reason}. Task not processed.",
            deliverables=[],
        )

    return ResponsePlan(
        intent=f"基于用户原文给出最佳回复(降级:{reason})",
        key_points=[cleaned[:200] if cleaned else "（用户消息为空）"],
        tone="自然",
        length_hint="短",
        avoid=[],
        callbacks=[],
        internal_note=f"fallback plan 触发:{reason}",
        deliverables=[],
    )


def build_recall_hint(tendency: TendencyAnalysis, *, user_message: str = "") -> str:
    topics = tendency.recall_topics or []
    layers = tendency.recall_layers or ["warm", "cold"]

    if not topics:
        return (
            "Round 1 marked `needs_recall=true`. Within the first three tool calls, use expand_warm or expand_cold "
            "to inspect relevant historical conversations or old task experience. Use key nouns from the user's question "
            "as the query when possible.\n\n"
            "需要召回历史时，前几个工具调用内先展开相关记忆。"
        )

    queries = ", ".join(f"{topic!r}" for topic in topics[:3])
    suggested = "expand_warm" if "warm" in layers else "expand_cold"
    return (
        f"Round 1 marked `needs_recall=true`. Relevant keywords: {queries}.\n"
        f"Within the first three tool calls, use {suggested} to inspect related history.\n\n"
        "用户问题关联历史经验，前几个工具调用内先展开相关记忆。"
    )
