from __future__ import annotations

from typing import Iterable


INEFFECTIVE_REPLY_PATTERNS: tuple[tuple[str, str], ...] = (
    ("没能真正完成", "admits_no_real_work"),
    ("没能真正处理", "admits_no_real_work"),
    ("没能真正执行", "admits_no_real_work"),
    ("没能真的完成", "admits_no_real_work"),
    ("我没有真正", "admits_no_real_work"),
    ("没有实际做", "admits_no_real_work"),
    ("没有实际完成", "admits_no_real_work"),
    ("没有实际处理", "admits_no_real_work"),
    ("没有实际操作", "admits_no_real_work"),
    ("没有实际帮", "admits_no_real_work"),
    ("没法真正", "admits_no_real_work"),
    ("无法真正", "admits_no_real_work"),
    ("没能实际", "admits_no_real_work"),
    ("没帮上忙", "admits_no_help"),
    ("过一会儿再发", "asks_retry_later"),
    ("稍后再发", "asks_retry_later"),
    ("脑子短路", "persona_failure_phrase"),
    ("接下来我会", "plan_instead_of_execution"),
    ("我会从几个方向", "plan_instead_of_execution"),
    ("我们来分步推进", "plan_instead_of_execution"),
    ("先看一下项目当前状态", "plan_instead_of_execution"),
    ("让我从检查现有代码开始", "plan_instead_of_execution"),
    ("然后逐个实现", "plan_instead_of_execution"),
    ("最后用 `python scripts/check_project.py`", "plan_instead_of_execution"),
)


def ineffective_reply_reasons(text: str, *, patterns: Iterable[tuple[str, str]] | None = None) -> list[str]:
    haystack = (text or "").strip()
    if not haystack:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for needle, reason in patterns or INEFFECTIVE_REPLY_PATTERNS:
        if needle and needle in haystack and reason not in seen:
            found.append(reason)
            seen.add(reason)
    return found


def is_effective_success(event: dict) -> bool:
    validation = event.get("validation_after")
    if isinstance(validation, dict) and validation.get("ok") is False:
        return False
    return event.get("ok") is True and not ineffective_reply_reasons(str(event.get("text") or ""))
