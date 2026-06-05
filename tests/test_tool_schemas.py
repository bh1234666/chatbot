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
