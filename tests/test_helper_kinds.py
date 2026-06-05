"""
helper_kinds 特征测试。从 delegate.py 原样抽出的 helper 类型/模式校验与任务 ID
分类逻辑;除 stdlib 外仅依赖 workspace_utils。可离线运行。
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.llm.tools.helper_kinds import (
    VALID_HELPER_KINDS,
    VALID_HELPER_MODES,
    _normalize_helper_kind_mode,
    _is_legacy_paired_task_id,
    get_helper_config,
    HELPER_CONFIGS,
)


def test_valid_constants_shape():
    assert isinstance(VALID_HELPER_KINDS, tuple) and "code" in VALID_HELPER_KINDS
    assert isinstance(VALID_HELPER_MODES, tuple) and "easy" in VALID_HELPER_MODES


def test_normalize_valid_passthrough():
    assert _normalize_helper_kind_mode("code", "easy") == ("code", "easy")


def test_normalize_returns_pair_of_valid():
    kind, mode = _normalize_helper_kind_mode("code", "hard")
    assert kind in VALID_HELPER_KINDS
    assert mode in VALID_HELPER_MODES


def test_is_legacy_paired_task_id_bool():
    # 仅断言返回布尔且不抛异常(行为快照)
    assert isinstance(_is_legacy_paired_task_id("some_task_123"), bool)
    assert isinstance(_is_legacy_paired_task_id(""), bool)


def test_get_helper_config_for_known_kinds():
    # 对每个合法 kind 都应能取到配置(不抛异常)
    for k in VALID_HELPER_KINDS:
        cfg = get_helper_config(k)
        assert cfg is not None
