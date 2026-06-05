"""Helper stuck detection and API stall thresholds for delegate helpers."""
from __future__ import annotations

from collections import Counter
import json
import logging
import os
import re

log = logging.getLogger(__name__)


class StuckDetector:
    _RECENT_WINDOW = 8
    _SAME_TOOL_FAIL_LIMIT = 4
    # B9 修复: 同一种语义错误超过 3 次就视为卡死(原 4)。
    # 实测 trace 3da78120 helper 因为 stderr 文本变化(临时文件名/行号)签名变化,
    # 导致即使根因相同也凑不到 4 次。新签名是语义级(undefined ref + symbol),
    # 重复 3 次足够确认是同一根因循环。
    _SAME_ERROR_LIMIT = 3
    # 收紧连续失败上限: helper 不应该连续失败 6 次还继续闷头试,4 次就停手
    _CONSECUTIVE_FAIL_LIMIT = 4
    # B9: build 类错误(linker undef / missing header / undecl)单独阈值 — 这类
    # 错误几乎肯定是 prompt 或环境问题,helper 自己改源码改不出来,2 次就停手让
    # 主线程介入。
    _BUILD_ERROR_LIMIT = 2
    # 2026-05-11 P14.A: 整个 helper 生命周期内同种错误累计上限
    # 病因(实测 bench_skiplist 17min): 编译错误反复 7+ 次, 每次间夹 edit_file 成功
    # → 窗口被刷新永远凑不齐 _SAME_ERROR_LIMIT → 不 stuck → 浪费时间。
    # 修法: 累计计数, 不受成功重置, 整个 helper 周期内同错误 ≥ N 次直接 stuck。
    _LIFETIME_SAME_ERROR_LIMIT = 6
    # build 类错误更严格(几乎肯定是接口/环境问题, helper 改不出来)
    _LIFETIME_BUILD_ERROR_LIMIT = 4

    # build 错误前缀(_error_signature 输出格式)
    _BUILD_ERROR_PREFIXES = (
        "linker_undef:", "missing_header:", "missing_decl:",
        "undecl_id:", "redef:",
    )

    # 2026-05-11 新增: 致命性错误 — 2 次重复就触发 stuck (不必等到 4 次)
    # 这类错误不是 helper "尝试不同方案" 能恢复的,继续重试只会浪费时间。
    # 实测教训 trace 15:34-15:43: abpt 3 次空 args 错误花了 9 分钟才 stuck,
    # 第 2 次时就应该停手让主线程接管。
    _FATAL_ERROR_LIMIT = 2
    _FATAL_ERROR_KEYWORDS = (
        "空对象 {}",                  # max_tokens 截断: workspace 收到空 args
        "SecurityError: name not",    # python 沙箱拒绝 (open / __import__ 等)
        "persona_veto",               # 人格守卫拒绝
    )

    # ── 2026-05-02 part13:edit 不验证检测(trace 74b1295b iter 80-118 教训)──
    # rdh_v2 末段 38 个 iter 里 edit_file 17 次 + read_file 24 次 + workspace.run 6 次
    # → 平均每 ~3 个 edit 才 compile 一次,且很多 edit 之间根本没 compile 直接又 read。
    # 模型陷入"修一处 → 翻代码 → 又修一处 → 又翻 → ..." 不验证,所以语法错只在
    # 几次后的 compile 时才暴露,debug 信号丢失。
    #
    # 触发:连续 ≥N 次 edit_file 没夹一次 workspace.run / python(任何 verify 动作)
    # 不是 stuck(还能恢复),只发提示让模型主动 compile。
    _EDIT_WITHOUT_VERIFY_LIMIT = 3
    _VERIFY_TOOLS = ("workspace", "python")  # 这些算 verify

    _ROUND_TRIP_KEYWORDS = ("Results:", "PASS", "FAIL", "passed", "failed",
                            "round-trip", "decompress", "mismatch", "expected",
                            "got ", "Test ", "✓", "✗")

    # ── 2026-05-02 part19:long-no-delegate 检测(trace 74769ad9 教训)──
    # 用户提"研究 6 算法压缩"任务 round1 判 parallelizable=True is_coding_task=True,
    # 但主线程跑了 38 个 iter **完全没调 delegate**,自己一个人调试 huffman bug
    # 7 分钟。最后 round3 因 abort 输出 104 字泄露内部状态。
    # round2 prompt 里已经详细说了 fan-out 用 delegate,但模型在 iter > 10 后陷入
    # 调试循环就忘了首要决策。需要**运行时**注入提醒。
    #
    # 触发:主线程 + 任务标 parallelizable=True + 累计 ≥ N 次 tool_call 仍没调过
    # delegate + 工具组合呈"个人编码"模式(edit_file/read_file/code_index/
    # workspace.run/python 累计高比例)→ 注入 hint。每次会话只发一次。
    _LONG_NO_DELEGATE_TOOL_LIMIT = 8  # 累计 N 次 tool_call 仍无 delegate
    # 2026-05-02 part19 实测调整:trace 74769ad9 主线程在 iter 12 时已经在 read_function
    # huff_encode → 进入"调试单算法"陷阱。原 12 太晚 — 模型在 iter 5-8 段开始进入
    # 调试模式时就该提醒,iter 12 时已经沉浸在 huffman 细节里很难拔出来。
    # 降到 8 让 hint 在"刚开始亲自调代码"时打断。
    _CODING_TOOLS = (
        "edit_file", "read_file", "code_index", "read_function",
        "search_in_file", "search_across_files", "insert_in_file",
        "workspace", "python", "inspect_file",
    )

    # ── 2026-05-02 part20:edit-on-same-file 跟踪(trace 74769ad9 教训)──
    # 主线程对 huffman.c 反复 edit + workspace.run 仍 fail,陷入"局部修补"循环。
    # 深层逻辑 bug 应该重写整个函数/文件,而不是连续 edit 试探。
    # 触发:同一文件 edit_file ≥ N 次 + 之间至少 1 次 workspace.run 仍 FAIL
    # → 注入 hint 让模型用 workspace.write 重写
    _EDIT_SAME_FILE_RETRY_LIMIT = 4  # 同文件 edit ≥4 次未通过测试
    _RECENT_RUNS_WINDOW = 6  # 看最近 6 次 workspace.run/python

    # ── 2026-05-15 P69: edit thrashing 硬升级(comp_bench 死循环教训)──
    # 病因(实测 comp_bench 16:57-18:35): 单 helper 内对 bench/benchmark.c 50 次 edit,
    # bench/compress.h 55 次 edit, 累计 66 次 bash 失败, 但因每次失败的 error_signature
    # 不同(linker error → 改后变 syntax error → 改后变 undef ref → ...), P14.A 累计
    # 错误计数器永远凑不齐同一 sig × 6 次, 不 stuck。same_file_edit_fail soft hint 只
    # 发一次, helper 不响应继续 edit 到 50+。
    # 修法: 当 same_file_edit_fail 触发后(N=4), 如果 edit 继续到 M=12, 升级为 hard stuck。
    # 主线程应该 kill 并换 task_id / 拆任务。
    _EDIT_SAME_FILE_HARD_STUCK = 12  # 同文件累计 12 次 edit + 仍有 run fail → 硬 stuck
    # ── 2026-05-15 P70: bash 失败率 stuck 检测(comp_bench 教训)──
    # comp_bench 1026 bash 调用里 66 次 FAIL: rc=N (6.4%), 但集中爆发 — 任一编译阶段
    # 连续多次失败时, helper 进入 "改→build → 还崩 → 改→build" 死循环。
    # 检测: 最近 N=12 次 bash 调用里, 失败率 >= 50% 且 helper 已 batch >= 8 → hard stuck。
    _BASH_FAIL_RATE_WINDOW = 12
    _BASH_FAIL_RATE_THRESHOLD = 0.5  # 50% 失败率
    _BASH_FAIL_RATE_MIN_BATCH = 8  # batch < 8 时不触发(helper 启动期会有失败)

    # ── 2026-05-05: 空编辑检测(old_string == new_string) ──
    _EMPTY_EDIT_CONSECUTIVE_LIMIT = 3  # 连续 ≥3 次空编辑 → soft_hint

    def __init__(self, task_id: str, *, parallelizable: bool = False, mode: str = "easy", kind: str = ""):
        self.task_id = task_id
        # 元素: (tool_name, ok: bool, error_sig: str)
        self._calls: list[tuple[str, bool, str]] = []
        self._consec_fails = 0
        self._stuck = False
        self._stuck_reason = ""
        self._kind = str(kind or "").strip().lower()
        # 2026-05-15 P106: 记 mode, hard 模式宽限阈值 — 资源无限策略
        # 用户要求: hard code helper 与普通 helper paired race 时, hard 应"资源无限"
        # 不被 stuck detector 提前 kill, 让它跑到底; 谁先成功谁赢。
        # 实现: 不完全跳过 (避免无限 loop), 而是把阈值放大 3x:
        #   - _SAME_ERROR_LIMIT: 3 → 9
        #   - _CONSECUTIVE_FAIL_LIMIT: 4 → 12
        #   - _LIFETIME_SAME_ERROR_LIMIT: 6 → 18
        #   - _LIFETIME_BUILD_ERROR_LIMIT: 4 → 12
        #   - _BUILD_ERROR_LIMIT: 2 → 6
        #   - _BASH_FAIL_RATE_THRESHOLD: 50% → 70%
        #   - _WIN_FATAL_STUCK_THRESHOLD: 5 → 12
        #   - _EDIT_SAME_FILE_HARD_STUCK: 12 → 30
        # soft_hints 仍正常发 (帮 helper 自救)
        self._mode = mode
        if mode == "hard":
            self._SAME_ERROR_LIMIT = 9
            self._CONSECUTIVE_FAIL_LIMIT = 12
            self._LIFETIME_SAME_ERROR_LIMIT = 18
            self._LIFETIME_BUILD_ERROR_LIMIT = 12
            self._BUILD_ERROR_LIMIT = 6
            self._BASH_FAIL_RATE_THRESHOLD = 0.7  # 70% (易模式 50%)
            self._FATAL_ERROR_LIMIT = 4  # 易模式 2
        # 2026-05-02 part13:edit-without-verify 软提示状态
        # 计数器:从上次 verify 之后累积了多少次 edit
        self._edits_since_verify = 0
        # 软提示标志:已发过多少次 "请 compile" hint(避免重复刷屏)
        self._soft_hints_sent: list[str] = []
        # 2026-05-02 part19:long-no-delegate 状态
        self._parallelizable = bool(parallelizable)
        self._is_main_thread = (task_id == "main_thread")
        self._total_tool_calls = 0
        self._delegate_calls = 0
        # 2026-05-02 part20:edit-on-same-file 状态
        # key=path, value=count(edit 次数,workspace.run 通过时清零)
        self._edits_per_file: dict[str, int] = {}
        # 最近的 workspace.run / python 结果是否含失败信号
        self._last_run_failed: bool = False
        # 当前正在反复 edit 的文件(触发 hint 时引用)
        self._most_edited_file: str = ""
        # 2026-05-05: 空编辑检测(old_string == new_string)
        self._consecutive_empty_edits = 0
        self._last_empty_edit_path: str = ""
        # 2026-05-11 P11: fetch-then-rewrite 反模式检测
        # 病因: helper fetch 了主区 PNG 后, 仍然写 matplotlib 重画图(浪费 + 引入错配)
        # 记录最近 fetched 来的 PNG basenames, 在 workspace.write .py 时 cross-check
        self._recent_fetched_pngs: list[str] = []   # 最近 fetch 的 PNG 文件名
        self._fetch_then_rewrite_hint_sent: bool = False  # 只警告 1 次

        # 2026-05-11 P15.C: 数据文件读取跟踪 (data-first-print 按需注入)
        # 病因(实测教训): helper 读了 CSV/JSON 等数据 → 凭印象写 df['Algorithm']/
        # row['algorithm'] 等访问 → 与实际列名不匹配 → KeyError 或数据错配。
        # 跟踪 read_file/fetch_to_temp 拿到的数据文件, 检测 workspace.write 写 .py
        # 引用列名时, 触发 "先 print 看真实数据" hint。
        self._recent_read_data_files: list[str] = []  # 最近读的 CSV/JSON/XLSX
        self._data_first_print_hint_sent: bool = False
        self._pending_data_first_print_hint: str | None = None

        # 2026-05-11 P15.D: bash 工具误用跟踪 (tool-selection 按需注入)
        # 病因: helper 用 bash cat/head/grep 读文件而不用 read_file/search_in_file
        # → bash 启动慢 + 输出可能截断 + 不易解析。累计 ≥ 3 次时提示。
        self._bash_misuse_count: int = 0  # bash 用于 cat/head/tail/grep 的次数
        self._tool_selection_hint_sent: bool = False

        # 2026-05-12 P22: "动不了手"检测(改用 batch 计数, 不计 tool_call 总数)
        # 用户洞察 1: 同步工具调用不应反复计数 — paper_final iter 1 一次性 55 个
        # todo_write 并行 call, 是同一次 LLM 决策的产物, 应算 1 batch 不是 55 次。
        # 用户洞察 2: 文件搜索应该由主进程指引 — paper_final 找不到 chart 是因为
        # 主进程 prompt 给错路径(说 temp 根目录, 实际在 _helpers_shared/.../). 
        # helper 在错路径死磕 → "动不了手"。
        # 检测: ≥ 3 batch 且产出工具 = 0 → 注入 hint 教 helper 自救(progress_note 求助)。
        self._product_tool_count: int = 0  # office/edit_file/multi_edit/insert_in_file/python/workspace.write
        self._batch_count: int = 0  # record_batch 被调次数 = LLM 响应轮数
        self._no_product_hint_sent: bool = False
        self._pending_no_product_hint: str | None = None

        # 2026-05-12 P27: "产出停滞"检测(P22 的兄弟维度)
        # 病因(实测 13:06 trace): paper kind=edit+mode=hard 前期 office.write 5 次产 docx 骨架,
        # 后续 14 batch 全在 workspace.run 'dir /b *.png' 死搜找图 → STUCK 退出.
        # P22 检测"从未产出"在这里失效(已经产了), 需要新维度: 启动后停滞.
        # 检测: 最近 N batch 内 product_count 无增长 → 注入 hint 教 helper 求助。
        self._product_count_history: list[int] = []  # 每个 batch 后的 product_count 快照
        self._stagnation_hint_sent: bool = False
        self._pending_stagnation_hint: str | None = None

        # 2026-05-12 P31: Python SyntaxError 反复检测(实测 12:10 trace)
        # 病因: merge_csv 8 次 / bench_bptree 4 次 / charts 4 次 SyntaxError,
        # 全是 "unterminated string literal (detected at line 1)" — LLM 用
        # workspace.run + 'python -c "..."' 时内层引号转义出错。
        # 修法: 检测 ≥ 3 次 SyntaxError 后 hint 教用 workspace.write 写 .py 再 run。
        self._python_syntax_error_count: int = 0
        self._python_syntax_hint_sent: bool = False

        # 2026-05-12 P24: 批量 todo_write 滥用检测
        # 病因(实测 09:14 paper_v2): LLM 把 todos 列表的每项作为独立 tool_call,
        # 一个 batch 输出 45 个并行 todo_write call → 360 次累计。
        # 应该用 todos 数组单次调用: todo_write(todos=[{..},{..},..])
        self._batch_todo_warn_sent: bool = False
        self._pending_batch_todo_hint: str | None = None
        self._pending_missing_dependency_hint: str | None = None
        # 2026-05-11 P12.H: progress_note 跟踪
        # 病因(实测 18:46-19:09 pptx): helper 跑 22 分钟没 progress_note,
        # 主线程不知道在干嘛, 也不知道何时该 kill 强制接管。
        # 跟踪自上次 progress_note 以来的工具调用数, 阈值后给 helper hint。
        self._tool_calls_since_progress_note: int = 0
        self._last_progress_note_at_call: int = 0
        self._progress_note_hint_sent: bool = False
        # 2026-05-15 P110: 周期触发计数器 (替代 P12.H one-shot)
        self._progress_note_hint_count: int = 0
        self._recent_office_actions: list[str] = []
        self._office_micro_edit_hint_sent: bool = False

        # 2026-05-11 P14.A: 累计同类错误跟踪(不受 success 重置)
        # 病因(实测 trace bench_skiplist): helper 17min 内编译错误重复 7+ 次,
        # 每次错误间夹有 edit_file 成功 → 老的 sig_counts 看的是 _calls 窗口
        # (只 8 个槽位, 还被成功打断重置) → 永远不触发 stuck → 浪费 1 小时。
        # 修法: 累计 (tool, error_sig) 出现次数, 整个 helper 生命周期内不重置,
        # 达 LIFETIME_LIMIT 强制 stuck — 即使其间有成功操作。
        from collections import Counter as _Counter_2
        self._cumulative_error_counts: _Counter_2 = _Counter_2()
        self._lifetime_stuck_hint_sent: set[tuple[str, str]] = set()

        # 2026-05-15 P70: bash 失败率窗口(独立于 _calls, 因 _calls 只有 8 槽不够)
        self._recent_bash_results: list[bool] = []  # True=ok, False=fail

        # 2026-05-15 P103: 渐进式 Windows 致命错误处理
        # 病因(实测 trace 16:24 压缩论文): comp_bench helper 56 bash 失败,
        # 其中 25 次是 Windows 致命错误 (HEAP_CORRUPTION/SEGFAULT)。
        # P70 (失败率) 没触发因为夹杂大量成功的 build 步骤稀释窗口。
        #
        # 老版 P103 设计: 累计 3 次直接 mark_stuck (kill helper)
        # 用户反馈合理: kill 后问题没解决, 还得新 helper 做同样事, 浪费工作。
        #
        # 新版 P103 设计: **渐进式 soft_hint 帮 helper 自救**
        #   count 1: 现有 fix_hint (workspace.py 自动注入, 不重复处理)
        #   count 2: P103.a soft_hint - 给具体 sanitizer 命令例子
        #   count 3: P103.b soft_hint - 建议"重写模块" (跳出当前 bug 思路)
        #   count 5: P103.c mark_stuck - 真的卡死了, 让主线程介入
        # 这样 helper 有机会自我修复, 只在真无解时才丢回主线程。
        self._win_fatal_count = 0
        # 0xC0000005 ACCESS_VIOLATION, 0xC0000374 HEAP_CORRUPTION,
        # 0xC0000409 STACK_BUFFER_OVERRUN, 0xC0000094 INT_DIVIDE_BY_ZERO,
        # 0xC00000FD STACK_OVERFLOW, 0xC0000025 NONCONTINUABLE_EXCEPTION
        self._WIN_FATAL_RCS = {
            3221225477, 3221226356, 3221226505,
            3221225620, 3221225725, 3221225509,
        }
        self._WIN_FATAL_SOFT_HINT_AT_2 = 2  # 第 2 次发 sanitizer 提示
        self._WIN_FATAL_SOFT_HINT_AT_3 = 3  # 第 3 次发"重写模块"提示
        # P106: hard 模式宽限 stuck 阈值 5 → 12 (与 paired race 配合, 资源无限策略)
        self._WIN_FATAL_STUCK_THRESHOLD = 12 if mode == "hard" else 5
        # 2026-05-15 P106: hard 模式 edit_same_file 阈值也宽限 (12 → 30)
        if mode == "hard":
            self._EDIT_SAME_FILE_HARD_STUCK = 30  # 易模式 12

        # 2026-06-04 P130: read helper "no evidence written" loop detection
        # 病因(实测 stage3 v3/v4/v5 trace): kind=read helper 启动后陷入
        # search_in_file/read_file 循环, 累计 8-10 次工具调用, 但从未 workspace.write
        # 写过 .txt evidence 文件; soft hint P22/P27 要 batch≥3/5 才触发, 已经太晚。
        # 在 read 维度专门数: 每次 read_file/search_in_file/search_files/code_index 等
        # "读取类"调用计数, 一旦 ≥ self._READ_NO_EVIDENCE_SOFT 且没产出 evidence_files
        # → 软提示要求停止读循环写 evidence; ≥ self._READ_NO_EVIDENCE_HARD 仍无写 → mark_stuck.
        self._read_calls_no_write: int = 0
        self._evidence_writes: int = 0
        self._read_no_evidence_hint_sent: bool = False
        self._read_no_evidence_strong_hint_sent: bool = False
        # 2026-06-05 软化: 之前 SOFT=8 / HARD=14 太紧, read helper 在体量大的工作区
        # 探索 / verify 任务下经常 14 次内无法收敛就 stuck (实测 trace 990126 19:42 +
        # 19:52 两次 read kind 撞墙, 浪费 ~3 分钟)。改为多级 soft hint:
        #   SOFT (8/14) -> 第一次普通提示 "请写 evidence 或停手"
        #   STRONG (16/24) -> 第二次强提示 "再不写 evidence 必停"
        #   HARD (28/40) -> 最终 stuck (避免无限循环)
        # 真在做 verify/inspect 类轻量任务的 read helper 在 16 次后会被 STRONG hint
        # 驱使收尾, 不会再因为 14 次硬阈值被无理由 kill。
        self._READ_NO_EVIDENCE_SOFT = 8 if mode != "hard" else 14
        self._READ_NO_EVIDENCE_STRONG = 16 if mode != "hard" else 24
        self._READ_NO_EVIDENCE_HARD = 28 if mode != "hard" else 40
        # 哪些工具算"读取类"
        self._READ_TOOLS_FOR_READ_HELPER = (
            "read_file", "search_in_file", "search_files",
            "search_across_files", "code_index", "read_function",
            "inspect_file",
        )

    def record(self, tool_name: str, result_str: str, args: dict | None = None):
        """记录一次工具调用结果。result_str 是 dispatcher 返回的 JSON 字符串。

        args(可选):工具的输入参数,用于 same-file edit 跟踪等场景。
        老调用方不传 args 仍兼容(part20 加)。
        """
        ok = self._is_success(result_str)
        sig = ""
        if not ok:
            # error 签名: 取 error / message 字段前 80 char,降噪
            sig = self._error_signature(tool_name, result_str)
            self._consec_fails += 1
            # 2026-05-11 P14.A: 累计同类错误(整个 helper 生命周期, 不受成功重置)
            if sig:
                key = (tool_name, sig)
                self._cumulative_error_counts[key] += 1
                _cum = self._cumulative_error_counts[key]
                # 判断是否 build 类错误(更严格阈值)
                _is_build = any(
                    sig.startswith(p) for p in self._BUILD_ERROR_PREFIXES
                )
                _limit = (self._LIFETIME_BUILD_ERROR_LIMIT if _is_build
                           else self._LIFETIME_SAME_ERROR_LIMIT)
                if _cum >= _limit and key not in self._lifetime_stuck_hint_sent:
                    self._lifetime_stuck_hint_sent.add(key)
                    self._mark_stuck(
                        f"P14.A lifetime repeated error: {tool_name} produced the same error signature "
                        f"{_cum} times (sig={sig[:60]!r}, limit={_limit}). Successful edit/write calls did "
                        f"not break the loop; the main process should reroute with a fresh task_id, keep the "
                        f"base helper kind, and consider hard mode with the observed blocker in the prompt.\n"
                        f"同类错误跨生命周期重复时，主进程应换新 task_id 并带上阻塞证据重新派发。"
                    )
                    # 早 return 之前 — 让 stuck 标记成功就行, 下面其他逻辑继续
        else:
            self._consec_fails = 0

        # ── 2026-05-02 part13:edit-without-verify 软追踪 ──
        # 不影响 stuck 判定,只用于产出 soft_hint(由 chat_with_tools_loop 注入到下一轮)
        if tool_name in ("edit_file", "insert_in_file"):
            self._edits_since_verify += 1
        elif tool_name in self._VERIFY_TOOLS:
            self._edits_since_verify = 0  # 跑了任何 workspace.run / python 都算 verify

        # ── 2026-05-02 part19:long-no-delegate 软追踪 ──
        self._total_tool_calls += 1
        if tool_name == "delegate":
            self._delegate_calls += 1

        # 2026-05-11 P12.H: progress_note 跟踪
        # progress_note 调用 → 重置计数
        # 其他调用 → 累加(达阈值给 hint)
        if tool_name == "progress_note":
            self._tool_calls_since_progress_note = 0
            self._last_progress_note_at_call = self._total_tool_calls
        else:
            self._tool_calls_since_progress_note += 1

        # ── 2026-05-05: 空编辑检测(old_string == new_string) ──
        # 检测后重置计数器(非 edit 操作或正常 edit 都重置)
        _is_empty_edit = False
        if tool_name == "edit_file":
            # 优先从 args 检测
            if args:
                old_str = str(args.get("old_str", "") or "")
                new_str = str(args.get("new_str", "") or "")
                if old_str and old_str == new_str:
                    _is_empty_edit = True
            # 回退:从 result 中的 _empty_edit 标志检测
            if not _is_empty_edit and '"_empty_edit": true' in (result_str or ""):
                _is_empty_edit = True
        if _is_empty_edit:
            self._consecutive_empty_edits += 1
            if args:
                self._last_empty_edit_path = str(args.get("path", "") or "")
        elif tool_name != "edit_file":
            self._consecutive_empty_edits = 0

        # ── 2026-05-02 part20:edit-on-same-file 软追踪 ──
        # 同一文件反复 edit + 测试仍 fail → 提示重写整个函数/文件
        if tool_name in ("edit_file", "insert_in_file") and args:
            path = str(args.get("path", "")).strip()
            if path:
                self._edits_per_file[path] = self._edits_per_file.get(path, 0) + 1
                if self._edits_per_file[path] > self._edits_per_file.get(
                    self._most_edited_file, 0
                ):
                    self._most_edited_file = path
        elif tool_name in self._VERIFY_TOOLS:
            # 用工具结果是否含 FAIL/error 等信号判断"测试是否通过"
            self._last_run_failed = self._has_failure_signal(result_str)
            if not self._last_run_failed:
                # 测试过了 → 清零这个文件的 edit count
                # (但保留 most_edited_file 记录,用于回溯)
                self._edits_per_file = {
                    p: 0 for p in self._edits_per_file
                }

        # ── 2026-05-15 P69 → 2026-06-05 软化: edit thrashing 不再硬 stuck ──
        # 用户要求: 不限制最大 edit 次数,除非导致上下文超限。
        # 仍记录 soft hint(由 same_file_edit_fail 路径在 4 次时发出),但不升级为 stuck。
        # helper 自决何时收手;只有在 _consec_fails / _BASH_FAIL_RATE / lifetime_same_error
        # 等其他独立硬约束触发时才会 stuck。

        # ── 2026-05-15 P70: bash 失败率 stuck ──
        # 最近 _BASH_FAIL_RATE_WINDOW 次 bash 调用里失败率 >= _BASH_FAIL_RATE_THRESHOLD,
        # 且 helper 已 batch >= _BASH_FAIL_RATE_MIN_BATCH (启动期不算) → hard stuck.
        # 病因(实测 comp_bench): 1026 bash 中 66 失败, 但集中爆发在编译阶段。
        # 错误签名总在变(linker → syntax → undef ref → ...), P14.A 累计计数永远凑不齐,
        # 但 helper 已陷"改→build→还崩"循环。失败率指标比签名计数更可靠。
        if tool_name == "bash":
            self._recent_bash_results.append(ok)
            # 限窗口大小
            if len(self._recent_bash_results) > self._BASH_FAIL_RATE_WINDOW:
                self._recent_bash_results.pop(0)
            if (self._batch_count >= self._BASH_FAIL_RATE_MIN_BATCH
                    and len(self._recent_bash_results) >= self._BASH_FAIL_RATE_WINDOW
                    and not self._stuck):
                _bash_fails = sum(1 for r in self._recent_bash_results if not r)
                _fail_rate = _bash_fails / len(self._recent_bash_results)
                if _fail_rate >= self._BASH_FAIL_RATE_THRESHOLD:
                    self._mark_stuck(
                        f"P70 bash failure loop: {_bash_fails}/{len(self._recent_bash_results)} recent bash "
                        f"calls failed ({_fail_rate:.0%}, threshold {self._BASH_FAIL_RATE_THRESHOLD:.0%}). "
                        f"The helper is cycling through build/run failures without convergence. The main "
                        f"process should preserve the report, identify the failure class, and reroute with "
                        f"a fresh task_id plus the attempted approaches.\n"
                        f"bash 失败率持续过高时，主进程应记录失败类型并带上已尝试路径重新派发。"
                    )
                    return

            # ── 2026-05-15 P103: Windows 致命错误累计 (渐进式) ──
            # 累计计数, 实际 hint 在 consume_soft_hint() 中按阈值分级发出。
            # 仅在累计 ≥5 次 (WIN_FATAL_STUCK_THRESHOLD) 才 mark_stuck — kill 是最后手段。
            if not ok:
                try:
                    import json as _json
                    _parsed = _json.loads(result_str) if isinstance(result_str, str) else (result_str or {})
                    _rc = _parsed.get("rc")
                    if isinstance(_rc, int) and _rc in self._WIN_FATAL_RCS:
                        self._win_fatal_count += 1
                        # 累计达 stuck 阈值才 kill
                        if self._win_fatal_count >= self._WIN_FATAL_STUCK_THRESHOLD and not self._stuck:
                            self._mark_stuck(
                                f"P103 Windows fatal return code: {self._win_fatal_count} fatal runtime "
                                f"errors observed (rc={_rc:#x}). Sanitizer/rewrite hints were already issued, "
                                f"but the helper is still cycling. The main process should use the helper "
                                f"report to locate the failing area, choose a sibling implementation when "
                                f"appropriate, or reroute with a fresh task_id and explicit diagnostic setup.\n"
                                f"Windows 致命运行错误反复出现时，主进程应基于报告重新派发诊断或替代实现。"
                            )
                            return
                except (ValueError, TypeError, KeyError):
                    pass



        self._calls.append((tool_name, ok, sig))
        if len(self._calls) > self._RECENT_WINDOW:
            self._calls.pop(0)

        # 2026-05-11 P11: fetch-then-rewrite 反模式跟踪
        # 1. fetch_to_temp 调用且参数含 .png → 记录这些 basename
        if tool_name == "fetch_to_temp" and args and ok:
            paths = args.get("paths", [])
            if isinstance(paths, list):
                for p in paths:
                    if isinstance(p, str) and p.lower().endswith(
                        (".png", ".jpg", ".jpeg", ".svg")
                    ):
                        import os as _os_p
                        bn = _os_p.path.basename(p)  # 2026-05-12 hotfix: os 没有 basename, 必须 os.path.basename
                        if bn and bn not in self._recent_fetched_pngs:
                            self._recent_fetched_pngs.append(bn)
                # 只留最近 20 个
                self._recent_fetched_pngs = self._recent_fetched_pngs[-20:]

        # 2026-05-11 P15.C: 数据文件跟踪 — fetch_to_temp 或 read_file 拿了数据文件
        if args and ok and tool_name in ("fetch_to_temp", "read_file"):
            _paths_to_check: list = []
            if tool_name == "fetch_to_temp":
                _paths_to_check = args.get("paths", []) or []
            elif tool_name == "read_file":
                _p = args.get("path", "")
                if _p: _paths_to_check = [_p]
            if isinstance(_paths_to_check, list):
                for p in _paths_to_check:
                    if isinstance(p, str) and p.lower().endswith(
                        (".csv", ".tsv", ".json", ".jsonl", ".xlsx", ".xls", ".parquet")
                    ):
                        import os as _os_d
                        bn = _os_d.path.basename(p)
                        if bn and bn not in self._recent_read_data_files:
                            self._recent_read_data_files.append(bn)
                self._recent_read_data_files = self._recent_read_data_files[-15:]

        # 2026-05-11 P15.C: 检测 workspace.write/python 引用数据列名却没先 print
        # 触发: 最近读过 data 文件 + 当前写 .py 含可疑的 column/key 访问模式
        if (not self._data_first_print_hint_sent
                and len(self._recent_read_data_files) >= 1
                and args):
            _content = ""
            if tool_name == "workspace" and str(args.get("action","")).lower() == "write":
                if str(args.get("path","")).lower().endswith(".py"):
                    _content = str(args.get("content", ""))[:3000]
            elif tool_name == "python":
                _content = str(args.get("code", ""))[:3000]
            if _content:
                # 检测引用列名/字段名的模式 — 但没看到 print/head/describe 的 EDA
                import re as _re_d
                # 模式 1: df['xxx'] / df["xxx"] 含字面字符串列名
                _has_col_access = bool(_re_d.search(
                    r"""\b(df|data|results?|rows?)\s*\[\s*['"][\w\-]+['"]\s*\]""",
                    _content
                ))
                # 模式 2: row.get('xxx') 或 r['xxx']
                _has_dict_access = bool(_re_d.search(
                    r"""\b\w+\s*(?:\.get\s*\(|\[)\s*['"][\w\-]+['"]""",
                    _content
                ))
                # 模式 3: 写算法/字段名字面值列表 (suspicious of hardcoded names)
                _has_hardcoded_names = bool(_re_d.search(
                    r"""algorithms?\s*=\s*\[\s*['"]""",
                    _content, _re_d.IGNORECASE
                ))
                # 看是否已有 EDA(print + 数据查看)
                _has_eda = any(p in _content for p in (
                    ".head(", ".describe(", ".info(", ".columns",
                    "print(rows[", "print(list(",
                    "pprint(", "for k in ", "for col in",
                    ".unique()", ".value_counts(",
                ))
                if (_has_col_access or _has_dict_access or _has_hardcoded_names) and not _has_eda:
                    self._data_first_print_hint_sent = True
                    _files_str = ", ".join(self._recent_read_data_files[:3])
                    if len(self._recent_read_data_files) > 3:
                        _files_str += f", ... (total {len(self._recent_read_data_files)})"
                    self._pending_data_first_print_hint = (
                        f"[SYSTEM_HINT/data_schema_evidence]\n"
                        f"You recently read data files:\n  {_files_str}\n"
                        f"The current `{args.get('path') or 'python code'}` references literal column or field names "
                        f"before showing schema evidence.\n"
                        f"\n"
                        f"Before relying on hardcoded names, inspect the actual schema and representative values with "
                        f"a small script or print statement. Then write the analysis against observed names rather than "
                        f"memory or task wording.\n"
                        f"\n"
                        f"If schema evidence already exists in recent tool output, continue with that evidence.\n"
                        f"数据分析先确认真实列名和值，再基于证据写代码。"
                    )

        # 2026-05-11 P15.D: bash 工具误用跟踪
        # 触发: bash 用 cat/head/tail/grep + 文件名 ≥ 3 次 (该用 read_file/search_in_file)
        if tool_name == "bash" and args and not self._tool_selection_hint_sent:
            _cmd = str(args.get("command", "") or args.get("cmd", ""))
            # 检测明显该用专用工具的 bash 命令(允许带参数)
            import re as _re_b
            _misuse = bool(_re_b.search(
                # cat/head/tail/less/more + 可选多段参数 + 文件名
                # 例: cat foo.csv | head -50 foo.csv | tail -n 100 foo.txt
                r"^\s*(?:cat|head|tail|less|more)\b[^|;<>]*?[\w./\\-]+\.\w+\s*(?:[|;\s]|$)|"
                # grep + 可选 flag + pattern + 文件名
                r"^\s*grep\b[^|;<>]*?\s[\w./\\-]+\.\w+\s*(?:[|;\s]|$)",
                _cmd
            ))
            if _misuse:
                self._bash_misuse_count += 1

        # 2026-05-12 P31: Python SyntaxError 反复检测
        # 病因: helper 用 workspace.run + 'python -c "..."' 时引号转义 → unterminated string
        # 检测条件: result_str 是 str 且含 SyntaxError 关键字
        # 2026-05-12 hotfix: 函数参数是 result_str 不是 result, P31 引入 NameError
        # 2026-05-12 hotfix #3: 模块用 'import re as _re' 别名, 没有裸 're', 需要本地 import
        if isinstance(result_str, str) and "SyntaxError" in result_str:
            # 匹配 stderr / FIX_HINT 里的 SyntaxError 模式
            import re as _re_p31
            if _re_p31.search(r"SyntaxError:\s*(unterminated\s+string|invalid\s+syntax|unexpected\s+character)", result_str):
                self._python_syntax_error_count += 1

        # 2026-05-12 P22: 产出工具计数(只看是否进入产出阶段, 不惩罚 todo_write/规划)
        # 用户洞察: 先规划再动手是 claudecode 鼓励的, todo_write 多次合理。
        # 真正病态: 工具调用 ≥ 15 但产出工具 = 0 → "动不了手"。
        if tool_name in ("office", "edit_file", "multi_edit", "insert_in_file", "python"):
            self._product_tool_count += 1
        elif tool_name == "workspace" and args:
            _wa = str(args.get("action", "")).lower()
            if _wa in ("write", "append", "commit_to_main"):
                self._product_tool_count += 1

        if tool_name == "office" and args:
            _oa = str(args.get("action", "")).lower()
            if _oa:
                self._recent_office_actions.append(_oa)
                if len(self._recent_office_actions) > 12:
                    self._recent_office_actions.pop(0)

        # 2026-06-04 P130: read helper 读循环 / 无 evidence 跟踪
        # 只对 kind=read|ocr 生效; 主线程或其他 helper 不计。
        if self._kind in ("read", "ocr"):
            if tool_name in self._READ_TOOLS_FOR_READ_HELPER:
                self._read_calls_no_write += 1
            elif tool_name == "workspace" and args:
                _wa = str(args.get("action", "")).lower()
                _wp = str(args.get("path", "")).lower()
                if _wa in ("write", "append") and _wp.endswith((".txt", ".md")):
                    self._evidence_writes += 1
                    self._read_calls_no_write = 0  # 写了 evidence 重置读循环计数
            # 2026-06-05 P130 修复: OCR 工具自身产出 .txt 文件应算 evidence write
            # (实测 trace 990126 19:42 read_classroom_exercises 用 ocr 处理 4 张图片
            # 共生成 14 个 .txt OCR 中间文件,但因为不是 workspace.write 没被记账,
            # 被 P130 误判 "0 evidence" 而 stuck)。OCR 输出本身就是结构化证据。
            elif tool_name == "ocr" and self._is_success(result_str):
                self._evidence_writes += 1
                self._read_calls_no_write = 0
            # 硬升级: 读类调用累计已达硬阈值且仍 0 evidence write
            if (self._evidence_writes == 0
                    and self._read_calls_no_write >= self._READ_NO_EVIDENCE_HARD
                    and not self._stuck):
                self._mark_stuck(
                    f"P130 read-helper no-evidence loop: {self._read_calls_no_write} read-class tool calls "
                    f"({', '.join(self._READ_TOOLS_FOR_READ_HELPER[:5])}, ...) without writing any .txt/.md "
                    f"evidence file. Either the staged inputs are helper-produced artifacts that should be "
                    f"consumed by `kind=edit` (not re-read by `kind=read`), or the read helper failed to converge. "
                    f"The main process should preserve the report, route the inputs to the correct consumer kind, "
                    f"and avoid spawning another read helper for the same artifacts.\n"
                    f"read helper 长时间循环读取却未写 evidence 时，主进程应改派 edit/code 等消费 kind，避免再次派 read。"
                )
                return

        # 2. 检测 workspace.write 写 .py 且文件名/内容像重画图 → 触发 hint
        if (tool_name == "workspace" and args and not self._fetch_then_rewrite_hint_sent
                and len(self._recent_fetched_pngs) >= 2):
            _w_action = str(args.get("action", "")).lower()
            _w_path = str(args.get("path", "")).lower()
            _w_content = str(args.get("content", ""))[:2000]  # 取前 2KB 看
            if (_w_action == "write" and _w_path.endswith(".py")):
                # 文件名含画图关键词 OR content 含 matplotlib/plt
                _name_is_chart = any(kw in _w_path for kw in (
                    "chart", "plot", "gen_chart", "draw", "fig", "graph"
                ))
                _content_is_plot = (
                    "matplotlib" in _w_content or "plt.savefig" in _w_content
                    or "import matplotlib" in _w_content
                )
                if _name_is_chart or _content_is_plot:
                    # 触发反模式 hint
                    self._fetch_then_rewrite_hint_sent = True
                    _fetched_str = ", ".join(self._recent_fetched_pngs[:5])
                    if len(self._recent_fetched_pngs) > 5:
                        _fetched_str += f", ... (total {len(self._recent_fetched_pngs)})"
                    self._pending_fetch_rewrite_hint = (
                        f"[SYSTEM_HINT/reuse_existing_visual]\n"
                        f"You recently fetched existing image assets:\n"
                        f"  {_fetched_str}\n"
                        f"The current `{args.get('path')}` appears to create a replacement plot from code.\n"
                        f"\n"
                        f"Choose deliberately: reuse the fetched image when the task only needs to embed or preserve "
                        f"an existing visual; regenerate only when the requested output needs new data, style, or "
                        f"corrections, and verify the source data schema first.\n"
                        f"\n"
                        f"For document insertion, pass the fetched image path to the document tool. For regenerated "
                        f"charts, verify data labels before plotting.\n"
                        f"已有图片优先复用；确需重画时先验证数据来源。"
                    )


        # ── 2026-05-04 Bug #28: stuck 自动清除逻辑 ──
        # 实测 trace 904c47ec: 一旦 stuck=True 就永远不清除,
        # 后续即使 LLM 成功写 CSV/生成图表/编译通过也不解 stuck,
        # _stuck_consecutive_iters 持续累加 → 最终误 abort。
        # 修法: 最近窗口内成功率 ≥ 50% 或最近连续 2 次成功 → 自动清除 stuck。
        if self._stuck:
            n_recent = len(self._calls)
            if n_recent >= 3:
                n_ok = sum(1 for c in self._calls if c[1])
                if n_ok * 2 >= n_recent:  # ≥50% success
                    self._clear_stuck(
                        f"recent window {n_ok}/{n_recent} success — recovered"
                    )
            # 最近连续 2 次成功也清除(即使窗口不大)
            if self._stuck and n_recent >= 2:
                if all(c[1] for c in self._calls[-2:]):
                    self._clear_stuck("2 consecutive successes — recovered")

        # 触发判断
        if self._consec_fails >= self._CONSECUTIVE_FAIL_LIMIT:
            self._mark_stuck(
                f"Consecutive tool failure: {self._consec_fails} tool calls failed without observable progress.\n"
                f"连续工具失败且无进展。"
            )
            return

        # B9: build 错误特殊路径 — 同一 build 错误模式 ≥2 次就停手。
        # 这类错误(undefined reference, missing header, undeclared id 等)通常是
        # 主线程 prompt 错或环境配置问题,helper 改源码改不出来。2 次足以确认。
        if not ok and any(sig.startswith(p) for p in self._BUILD_ERROR_PREFIXES):
            from collections import Counter
            same_build = Counter(
                c[2] for c in self._calls
                if not c[1] and any(c[2].split("|", 1)[-1].startswith(p)
                                    for p in self._BUILD_ERROR_PREFIXES)
            )
            for build_sig, cnt in same_build.items():
                if cnt >= self._BUILD_ERROR_LIMIT:
                    pretty = build_sig.split("|", 1)[-1]
                    self._mark_stuck(
                        f"Repeated build error: the same build signature appeared {cnt} times ({pretty}). "
                        f"The main process should inspect the helper report, verify the build command and "
                        f"available dependencies, then reroute or resume with explicit build evidence.\n"
                        f"同一构建错误重复出现时，主进程应核对构建命令和依赖后再续作或重派。"
                    )
                    return

        # 2026-05-11 新增: 致命错误特殊路径 — max_tokens 截断/SecurityError/persona_veto
        # 这类错误重复重试不可能恢复, 2 次足以确认。比通用 SAME_ERROR_LIMIT(3) 更激进。
        # 实测教训 trace 15:34-15:43: abpt 3 次空 args 错误花了 9 分钟才 stuck —
        # 第 2 次时就应该停, 省 5 分钟。
        if not ok and any(kw in result_str for kw in self._FATAL_ERROR_KEYWORDS):
            # 数本错误关键词在 _calls 历史里出现次数
            matched_kw = next((kw for kw in self._FATAL_ERROR_KEYWORDS
                               if kw in result_str), None)
            if matched_kw:
                same_fatal = sum(
                    1 for c in self._calls
                    if not c[1] and matched_kw in (c[2] or "")
                )
                # 本次也算上(_calls 已含本次,因为 record 流程已加)
                if same_fatal >= self._FATAL_ERROR_LIMIT:
                    self._mark_stuck(
                        f"Repeated fatal error: `{matched_kw}` appeared {same_fatal} times. The main process "
                        f"should inspect the helper report and reroute with a smaller scope, clearer permissions, "
                        f"or a corrected prompt rather than repeatedly resuming the same failing path.\n"
                        f"致命错误重复时，主进程应缩小范围或修正权限/提示词后重新派发。"
                    )
                    return

        # 只在 recent window 内"失败居多"时才检查同错重复 — 避免误判健康循环
        # (如:一半成功一半失败,但失败错误一致)。要求 fails > 总数的 60%。
        n_recent = len(self._calls)
        n_fails = sum(1 for c in self._calls if not c[1])
        if n_recent < 4 or n_fails * 10 < n_recent * 6:  # 失败 < 60% 直接返回
            return

        # 同一 (tool, error_sig) 在 window 内出现 ≥ N 次
        # ── 2026-05-04 改进:区分"探索性换路"vs"真死循环" ──
        # 旧逻辑用 Counter 统计窗口内所有失败,只要任一签名 ≥ 3 就 stuck。
        # 但 bwt_final 实测:3 次 file-not-found + 3 次 absolute-not-allowed,
        # 模型其实在**主动换思路**(从相对路径换到绝对路径试),却被判 stuck。
        # 修法:只看**最近** SAME_ERROR_LIMIT 次失败是否签名一致。如果模型切换了
        # 策略(签名变了),计数自动从最新签名开始重数,允许探索。
        # 这是真死循环的强信号:连续 N 次同样的错。
        from collections import Counter
        # 取最近的失败(从 window 末尾往前数,直到拿到 _SAME_ERROR_LIMIT 个失败 or 用完)
        recent_fails: list[tuple[str, str]] = []
        for c in reversed(self._calls):
            if not c[1]:  # not ok
                recent_fails.append((c[0], c[2]))
                if len(recent_fails) >= self._SAME_ERROR_LIMIT:
                    break
        # 这 _SAME_ERROR_LIMIT 个失败签名是否全一致?
        if (
            len(recent_fails) >= self._SAME_ERROR_LIMIT
            and recent_fails[0][1]  # 有签名
            and all(f == recent_fails[0] for f in recent_fails)
        ):
            tname, esig = recent_fails[0]
            self._mark_stuck(
                f"Repeated recent error: the last {self._SAME_ERROR_LIMIT} failures share the same signature "
                f"(tool={tname}, summary={esig[:60]!r}).\n"
                f"最近失败签名一致，说明当前路径未收敛。"
            )
            return
        # 旧的"窗口总计"判定保留作为更宽松的兜底,但提高阈值到 4(不再被探索性换路误伤)
        sig_counts = Counter(
            (c[0], c[2]) for c in self._calls if not c[1]
        )
        for (tname, esig), cnt in sig_counts.items():
            if cnt >= self._SAME_ERROR_LIMIT + 1 and esig:  # +1 = 比"最近连续"更宽松的兜底
                self._mark_stuck(
                    f"Repeated window error: one signature appeared {cnt} times in the recent window "
                    f"(tool={tname}, summary={esig[:60]!r}).\n"
                    f"窗口内同一错误重复过多。"
                )
                return

        # 同一工具连续失败 N 次(不被 success 打断)
        recent_tool = [c for c in self._calls if c[0] == tool_name]
        if len(recent_tool) >= self._SAME_TOOL_FAIL_LIMIT and \
                all(not c[1] for c in recent_tool[-self._SAME_TOOL_FAIL_LIMIT:]):
            self._mark_stuck(
                f"Repeated tool failure: `{tool_name}` failed in the last {self._SAME_TOOL_FAIL_LIMIT} "
                f"calls, indicating a repeated pattern.\n"
                f"同一工具连续失败，当前调用方式需要调整。"
            )

        # 2026-05-12 P25: "无产出 stuck" — P22 hint 注入后 helper 仍不动手, 触发 stuck
        # 病因(实测 09:14 trace): paper_final kind=edit 跑 12 iter, 0 office, 1m20s
        # 现有 stuck detector 只看时间间隔(90s 没 tool 返回)+ 错误重复, 不看"无产出"。
        # P22 在 batch=3 时注入 hint, 这里在 batch=6 时仍 0 产出 → 视为 stuck:
        #   - 给 helper 2 batch 缓冲(看到 hint 反应)
        #   - 还没反应 → kill, 主线程换 task_id 重试(铁律 4 路径)
        if (self._no_product_hint_sent
                and self._batch_count >= 6
                and self._product_tool_count == 0):
            self._mark_stuck(
                f"P25 no-product stuck: after {self._batch_count} LLM batches, no output-producing tool "
                f"has been used (office/edit_file/python/workspace.write). The earlier production hint "
                f"did not change behavior. The main process should inspect the helper report and reroute "
                f"with clearer resources, scope, or file paths under a fresh task_id.\n"
                f"多轮无产出时，主进程应检查报告并用更清晰的资源和范围重新派发。"
            )
            return

    def record_batch(self, results: list) -> None:
        """便利方法:一次记录主线程并行 tool_calls 的所有结果。
        results 格式:
        - (tc_id, tool_name, result_str)              ← 老格式
        - (tc_id, tool_name, result_str, args_dict)   ← part20 新格式(用于 same-file 跟踪)
        被主线程 chat_with_tools_loop 的 stuck_main detector 使用。
        2026-05-12 P22: 增 _batch_count(用于"动不了手"检测,
        一次 LLM 响应里多个 parallel tool_call 算 1 batch, 不重复计数)。
        2026-05-12 P24: 检测单 batch 多 todo_write 并行 call (LLM 输出格式异常 —
        把 todos 列表的每项作为独立 tool_call, 而不是用 todos 数组单次调用)。
        实测 09:14: paper_v2 8 个 batch × 45 个并行 todo_write call = 360 次。
        """
        if results:  # 空批次不计数
            self._batch_count += 1
        _missing_probe_paths = self._batch_missing_dependency_probe(results)
        # P24: 数本 batch 内 todo_write 出现次数
        _todo_in_batch = sum(
            1 for entry in results
            if (entry[1] if len(entry) >= 2 else "") == "todo_write"
        )
        if _todo_in_batch >= 5 and not self._batch_todo_warn_sent:
            self._batch_todo_warn_sent = True
            # 标记 — 给下次工具结果附加 hint(在 get_next_hint 里返回)
            self._pending_batch_todo_hint = (
                f"[SYSTEM_HINT/todo_batch_shape]\n"
                f"The previous batch emitted {_todo_in_batch} separate `todo_write` calls, each carrying one todo item.\n"
                f"\n"
                f"Use one `todo_write` call with a `todos` array that contains all items. This keeps planning "
                f"visible without spending multiple tool round trips on the same planning batch.\n"
                f"\n"
                f"Preferred shape:\n"
                f"```\n"
                f"todo_write(todos=[\n"
                f"  {{\"id\": \"1\", \"content\": \"inspect X\", \"status\": \"pending\"}},\n"
                f"  {{\"id\": \"2\", \"content\": \"modify Y\", \"status\": \"pending\"}},\n"
                f"  {{\"id\": \"3\", \"content\": \"verify Z\", \"status\": \"pending\"}},\n"
                f"])\n"
                f"```\n"
                f"规划项应合并为一次 todo_write 调用，避免同一批计划拆成多个工具调用。"
            )
        for entry in results:
            if len(entry) >= 4:
                _id, name, res, args = entry[0], entry[1], entry[2], entry[3]
            else:
                _id, name, res = entry[0], entry[1], entry[2]
                args = None
            if _missing_probe_paths and self._entry_is_missing_dependency_probe(entry):
                self._total_tool_calls += 1
                continue
            self.record(name, res, args=args)
        if _missing_probe_paths:
            self._consec_fails = 0
            if self._stuck and (
                "Consecutive tool failure" in self._stuck_reason
                or "Repeated tool failure" in self._stuck_reason
            ):
                self._clear_stuck("batch missing dependency probe should recover through locate/resource request")
            if "missing_dependency_probe" not in self._soft_hints_sent:
                self._soft_hints_sent.append("missing_dependency_probe")
                shown = ", ".join(_missing_probe_paths[:6])
                more = f", ... total {len(_missing_probe_paths)}" if len(_missing_probe_paths) > 6 else ""
                self._pending_missing_dependency_hint = (
                    "[SYSTEM_HINT/missing_dependency_paths]\n"
                    f"Several read attempts in one batch failed because files were missing: {shown}{more}.\n"
                    "Treat this as a path/resource issue, not proof that the task is impossible. Before another "
                    "read batch, inspect the tool `_suggestions`, use workspace locate once, and check any "
                    "main-process helper result fields such as file_map, main_available_files, copy_stats, "
                    "env_copied_files, or internal_evidence_files. If the files are same-batch producer outputs "
                    "or unstaged project files, request the exact resource and preserve your current state.\n"
                    "多文件读取同时缺失时，先查建议、locate 和主进程结果映射；同批产物或未暂存项目文件应请求资源。"
                )

        # 2026-05-12 P27: 记录本 batch 后的 product_count, 检测停滞
        # 病因: paper 启动后 office 5 次产 docx 骨架, 然后 14 batch 全 workspace.run 找图 → STUCK
        # P22 看"从未产出"会漏这种"启动后停滞", P27 看"最近 N batch 无增长"
        if self._batch_count > 0:
            self._product_count_history.append(self._product_tool_count)
            if len(self._product_count_history) > 8:
                self._product_count_history.pop(0)

    @staticmethod
    def _batch_missing_dependency_probe(results: list) -> list[str]:
        """Return paths when one LLM batch is mainly probing missing files."""
        paths: list[str] = []
        total = 0
        for entry in results or []:
            if len(entry) < 3:
                continue
            tool_name = str(entry[1] or "")
            result_str = str(entry[2] or "")
            if tool_name not in {"read_file", "workspace", "search_in_file", "code_index", "read_function"}:
                continue
            total += 1
            lower = result_str.lower()
            if "file not found" not in lower and "no such file" not in lower:
                continue
            path = ""
            if len(entry) >= 4 and isinstance(entry[3], dict):
                raw = entry[3].get("path") or entry[3].get("file_path") or entry[3].get("filename")
                path = str(raw or "").strip()
            if not path:
                try:
                    data = json.loads(result_str)
                    path = str(data.get("path") or data.get("blocked_path") or "").strip()
                except Exception:
                    path = ""
            if path:
                paths.append(path.replace("\\", "/"))
        if len(paths) >= 3 or (total >= 4 and len(paths) >= 2):
            return list(dict.fromkeys(paths))
        return []

    @staticmethod
    def _entry_is_missing_dependency_probe(entry) -> bool:
        if len(entry) < 3:
            return False
        tool_name = str(entry[1] or "")
        if tool_name not in {"read_file", "workspace", "search_in_file", "code_index", "read_function"}:
            return False
        result_str = str(entry[2] or "").lower()
        return "file not found" in result_str or "no such file" in result_str

    def _mark_stuck(self, reason: str):
        if not self._stuck:
            self._stuck = True
            self._stuck_reason = reason
            log.warning(
                "helper %s STUCK detected: %s", self.task_id, reason,
            )

    def _clear_stuck(self, reason: str = ""):
        """2026-05-04 Bug #28: stuck 状态自动清除。

        实测 trace 904c47ec: StuckDetector 一旦触发就永远 stuck,
        即使后续 iter 里 LLM 成功写 CSV、生成图表,stuck 仍不解除,
        导致 _stuck_consecutive_iters 持续累加 → 最终强制 abort 误杀正常任务。
        """
        if self._stuck:
            self._stuck = False
            self._stuck_reason = ""
            self._consec_fails = 0
            if reason:
                log.info(
                    "helper %s STUCK cleared: %s", self.task_id, reason,
                )
            else:
                log.info(
                    "helper %s STUCK cleared (auto-recovery)", self.task_id,
                )

    @property
    def stuck(self) -> bool:
        return self._stuck

    @property
    def stuck_reason(self) -> str:
        return self._stuck_reason

    # L8-3 (2026-05-09): 同 error ≥ 5 次 → 直接升级,跳过 meta_judge
    @property
    def should_skip_meta_judge(self) -> bool:
        """≥ 5 次同错误签名 → meta_judge 几乎一定同意,跳过以省 30-100s。"""
        return self._consec_fails >= 5

    def consume_soft_hint(self) -> str | None:
        """2026-05-02 part13:每轮拉取一条软提示(若有),不命中返回 None。
        被 chat_with_tools_loop 在每个 iter 末调用,得到的字符串作为 system msg
        注入到下一轮模型输入,引导模型自纠 — 不强制中断。

        当前监测:
        - edit-without-verify(连续 edit ≥3 次没 compile)
        - long-no-delegate(主线程 parallelizable 任务跑久了仍没用 delegate)
        每种 hint 只发一次,避免刷屏。
        """
        if (self._edits_since_verify >= self._EDIT_WITHOUT_VERIFY_LIMIT
                and "edit_without_verify" not in self._soft_hints_sent):
            self._soft_hints_sent.append("edit_without_verify")
            n = self._edits_since_verify
            return (
                f"[SYSTEM_HINT/verify_after_edits]\n"
                f"You have made {n} consecutive edit_file calls without a compile/run verification step. "
                f"Pause local editing and run the smallest relevant verification command now, then use its "
                f"output to decide the next edit.\n"
                f"连续编辑后应先验证，再基于验证结果继续修改。"
            )

        # 2026-05-02 part19:主线程 parallelizable 任务长时间没 delegate
        # 触发条件 ALL of:
        # - 是主线程(helper 一般不该 spawn 二级 helper)
        # - round1 已判任务可并行
        # - 累计 tool_call ≥ 12 次 + 没调过 delegate
        # - recent window 里编码工具占比 ≥ 60%(确认在闷头编码而不是看资料)
        # 一次会话只发一次。
        if (
            self._is_main_thread
            and self._parallelizable
            and self._total_tool_calls >= self._LONG_NO_DELEGATE_TOOL_LIMIT
            and self._delegate_calls == 0
            and "long_no_delegate" not in self._soft_hints_sent
        ):
            n_recent = len(self._calls)
            n_coding = sum(1 for c in self._calls if c[0] in self._CODING_TOOLS)
            if n_recent >= 4 and n_coding * 10 >= n_recent * 6:
                self._soft_hints_sent.append("long_no_delegate")
                return (
                    f"[SYSTEM_HINT/consider_delegation]\n"
                    f"This task was marked parallelizable, but the main process has made "
                    f"{self._total_tool_calls} tool calls without delegating. Reassess the remaining work: "
                    f"if independent subproblems exist, spawn helpers for them now and keep the main process "
                    f"focused on coordination, evidence review, and final integration. Continue serially only "
                    f"when the next step truly depends on the previous result.\n"
                    f"可并行任务长时间未派发时，主进程应重新评估拆分并使用 helper。"
                )

        # 2026-05-05: 空编辑检测 — 连续 ≥3 次 old_string == new_string
        if (
            self._consecutive_empty_edits >= self._EMPTY_EDIT_CONSECUTIVE_LIMIT
            and "empty_edit" not in self._soft_hints_sent
        ):
            self._soft_hints_sent.append("empty_edit")
            n = self._consecutive_empty_edits
            path_hint = (
                f" on `{self._last_empty_edit_path}`"
                if self._last_empty_edit_path else ""
            )
            return (
                f"[SYSTEM_HINT/empty_edit_recovery]\n"
                f"You submitted {n} empty edits{path_hint} where old_string and new_string were identical. "
                f"Refresh the target file content, rebuild the edit from current evidence, or move to the "
                f"next relevant task if no change is needed.\n"
                f"空编辑重复出现时，先重读目标文件并基于最新内容重新构造修改。"
            )

        if (
            not self._office_micro_edit_hint_sent
            and self.task_id != "main_thread"
            and len(self._recent_office_actions) >= 6
        ):
            _tail = self._recent_office_actions[-8:]
            _reads = sum(1 for a in _tail if a == "read")
            _micro = sum(
                1 for a in _tail
                if a in ("replace_block", "replace_blocks", "insert_block", "delete_block", "replace_section")
            )
            _writes = sum(1 for a in _tail if a in ("write", "append", "insert_image", "update_cells"))
            _total_office = len(self._recent_office_actions)
            if (_reads >= 2 and _micro >= 4) or (_total_office >= 8 and (_writes + _micro) >= 5):
                self._office_micro_edit_hint_sent = True
                self._soft_hints_sent.append("office_micro_edit_loop")
                return (
                    "[SYSTEM_HINT/office_finish_review]\n"
                    "The Office artifact has gone through several write/read/replace actions. Switch from "
                    "micro-editing to final review: check whether any acceptance requirement is still missing, "
                    "inspect the document once, and finish the helper report when the artifact is complete. "
                    "Use batch replacement only for substantive factual or structural issues, and use Office "
                    "tools rather than text-file tools for Office documents.\n"
                    "Office 产物多轮修改后应进入验收收尾，避免无意义微修循环。"
                )

        # 2026-05-02 part20:same-file edit ≥ N 次 + 测试仍 fail
        # → 提示放弃局部修补改用 workspace.write 重写整个函数/文件
        if (
            self._most_edited_file
            and self._edits_per_file.get(self._most_edited_file, 0) >= self._EDIT_SAME_FILE_RETRY_LIMIT
            and self._last_run_failed
            and "same_file_edit_fail" not in self._soft_hints_sent
        ):
            self._soft_hints_sent.append("same_file_edit_fail")
            n_edits = self._edits_per_file[self._most_edited_file]
            return (
                f"[SYSTEM_HINT/rethink_repeated_file_edits]\n"
                f"`{self._most_edited_file}` has received {n_edits} edits while verification still fails. "
                f"Stop the local patch loop and rebuild the mental model from the full relevant file or "
                f"function. Then make a root-cause edit, rewrite the affected unit if needed, or ask the "
                f"main process to spawn a fresh helper with the failure evidence.\n"
                f"同一文件反复修补仍失败时，应回到完整上下文做根因修改或重新派发。"
            )

        # 2026-05-15 P103.a: Windows 致命错误第 2 次 → 推 sanitizer 命令
        # 病因: 仅靠 fix_hint 字符串经常被 LLM 忽视, 升级到 system msg 注入。
        # 给具体 bash 命令例子, 让 LLM 直接抄走用。
        if (self._win_fatal_count >= self._WIN_FATAL_SOFT_HINT_AT_2
                and "p103_sanitizer" not in self._soft_hints_sent):
            self._soft_hints_sent.append("p103_sanitizer")
            # 2026-05-21: 据实判断 ASan 是否可用。Windows MinGW 缺 libasan,
            # 在那里推 -fsanitize=address 反而 cannot find -lasan 撞墙(实测 trace c6e42ed6)。
            try:
                from app.llm.tools.workspace import has_asan as _has_asan
                _asan_ok = _has_asan()
            except Exception:
                _asan_ok = False
            if _asan_ok:
                return (
                    f"[SYSTEM_HINT/native_crash_diagnostics]\n"
                    f"{self._win_fatal_count} native fatal runtime errors were observed. AddressSanitizer "
                    f"appears available; use it to obtain concrete memory-error evidence before more edits.\n"
                    f"\n"
                    f"```bash\n"
                    f"gcc -g -O0 -fsanitize=address -fno-omit-frame-pointer your_code.c -o your_test\n"
                    f"./your_test\n"
                    f"```\n"
                    f"\nUse the sanitizer report to identify the failing ownership, bounds, or lifetime path, then edit.\n"
                    f"原生崩溃反复出现时，先用 sanitizer 取证再修改。"
                )
            return (
                f"[SYSTEM_HINT/native_crash_diagnostics]\n"
                f"{self._win_fatal_count} native fatal runtime errors were observed, but AddressSanitizer "
                f"does not appear available in this toolchain. Use smaller reproduction inputs, assertions, "
                f"UBSan if available, and boundary/lifetime logging to locate the failing operation.\n"
                f"\n"
                f"Use evidence from the smallest reproducible case before changing implementation strategy.\n"
                f"ASan 不可用时，用最小复现、断言和边界日志定位后再改。"
            )

        # 2026-05-15 P103.b: 第 3 次 → 建议跳出当前 bug 思路, 重写模块
        # 同 P14.A 思想 (累计错重写), 针对内存 bug 场景。
        if (self._win_fatal_count >= self._WIN_FATAL_SOFT_HINT_AT_3
                and "p103_rewrite" not in self._soft_hints_sent):
            self._soft_hints_sent.append("p103_rewrite")
            return (
                f"[SYSTEM_HINT/native_crash_strategy_reset]\n"
                f"{self._win_fatal_count} native crashes have occurred after diagnostic hints. If the same "
                f"area continues to fail, stop incremental patching and rebuild the affected function or "
                f"module from the full source context. Verify first on a small reproducible input, then scale "
                f"the test. If still blocked, publish progress_note with the suspected failing area and return "
                f"a partial report for rerouting.\n"
                f"原生崩溃多次未收敛时，停止补丁循环，基于完整上下文重写并小规模验证。"
            )


        # 2026-05-11 P14.E: 接口不兼容专用提示(catch 早不要硬改源码)
        # 病因(实测 bench_skiplist): "X has no member named Y" 这类错误反复 7+ 次,
        # 根因是 skiplist.c (旧接口) vs _shared/common.h (新接口) 不兼容,
        # helper 反复尝试改 skiplist.c 适配但根本逻辑不对。
        # 修法: 检测此类错误 ≥ 2 次, 提示"这是接口不兼容, 不是 typo, 不要硬改"。
        if "interface_mismatch" not in self._soft_hints_sent:
            recent_fails = [c[2] for c in self._calls if not c[1]]
            interface_patterns = [
                "has no member named",
                "incompatible pointer type",
                "conflicting types",
                "too many arguments to function",
                "too few arguments to function",
                "expected .* but argument is of type",
                "AttributeError",  # Python 接口错误
                "TypeError: .* takes",  # Python 函数签名错
                "missing 1 required positional argument",
            ]
            import re as _re_mm
            matched = sum(
                1 for f in recent_fails
                if any(_re_mm.search(pat, f or "") for pat in interface_patterns)
            )
            if matched >= 2:
                self._soft_hints_sent.append("interface_mismatch")
                return (
                    f"[SYSTEM_HINT/interface_contract_mismatch]\n"
                    f"{matched} related interface errors were detected. Treat this as a contract mismatch: "
                    f"compare the caller with the actual header, schema, class, or function definition. Adapt "
                    f"owned source to the current contract, or publish progress_note if the contract itself "
                    f"must be changed by the main process.\n"
                    f"接口类错误重复出现时，应先核对真实契约，再决定适配或请求主进程重派。"
                )

        # 2026-05-11 P11: fetch-then-rewrite hint (在 record 里检测, 这里 consume)
        _pending = getattr(self, "_pending_fetch_rewrite_hint", None)
        if _pending:
            self._pending_fetch_rewrite_hint = None  # 消费掉
            return _pending

        # 2026-05-11 P15.C: data-first-print hint (在 record 里检测, 这里 consume)
        _pending_data = getattr(self, "_pending_data_first_print_hint", None)
        if _pending_data:
            self._pending_data_first_print_hint = None
            return _pending_data

        # 2026-05-12 P24: 批量 todo_write 滥用 hint (在 record_batch 里检测, 这里 consume)
        _pending_batch_todo = getattr(self, "_pending_batch_todo_hint", None)
        if _pending_batch_todo:
            self._pending_batch_todo_hint = None
            return _pending_batch_todo

        _pending_missing = getattr(self, "_pending_missing_dependency_hint", None)
        if _pending_missing:
            self._pending_missing_dependency_hint = None
            return _pending_missing

        # 2026-05-12 P27: 产出停滞检测(P22 的兄弟维度)
        # 病因(实测 13:06 paper): kind=edit+mode=hard 前期 office.write 5 次, 后 14 batch
        # 全 workspace.run 'dir /b *.png' 死搜 → STUCK. P22 看"从未产出"漏这种场景。
        # 触发条件: 已产出过 (count > 0) + 最近 5 个 batch product_count 无增长
        # 注意: 不和 P22 重叠 — P22 是 count=0, P27 是 count>0 但停滞
        # 2026-05-12 P28 修正: 主线程跳过(同 P22)。
        if (not self._stagnation_hint_sent
                and self.task_id != "main_thread"
                and self._product_tool_count > 0
                and len(self._product_count_history) >= 5
                and self._product_count_history[-1] == self._product_count_history[-5]):
            self._stagnation_hint_sent = True
            _stagnant_batches = 0
            # 数从尾巴往前看有几个 batch 一直没变
            for i in range(len(self._product_count_history) - 1, 0, -1):
                if self._product_count_history[i] == self._product_count_history[-1]:
                    _stagnant_batches += 1
                else:
                    break
            log.info(
                "P27 hint triggered for helper '%s' at product_count=%d (stagnant %d batches)",
                self.task_id, self._product_tool_count, _stagnant_batches
            )
            return (
                f"[SYSTEM_HINT/output_stagnation]\n"
                f"Output has stagnated: after initial production, the last {_stagnant_batches} batches added no new "
                f"productive tool output, while product_tool_count remains {self._product_tool_count}. "
                f"Pause the current tactic and choose the next evidence-producing step.\n\n"
                f"Likely recovery paths:\n"
                f"- Missing referenced file: publish progress_note with the exact missing file and the path clue from the prompt, "
                f"then wait for the main process to provide or reroute the resource.\n"
                f"- Repeated same-kind error: stop local trial edits, identify the root cause from evidence, or report the blocker "
                f"so the main process can resume/escalate/reroute.\n"
                f"- Enough evidence already exists: return to the assigned output-producing tool and complete the artifact.\n\n"
                f"Continue only with an action that can add new evidence or a verified artifact.\n"
                f"产出停滞时报告缺资源、换证据路径，或回到产物工具完成任务。"
            )

        # 2026-05-12 P31: Python SyntaxError 反复 (≥ 3 次) → 教用 workspace.write
        # 实测 12:10 trace: merge_csv 8 次 / bench_bptree 4 次 SyntaxError, 全是
        # python -c "..." 内层引号转义出错(unterminated string literal at line 1)。
        if (not self._python_syntax_hint_sent
                and self.task_id != "main_thread"
                and self._python_syntax_error_count >= 3):
            self._python_syntax_hint_sent = True
            log.info(
                "P31 hint triggered for helper '%s' at SyntaxError count=%d",
                self.task_id, self._python_syntax_error_count
            )
            return (
                f"[SYSTEM_HINT/python_quote_recovery]\n"
                f"Repeated Python SyntaxError detected ({self._python_syntax_error_count} times), especially line-1 "
                f"unterminated string literal patterns. This usually means `python -c \"...\"` quoting was damaged by "
                f"the shell. Stop retrying one-line commands and write a temporary .py script, then run it.\n\n"
                f"Recommended pattern:\n"
                f"```\n"
                f"workspace(action='write', path='check.py', content='''\n"
                f"import csv\n"
                f"with open('data.csv') as f:\n"
                f"    print(list(csv.DictReader(f))[:3])\n"
                f"''')\n"
                f"workspace(action='run', command='python check.py')\n"
                f"```\n"
                f"\nScript files avoid shell quote interpretation, handle multiline code safely, and produce real line numbers.\n"
                f"复杂 Python 检查写成脚本再运行，避免 shell 引号破坏。"
            )

        # 2026-05-11 P15.D: tool_selection 误用 ≥ 3 次触发
        if (not self._tool_selection_hint_sent
                and self._bash_misuse_count >= 3):
            self._tool_selection_hint_sent = True
            return (
                f"[SYSTEM_HINT/tool_selection]\n"
                f"You have used bash-like commands {self._bash_misuse_count} times for file reading/searching. "
                f"Prefer dedicated tools for structured evidence: read_file for one file, search_in_file for content "
                f"inside a file, and search_files for filenames. Use shell commands for compilation, executing programs, "
                f"git, and real pipelines.\n"
                f"读取/搜索文件优先用专用工具；bash 主要用于编译、运行和管道。"
            )
        # 2026-05-11 P12.H: 长跑无 progress_note 提醒
        # 病因(实测 pptx 跑 22 分钟无 progress_note): 主线程不知道 helper 在干嘛,
        # wait_window 满 600s 后只能瞎猜要不要 kill。
        # 触发: ≥ 30 次工具调用没 progress_note
        # 2026-05-15 P110: 改为**周期性提醒** (而非 one-shot)。
        # 病因(实测排序论文 trace): sort_heap 62 调用 / sort_acms 64 调用 全程 0 progress_note。
        # P12.H one-shot 命中后失效 → 长跑 helper 仍然不发 → 主线程 wait_window 反复 timeout
        # 实测 11 次 wait_window.timeout in 压缩 trace。
        # 修法: 每 30 调用未 progress_note 就重发 (用 _progress_note_hint_count 计数防过频)。
        if self._tool_calls_since_progress_note >= 30:
            # 每 30 调用提示一次, 不再 one-shot
            self._progress_note_hint_count += 1
            _hint_n = self._progress_note_hint_count
            self._progress_note_hint_sent = True  # 老 flag 兼容
            self._tool_calls_since_progress_note = 0  # 重置, 等下一个 30 才再提醒
            return (
                f"[SYSTEM_HINT/progress_visibility]\n"
                f"You have made 30 tool calls without progress_note (reminder {_hint_n}). Long helper tasks should "
                f"publish a short factual note every 10-15 meaningful tool calls: completed work, current action, "
                f"remaining steps, and rough time/uncertainty. This lets the main process monitor without unnecessary resumes.\n"
                f"长任务需定期 progress_note，让主进程看到进展并减少无效续作。"
            )

        # 2026-06-04 P130: read helper 读循环软提示
        # 软阈值: 累计 _READ_NO_EVIDENCE_SOFT 次读取却 0 evidence write
        # 教 helper 立即停止读循环, 写 evidence 或 progress_note 报告 helper-produced 输入。
        if (self._kind in ("read", "ocr")
                and not self._read_no_evidence_hint_sent
                and self._evidence_writes == 0
                and self._read_calls_no_write >= self._READ_NO_EVIDENCE_SOFT):
            self._read_no_evidence_hint_sent = True
            return (
                f"[SYSTEM_HINT/read_helper_no_evidence]\n"
                f"You have made {self._read_calls_no_write} read-class tool calls (read_file/search_in_file/...) "
                f"without writing any `.txt` or `.md` evidence file. Either:\n"
                f"  1) The staged inputs are helper-produced artifacts (filenames like `*_analysis.md`, "
                f"`framework_contract*`, `*_evidence.txt`, `*_inventory.md`) — your scope is user-provided source "
                f"material, not sibling helper output. Stop now, write a short report `Infeasible: staged inputs "
                f"are helper-produced; route to kind=edit (or appropriate consumer) instead`, and finish.\n"
                f"  2) You have enough coverage to write the evidence file. Write it now (workspace.write to a "
                f"`*_evidence.txt`) and finalize the short report; further reads should be limited to named gaps.\n"
                f"读取类调用累计较多但未写 evidence 时，先判断输入是否为 helper 产物（应改派消费 kind），否则立即写 evidence 并收尾。"
            )

        # 2026-06-05 P130 第二级强提示: 第一次软提示后 helper 仍在读循环, 距离硬阈值
        # 还有 ~12 次余量, 但要明确警告"不写 evidence 必停手", 给 LLM 最后一次自决机会。
        if (self._kind in ("read", "ocr")
                and self._read_no_evidence_hint_sent
                and not self._read_no_evidence_strong_hint_sent
                and self._evidence_writes == 0
                and self._read_calls_no_write >= self._READ_NO_EVIDENCE_STRONG):
            self._read_no_evidence_strong_hint_sent = True
            return (
                f"[SYSTEM_HINT/read_helper_no_evidence_FINAL]\n"
                f"You are now at {self._read_calls_no_write} reads with 0 evidence writes. Hard stop will trigger "
                f"at {self._READ_NO_EVIDENCE_HARD}. THIS IS THE LAST WARNING. Pick ONE in your very next tool call:\n"
                f"  (a) workspace.write a `*_evidence.txt` with everything you've found so far, then finalize.\n"
                f"  (b) Output the final report immediately with whatever conclusion the existing reads support, "
                f"even if partial. A short honest 'partial coverage of X, missing Y' is better than another read.\n"
                f"  (c) If the task is impossible (inputs are helper-produced, or files don't exist), state that "
                f"explicitly in the final report and stop.\n"
                f"再读必停手；下次工具调用必须是写 evidence 或最终报告，不接受继续读取。"
            )


        # 病因(实测 09:14 trace): paper_final 12 iter, 1m20s, 134 次工具调用, 
        # 0 次产出 → STUCK 杀。根因: 主进程 prompt 说"chart 在 temp 根目录", 
        # 实际带 helper 前缀在 _helpers_shared/.../ → helper 在错路径死搜。
        # 用户洞察:
        #   ① 不应惩罚 todo_write(claudecode 鼓励先规划)
        #   ② 一次 LLM 响应 N 个并行 call 算 1 batch, 不是 N 次
        #   ③ 文件搜索应该主进程指引, helper 找不到 → 立即 progress_note 求助
        # 触发: ≥ 3 batch 但产出工具 = 0 → 教 helper 自救
        # 2026-05-12 P28 修正: 主线程跳过 (主线程是调度, 不该产出工具,
        # 实测 12:11 trace 主线程误触发 P22). 只 helper 触发。
        if (not self._no_product_hint_sent
                and self.task_id != "main_thread"
                and self._batch_count >= 3
                and self._product_tool_count == 0):
            self._no_product_hint_sent = True
            log.info(
                "P22 hint triggered for helper '%s' at batch_count=%d (product_count=0)",
                self.task_id, self._batch_count
            )
            return (
                f"[SYSTEM_HINT/no_product_progress]\n"
                f"You have completed {self._batch_count} LLM batches without any output-producing tool call "
                f"(office/edit_file/python/workspace.write). Planning is useful, but this helper now needs "
                f"an evidence-producing or artifact-producing action. If a required resource or path is missing, "
                f"publish progress_note with the exact blocker; otherwise start the smallest concrete output step.\n"
                f"多轮无产出时，应报告缺失资源或开始最小可验证产出。"
            )

        # 2026-05-11 P15.A: 按需注入纪律提示
        # 设计哲学:不在 system prompt 塞所有教学(LLM 看不进去),
        # 触发时机才注入相关教学内容(LLM 当下需要才看到)。
        # 之前 _SHARED_TIMEOUT / _SHARED_C_BUGS / _SHARED_DEBUG 段定义了不用 = 死代码。
        # 现在变成:检测到对应触发条件 → inject 简版教学。

        recent_fails = [c[2] for c in self._calls if not c[1]]
        recent_fail_strs = [s for s in recent_fails if isinstance(s, str)]

        # P15.A.1: timeout 处理
        # 触发: 最近 _calls 中有 timed_out 信号 + 未提过 + 不在 stuck 状态
        if "timeout_discipline" not in self._soft_hints_sent:
            timeout_count = sum(
                1 for s in recent_fail_strs
                if ("tool_timeout" in s) or ('timed_out' in s and 'true' in s)
            )
            if timeout_count >= 1:
                self._soft_hints_sent.append("timeout_discipline")
                return (
                    "[SYSTEM_HINT/timeout_discipline]\n"
                    "A tool result reported `timed_out: true`. Treat timeout as an execution-budget signal first: "
                    "retry with an appropriate timeout or smaller input, and only change implementation when "
                    "evidence shows incorrectness or non-converging performance.\n"
                    "超时优先视为执行预算信号，先调预算或缩小输入，再按证据决定是否改实现。"
                )

        # P15.A.2: C 编译错误纪律
        # 触发: 最近失败含 gcc/编译错误信号 ≥ 2 次
        if "c_compile_discipline" not in self._soft_hints_sent:
            compile_count = sum(
                1 for s in recent_fail_strs
                if any(p in s for p in (
                    "fatal error:", "error:", "undeclared",
                    "implicit declaration", "warning:", "undefined reference",
                ))
            )
            if compile_count >= 2:
                self._soft_hints_sent.append("c_compile_discipline")
                return (
                    "[SYSTEM_HINT/compile_error_discipline]\n"
                    "Compile-related errors appeared {} times. Read the complete compiler/linker output, "
                    "map it to the relevant source and interface contract, then make the smallest evidence-based "
                    "change and verify immediately. If the error class changes, reassess from the new evidence "
                    "instead of continuing the same patch loop.\n"
                    "编译错误重复出现时，先完整读错误与相关接口，再做最小证据化修改并立即验证。"
                ).format(compile_count)

        # P15.A.3: 工具选择纪律
        # 触发: 最近 _calls 中 bash 用于明显该 read_file 的场景 ≥ 3 次
        # (启发式: bash 命令含 cat/head/tail/grep + 文件名)
        if "tool_selection_discipline" not in self._soft_hints_sent:
            bash_misuse_count = 0
            for c in self._calls:
                if c[0] == "bash" and c[1]:  # bash 调用 (无论成功失败)
                    # 没办法直接拿 args, 只能粗略看错误签名
                    pass  # 暂不实现 (难精准检测)
            # 暂留接口, 后续可加 args 跟踪

        return None

    @staticmethod
    def _is_success(result_str: str) -> bool:
        """粗判工具结果是否成功。无法解析时保守视为成功(避免误触发)。"""
        s = (result_str or "").strip()
        if not s:
            return True
        # 2026-05-02 Bug I 修:rate_limited 是配额限制,不是能力失败,不算失败。
        # 实测 trace e4eeb133: 主线程 4 次连续 rate_limited 被 stuck detector 误判成
        # "无任何进展" → meta-judge 升级 hard→veryhard → 多跑 30 分钟。
        if '"rate_limited": true' in s or '"rate_limited":true' in s:
            return True
        # 2026-06-05: Windows git-bash Cygwin fork 资源耗尽属环境抖动,不是 LLM 策略错误。
        # 病因(实测 trace 394304 14:45-15:05): 多 helper 并行 + 长复合命令导致 Cygwin
        # 多次 dofork 失败 (errno 11 / Resource temporarily unavailable) 返回 rc=254。
        # 这种失败 LLM 重试同一命令几乎一定继续失败,但已经通过 fix_hint 提示拆分;
        # 不再计入 _consec_fails 避免凑齐 4 次环境抖动就触发 stuck (浪费 helper 续作)。
        if "dofork: child" in s and "Resource temporarily unavailable" in s:
            return True
        # 显式 error 字段
        if '"error":' in s and '"ok": false' in s:
            return False
        if '"ok": false' in s:
            return False
        if s.startswith('{"error"'):
            return False
        # workspace.run rc!=0 视为失败
        import re as _re_local
        m = _re_local.search(r'"returncode":\s*(-?\d+)', s)
        if m and int(m.group(1)) != 0:
            return False
        return True

    @staticmethod
    def _has_failure_signal(result_str: str) -> bool:
        """检测 workspace.run/python 输出是否含测试失败信号。

        part20:用于 same-file edit 检测 — 工具本身 ok=true(命令跑了) 但 stdout 含 FAIL/error
        说明逻辑 bug 还在。区别于 _is_success(它只看工具调用是否成功)。

        ── 2026-05-04 Bug #2 修复(Razor 虚报):"假 PASS"模式 ──
        Razor helper trace:测了 3 个 edge case + 12 个 SKIP(因为路径解析错误读不到
        测试数据),最后输出 "Round-trip: 3/3 passed, 0 failed"。从 returncode 看
        ok,从"3/3 passed"看 ok,但实际 12 个文件根本没测。helper system prompt 的
        铁律#5 早写过"声称 round-trip 通过前必须 SHA-256 比对",但模型还是栽了。
        这里在工具层兜底:输出含"SKIP"/"can't read"/"N/A"等 sentinel 占多数时,
        视同 failure 让 stuck detector 反应。
        """
        s = (result_str or "")
        if not s:
            return False
        # workspace.run 失败或脚本 raise
        if '"returncode":' in s:
            import re as _re_local
            m = _re_local.search(r'"returncode":\s*(-?\d+)', s)
            if m and int(m.group(1)) != 0:
                return True
        # 测试输出关键字
        # ── 2026-05-04 修(pre-existing 误报) ──
        # 旧版 " failed" 会匹配 "0 failed"(干净输出最后一行 "3/3 passed, 0 failed")
        # 导致 _last_run_failed 永远 True,same-file edit count 永不清零,
        # 错误触发"重写整个文件"软提示。
        # 修法:" failed" 改用 regex 要求前面有非零数字,或完整短语 "build failed" / "test failed"。
        FAIL_PATTERNS = (
            "FAIL:", "FAIL ", "FAILED", "Error:", "error:",
            "AssertionError", "Traceback", "round-trip mismatch",
            "mismatch!", "不匹配", "❌", "Test failed",
            "build failed", "Build failed", "compilation failed",
        )
        if any(p in s for p in FAIL_PATTERNS):
            return True
        # 数字 + " failed":要求非零计数
        import re as _re_failcheck
        m_fail = _re_failcheck.search(r"\b([1-9]\d*)\s+failed\b", s)
        if m_fail:
            return True

        # ── Bug #2:扫"假 PASS"模式 ──
        # SKIP/N/A/can't read 占测试输出比例过高 → 视为 failure
        # (即使输出最后写"3/3 passed",那是它自欺欺人)
        import re as _re_local2
        FAKE_PASS_TOKENS = (
            "SKIP:", "SKIP ", " skipped", "Skipped",
            "can't read", "cannot read", "cannot open", "can't open",
            "n/a", "N/A", "not available", "Not available",
            "not found", "Not found",
        )
        skip_count = sum(s.count(tok) for tok in FAKE_PASS_TOKENS)
        # round-trip / Test 标记数:粗判"测试规模"
        m_total = _re_local2.search(r"(\d+)/(\d+)\s+passed", s, _re_local2.IGNORECASE)
        if m_total:
            try:
                passed = int(m_total.group(1))
                total = int(m_total.group(2))
                # passed 数远小于 SKIP 数 → 数据明显被跳过却报 PASS
                if skip_count >= max(3, passed) and skip_count >= total // 2:
                    return True
            except (ValueError, IndexError):
                pass
        # 即使没 X/Y passed 标记,只要 SKIP > 5 次也很可疑(批量跳过)
        if skip_count >= 5:
            return True
        return False

    @staticmethod
    def _error_signature(tool_name: str, result_str: str) -> str:
        """提取错误签名 — 用于判同。

        2026-05-02 改进 (B9): 改为提取**语义级错误模式**而非原始 stderr。
        旧版直接用 stderr 前 200 char,但 gcc 错误每次报告的临时文件名/行号不同,
        即使根因相同(比如反复 undefined reference)签名也不一样,stuck_detector
        触发不了。

        新版规则: 从 stderr/stdout 中提取错误"指纹"(类型 + 关键 symbol):
          - "undefined reference to `<symbol>`" → "linker:undef:<symbol>"
          - "fatal error: <header>: No such file" → "missing_header:<header>"
          - "error: implicit declaration of function '<fn>'" → "missing_decl:<fn>"
          - "error: '<id>' undeclared" → "undecl_id:<id>"
          - "error: redefinition of '<id>'" → "redef:<id>"
          - "Segmentation fault" / "core dumped" → "segfault"
          - "TimeoutError" / 工具超时 → "tool_timeout"
          - 其他: 取 stderr 第一个 "error:" 行(去数字/路径)
        """
        import re as _re_local
        s = result_str or ""
        # 取 error / stderr / stdout 字段(优先 stderr)
        text = ""
        for fld in ("stderr", "stdout", "error"):
            m = _re_local.search(rf'"{fld}":\s*"([^"]{{0,2000}})"', s)
            if m:
                text += " " + m.group(1)
        if not text and '"ok"' in s:
            text = s[:500]

        # ── 语义模式提取(优先级从特定到通用) ──
        # 1. linker undefined reference (最常见 gcc multi-file 错误)
        m = _re_local.search(r"undefined reference to `([A-Za-z_][A-Za-z0-9_]*)'", text)
        if m:
            return f"{tool_name}|linker_undef:{m.group(1)}"
        # 2. missing header
        m = _re_local.search(r"fatal error: ([^:\\\\\\n]+): No such file", text)
        if m:
            return f"{tool_name}|missing_header:{m.group(1).strip()}"
        # 3. implicit declaration
        m = _re_local.search(r"implicit declaration of function ['`]([A-Za-z_][A-Za-z0-9_]*)['`]", text)
        if m:
            return f"{tool_name}|missing_decl:{m.group(1)}"
        # 4. undeclared identifier
        m = _re_local.search(r"['`]([A-Za-z_][A-Za-z0-9_]*)['`] undeclared", text)
        if m:
            return f"{tool_name}|undecl_id:{m.group(1)}"
        # 5. redefinition
        m = _re_local.search(r"redefinition of ['`]([A-Za-z_][A-Za-z0-9_]*)['`]", text)
        if m:
            return f"{tool_name}|redef:{m.group(1)}"
        # 6. crash / segfault
        if _re_local.search(r"Segmentation fault|core dumped|SIGSEGV", text):
            return f"{tool_name}|crash_segfault"
        # 7. abort
        if _re_local.search(r"Assertion .* failed|assert.*failed|SIGABRT", text):
            return f"{tool_name}|crash_assert"
        # 8. python: exception type
        m = _re_local.search(
            r"\\n([A-Z][A-Za-z]+(?:Error|Exception)): ", text,
        )
        if m:
            return f"{tool_name}|py_exc:{m.group(1)}"
        # 9. tool timeout
        if "timeout" in text.lower() or "timed out" in text.lower():
            return f"{tool_name}|tool_timeout"
        # 10. error 字段(workspace 之外)
        m = _re_local.search(r'"error":\s*"([^"]{0,150})"', s)
        if m:
            err = m.group(1)
        else:
            # 取第一行 error: ...
            m2 = _re_local.search(r"error:\s*([^\\\\n]{0,80})", text)
            err = m2.group(1) if m2 else text[:80]
        # 去数字、长 hex、绝对路径
        err = _re_local.sub(r"\d+", "#", err)
        err = _re_local.sub(r"[a-fA-F0-9]{6,}", "#HEX#", err)
        err = _re_local.sub(r"[A-Z]:[\\\\/][^\\s'\"]+", "#PATH#", err)  # Windows 路径
        err = _re_local.sub(r"/[a-zA-Z0-9_/.-]+", "#PATH#", err)  # POSIX 路径
        return f"{tool_name}|{err[:80]}"


def _estimate_msgs_tokens(messages: list[dict]) -> int:
    """粗估 messages 总 tokens (1 token ≈ 2.5 bytes UTF-8 中英混合)。"""
    total_bytes = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            total_bytes += len(c.encode("utf-8", errors="replace"))
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict):
                    text = part.get("text") or ""
                    total_bytes += len(str(text).encode("utf-8", errors="replace"))
    return total_bytes // 3  # 约略 token 数


def _stall_threshold_for(messages: list[dict]) -> float:
    """阈值: <50K tok 用 90s, 50-100K 用 150s, 100-200K 用 240s, >200K 用 360s。"""
    t = _estimate_msgs_tokens(messages)
    if t > 200_000:
        return 360.0
    if t > 100_000:
        return 240.0
    if t > 50_000:
        return 150.0
    return 90.0
