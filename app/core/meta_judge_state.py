from __future__ import annotations

_META_JUDGE_DECISIONS: dict[str, list[bool]] = {"high": [], "normal": []}
_META_JUDGE_BUFFER_MAX = 8


def record_cross_llm_outcome(priority: str, upgraded: bool) -> None:
    bucket = _META_JUDGE_DECISIONS.setdefault(priority, [])
    bucket.append(bool(upgraded))
    if len(bucket) > _META_JUDGE_BUFFER_MAX:
        del bucket[: len(bucket) - _META_JUDGE_BUFFER_MAX]


def should_skip_cross_llm(priority: str) -> tuple[bool, float]:
    bucket = _META_JUDGE_DECISIONS.get(priority, [])
    if len(bucket) < 4:
        return False, 0.0
    declined = sum(1 for upgraded in bucket if not upgraded)
    fp_rate = declined / len(bucket)
    return fp_rate >= 0.6, fp_rate


def reset_cross_llm_outcomes() -> None:
    _META_JUDGE_DECISIONS.clear()
    _META_JUDGE_DECISIONS.update({"high": [], "normal": []})
