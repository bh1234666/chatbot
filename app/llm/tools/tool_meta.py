from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SideEffect = Literal["none", "memory", "workspace", "external"]
PermissionName = Literal[
    "observe_only",
    "chat",
    "retrieve_memory",
    "read_group_file",
    "generate_file",
    "execute_command",
    "admin_config",
]


@dataclass(frozen=True)
class ToolMeta:
    name: str
    schema: dict
    read_only: bool
    side_effect: SideEffect
    requires_permission: PermissionName
    max_result_chars: int | None = None
    main_thread_allowed: bool = True


def schema_name(schema: dict) -> str:
    return str(schema["function"]["name"])


def tool_meta(
    schema: dict,
    *,
    read_only: bool,
    side_effect: SideEffect,
    requires_permission: PermissionName,
    max_result_chars: int | None = None,
    main_thread_allowed: bool = True,
) -> ToolMeta:
    return ToolMeta(
        name=schema_name(schema),
        schema=schema,
        read_only=read_only,
        side_effect=side_effect,
        requires_permission=requires_permission,
        max_result_chars=max_result_chars,
        main_thread_allowed=main_thread_allowed,
    )


def schemas_for_main_thread(metas: list[ToolMeta]) -> list[dict]:
    return [meta.schema for meta in metas if meta.main_thread_allowed]


def meta_by_name(metas: list[ToolMeta]) -> dict[str, ToolMeta]:
    return {meta.name: meta for meta in metas}


def validate_aliases(
    aliases: dict[str, tuple[str, dict]],
    blocked_aliases: dict[str, str],
    metas: list[ToolMeta],
) -> None:
    allowed = {meta.name for meta in metas if meta.main_thread_allowed}
    blocked_targets = set(blocked_aliases)
    for alias, (target, _) in aliases.items():
        if alias in blocked_targets:
            raise RuntimeError(f"tool alias '{alias}' is both allowed and blocked")
        if target not in allowed:
            raise RuntimeError(f"tool alias '{alias}' points to non-main-thread tool '{target}'")
