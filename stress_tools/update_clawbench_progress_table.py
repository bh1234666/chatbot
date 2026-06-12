from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNS_ROOT = PROJECT_ROOT / "stress_tools" / "runs" / "clawbench_current_scored"
OUT_MD = PROJECT_ROOT / "stress_tools" / "CLAWBENCH_CURRENT_PROGRESS.md"
OUT_JSON = PROJECT_ROOT / "stress_tools" / "clawbench_current_progress.json"
REFERENCE_AGGREGATE = (
    PROJECT_ROOT
    / ".benchmarks"
    / "clawbench_original_runs"
    / "stability_parallel5_20260609_175622_aggregate.json"
)

_CACHE_STATS_RE = re.compile(
    r"\[llm\.cache_stats\].*?\[(?P<tag>[^\]]+)\]:.*?\bprompt=(?P<prompt>\d+)",
    re.IGNORECASE,
)
_SHAPE_RE = re.compile(
    r"\[llm\.prompt_cache_shape\]\s+(?P<label>\S+):.*?\bstatic=(?P<static>\d+)\s+dynamic=(?P<dynamic>\d+)",
    re.IGNORECASE,
)


@dataclass
class RunMetric:
    task_id: str
    run_id: str
    run_dir: Path
    score: float | None
    completion: float | None
    trajectory: float | None
    behavior: float | None
    duration_ms: float | None
    input_tokens: float | None
    output_tokens: float | None
    total_tokens: float | None
    max_main_prompt_tokens: int | None
    max_main_shape_chars: int | None
    reference_completed_runs: int | None = None
    reference_completed_fastest_ms: float | None = None
    reference_completed_best_score: float | None = None
    reference_completed_best_score_ms: float | None = None
    runtime_vs_reference_fastest: float | None = None


@dataclass
class ReferenceMetric:
    completed_runs: int
    completed_fastest_ms: float | None
    completed_best_score: float | None
    completed_best_score_ms: float | None


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _is_main_tag(tag: str) -> bool:
    low = str(tag or "").lower()
    return bool(low.startswith("main") or low in {"chat_stream", "json.round1_intent"})


def _is_main_shape(label: str) -> bool:
    low = str(label or "").lower()
    return "helper." not in low and (
        ".main" in low
        or low.endswith(".main")
        or "chat_stream" in low
        or "json.round1_intent" in low
    )


def _run_context_maxima(run_dir: Path) -> tuple[int | None, int | None]:
    max_prompt: int | None = None
    max_shape: int | None = None
    for log_path in sorted((run_dir / "debug_logs").glob("*.log")):
        try:
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                match = _CACHE_STATS_RE.search(line)
                if match and _is_main_tag(match.group("tag")):
                    value = int(match.group("prompt"))
                    max_prompt = value if max_prompt is None else max(max_prompt, value)
                shape = _SHAPE_RE.search(line)
                if shape and _is_main_shape(shape.group("label")):
                    value = int(shape.group("static")) + int(shape.group("dynamic"))
                    max_shape = value if max_shape is None else max(max_shape, value)
        except OSError:
            continue
    return max_prompt, max_shape


def _metrics_from_run(run_dir: Path) -> list[RunMetric]:
    summary = _read_json(run_dir / "current_agent_clawbench_scored.json")
    if not summary:
        summary = _read_json(run_dir / "summary.json")
    if not summary:
        return []
    max_prompt, max_shape = _run_context_maxima(run_dir)
    rows: list[RunMetric] = []
    for task in summary.get("task_results") or []:
        if not isinstance(task, dict):
            continue
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            continue
        rows.append(
            RunMetric(
                task_id=task_id,
                run_id=run_dir.name,
                run_dir=run_dir,
                score=_float(task.get("mean_task_score")),
                completion=_float(task.get("mean_completion_score")),
                trajectory=_float(task.get("mean_trajectory_score")),
                behavior=_float(task.get("mean_behavior_score")),
                duration_ms=_float(task.get("mean_duration_ms")),
                input_tokens=_float(task.get("mean_input_tokens")),
                output_tokens=_float(task.get("mean_output_tokens")),
                total_tokens=_float(task.get("mean_total_tokens")),
                max_main_prompt_tokens=max_prompt,
                max_main_shape_chars=max_shape,
            )
        )
    return rows


def _float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "1", "yes", "pass", "passed"}:
            return True
        if low in {"false", "0", "no", "fail", "failed"}:
            return False
    return None


def _is_reference_completed(task: dict[str, Any]) -> bool:
    """Return whether a reference run completed the task.

    Original ClawBench result files do not expose per-run delivery_outcome, but
    they do expose pass_at_1/pass_rate and completion. Failed or partial items
    should not define speed targets; completed items can be used for timing.

    原始结果无单次 delivery_outcome 时，用 pass_at_1/完成度判断是否完成。
    """
    pass_at_1 = _bool(task.get("pass_at_1"))
    if pass_at_1 is not None:
        return pass_at_1
    pass_rate = _float(task.get("pass_rate"))
    if pass_rate is not None:
        return pass_rate >= 1.0
    completion = _float(task.get("mean_completion_score"))
    return bool(completion is not None and completion >= 1.0)


def _load_reference_metrics(aggregate_path: Path = REFERENCE_AGGREGATE) -> dict[str, ReferenceMetric]:
    aggregate = _read_json(aggregate_path)
    result_jsons = aggregate.get("result_jsons") or []
    rows: dict[str, list[dict[str, float]]] = {}
    for raw_path in result_jsons:
        path = Path(str(raw_path))
        if not path.exists():
            continue
        result = _read_json(path)
        for task in result.get("task_results") or []:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("task_id") or "").strip()
            duration_ms = _float(task.get("mean_duration_ms"))
            score = _float(task.get("mean_task_score"))
            if not task_id or duration_ms is None or not _is_reference_completed(task):
                continue
            rows.setdefault(task_id, []).append({
                "duration_ms": duration_ms,
                "score": score if score is not None else 0.0,
            })

    out: dict[str, ReferenceMetric] = {}
    for task_id, items in rows.items():
        fastest = min(items, key=lambda item: item["duration_ms"])
        best = max(items, key=lambda item: (item["score"], -item["duration_ms"]))
        out[task_id] = ReferenceMetric(
            completed_runs=len(items),
            completed_fastest_ms=fastest["duration_ms"],
            completed_best_score=best["score"],
            completed_best_score_ms=best["duration_ms"],
        )
    return out


def _fmt_float(value: float | None, digits: int = 4) -> str:
    if value is None:
        return "-"
    return f"{value:.{digits}f}"


def _fmt_int(value: float | int | None) -> str:
    if value is None:
        return "-"
    return f"{int(round(float(value))):,}"


def _fmt_sec(ms: float | None) -> str:
    if ms is None:
        return "-"
    return f"{ms / 1000.0:.1f}s"


def latest_metrics(runs_root: Path = RUNS_ROOT) -> list[RunMetric]:
    latest: dict[str, RunMetric] = {}
    if not runs_root.exists():
        return []
    for run_dir in sorted((p for p in runs_root.iterdir() if p.is_dir()), key=lambda p: p.name):
        for metric in _metrics_from_run(run_dir):
            prior = latest.get(metric.task_id)
            if prior is None or metric.run_id >= prior.run_id:
                latest[metric.task_id] = metric
    return [latest[key] for key in sorted(latest)]


def _attach_reference_metrics(rows: list[RunMetric]) -> None:
    reference = _load_reference_metrics()
    for row in rows:
        ref = reference.get(row.task_id)
        if ref is None:
            continue
        row.reference_completed_runs = ref.completed_runs
        row.reference_completed_fastest_ms = ref.completed_fastest_ms
        row.reference_completed_best_score = ref.completed_best_score
        row.reference_completed_best_score_ms = ref.completed_best_score_ms
        if row.duration_ms is not None and ref.completed_fastest_ms:
            row.runtime_vs_reference_fastest = row.duration_ms / ref.completed_fastest_ms


def write_progress_table(
    *,
    runs_root: Path = RUNS_ROOT,
    out_md: Path = OUT_MD,
    out_json: Path = OUT_JSON,
) -> list[RunMetric]:
    rows = latest_metrics(runs_root)
    _attach_reference_metrics(rows)
    payload = [
        {
            "task_id": row.task_id,
            "run_id": row.run_id,
            "score": row.score,
            "completion": row.completion,
            "trajectory": row.trajectory,
            "behavior": row.behavior,
            "duration_ms": row.duration_ms,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "total_tokens": row.total_tokens,
            "max_main_prompt_tokens": row.max_main_prompt_tokens,
            "max_main_shape_chars": row.max_main_shape_chars,
            "reference_completed_runs": row.reference_completed_runs,
            "reference_completed_fastest_ms": row.reference_completed_fastest_ms,
            "reference_completed_best_score": row.reference_completed_best_score,
            "reference_completed_best_score_ms": row.reference_completed_best_score_ms,
            "runtime_vs_reference_fastest": row.runtime_vs_reference_fastest,
            "run_dir": str(row.run_dir),
        }
        for row in rows
    ]
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# CLAWBENCH Current Progress",
        "",
        "Latest scored run per task. Reference timing uses only completed original-agent runs (`pass_at_1`/completion), so failed or partial reference attempts do not define speed targets. `runtime/ref fastest` is current runtime divided by the fastest completed reference runtime; values above 1.0 are slower. `max_main_prompt_tokens` is parsed from main-process `llm.cache_stats prompt=...`; `max_main_shape_chars` is parsed from main-process prompt shape static+dynamic chars when present.",
        "",
        "| task | latest run | score | completion | trajectory | behavior | runtime | ref completed runs | ref fastest | runtime/ref fastest | ref best score | ref best score runtime | input tok | output tok | total tok | max main prompt tok | max main shape chars |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    row.task_id,
                    row.run_id,
                    _fmt_float(row.score),
                    _fmt_float(row.completion),
                    _fmt_float(row.trajectory),
                    _fmt_float(row.behavior),
                    _fmt_sec(row.duration_ms),
                    _fmt_int(row.reference_completed_runs),
                    _fmt_sec(row.reference_completed_fastest_ms),
                    _fmt_float(row.runtime_vs_reference_fastest, digits=2),
                    _fmt_float(row.reference_completed_best_score),
                    _fmt_sec(row.reference_completed_best_score_ms),
                    _fmt_int(row.input_tokens),
                    _fmt_int(row.output_tokens),
                    _fmt_int(row.total_tokens),
                    _fmt_int(row.max_main_prompt_tokens),
                    _fmt_int(row.max_main_shape_chars),
                ]
            )
            + " |"
        )
    out_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rows


def main() -> None:
    rows = write_progress_table()
    print(json.dumps({"rows": len(rows), "markdown": str(OUT_MD), "json": str(OUT_JSON)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
