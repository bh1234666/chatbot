"""orchestrator 校验/自动修复 helpers(macro 升级信号 / autofix deliverables / sibling 文件检查)。
2026-05-20 从 orchestrator.py 抽出;re-export 兼容。
"""
from __future__ import annotations
import ast
import json
import logging
import os
import re
from app.config import settings
from app.schemas.api import ResponsePlan
from app.core import debug
from app.core.orchestrator_utils import (
    _is_internal_deliverable_file,
    _is_ocr_intermediate_image,
    _AUTOFIX_INTERNAL_BLACKLIST_PATTERNS,
    _is_internal_file,
)
from app.llm.tools import workspace as ws_tool

log = logging.getLogger("app.core.orchestrator")


# ─── Macro escalation signals (绕开 meta_judge 的硬规则) ────────────────────
# 实测 trace 8b60c2 教训:meta_judge 8 轮全说"问题在收敛",理由全是
# "最近 3 步成功"——完全忽略 macro 信号(40 分钟无产出 / 6/7 helper batch timeout)。
# 这里提供独立于 meta_judge 的硬触发器。
#
# B7 修订 (2026-05-02): 用户要求"放宽限制,不怕跑久"。这些阈值不再是"结束门",
# 仅是"建议升级"信号。即使触发,只是把模型从 lite 升 hard,不会强制结束 round2。
# 既然要放宽,阈值也调宽:
#   - iter 50 才升 hard(原 30)
#   - 30min 才升 hard(原 15min)
#   - iter 30 / 15min 才进黄灯(原 18 / 8min)
# batch_timeout 仍然立刻红灯 — 多个 helper 同时撞墙说明任务粒度有大问题,
# 这个信号比时间更可靠。
_MACRO_HARD_ITER_THRESHOLD = settings.macro_hard_iter         # 硬触发: iter > N + 无 deliverable 实际产出 (默认 50)


_MACRO_YELLOW_ITER_THRESHOLD = settings.macro_yellow_iter     # 黄灯: 让 cross-LLM 评估 (默认 30)


_MACRO_BATCH_TIMEOUT_KEYWORDS = (
    '"batch_timeout_majority": true',  # JSON 字符串里出现 = delegate 多数撞墙
    '"batch_timeout_majority":true',
)


def _check_macro_escalation_signals(
    final_msgs: list[dict],
    plan: 'ResponsePlan',
    macro_signals: dict | None = None,
    *,
    yellow_only: bool = False,
    workspace_dir: str = "",
) -> dict:
    """检查 macro escalation 信号(硬触发 / 黄灯触发)。

    硬触发(yellow_only=False): 任一满足即返回 should_escalate=True
    黄灯触发(yellow_only=True): 较低阈值,仅做 cross-LLM 第二意见的入口判定

    Args:
        final_msgs: chat_with_tools_loop 返回的完整 message 历史
        plan: 已解析的 ResponsePlan(用于看 deliverables)
        macro_signals: 可选,live tracking dict 含 iter_count / start_time /
                       batch_timeout_seen / workspace_snapshot / last_file_check_iter。
                       如提供则优先使用真实 elapsed 数据,
                       否则 fallback 到从 final_msgs 估算 iter 数。
        workspace_dir: 工作区路径,用于文件系统级别的 stall 检测

    Returns: {"should_escalate": bool, "reason": str, "level": "red"|"yellow"|None,
              "signals": dict}
    """
    import time as _t

    # ── 信号采集:优先 live macro_signals,fallback 到 final_msgs 估算 ──
    if macro_signals is not None:
        iter_count = macro_signals.get("iter_count", 0)
        start_time = macro_signals.get("start_time", _t.monotonic())
        elapsed = _t.monotonic() - start_time
        batch_timeout_seen = macro_signals.get("batch_timeout_seen", False)
        batch_timeout_advice = macro_signals.get("batch_timeout_advice", "")
        workspace_snapshot = macro_signals.get("workspace_snapshot", frozenset())
    else:
        # 估算 — tool_msgs 数 ≈ iter 数
        iter_count = sum(1 for m in final_msgs if m.get("role") == "tool")
        elapsed = 0.0  # 没有 start_time 时无法估,跳过时间检查
        batch_timeout_seen = False
        batch_timeout_advice = ""
        workspace_snapshot = frozenset()
        for m in final_msgs:
            if m.get("role") != "tool":
                continue
            content = str(m.get("content", ""))
            if any(kw in content for kw in _MACRO_BATCH_TIMEOUT_KEYWORDS):
                batch_timeout_seen = True
                break

    iter_threshold = (
        _MACRO_YELLOW_ITER_THRESHOLD if yellow_only
        else _MACRO_HARD_ITER_THRESHOLD
    )

    # ── 工作区 stall 检测:对比当前文件 vs 快照 ──
    # 如果工作区有新文件(自 snapshot 以来) → 有实质进展,不应因 iter 数触发升级
    has_new_files_since_snapshot = False
    if workspace_dir and workspace_snapshot:
        try:
            current_files = frozenset(ws_tool.list_generated_files(workspace_dir))
            has_new_files_since_snapshot = not current_files.issubset(workspace_snapshot)
        except OSError:
            pass

    # 信号 3: deliverables 列了但工作区里没产出 — "空转"判断
    deliverables_listed = bool(plan.deliverables)
    no_real_output = (
        deliverables_listed
        and iter_count > iter_threshold
        and not _has_workspace_files_produced(
            final_msgs,
            workspace_dir=workspace_dir,
            snapshot=workspace_snapshot,
        )
        and not has_new_files_since_snapshot
    )

    signals = {
        "iter_count": iter_count,
        "elapsed_sec": round(elapsed, 1),
        "batch_timeout_seen": batch_timeout_seen,
        "no_real_output": no_real_output,
        "deliverables_count": len(plan.deliverables),
        "has_new_files_since_snapshot": has_new_files_since_snapshot,
    }

    # ── 硬触发(yellow_only=False)── 严重信号,直接 escalate ──
    if not yellow_only:
        if batch_timeout_seen:
            return {
                "should_escalate": True, "level": "red",
                "reason": (
                    f"delegate batch_timeout_majority=true indicates multiple helpers reported a timeout-majority signal. "
                    f"Main-thread serial takeover is likely slower; advice={batch_timeout_advice[:100]}.\n"
                    f"多个 helper 超时信号触发，建议升级或调整协作。"
                ),
                "signals": signals,
            }
        # 35+ iter 且 elapsed > 10min (双条件)
        if iter_count >= 35 and elapsed > 600:
            return {
                "should_escalate": True, "level": "red",
                "reason": (
                    f"Iteration count is {iter_count} and elapsed time is {elapsed:.0f}s (>10min); "
                    f"the workflow appears stuck despite local progress signals.\n"
                    f"长时间多轮迭代后仍疑似卡住。"
                ),
                "signals": signals,
            }
        # 20 分钟硬上限(无论 iter 数)
        if elapsed > 1200:
            return {
                "should_escalate": True, "level": "red",
                "reason": f"Elapsed time is {elapsed:.0f}s (>20min); escalation is required.\n超过 20 分钟需要升级。",
                "signals": signals,
            }
        # iter 数超阈值 — 只有无新增文件时才升级(真停滞),有产出则不升级
        if iter_count > _MACRO_HARD_ITER_THRESHOLD:
            if not has_new_files_since_snapshot:
                return {
                    "should_escalate": True, "level": "red",
                    "reason": (
                        f"The tool loop has run {iter_count} iterations with no new workspace files "
                        f"(hard threshold {_MACRO_HARD_ITER_THRESHOLD}); confirmed stagnation.\n"
                        f"多轮工具调用无新增文件，确认停滞。"
                    ),
                    "signals": signals,
                }
            # 有新增文件 → 只是步骤多,不触发升级
        if no_real_output:
            return {
                "should_escalate": True, "level": "red",
                "reason": (
                    f"After {iter_count} iterations, the plan lists {len(plan.deliverables)} deliverable(s) "
                    f"but the workspace has no new files; likely no real output.\n"
                    f"计划有产物但工作区无新增文件，疑似空转。"
                ),
                "signals": signals,
            }

    # ── 黄灯(仅 yellow_only=True 时返回 should_escalate=True)──
    # 关键修复(实测 trace 9ca732f4 教训):yellow_only=False 时,黄灯不能自动 escalate,
    # 否则会绕过 cross-LLM 二审直接升级。这就是 6 分钟刚跑完 benchmark 就升 hard 的根因 —
    # macro 信号阈值低(iter≥18 / elapsed>300s)正常长任务都触达,只能作为"建议二审"信号。
    if iter_count >= _MACRO_YELLOW_ITER_THRESHOLD or elapsed > 300:
        if yellow_only:
            # 调用方明确要"是否需要 cross-LLM 二审" → True
            return {
                "should_escalate": True, "level": "yellow",
                "reason": (
                    f"yellow signals: iter={iter_count}, elapsed={elapsed:.0f}s "
                    f"may require cross-LLM review because local progress can mask stagnation.\n"
                    f"黄灯信号，建议二审判断是否卡住。"
                ),
                "signals": signals,
            }
        # else: 红灯检查没触发,黄灯不自动升级 — 调用方应再调 yellow_only=True 走二审

    return {"should_escalate": False, "reason": "", "level": None, "signals": signals}


def _has_workspace_files_produced(
    final_msgs: list[dict],
    *,
    workspace_dir: str | None = None,
    snapshot: frozenset | None = None,
) -> bool:
    """粗判 — workspace.write 或 office tool 是否产出过文件。

    当 snapshot 提供时：检查是否有**新增**文件(文件系统 vs 快照的差集)，
    这是 stall 检测的核心——自 round2 开始以来工作区有无实质变化。

    当只有 workspace_dir 时：检查工作区是否有任何文件。

    兜底：扫描工具输出的字符串模式。
    """
    # 优先: 文件系统检查(带 snapshot 差集)
    if workspace_dir:
        try:
            current = ws_tool.list_generated_files(workspace_dir)
            if snapshot is not None:
                # 有快照 → 检查是否有**新**文件(不在快照中的)
                if any(f for f in current if f not in snapshot):
                    return True
            elif current:
                return True
        except OSError:
            pass
    # 兜底: 旧的字符串扫
    for m in final_msgs:
        if m.get("role") != "tool":
            continue
        content = str(m.get("content", ""))
        # workspace.write 成功 / office 成功 / python 创建图片
        if (
            ('"action": "write"' in content and '"ok": true' in content)
            or ('"action":"write"' in content and '"ok":true' in content)
            or ('"saved_path"' in content)
            or ('.png' in content and '"ok": true' in content)
            or ('.docx' in content and '"ok": true' in content)
            or ('.pptx' in content and '"ok": true' in content)
            or ('.xlsx' in content and '"ok": true' in content)
        ):
            return True
    return False


# ── deliverables 兜底 ────────────────────────────────────────
# 设计动机:模型常忘了把交付物列入 plan.deliverables(把 key_points/round3 回复
# 贴代码当成"已经给用户了"),导致用户拿不到文件。这里按启发式补充,只在:
#   - needs_tools=True
#   - plan.deliverables 为空
#   - 工作区有本轮新生成的非临时文件
# 三个条件都满足时触发。
_AUTOFIX_DELIVERY_EXTS = {
    # 用户日常关注的产物类型(非临时脚本)
    ".c", ".cpp", ".h", ".hpp",
    ".py", ".js", ".ts", ".java", ".go", ".rs", ".rb", ".php",
    ".sh", ".bat", ".ps1",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
    ".xlsx", ".xls", ".csv",
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".txt", ".md", ".rtf",
    ".html", ".htm", ".json", ".xml", ".yaml", ".yml",
    ".zip", ".tar", ".gz", ".7z", ".rar",
    ".mp3", ".mp4", ".wav", ".ogg",
    # 注:**不**含 .exe — chat.py 的 _BLOCKED_EXTENSIONS 一律 403 拒下载,
    # autofix 选了 .exe 等于白选(实测 trace 77310db6:autofix 选了
    # lz78_final_test.exe,download_file API 拒绝,用户拿不到)。
}


_AUTOFIX_SKIP_PATTERNS = (
    "__pycache__", ".pyc", ".tmp", ".bak", ".log", ".swp",
    "_delegate_",                   # delegate helper 工作区目录前缀
    "extract.py", "convert.py", "save.py",  # 模型常用的转换脚本名
    # 2026-05-04 v19: helper 内部 summary / 状态文件,绝不该作为 deliverable 推送
    # 实测 trace 1fbb00b6:autofix 选了 .helper_sort_paper_summary.txt 当头号
    # deliverable(因为命中下面 _AUTOFIX_PRODUCTION_HINTS 的 "summary" 关键词
    # 得 10 分),用户拿到的是 helper 内部状态文件不是论文。
    ".helper_",                     # .helper_<task>_summary.txt
    ".read_history.json", ".todos.json", ".edit_history.json",  # v18 P1 防御
    "_workspace_files.txt", "__shared_list.txt",  # helper 调试用临时清单
    "_test_image.",                 # helper 自测图片(如 sort_ppt_test_image.pptx)
)


# 中间脚本的动词命名前缀——这类源码文件几乎都是"为了跑出产物"的工具脚本,不该作为交付物
_AUTOFIX_INTERMEDIATE_SCRIPT_PREFIXES = (
    "make_", "build_", "run_", "test_", "extract_", "convert_",
    "export_", "import_", "process_", "generate_", "save_",
    "load_", "fetch_",
    # 2026-05-04 v19: helper 内部脚本前缀(常见于 sort_paper / sort_ppt helper)
    "check_", "dump_", "verify_", "analyze_", "analyze",
    "_build_", "_inspect_", "gen_",
)


_AUTOFIX_PRODUCTION_HINTS = (       # 命名暗示是"最终产物"
    "fixed", "final", "output", "result", "out_", "_out",
    "_fix", "_final", "report", "answer", "solution",
    # 2026-05-04 v19: 删除 "summary" — 实测 trace 1fbb00b6 它误命中
    # .helper_*_summary.txt 让 helper 内部状态文件得 10 分排名第一,
    # autofix 把 helper 中断报告当 deliverable 推给用户。
    # "summary" 触发率太高且与"用户最终成果"语义弱关联,删除。
)


_AUTOFIX_FILE_INTENT_KEYWORDS = (   # 用户消息里暗示要文件交付的关键词
    # 注意：不包含"给我"/"完成"/"实现"/"研究"等短词——它们在日常对话太常见，误匹配率高
    "输出", "下载", "保存", "导出", "发我",
    "再发", "重发", "重新发", "没发", "没收到", "发一遍", "传一遍",
    "修代码", "改代码", "帮我修", "帮我改", "帮我写",
    "做一份", "做个", "画一", "画个", "画张", "画下",
    "生成", "做表", "写一个",
    "完成任务", "论文", "汇报",
)


# 2026-05-04 v19: 同分排序时,这些扩展名优先(它们是用户最终想要的核心交付物)
_AUTOFIX_FINAL_DELIVERABLE_EXTS = {
    ".docx", ".pptx", ".xlsx", ".pdf", ".csv",
    ".mp3", ".wav", ".ogg",
}


def _check_sibling_files(
    plan: ResponsePlan,
    workspace_dir: str,
    files_before: set,
) -> None:
    """P91: 检查 plan.deliverables 是否漏了"兄弟文件"——同前缀同扩展的同类文件。

    规则:
      1. 把 plan.deliverables 中文件按 (前缀, 扩展名) 分组
      2. 若某组有 ≥2 个文件 → 这是一个"系列"
      3. 工作区里同模式的新文件如果不在 plan.deliverables → 自动加进去

    实测 trace(00:45 排序论文): plan 列了 results_quick/merge/heap/timsort/acms.csv 5 个,
    但漏 results_insertion.csv。前缀 "results", 扩展 ".csv" 是同一系列, insertion helper
    已成功产出, 应自动补。
    """
    import re as _re_p91
    import os as _os_p91

    # 收集 plan.deliverables 中的文件模式
    _patterns: dict[tuple[str, str], list[str]] = {}
    for f in plan.deliverables:
        _base = _os_p91.path.basename(f).replace("\\", "/")
        _name, _ext = _os_p91.path.splitext(_base)
        _parts = _name.split("_")
        if len(_parts) >= 2:
            _prefix = _parts[0]
            _patterns.setdefault((_prefix, _ext.lower()), []).append(_base)

    # 没有 ≥2 文件的系列 → 不做任何事 (避免误判单文件)
    _series = {k: v for k, v in _patterns.items() if len(v) >= 2}
    if not _series:
        return

    # 看工作区有哪些新文件
    try:
        _all_ws = ws_tool.list_generated_files(workspace_dir)
    except Exception:
        return
    _new_ws_files = [f for f in _all_ws if f not in files_before]
    if not _new_ws_files:
        return

    # 对每个系列, 找漏掉的兄弟
    _existing_names = {_os_p91.path.basename(f).replace("\\", "/") for f in plan.deliverables}
    _added: list[str] = []
    for (_prefix, _ext), _siblings in _series.items():
        # 模式: <prefix>_<word>.<ext>, 仅匹配 "prefix_" 开头加任意单词然后扩展名
        _pattern = _re_p91.compile(
            rf"^{_re_p91.escape(_prefix)}_\w+{_re_p91.escape(_ext)}$",
            _re_p91.IGNORECASE,
        )
        for _wf in _new_ws_files:
            _base = _os_p91.path.basename(_wf).replace("\\", "/")
            if not _pattern.match(_base):
                continue
            if _base in _existing_names:
                continue
            if _is_ocr_intermediate_image(_wf.lower()):
                continue
            # P15.F 内部文件黑名单守门 (避免误推内部元数据)
            _base_low = _base.lower()
            if any(p in _base_low for p in _AUTOFIX_INTERNAL_BLACKLIST_PATTERNS):
                continue
            # 排除中间脚本前缀
            if any(_base_low.startswith(p) for p in _AUTOFIX_INTERMEDIATE_SCRIPT_PREFIXES):
                continue
            # 找到漏掉的兄弟 — 加进去
            plan.deliverables.append(_wf)
            _existing_names.add(_base)
            _added.append(_base)

    if _added:
        debug.log(
            "round2.deliverables.sibling_autofix",
            f"P91: plan.deliverables 漏了兄弟文件 {_added} — 已自动补加 "
            f"(原 plan 已有同模式 {sum(len(v) for v in _series.values())} 个文件)",
            {"added": _added, "series": {f"{k[0]}_*{k[1]}": v for k, v in _series.items()}},
        )


def _add_mentioned_existing_deliverables(
    plan: ResponsePlan,
    workspace_dir: str,
    files_before: set,
) -> None:
    """Add files explicitly mentioned by the plan text when they exist in workspace.

    This is intentionally narrower than the empty-plan autofix: it does not scan
    for arbitrary workspace outputs. It only closes contradictions where the
    model says a file was generated/sent in intent/key_points/internal_note but
    forgot to include that exact file in deliverables.
    """
    if not workspace_dir:
        return
    plan_text = "\n".join([
        str(plan.intent or ""),
        "\n".join(str(x) for x in (plan.key_points or [])),
        str(plan.internal_note or ""),
    ])
    if not plan_text.strip():
        return
    try:
        all_files = ws_tool.list_generated_files(workspace_dir)
    except Exception:
        return
    existing_by_base = {os.path.basename(f): f for f in all_files}
    current = {os.path.basename(str(f)) for f in (plan.deliverables or [])}
    added: list[str] = []
    upload_prefixes = ("uploaded_files/", "uploaded_files\\", "group_files/", "group_files\\")
    for match in re.finditer(
        r"(?<![\w.-])([\w\u4e00-\u9fff][\w\u4e00-\u9fff .()\-\[\]]{0,120}\."
        r"(?:docx|pptx|xlsx|pdf|md|txt|csv|json|png|jpg|jpeg|svg|zip|html|wav|mp3|ogg|m4a))(?![\w.-])",
        plan_text,
        re.IGNORECASE,
    ):
        name = match.group(1).strip().strip("`'\"，。；;:、)")
        base = os.path.basename(name)
        if base not in existing_by_base:
            suffix_matches = [
                existing for existing in existing_by_base
                if base.endswith(existing)
            ]
            if suffix_matches:
                base = max(suffix_matches, key=len)
        if not base or base in current:
            continue
        low = base.lower()
        if _is_internal_file(low):
            continue
        if base not in existing_by_base:
            continue
        rel = existing_by_base[base]
        rel_norm = str(rel).replace("\\", "/")
        if rel in files_before or rel_norm in files_before:
            continue
        if rel_norm.startswith(upload_prefixes):
            continue
        full = os.path.join(workspace_dir, rel)
        try:
            if os.path.getsize(full) == 0:
                continue
        except OSError:
            continue
        plan.deliverables.append(rel)
        current.add(base)
        added.append(base)
        if len(added) >= 12:
            break
    if added:
        debug.log(
            "round2.deliverables.mentioned_autofix",
            f"added files mentioned in plan text but missing from deliverables: {added}",
            {"added": added},
        )


def _autofix_deliverables(
    plan: ResponsePlan,
    *,
    user_message: str,
    needs_tools: bool,
    workspace_dir: str,
    files_before: set,
) -> None:
    """plan.deliverables 为空时按启发式补充。原地修改 plan。

    保守策略——只在三种情况下补:
      1. 用户消息含明显的"要文件"信号(输出/给我/修代码 等)
      2. 工作区有命名带 fixed/final/output 等"产物"暗示的文件
      3. 工作区有图表/文档/压缩包/可执行等"非纯源码"产物(用户场景下罕见做但不交付)

    源码文件(.c/.py/.js 等)默认 NOT 自动补,**除非**满足 1 或 2——
    防止把测试脚本误推送给用户。

    2026-05-15 P91: 即使 plan.deliverables 非空, 也补"漏掉的兄弟文件"。
    病因(实测 00:45 排序论文 trace): LLM 在 plan 里列了 5 个 results_*.csv (quick/
    merge/heap/timsort/acms), 但忘了 results_insertion.csv (insertion helper 已成功
    完成)。论文里提 6 个算法, 但只交付 5 个 CSV — 用户拿到不完整数据。
    """
    # 2026-05-15 P91: 即使 plan.deliverables 非空, 检查命名模式有没有漏掉的兄弟
    if plan.deliverables and workspace_dir:
        try:
            _check_sibling_files(plan, workspace_dir, files_before)
        except Exception:
            log.exception("P91 sibling check failed")
        try:
            _add_mentioned_existing_deliverables(plan, workspace_dir, files_before)
        except Exception:
            log.exception("mentioned deliverable autofix failed")
    if plan.deliverables:
        return  # 模型已经填了, 不再做空 → 补的兜底
    if not needs_tools:
        return
    if not workspace_dir:
        return

    try:
        all_files = ws_tool.list_generated_files(workspace_dir)
    except Exception:
        return
    new_files = [f for f in all_files if f not in files_before]
    candidate_source = list(new_files)
    if not candidate_source:
        return

    # 用户消息暗示要交付文件吗?(中文/小写都查)
    msg_low = user_message.lower()
    has_file_intent = any(kw in msg_low for kw in _AUTOFIX_FILE_INTENT_KEYWORDS)

    candidates: list[tuple[int, str]] = []
    for fname in candidate_source:
        fname_low = fname.lower()
        basename_low = os.path.basename(fname_low)
        if _is_ocr_intermediate_image(fname_low):
            continue
        if _is_internal_deliverable_file(fname):
            continue

        # 临时文件直接跳过
        if any(p in fname for p in _AUTOFIX_SKIP_PATTERNS):
            continue
        ext = os.path.splitext(fname)[1].lower()
        if ext not in _AUTOFIX_DELIVERY_EXTS:
            continue

        # 2026-05-06 §C6.1: 零字节 + 语法正确性最低限度检查
        full_path = os.path.join(workspace_dir, fname)
        try:
            if os.path.getsize(full_path) == 0:
                continue
        except OSError:
            continue
        if ext == ".py":
            try:
                ast.parse(open(full_path, encoding="utf-8", errors="replace").read())
            except (SyntaxError, OSError):
                continue
        elif ext == ".json":
            try:
                json.load(open(full_path, encoding="utf-8", errors="replace"))
            except (ValueError, OSError):
                continue

        # 2026-05-12 P15.F: 内部元数据文件硬黑名单(无论 score 多高都跳过)
        # 实测 trace 23:46+: auto-added 8 个全是 .rewrite_count.json 等内部文件,
        # 用户拿到一堆垃圾。这些文件**绝对**不该出现在 deliverables 里。
        if _is_internal_file(fname_low):
            continue

        # 中间脚本(make_xxx.py / export_xxx.py 这种)——动词命名+源码扩展名,跳过
        is_source = ext in (".c", ".cpp", ".h", ".hpp", ".py", ".js", ".ts",
                            ".java", ".go", ".rs", ".sh", ".bat", ".ps1")
        if is_source and any(basename_low.startswith(pre) for pre in _AUTOFIX_INTERMEDIATE_SCRIPT_PREFIXES):
            continue

        score = 0

        # 命名暗示是产物
        if any(h in fname_low for h in _AUTOFIX_PRODUCTION_HINTS):
            score += 10

        # 用户消息提到了文件类型
        if ext.lstrip(".") in msg_low:
            score += 5

        # 二进制/结构化产物(非源码)倾向是用户最终想要的(.exe 已不在白名单,
        # 因为下载会被 chat.py 拒绝)
        if ext in (
            ".png", ".jpg", ".svg", ".pdf", ".xlsx", ".docx", ".pptx",
            ".ppt", ".csv", ".zip", ".html", ".mp3", ".wav", ".ogg",
        ):
            score += 4

        # 用户明确说"要文件"
        if has_file_intent:
            score += 6

        # 源码文件需要更强的信号才入选,防止把测试/中间脚本误推
        # 默认阈值 10(命中 production hint);用户明确说要文件(has_file_intent)
        # 降为 6——意味着「用户喊『帮我修代码』时,工作区里所有非中间脚本前缀的
        # 源码都该作为交付物」。trace 6353027e 实测:用户「帮我修一下」→ 模型修
        # hello.c、生成 hello_fixed.exe → 旧阈值 10 只补了 .exe,用户没拿到修好
        # 的源码。
        src_threshold = 6 if has_file_intent else 10
        if is_source and score < src_threshold:
            continue

        if score > 0:
            candidates.append((score, fname))

    if not candidates:
        return

    # 2026-05-04 v19: 同分时,docx/pptx/xlsx/pdf 等"最终交付物扩展名"优先级高于源码
    # 实测 trace 1fbb00b6: 排序算法汇报.pptx 和 sort_ppt_chart_*.png 同 4 分,
    # 切片只取 3 个时 .pptx 被切掉,用户拿不到核心产物。
    def _sort_key(item):
        score, fname = item
        ext_lower = os.path.splitext(fname)[1].lower()
        ext_priority = 0 if ext_lower in _AUTOFIX_FINAL_DELIVERABLE_EXTS else 1
        return (-score, ext_priority, fname)
    candidates.sort(key=_sort_key)

    # 2026-05-04 v19: 上限 6 → 8(原 3 → v18 改 6 → v19 改 8)
    # 大型研究任务(论文+PPT+Excel+图表+源码)合理 deliverable 可达 10+,
    # 6 仍可能漏掉源码或 chart。8 给足空间,极端噪声场景 LLM 自己应该填 plan.deliverables。
    auto_picks = [f for _, f in candidates[:8]]

    plan.deliverables = list(auto_picks)
    debug.log(
        "round2.deliverables.autofix",
        f"plan.deliverables was empty → auto-added {auto_picks}",
        {
            "all_new_files": new_files,
            "scored_candidates": [(s, f) for s, f in candidates],
            "user_msg_intent": has_file_intent,
        },
    )
