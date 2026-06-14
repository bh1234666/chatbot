from __future__ import annotations

from app.schemas.api import ResponsePlan


def build_bot_log(
    plan: ResponsePlan | None,
    generated_files: list[tuple[str, str, str]] | None,
    complexity: str,
    was_aborted: bool,
    *,
    promoted_to_main: list[str] | None = None,
    helper_status: dict[str, dict] | None = None,
    internal_note: str = "",
) -> str:
    if (
        complexity == "easy"
        and not generated_files
        and not helper_status
        and not promoted_to_main
        and not (plan and plan.delivery_partial)
        and not (plan and plan.intent)
        and not (plan and plan.key_points)
        and not (internal_note and len(internal_note) > 5)
        and not was_aborted
    ):
        return ""
    parts: list[str] = []
    parts.append(f"complexity={complexity}")
    if was_aborted:
        parts.append("aborted=true")
    if plan and plan.intent:
        parts.append(f"intent={plan.intent[:80]}")
    if plan and plan.key_points:
        key_points = "; ".join(point[:40] for point in plan.key_points[:5])
        parts.append(f"key_points=[{key_points}]")
    if generated_files:
        names = ",".join(file_name for file_name, _url, _path in generated_files[:8])
        parts.append(f"deliverables=[{names}]")
    if plan and plan.delivery_partial:
        parts.append(f"delivery_partial=[{','.join(plan.delivery_partial[:5])}]")
    if promoted_to_main:
        parts.append(f"in_main=[{','.join(promoted_to_main[:6])}]")
    if helper_status:
        done_units = [task for task, state in helper_status.items() if state.get("status") == "done"]
        running_units = [task for task, state in helper_status.items() if state.get("status") == "running"]
        failed_units = [
            task for task, state in helper_status.items()
            if state.get("status") in ("failed", "aborted", "stuck")
        ]
        if done_units or running_units or failed_units:
            segments = []
            if done_units:
                segments.append(f"done:[{','.join(done_units[:5])}]")
            if running_units:
                segments.append(f"running:[{','.join(running_units[:5])}]")
            if failed_units:
                segments.append(f"failed:[{','.join(failed_units[:5])}]")
            parts.append("processing_records={" + ",".join(segments) + "}")
    if internal_note and len(internal_note) > 5:
        parts.append(f"note={internal_note[:120]}")
    return " | ".join(parts)[:800]
