import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_permission_levels_order_and_tool_requirements():
    from app.core.permissions import PermissionLevel, check_tool_permission

    allowed, required, granted = check_tool_permission("fetch_indexed_file", PermissionLevel.retrieve_memory)
    assert not allowed
    assert required is PermissionLevel.read_group_file
    assert granted is PermissionLevel.retrieve_memory

    allowed, required, granted = check_tool_permission("fetch_group_file", PermissionLevel.retrieve_memory)
    assert not allowed
    assert required is PermissionLevel.read_group_file
    assert granted is PermissionLevel.retrieve_memory

    allowed, required, granted = check_tool_permission("expand_kb", PermissionLevel.retrieve_memory)
    assert allowed
    assert required is PermissionLevel.retrieve_memory


def test_permission_parser_defaults_invalid_values_to_generate_file():
    from app.core.permissions import PermissionLevel, parse_permission_level

    assert parse_permission_level(None) is PermissionLevel.generate_file
    assert parse_permission_level("") is PermissionLevel.generate_file
    assert parse_permission_level("unknown") is PermissionLevel.generate_file
    assert parse_permission_level(" CHAT ") is PermissionLevel.chat


def test_unknown_tool_defaults_to_generate_file_permission():
    from app.core.permissions import PermissionLevel, check_tool_permission, required_permission_for_tool

    assert required_permission_for_tool("missing_tool") is PermissionLevel.generate_file
    allowed, required, granted = check_tool_permission("missing_tool", "read_group_file")
    assert not allowed
    assert required is PermissionLevel.generate_file
    assert granted is PermissionLevel.read_group_file


async def test_dispatch_denies_tool_when_permission_too_low():
    from app.llm.tools import registry

    result = await registry.dispatch(
        "fetch_indexed_file",
        {"kb_node_id": "c_1"},
        archive_id="archive",
        group_id="group",
        user_id="user",
        workspace_dir="",
        permission_level="retrieve_memory",
    )
    data = json.loads(result)

    assert data["ok"] is False
    assert data["error"] == "permission denied for tool 'fetch_indexed_file'"
    assert data["required_permission"] == "read_group_file"
    assert data["granted_permission"] == "retrieve_memory"
