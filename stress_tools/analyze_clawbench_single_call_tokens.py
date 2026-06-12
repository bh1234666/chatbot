#!/usr/bin/env python3
"""Analyze per-call token usage from ClawBench run-cache transcripts.

ClawBench result JSON aggregates token usage at the task/run level. For
"maximum context used in one model call", inspect each TranscriptMessage.usage
in the persisted TaskRunResult cache instead.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _usage(message: dict[str, Any]) -> dict[str, int]:
    raw = message.get("usage") or {}
    if not isinstance(raw, dict):
        raw = {}
    input_tokens = _as_int(raw.get("input_tokens", raw.get("input", raw.get("inputTokens", 0))))
    output_tokens = _as_int(raw.get("output_tokens", raw.get("output", raw.get("outputTokens", 0))))
    reasoning_tokens = _as_int(
        raw.get("reasoning_tokens", raw.get("reasoning", raw.get("reasoningTokens", 0)))
    )
    cache_read_tokens = _as_int(raw.get("cache_read_tokens", raw.get("cacheReadTokens", 0)))
    cache_write_tokens = _as_int(raw.get("cache_write_tokens", raw.get("cacheWriteTokens", 0)))
    total_tokens = _as_int(raw.get("total_tokens", raw.get("total", raw.get("totalTokens", 0))))
    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens + reasoning_tokens
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "total_tokens": total_tokens,
    }


def _iter_run_files(run_root: Path) -> list[Path]:
    cache = run_root / "data" / "run_cache"
    if cache.exists():
        return sorted(cache.rglob("run*.json"))
    return sorted(run_root.rglob("run*.json"))


def analyze(run_root: Path) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    task_totals: dict[str, dict[str, int]] = {}
    for run_file in _iter_run_files(run_root):
        try:
            run = json.loads(run_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        task_id = str(run.get("task_id") or run_file.parent.name)
        run_index = _as_int(run.get("run_index", 0))
        messages = ((run.get("transcript") or {}).get("messages") or [])
        if not isinstance(messages, list):
            continue
        for message_index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue
            usage = _usage(message)
            if not any(usage.values()):
                continue
            call = {
                "task_id": task_id,
                "run_index": run_index,
                "message_index": message_index,
                "role": message.get("role", ""),
                **usage,
                "run_file": str(run_file),
            }
            calls.append(call)
            row = task_totals.setdefault(
                task_id,
                {
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                    "total_tokens": 0,
                    "max_single_input_tokens": 0,
                    "max_single_total_tokens": 0,
                },
            )
            row["calls"] += 1
            for key in ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"):
                row[key] += usage[key]
            row["max_single_input_tokens"] = max(row["max_single_input_tokens"], usage["input_tokens"])
            row["max_single_total_tokens"] = max(row["max_single_total_tokens"], usage["total_tokens"])

    def max_by(key: str) -> dict[str, Any] | None:
        return max(calls, key=lambda item: item.get(key, 0), default=None)

    return {
        "run_root": str(run_root),
        "run_files": len(_iter_run_files(run_root)),
        "calls_with_usage": len(calls),
        "max_single_input_call": max_by("input_tokens"),
        "max_single_total_call": max_by("total_tokens"),
        "task_totals": task_totals,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("run_root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = analyze(args.run_root)
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
