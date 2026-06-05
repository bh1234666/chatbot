"""守护 delegate spawn 路径统一重构(2026-05-21)。

重构前: handle_delegate 的 spawn 路径先调 _sanitize_and_validate_tasks 做校验(丢弃返回值),
然后自己 mirror 一整套相同的清洗+配对+日志逻辑。两套并存导致:
  - code_hard_paired / delegate.start / kind.auto_corrected 日志双打印
  - 维护负担(上轮 resume kind 继承只进了 mirror,sanitize 漏了 → 两套不等价)
  - mirror 路径 early-return 校验不全(只 2 个 vs sanitize 7 个)

重构后: sanitize 是唯一清洗入口,spawn 用 `cleaned = cleaned_tasks` 直接复用其结果。

本测试通过静态分析源码,断言重构后的关键不变量成立(不依赖运行时/第三方库)。
"""
import re
import os

_SRC_PATH = os.path.join(
    os.path.dirname(__file__), "..", "llm", "tools", "delegate.py"
)
_ACTIONS_SRC_PATH = os.path.join(
    os.path.dirname(__file__), "..", "llm", "tools", "delegate_actions.py"
)


def _src():
    chunks = []
    for path in (_SRC_PATH, _ACTIONS_SRC_PATH):
        with open(path, encoding="utf-8") as f:
            chunks.append(f.read())
    return "\n".join(chunks)


def test_spawn_log_events_single_source():
    """关键 spawn 日志事件各只剩 1 处调用(不再双打印)。"""
    src = _src()
    for event in ('"delegate.start"', '"delegate.code_hard_paired"',
                  '"delegate.resume.kind_inherited"'):
        # debug.log("event", ...) 形式;统计该字面量作为日志事件名的出现次数
        n = len(re.findall(re.escape(event), src))
        assert n == 1, f"{event} 出现 {n} 次(期望 1,双打印应已消除)"


def test_resume_kind_inherit_in_sanitize():
    """resume kind 继承逻辑必须存在(重构时迁移进 sanitize,不能丢)。"""
    src = _src()
    assert "delegate.resume.kind_inherited" in src
    assert "从 ledger 继承原 kind" in src


def test_mirror_cleaning_removed():
    """mirror 路径的标志注释存在,确认已替换为单一来源。"""
    src = _src()
    assert "唯一清洗入口" in src
    assert "cleaned = cleaned_tasks" in src


def test_no_silent_flag_residue():
    """临时的 _sanitize_silent / _spawn_log 机制应已完全移除。"""
    src = _src()
    assert "_sanitize_silent" not in src
    assert "_spawn_log" not in src


def test_validation_checks_in_sanitize():
    """所有 early-return 校验都在 delegate 清洗链路内。"""
    src = _src()
    # 截取 _sanitize_and_validate_tasks 到下一个明显函数边界
    m = re.search(r"async def _sanitize_and_validate_tasks\(.*?\n(?:async def |def |\Z)",
                  src, re.S)
    body = src[m.start():m.end()] if m else src
    for check in ("_check_wait_stub_anti_pattern", "_detect_broad_code_task_warning",
                  "blanket_resume_blocked", "_detect_broad_code_task_warning_v2"):
        assert check in body, f"校验 {check} 应在 sanitize 内"


def test_environment_greenfield_vertical_slice_not_blocked():
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.delegate import _detect_broad_code_task_warning_v2

    task = {
        "task_id": "first_vertical_slice",
        "kind": "code",
        "mode": "easy",
        "prompt": (
            "This empty directory is a greenfield project. Build the first runnable "
            "vertical slice from scratch with Python core, JavaScript UI, a small "
            "native utility, smoke tests, README, and scripts/check_project.py. "
            "Write only the declared _env project files and verify the self-check."
        ),
        "expected_outputs": [
            "_env/core/__init__.py",
            "_env/core/config.py",
            "_env/core/tools.py",
            "_env/core/memory.py",
            "_env/core/agent.py",
            "_env/ui/index.html",
            "_env/ui/app.js",
            "_env/ui/style.css",
            "_env/native/strutil.c",
            "_env/native/strutil.h",
            "_env/tests/__init__.py",
            "_env/tests/conftest.py",
            "_env/tests/test_agent.py",
            "_env/fixtures/sample_config.json",
            "_env/scripts/check_project.py",
            "_env/README.md",
        ],
    }
    env = EnvironmentContext(
        root_dir="F:/tmp/project",
        archive_id="arch",
        group_id="group",
        user_id="user",
        project_key="project",
    )
    with runtime_context("environment", env):
        assert _detect_broad_code_task_warning_v2([task]) is None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
    print(f"test_sanitize_single_entry: {len(fns)} passed")
