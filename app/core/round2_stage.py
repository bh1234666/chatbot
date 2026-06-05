from __future__ import annotations

from typing import Protocol


class Round2PlanLike(Protocol):
    upgrade_to_hard: bool
    upgrade_to_veryhard: bool


def progress_payload(kind: str, round_name: str, message: str, **extra) -> dict:
    return {"kind": kind, "round": round_name, "message": message, **extra}


R2_STAGE_TABLE: dict[str, dict] = {
    "medium": {
        "task_name": "round2_medium", "helper_lite": True, "max_iter": None,
        "section_title": "ROUND 2 — planning with tools (medium)",
        "progress_event": progress_payload("planning_tools", "planning", "正在规划要调用的工具"),
        "log_event": "round2.medium",
    },
    "medium_coding": {
        "task_name": "round2_medium_coding", "helper_lite": False, "max_iter": None,
        "section_title": "ROUND 2 — planning with tools (medium-coding)",
        "progress_event": progress_payload("planning_tools", "planning", "正在规划要调用的工具"),
        "log_event": "round2.medium_coding",
    },
    "hard": {
        "task_name": "round2_hard", "helper_lite": True, "max_iter": None,
        "section_title": "ROUND 2 — planning with tools (hard)",
        "progress_event": progress_payload("planning_tools", "planning", "正在规划要调用的工具"),
        "log_event": "round2.hard",
    },
    "veryhard": {
        "task_name": "round2_veryhard", "helper_lite": False, "max_iter": None,
        "section_title": "ROUND 2 — planning with tools (veryhard)",
        "progress_event": progress_payload("planning_tools", "planning", "正在规划要调用的工具"),
        "log_event": "round2.veryhard",
    },
}


def next_r2_stage(current: str, plan: Round2PlanLike, aborted: bool) -> str | None:
    if aborted:
        return None
    if current == "medium" and plan.upgrade_to_hard:
        return "hard"
    if current in ("medium_coding", "hard") and plan.upgrade_to_veryhard:
        return "veryhard"
    return None
