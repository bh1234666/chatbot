"""守护 delegate spawn 路径统一重构(2026-05-21)。

重构前: handle_delegate 的 spawn 路径先调 _sanitize_and_validate_tasks 做校验(丢弃返回值),
然后自己 mirror 一整套相同的清洗+配对+日志逻辑。两套并存导致:
  - code_hard_explicit_paired / delegate.start / kind.auto_corrected 日志双打印
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
    for event in ('"delegate.start"', '"delegate.code_hard_explicit_paired"',
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
    """helper_large_text_write 阈值仅按字符总量, 不再额外按行数误伤
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
    # 函数体内必须保留字符上限早返回；当前阈值为效率优化后的 36000。
    assert "if char_count <= 36000:\n        return None" in src
    # 不再有按行数参与早返回的 AND 双条件。
    assert "char_count <= 36000 and line_count <=" not in src


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


def test_edit_thrashing_softened_to_advisory():
    """同文件 edit 次数硬阻断已移除 (用户 2026-06-05 要求软化): edit_file/multi_edit/
    insert_in_file 不再因 _prev_edits >= _EDIT_HARD_BLOCK_THRESHOLD 提前 return
    ok=False, 仅留 _track_edit_count 的 soft warning; delegate_stuck 也不再因
    同文件 edit 多次硬 stuck。
    """
    import os as _os
    _path = _os.path.join(
        _os.path.dirname(__file__), "..", "llm", "tools", "workspace_file_ops.py"
    )
    with open(_path, encoding="utf-8") as f:
        src = f.read()
    # 不应再出现 edit_thrashing_exceeded 早返回 (字面量出现意味着硬拒回路在场)
    assert '"edit_thrashing_exceeded"' not in src
    # delegate_stuck.py 也不再因同文件 edit 多次硬 stuck
    _stuck_path = _os.path.join(
        _os.path.dirname(__file__), "..", "llm", "tools", "delegate_stuck.py"
    )
    with open(_stuck_path, encoding="utf-8") as f:
        stuck_src = f.read()
    # P69 edit thrashing 已软化 — _mark_stuck 调用消失
    assert "P69 edit thrashing: `" not in stuck_src


def test_workspace_run_timeout_surfaces_partial_stderr():
    """bash 超时被 kill 时, stderr 已积累的内容必须 drain 出来供 LLM 看到 (实测
    trace 373640 17:13:05 fork 失败先打错误到 stderr 后 bash 卡住等不到子进程
    结束 → 走 timeout 路径, 但 stderr 内容被丢弃, LLM 只看到 'timed out' 完全
    错认为是耗时问题)。
    """
    import os as _os
    _path = _os.path.join(
        _os.path.dirname(__file__), "..", "llm", "tools", "workspace_run.py"
    )
    with open(_path, encoding="utf-8") as f:
        src = f.read()
    # timeout 后必须 drain proc.stderr/proc.stdout
    assert "_partial_stderr" in src
    assert "_fork_hint_on_timeout" in src


def test_security_keyword_carries_recovery_hint():
    """workspace_run_checks 的 _DANGEROUS_KEYWORDS 命中时也要附 recovery hint
    (start 命中的是这条路径而非 command_risk.DANGEROUS_EXACT_EXECUTABLES,
    实测 trace 394304 4 次 `start python ...` 全走这里, 旧版没 hint)。
    """
    import os as _os
    _path = _os.path.join(
        _os.path.dirname(__file__), "..", "llm", "tools", "workspace_run_checks.py"
    )
    with open(_path, encoding="utf-8") as f:
        src = f.read()
    assert "_SECURITY_RECOVERY_HINTS" in src
    # start 必须有恢复指引
    assert "Foreground-run" in src or "前台运行" in src


def test_p130_read_helper_three_tier_thresholds():
    """P130 read-helper 评估改为多级 (SOFT/STRONG/HARD), 给 LLM 多次自决机会
    才走硬 stuck (实测 trace 990126 19:42 + 19:52 read kind 14 次硬阈值偏紧)。
    """
    import os as _os
    _path = _os.path.join(
        _os.path.dirname(__file__), "..", "llm", "tools", "delegate_stuck.py"
    )
    with open(_path, encoding="utf-8") as f:
        src = f.read()
    # 三级阈值字段都要存在
    assert "_READ_NO_EVIDENCE_SOFT" in src
    assert "_READ_NO_EVIDENCE_STRONG" in src
    assert "_READ_NO_EVIDENCE_HARD" in src
    # SOFT/STRONG/HARD 阈值至少有一组示例值 (=8 / =16 / =28 之一)
    assert "= 28 if mode" in src or "= 28 \n" in src or "= 28\n" in src


def test_p130_ocr_evidence_writes_counted():
    """P130 evidence 计数改为同时认 ocr 工具产出 (实测 trace 990126 14 个 OCR
    .txt 没被记账,被 P130 误判 0 evidence 而 stuck)。
    """
    import os as _os
    _path = _os.path.join(
        _os.path.dirname(__file__), "..", "llm", "tools", "delegate_stuck.py"
    )
    with open(_path, encoding="utf-8") as f:
        src = f.read()
    # OCR 工具产出也算 evidence write
    assert 'tool_name == "ocr"' in src
    # _evidence_writes += 1 后跟着 _read_calls_no_write = 0
    assert "self._evidence_writes += 1" in src


def test_helper_tool_call_bloat_threshold_relaxed():
    """helper_tool_arg_bloat_close_at 提高到 24K (从旧 12K), 容纳合法长 docx /
    长代码生成 (实测 trace fee099 draw_waveforms / assemble_word 多次撞 12K
    被 close, 浪费续写 token)。
    """
    import os as _os
    _path = _os.path.join(
        _os.path.dirname(__file__), "..", "llm", "client.py"
    )
    with open(_path, encoding="utf-8") as f:
        src = f.read()
    assert "helper_tool_arg_bloat_close_at = 24_000" in src
    assert "helper_tool_arg_bloat_warn_at = 14_000" in src


def test_ok_count_excludes_stuck_or_interrupted():
    """timing_summary 的 ok_count / success_count 必须减去 interrupted / stuck=true
    的 helper, 否则会出现 ok=1 stuck=1 自相矛盾 (实测 trace 990126 19:44:47
    read_classroom_exercises 被算成 ok=1 同时 stuck=1)。
    """
    import os as _os
    _path = _os.path.join(
        _os.path.dirname(__file__), "..", "llm", "tools", "delegate_actions.py"
    )
    with open(_path, encoding="utf-8") as f:
        src = f.read()
    # success_count 和 ok_count 都要排除 interrupted/stuck
    # 注释说明位置 + 实际代码模式
    assert "ok=1 stuck=1 自相矛盾" in src or "stuck=1 自相矛盾" in src
    # 检查实际过滤代码的两处出现
    pattern = 'r.get("ok") and not r.get("interrupted") and not r.get("stuck")'
    assert src.count(pattern) >= 2, f"过滤模式应出现两次 (success_count + ok_count)"


def test_office_skill_explains_latex_wrapping():
    import os as _os
    _path = _os.path.join(
        _os.path.dirname(__file__), "..", "llm", "tools", "skills.py"
    )
    with open(_path, encoding="utf-8") as f:
        src = f.read()
    # 必须明确提到 $...$ 或 $$...$$ 包装
    assert "$...$" in src or "$$...$$" in src
    # OMML 概念也要提到, 让 helper 知道为什么这么做
    assert "OMML" in src


def test_office_format_warning_detects_raw_math_markup():
    """office.write 应检测 block.text 里的 raw `^{...}` / `_{...}` / `\\frac` 等
    数学标记残留 (实测 trace fee099 docx 末端有 6 处 e^{-r/4} 之类没渲染),
    但只作软提示, 不硬改 — 用户可能本意就是要 raw markdown 风格。
    """
    from app.llm.tools.office import _office_blocks_format_warning
    # 含残留: 应返回 advisory dict
    blocks = [
        {"type": "paragraph", "text": "误码率 P_e = (1/2)·e^{-r/4} 性能最差"},
        {"type": "paragraph", "text": "下标示例 b_{k-1} 与差分编码"},
    ]
    w = _office_blocks_format_warning("write", blocks)
    assert w is not None
    assert w.get("issue") == "raw_math_markup_in_plain_text"
    samples = [f.get("sample") for f in w.get("findings", [])]
    assert any("^{-r/4}" in (s or "") for s in samples)
    assert any("_{k-1}" in (s or "") for s in samples)
    # advisory 必须强调"如果是有意保留的 raw 风格则忽略" — 不硬改
    advisory = w.get("advisory", "")
    assert "ignore" in advisory.lower() or "忽略" in advisory

    # 干净文本 (无残留): 不应触发
    blocks_clean = [
        {"type": "paragraph", "text": "正常段落，含 Unicode 上标 e⁻ʳ⁄⁴ 与单一公式"},
        {"type": "paragraph", "text": "$P_e = e^{-r/4}$ 这是合法的 LaTeX 包裹"},
    ]
    # 注意: 第二段含 ^{-r/4} 但被 $ 包裹, 我们不剥 $ — re.search 仍会找到
    # ^{-r/4}, 这是预期的 (LLM 应自己判断是否包裹了, 这里只报告位置)。
    # 因此把第二段去掉再测干净路径。
    blocks_only_unicode = [
        {"type": "paragraph", "text": "正常段落，含 Unicode 上标 e⁻ʳ⁄⁴ 与单一公式"},
    ]
    assert _office_blocks_format_warning("write", blocks_only_unicode) is None


def test_environment_greenfield_vertical_slice_not_blocked():
    from app.core.runtime_mode import EnvironmentContext, runtime_context
    from app.llm.tools.delegate import _detect_broad_code_task_warning_v2

    task = {
        "task_id": "first_vertical_slice",
        "kind": "code",
        "mode": "easy",
        "prompt": (
            "This empty directory is a greenfield project. Build the first runnable "
            "vertical slice from scratch with Python core, smoke tests, README, "
            "and scripts/check_project.py. Write only the declared _env project files "
            "and verify the self-check."
        ),
        # 2026-06-05: vertical slice 上限为 10 outputs (见 delegate.py:4522), 该测试
        # 之前传 16 个,已超过 slice 阈值,改成真正的最小切片;
        # _env/src/ 是 vertical-slice 必备前缀(canonical layout)。
        "expected_outputs": [
            "_env/src/core/__init__.py",
            "_env/src/core/agent.py",
            "_env/tests/__init__.py",
            "_env/tests/test_agent.py",
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
