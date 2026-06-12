"""
tool_schemas 特征测试。从 registry.py 抽出的 30 个工具 JSON Schema(纯字面量,零依赖)。
可离线运行。同时验证 registry 经 re-export 仍可访问这些 schema。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.llm.tools import tool_schemas


def _iter_schema_dicts():
    for name in dir(tool_schemas):
        if name.endswith("_SCHEMA"):
            yield name, getattr(tool_schemas, name)


def test_has_30_schemas():
    names = [n for n, _ in _iter_schema_dicts()]
    assert len(names) >= 30


def test_known_schemas_present_and_named():
    # 关键工具 schema 应存在且 function.name 与预期一致
    expected = {
        "PYTHON_TOOL_SCHEMA": "python",
        "EXPAND_WARM_SCHEMA": "expand_warm",
        "EXPAND_COLD_SCHEMA": "expand_cold",
        "EXPAND_KB_SCHEMA": "expand_kb",
        "FETCH_GROUP_FILE_SCHEMA": "fetch_indexed_file",
    }
    for const, expect_name in expected.items():
        sch = getattr(tool_schemas, const)
        assert isinstance(sch, dict)
        # OpenAI 风格: {"type":"function","function":{"name":...}}
        blob = repr(sch)
        assert expect_name in blob


def test_schemas_are_dicts():
    for name, sch in _iter_schema_dicts():
        assert isinstance(sch, dict), f"{name} 不是 dict"


def test_registry_reexports_schemas():
    # registry 经 re-export 仍可访问(调用方零改动)。
    # registry 依赖第三方库(ws_tool/group_files 等),离线环境可能 import 失败 → 跳过;
    # 本地完整环境下应通过。
    try:
        from app.llm.tools import registry
    except Exception:
        import pytest
        pytest.skip("registry 需运行时依赖,离线跳过(本地完整环境覆盖)")
    assert getattr(registry, "PYTHON_TOOL_SCHEMA") is tool_schemas.PYTHON_TOOL_SCHEMA
    assert getattr(registry, "WORKSPACE_TOOL_SCHEMA") is tool_schemas.WORKSPACE_TOOL_SCHEMA


def test_memory_expand_schemas_require_current_index_ids():
    for schema_name in ("EXPAND_WARM_SCHEMA", "EXPAND_COLD_SCHEMA", "EXPAND_KB_SCHEMA"):
        desc = getattr(tool_schemas, schema_name)["function"]["description"]
        assert "current" in desc.lower()
        assert "semantic guesses" in desc
        assert "not evidence IDs" in desc
        assert "主题词猜测不是 ID" in desc


def test_delegate_expected_outputs_description_distinguishes_inputs_from_owned_outputs():
    task_props = (
        tool_schemas.DELEGATE_TOOL_SCHEMA["function"]["parameters"]["properties"]["tasks"]["items"]["properties"]
    )
    desc = task_props["expected_outputs"]["description"]

    assert "input_files" in desc
    assert "expected_outputs" in desc
    assert "produce/modify ownership" in desc
    assert "copyback and acceptance" in desc
    assert "bare filename is only a chat-workspace artifact" in desc
    assert "not project-verifier-visible" in desc
    assert "input_files 是可读输入" in desc


def test_delegate_prompt_description_preserves_structured_source_field_ambiguity():
    task_props = (
        tool_schemas.DELEGATE_TOOL_SCHEMA["function"]["parameters"]["properties"]["tasks"]["items"]["properties"]
    )
    desc = task_props["prompt"]["description"]

    assert "raw field names, values, notes, and acceptance constraints" in desc
    assert "counts, units, durations, quantities, booleans, or risk flags" in desc
    assert "ask the helper to compute from evidence" in desc
    assert "结构化源字段保留原始字段和值" in desc


def test_ask_user_question_schema_treats_requested_artifacts_as_authorized():
    desc = tool_schemas.ASK_USER_QUESTION_SCHEMA["function"]["description"]

    assert "save, create, edit, or jot down an artifact is already task authorization" in desc
    assert "content or destination facts are missing" in desc
    assert "用户已要求保存/创建/记录时即有该产物授权" in desc


def test_helper_request_envelope_states_project_visibility_fact():
    from app.llm.tools.delegate_framework import format_helper_request_envelope

    text = format_helper_request_envelope({
        "task_id": "report",
        "kind": "edit",
        "prompt": "Create the requested project report.",
        "expected_outputs": ["triage_report.txt"],
    })

    assert "Project Visibility Fact" in text
    assert "Bare output filenames are chat-workspace artifacts" in text
    assert "`_env/<project-relative-path>`" in text
    assert "项目验收可见产物需要" in text
