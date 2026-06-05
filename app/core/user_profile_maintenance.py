from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.core import user_profile

log = logging.getLogger(__name__)


PROFILE_EXTRACTION_SYSTEM = (
    "Extract newly stated, explicit user preferences or stable long-term facts from this single turn.\n"
    "Be conservative: use only information the user directly stated or clearly confirmed in this turn. "
    "If the turn contains no clear new preference or fact, return empty arrays/objects.\n"
    "\n## Output Format\n"
    "Strict JSON, no markdown: "
    '{"preferences":{"code_style":"...","language_preference":"...",'
    '"response_length":"...","humor_tolerance":"..."},'
    '"interests":["..."],"avoid_topics":["..."],"long_term_facts":["..."]}\n'
    "\n## Field Meaning\n"
    "- preferences: concrete response-style preferences; leave unspecified fields empty.\n"
    "- interests: topics the user mentioned or showed interest in, up to 3 per extraction.\n"
    "- avoid_topics: topics the user explicitly prefers to keep out of proactive replies, up to 2 per extraction.\n"
    "- long_term_facts: stable facts about the user's work/tools/projects, up to 2 per extraction.\n"
    "- Skip sensitive/private identity details and short-lived moods.\n\n"
    "从单轮对话中保守抽取新增偏好、兴趣、少谈主题和长期事实。"
)


def _build_profile_extraction_user_payload(user_message: str, assistant_message: str) -> str:
    payload = {
        "assistant": str(assistant_message or "")[:1000],
        "user": str(user_message or "")[:1000],
    }
    return (
        "## Runtime Facts\n"
        + json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n\n输出本轮新增的稳定用户画像事实。"
    )


async def bg_user_profile_update(
    *,
    archive_id: str,
    user_id: str,
    user_message: str,
    assistant_message: str,
    trace_id: str,
    debug: Any | None = None,
) -> None:
    try:
        new_count = await user_profile.increment_chat_count(
            archive_id=archive_id,
            user_id=user_id,
        )
    except Exception:
        log.exception("[%s] user_profile increment_chat_count failed", trace_id)
        return

    if new_count % user_profile.EXTRACTION_INTERVAL != 0:
        return

    if debug is not None:
        debug.log(
            "user_profile.extract_trigger",
            f"chat_count={new_count} (every {user_profile.EXTRACTION_INTERVAL}), "
            f"running lite extraction",
        )
    try:
        from app.llm.model_pool import resolve_task
        from app.llm.client import _client_for_spec, _retry, _log_prompt_cache_shape, _record_response_usage

        spec = resolve_task("user_profile")
        client = _client_for_spec(spec)
        messages = [
            {"role": "system", "content": PROFILE_EXTRACTION_SYSTEM},
            {"role": "user", "content": _build_profile_extraction_user_payload(user_message, assistant_message)},
        ]
        _log_prompt_cache_shape(
            label="user_profile.extract",
            model=spec.model,
            messages=messages,
        )
        resp = await asyncio.wait_for(
            _retry(
                lambda: client.chat.completions.create(
                    model=spec.model,
                    messages=messages,
                    stream=False,
                    max_tokens=400,
                    extra_body={"thinking": {"type": "disabled"}},
                    response_format={"type": "json_object"},
                ),
                label="user_profile.extract",
                provider=getattr(spec, "provider", None),
            ),
            timeout=10.0,
        )
        _record_response_usage(resp, model=spec.model, tag="user_profile.extract")
        content = resp.choices[0].message.content or ""
        try:
            increments = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            log.warning("[%s] user_profile extraction returned non-JSON: %s", trace_id, content[:200])
            return
        if not isinstance(increments, dict):
            return
        ok = await user_profile.merge_into_profile(
            archive_id=archive_id,
            user_id=user_id,
            increments=increments,
        )
        if debug is not None:
            debug.log(
                "user_profile.extract.done",
                f"merge ok={ok}; got prefs={len(increments.get('preferences') or {})} "
                f"interests={len(increments.get('interests') or [])} "
                f"avoid={len(increments.get('avoid_topics') or [])} "
                f"facts={len(increments.get('long_term_facts') or [])}",
            )
    except asyncio.TimeoutError:
        log.warning("[%s] user_profile extraction timed out (>10s); skipped", trace_id)
    except Exception:
        log.exception("[%s] user_profile extraction failed (non-fatal)", trace_id)
