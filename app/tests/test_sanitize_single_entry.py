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


def test_shared_path_rewrite_in_sanitize():
    """expected_outputs / write_scopes / prompt 里的 `_shared/` helper 写产物必须在
    sanitize 阶段改写到 `_helpers_shared/`。

    病因(2026-06-05 trace 394304bbb02940e7): 主线程把 `_shared/file_map.json` 列入
    expected_outputs,helper 反复尝试 workspace.write 全部被 read-only 守卫拦截,
    framework_design helper 在 iter 18-23 连续失败后 stuck,浪费 224s 后中断。

    本测试断言 sanitize 链路的字符串特征,确保改写仍然在场,后续任何重构不会丢。
    """
    src = _src()
    # 唯一改写函数定义
    assert "_rewrite_shared_to_helpers_shared" in src
    # 改写时记日志的事件名
    assert "delegate.expected_outputs.shared_rewritten" in src
    # 出错原因解释(中英文成对出现,以便 helper / 主线程都能消费)
    assert "_shared/" in src and "_helpers_shared/" in src


def test_kind_guard_ignores_code_to_edit_for_source_outputs():
    """LLM-guard 若把 code 任务改成 edit 但产物是源码 (.py/.c/.h 等), 必须忽略。

    病因(2026-06-05 trace 394304 14:48:58): impl_new 任务声明产 acb_tree.py
    (Python 源码),guard LLM 因用户最终目标含 docx 论文,推 code→edit;主线程
    被 hard-block 重派,浪费 1 轮 LLM 调用。
    """
    src = _src()
    assert "delegate.guard_kind.ignored_code_to_edit_for_source_output" in src
    # 调用了 _has_non_text_implementation_output (源码/数据文件检测)
    assert "_has_non_text_implementation_output" in src


def test_helper_large_text_write_uses_char_count_only():
    """helper_large_text_write 阈值仅按字符总量 (6000), 不再额外按行数 (140) 误伤
    短 bullet 大纲。

    病因(2026-06-05 trace 394304 14:44:31): paper_outline.md 2786 字符 / 167 行
    短 bullet 触发旧的 line_count > 140 硬拒, helper 卡 1 分钟。
    """
    import os as _os
    _registry_path = _os.path.join(
        _os.path.dirname(__file__), "..", "llm", "tools", "registry.py"
    )
    with open(_registry_path, encoding="utf-8") as f:
        src = f.read()
    # 函数体内必须保留字符上限 6000 的早返回
    assert "if char_count <= 6000:\n        return None" in src
    # 不再有"and line_count <= 140"形式的 AND 双条件早返回
    assert "char_count <= 6000 and line_count <= 140" not in src


def test_cygwin_fork_failure_recovery_hint_present():
    """workspace.run 必须在 git-bash/Cygwin fork 资源耗尽时给恢复提示, 且 stuck
    detector 不把它计入连续失败。

    病因(2026-06-05 trace 394304 14:45-15:05): 多 helper 并行 + 长复合命令导致
    Windows Cygwin dofork 失败 (errno 11) 十余次,LLM 反复重试同一长命令。
    """
    import os as _os
    _ws_run_path = _os.path.join(
        _os.path.dirname(__file__), "..", "llm", "tools", "workspace_run.py"
    )
    with open(_ws_run_path, encoding="utf-8") as f:
        ws_src = f.read()
    assert "_cygwin_fork_exhaustion_hint" in ws_src
    assert "dofork: child" in ws_src
    assert "Resource temporarily unavailable" in ws_src

    _stuck_path = _os.path.join(
        _os.path.dirname(__file__), "..", "llm", "tools", "delegate_stuck.py"
    )
    with open(_stuck_path, encoding="utf-8") as f:
        stuck_src = f.read()
    # _is_success 中必须把 fork 资源耗尽视作环境抖动 (return True)
    assert "dofork: child" in stuck_src and "Resource temporarily unavailable" in stuck_src


def test_dangerous_executable_block_carries_recovery_hint():
    """被 DANGEROUS_EXACT_EXECUTABLES 拦截的命令需要给可操作恢复提示,避免 LLM
    反复尝试同一可执行 (实测 trace 394304 14:55:28 / 14:57:33 / 14:57:39
    impl_new 三次 `start python ...` 被拦)。
    """
    import os as _os
    _path = _os.path.join(
        _os.path.dirname(__file__), "..", "llm", "tools", "command_risk.py"
    )
    with open(_path, encoding="utf-8") as f:
        src = f.read()
    assert "recovery_hints" in src
    # start 必须有恢复指引
    assert "Foreground-run" in src or "前台运行" in src


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
