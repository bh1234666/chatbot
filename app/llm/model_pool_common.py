"""Shared model-pool API surface.

Concrete pool files define providers, model maps, and task routing. This module
keeps dataclasses and LLM facade signatures identical across all switchable
model pools.

具体模型池只定义供应商、模型映射和任务路由；公共接口在这里统一。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Callable


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    api_key: str
    base_url: str


@dataclass(frozen=True)
class ModelSpec:
    model: str
    reasoning: str
    provider: ProviderConfig


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
    resolver: Callable[[bool, str], ModelSpec] | None = None,
) -> dict:
    from app.llm.client import chat_json as _chat_json

    if model_spec is None and think is not None and tier is not None:
        if resolver is None:
            from app.llm.model_pool import resolve as resolver
        model_spec = resolver(think, tier)

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
    resolver: Callable[[bool, str], ModelSpec] | None = None,
) -> AsyncIterator[str]:
    from app.llm.client import chat_stream as _chat_stream

    if model_spec is None and think is not None and tier is not None:
        if resolver is None:
            from app.llm.model_pool import resolve as resolver
        model_spec = resolver(think, tier)

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
    resolver: Callable[[bool, str], ModelSpec] | None = None,
) -> str:
    from app.llm.client import chat_with_tools_loop as _chat_with_tools_loop

    if model_spec is None and think is not None and tier is not None:
        if resolver is None:
            from app.llm.model_pool import resolve as resolver
        model_spec = resolver(think, tier)

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
