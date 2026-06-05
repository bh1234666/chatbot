import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_main_thread_tools_generated_from_tool_meta():
    from app.llm.tools import registry

    meta_names = [meta.name for meta in registry.MAIN_THREAD_TOOL_METAS if meta.main_thread_allowed]
    tool_names = [tool["function"]["name"] for tool in registry.MAIN_THREAD_TOOLS]

    assert tool_names == meta_names
    assert registry.ROUND2_TOOLS is registry.MAIN_THREAD_TOOLS


def test_tool_meta_marks_read_only_and_side_effects():
    from app.llm.tools import registry

    by_name = {meta.name: meta for meta in registry.MAIN_THREAD_TOOL_METAS}

    assert by_name["read_file"].read_only is True
    assert by_name["read_file"].side_effect == "none"
    assert by_name["workspace"].read_only is False
    assert by_name["workspace"].side_effect == "workspace"
    assert by_name["mark_avoid_mention"].side_effect == "memory"
    assert by_name["ask_user_question"].side_effect == "external"


def test_tool_aliases_only_target_main_thread_tools():
    from app.llm.tools import registry
    from app.llm.tools.tool_meta import validate_aliases

    validate_aliases(
        registry._TOOL_ALIASES,
        registry._BLOCKED_MAIN_THREAD_ALIASES,
        registry.MAIN_THREAD_TOOL_METAS,
    )


def test_validate_aliases_rejects_implementation_target():
    from app.llm.tools import registry
    from app.llm.tools.tool_meta import validate_aliases

    try:
        validate_aliases(
            {"bad": ("python", {})},
            registry._BLOCKED_MAIN_THREAD_ALIASES,
            registry.MAIN_THREAD_TOOL_METAS,
        )
    except RuntimeError as exc:
        assert "non-main-thread" in str(exc)

def test_progress_note_is_blocked_from_main_thread_aliases():
    from app.llm.tools import registry

    assert registry._BLOCKED_MAIN_THREAD_ALIASES["progress_note"].startswith("helper-only")



    from app.core.permissions import PermissionLevel, required_permission_for_tool
    from app.llm.tools import registry

    for meta in registry.MAIN_THREAD_TOOL_METAS:
        assert required_permission_for_tool(meta.name) is PermissionLevel[meta.requires_permission]


def test_sync_tool_permissions_rejects_unknown_meta_permission():
    from dataclasses import replace

    import pytest

    from app.core.permissions import sync_tool_permissions_from_meta
    from app.llm.tools import registry

    bad_meta = replace(registry.MAIN_THREAD_TOOL_METAS[0], requires_permission="not_a_permission")
    with pytest.raises(RuntimeError):
        sync_tool_permissions_from_meta([bad_meta])
