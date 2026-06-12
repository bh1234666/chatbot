"""Unified dedup/lifecycle tracker for tool-loop guidance hints.

Round 17 (#1, incremental): chat_with_tools_loop accumulated 15 SYSTEM_HINT
injection sites guarded by ~31 ad-hoc boolean/counter flags. Flags were
declared far from their hints, interactions were unaudited, and every new hint
hand-rolled its own dedup — the source of three guidance-conflict bugs this
week. This tracker centralizes "should this hint fire" bookkeeping; injection
call sites keep their conditions but route dedup through one object.

Usage inside the loop:
    guidance = GuidanceTracker()
    ...
    if <condition> and guidance.should_emit("main_final_contract_snapshot"):
        _append_tool_loop_dynamic_guidance(msgs, ...)

Hints default to once-per-loop. Hints that may re-fire on a new fact key use
should_emit(hint_id, key=...) — the same (hint_id, key) pair fires once.
Counter-limited hints use max_count.

统一管理 tool-loop 提示注入的去重与生命周期；替代散落的布尔旗标。
"""
from __future__ import annotations

from collections import Counter


class GuidanceTracker:
    """Per-loop-instance dedup bookkeeping for guidance hints."""

    __slots__ = ("_fired", "_counts")

    def __init__(self) -> None:
        self._fired: set[tuple[str, str]] = set()
        self._counts: Counter[str] = Counter()

    def should_emit(self, hint_id: str, *, key: str = "", max_count: int = 1) -> bool:
        """Return True (and record the firing) when the hint may emit.

        - Default: once per loop per hint_id.
        - key: scope dedup to (hint_id, key) — a new key re-arms the hint.
        - max_count: allow up to N firings for the same (hint_id, key).
        """
        slot = (hint_id, key)
        if self._counts[slot_key := f"{hint_id}\x00{key}"] >= max_count:
            return False
        self._counts[slot_key] += 1
        self._fired.add(slot)
        return True

    def has_fired(self, hint_id: str, *, key: str = "") -> bool:
        return (hint_id, key) in self._fired

    def fired_count(self, hint_id: str, *, key: str = "") -> int:
        return self._counts[f"{hint_id}\x00{key}"]

    def reset(self, hint_id: str, *, key: str = "") -> None:
        """Re-arm a hint (e.g., after the underlying fact changes)."""
        self._fired.discard((hint_id, key))
        self._counts[f"{hint_id}\x00{key}"] = 0

    def snapshot(self) -> dict[str, int]:
        """Debug view: hint -> total firings."""
        agg: dict[str, int] = {}
        for slot_key, n in self._counts.items():
            hint_id = slot_key.split("\x00", 1)[0]
            agg[hint_id] = agg.get(hint_id, 0) + n
        return agg
