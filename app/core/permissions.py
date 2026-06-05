from __future__ import annotations

from enum import IntEnum

from app.llm.tools.tool_meta import ToolMeta


class PermissionLevel(IntEnum):
    observe_only = 0
    chat = 10
    retrieve_memory = 20
    read_group_file = 30
    generate_file = 40
    execute_command = 50
    admin_config = 60


TOOL_PERMISSIONS: dict[str, PermissionLevel] = {
    "expand_warm": PermissionLevel.retrieve_memory,
    "expand_cold": PermissionLevel.retrieve_memory,
    "expand_kb": PermissionLevel.retrieve_memory,
    "search_files": PermissionLevel.retrieve_memory,
    "fetch_indexed_file": PermissionLevel.read_group_file,
    "fetch_group_file": PermissionLevel.read_group_file,
    "read_file": PermissionLevel.generate_file,
    "search_in_file": PermissionLevel.generate_file,
    "code_index": PermissionLevel.generate_file,
    "read_function": PermissionLevel.generate_file,
    "search_across_files": PermissionLevel.generate_file,
    "fetch_to_temp": PermissionLevel.generate_file,
    "workspace": PermissionLevel.generate_file,
    "commit_to_main": PermissionLevel.generate_file,
    "inspect_file": PermissionLevel.generate_file,
    "delegate": PermissionLevel.generate_file,
    "processes": PermissionLevel.generate_file,
    "todo_write": PermissionLevel.chat,
    "todo_read": PermissionLevel.chat,
    "recall_thread": PermissionLevel.chat,
    "mark_avoid_mention": PermissionLevel.chat,
    "ask_user_question": PermissionLevel.chat,
    "ocr": PermissionLevel.generate_file,
    "tts": PermissionLevel.generate_file,
}


def sync_tool_permissions_from_meta(metas: list[ToolMeta]) -> None:
    for meta in metas:
        try:
            TOOL_PERMISSIONS[meta.name] = PermissionLevel[meta.requires_permission]
        except KeyError as exc:
            raise RuntimeError(
                f"tool meta for {meta.name!r} uses unknown permission {meta.requires_permission!r}"
            ) from exc


def parse_permission_level(value: str | PermissionLevel | None) -> PermissionLevel:
    if isinstance(value, PermissionLevel):
        return value
    if not value:
        return PermissionLevel.generate_file
    key = str(value).strip().lower()
    try:
        return PermissionLevel[key]
    except KeyError:
        return PermissionLevel.generate_file


def required_permission_for_tool(tool_name: str) -> PermissionLevel:
    return TOOL_PERMISSIONS.get(tool_name, PermissionLevel.generate_file)


def check_tool_permission(tool_name: str, granted: str | PermissionLevel | None) -> tuple[bool, PermissionLevel, PermissionLevel]:
    granted_level = parse_permission_level(granted)
    required = required_permission_for_tool(tool_name)
    return granted_level >= required, required, granted_level
