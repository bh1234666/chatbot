"""helper 类型/模式的常量、校验、配置查询,以及任务 ID 分类工具。

2026-05-20 重构: 从 llm/tools/delegate.py 原样抽出。经 tools/extract_analysis.py
--closure 验证自包含(10 符号, 0 unsafe)。除 stdlib(re)外仅依赖 workspace_utils
的 _has_office_document_output(纯叶子模块,无循环风险)。delegate.py 通过 re-export
保持兼容,调用点零改动。
"""
import re
import re as _re
from app.llm.tools.workspace_utils import _has_office_document_output


_HELPER_TOOL_FILTER_CACHE: dict[tuple[str, tuple[tuple[str, int], ...]], list] = {}


def _helper_tool_filter_signature(all_tools: list) -> tuple[tuple[str, int], ...]:
    """Return a cheap stable signature for helper tool-filter cache invalidation."""
    signature: list[tuple[str, int]] = []
    for tool in all_tools:
        if isinstance(tool, dict):
            function = tool.get("function", {}) or {}
            signature.append((str(function.get("name", "")), id(tool)))
        else:
            signature.append((type(tool).__name__, id(tool)))
    return tuple(signature)


# ── helper kind/mode 白名单 ──
MODEL_VISIBLE_HELPER_KINDS = (
    "code", "edit", "verify", "draw", "tts", "read",
    "project_map", "file_summary", "impact_review", "inventory",
)


LEGACY_HELPER_KIND_ALIASES = ("summarize",)


VALID_HELPER_KINDS = MODEL_VISIBLE_HELPER_KINDS + LEGACY_HELPER_KIND_ALIASES


VALID_HELPER_MODES = ("easy", "hard")


def _normalize_helper_kind_mode(kind: object = None, mode: object = None) -> tuple[str, str]:
    """Normalize main-visible helper fields while preserving old callers."""
    k = str(kind or "code").strip().lower()
    m = str(mode or "easy").strip().lower()

    if k == "coding":
        k = "code"
    elif k == "final":
        k = "code"
        m = "hard"
    elif k == "ocr":
        k = "read"
    elif k == "summarize":
        k = "inventory"

    if m == "normal":
        m = "easy"
    elif m == "final":
        m = "hard"

    if k not in VALID_HELPER_KINDS:
        k = "code"
    if m not in VALID_HELPER_MODES:
        m = "easy"
    return k, m


def _is_legacy_paired_task_id(tid: str) -> bool:
    return (
        tid.endswith("_hard")
        or bool(_re.search(r"_hard_\d+$", tid))
        or tid.endswith("_final")
        or tid.endswith("_final_auto")
        or bool(_re.search(r"_final_\d+$", tid))
    )


def _is_legacy_paired_hard_task(task: dict) -> bool:
    tid = str(task.get("task_id") or "")
    if task.get("mode") != "hard":
        return False
    return _is_legacy_paired_task_id(tid)


def _is_scaffold_task(prompt: str, expected_outputs: list[str] | None = None) -> list[str]:
    """检测任务是否为脚手架/框架建设(2026-05-15 P63)。

    脚手架任务的特征: 建立**接口耦合的多文件骨架**, 拆开后兄弟 helper 各发明各的
    接口集成必崩 (实测教训 trace 822f2aaa skiplist `create_ops()` vs 其他算法
    `extern const xxx_ops` 接口不一致; 05-15 16:26 comp_framework 被守卫拆解后浪费一轮 LLM)。

    返回触发的信号 list (空 = 不是脚手架)。
    """
    p = prompt or ""
    outputs = list(expected_outputs or [])
    signals: list[str] = []

    # 显式关键词
    _scaffold_keywords = (
        "统一接口", "公共框架", "脚手架", "框架先行", "统一框架", "基础框架",
        "interface header", "scaffold", "baseline framework", "framework header",
        "common interface", "unified interface",
    )
    if any(k in p for k in _scaffold_keywords):
        signals.append("scaffold_keyword")

    # expected_outputs 含 .h + .c + (Makefile/CMakeLists) 组合 — 经典脚手架信号
    _has_header = any(str(x).endswith((".h", ".hpp", ".hxx")) for x in outputs)
    _has_source = any(str(x).endswith((".c", ".cpp", ".cc")) for x in outputs)
    _has_build = any(
        str(x).rsplit("/", 1)[-1] in ("Makefile", "CMakeLists.txt")
        or str(x).endswith((".cmake",))
        for x in outputs
    )
    if _has_header and _has_source and _has_build:
        signals.append("outputs_h+c+build")
    elif _has_header and _has_source and len(outputs) >= 4:
        signals.append("outputs_mixed_h+c_4plus")

    # prompt 引用 .h 头文件并定义 typedef/struct 等接口 - 同时给出 .c 实现 + Makefile
    _prompt_has_h = bool(re.search(r"\b\w+\.h\b", p))
    _prompt_has_c = bool(re.search(r"\b\w+\.(?:c|cpp)\b", p))
    _prompt_has_makefile = "Makefile" in p or "CMakeLists" in p
    _prompt_has_typedef = "typedef" in p or "struct " in p or "extern const" in p
    if _prompt_has_h and _prompt_has_c and _prompt_has_makefile and _prompt_has_typedef:
        signals.append("prompt_full_scaffold")

    return signals


def _is_substantive_analysis_or_code_task(prompt: str, expected_outputs: list[str] | None = None) -> bool:
    """Detect tasks that need code/analysis before document assembly."""
    p = prompt or ""
    text = f"{p.lower()} {' '.join(str(x) for x in (expected_outputs or [])).lower()}"
    english_signals = (
        "data analysis", "analyze data", "analyse data", "statistical",
        "statistics", "regression", "correlation", "forecast", "modeling",
        "modelling", "optimization", "simulation", "benchmark", "compile",
        "run tests", "python script", "write script", "generate csv",
        "process csv", "process dataset", "etl", "pandas", "numpy", "scipy",
        "sklearn", "matplotlib", "plotly", "seaborn", "sql", "notebook",
        "directory statistics", "file count", "line count", "character count",
        "source analysis", "batch source", "walk the directory", "walk directory",
        "run command", "shell command", "command output", "largest files",
        "top files", "loc count", "count lines", "count characters",
    )
    if any(s in text for s in english_signals):
        return True
    chinese_signals = (
        "\u6570\u636e\u5206\u6790", "\u7edf\u8ba1\u5206\u6790",
        "\u56de\u5f52\u5206\u6790", "\u76f8\u5173\u6027",
        "\u9884\u6d4b", "\u5efa\u6a21", "\u4f18\u5316\u6c42\u89e3",
        "\u4eff\u771f", "\u8bfb\u53d6 csv", "\u5904\u7406 csv",
        "\u5904\u7406\u6570\u636e", "\u6e05\u6d17\u6570\u636e",
        "\u751f\u6210 csv", "\u7f16\u5199\u811a\u672c",
        "\u8fd0\u884c\u811a\u672c", "\u7f16\u8bd1",
        "\u8dd1\u6d4b\u8bd5", "\u7b97\u6cd5\u5b9e\u73b0",
        "\u53ef\u89c6\u5316",
        "\u76ee\u5f55\u7edf\u8ba1", "\u6587\u4ef6\u6570\u91cf",
        "\u884c\u6570", "\u5b57\u7b26\u6570", "\u6700\u5927\u7684",
        "\u6700\u5927\u6587\u4ef6", "\u6279\u91cf\u6e90\u7801",
        "\u6e90\u7801\u5206\u6790", "\u8fd0\u884c\u547d\u4ee4",
        "\u547d\u4ee4\u8f93\u51fa", "\u904d\u5386\u76ee\u5f55",
    )
    if any(s in p for s in chinese_signals):
        return True
    if re.search(r"\b[\w./\\-]+\.(?:csv|jsonl|parquet|sqlite|db|xlsx)\b", text):
        return True
    if re.search(r"\b[\w./\\-]+\.(?:py|ipynb|sql|c|cpp|js|ts|go|rs)\b", text):
        return True
    return False


def _is_document_assembly_task(prompt: str) -> bool:
    """Detect final document assembly from already-produced evidence/results."""
    p = prompt or ""
    lower = p.lower()
    english_signals = (
        "assemble", "write the final", "final report", "document assembly",
        "based on existing", "based on the existing", "use existing results",
        "use the provided results", "existing results", "existing benchmark",
        "from existing", "from the existing", "do not recompute", "do not rerun",
        "editorial", "draft into docx", "turn into a docx",
    )
    chinese_signals = (
        "\u6210\u6587", "\u7ec4\u88c5", "\u6574\u5408", "\u6c47\u603b",
        "\u64b0\u5199\u62a5\u544a", "\u751f\u6210\u62a5\u544a",
        "\u57fa\u4e8e\u5df2\u6709", "\u57fa\u4e8e\u73b0\u6709",
        "\u5df2\u6709\u7ed3\u679c", "\u73b0\u6709\u7ed3\u679c",
        "\u4e0d\u8981\u91cd\u65b0\u8ba1\u7b97", "\u4e0d\u8981\u91cd\u8dd1",
        "\u4e0d\u518d\u8ba1\u7b97", "\u53ea\u8d1f\u8d23\u6210\u6587",
        "\u7f16\u8f91 helper", "\u7f16\u8f91\u5e2e\u624b",
        "\u6392\u7248", "\u5199\u5165 docx", "\u751f\u6210 docx",
    )
    return any(s in lower for s in english_signals) or any(s in p for s in chinese_signals)


def _has_text_report_output(prompt: str, expected_outputs: list[str] | None = None) -> bool:
    """Detect user-facing prose/report artifacts that should be assembled by edit helpers."""
    p = prompt or ""
    lower = p.lower()
    outputs = [str(x or "").replace("\\", "/").lower() for x in (expected_outputs or [])]
    prose_exts = (".md", ".markdown", ".rst", ".txt", ".html", ".htm")
    if any(o.endswith(prose_exts) for o in outputs):
        return True
    prose_words = (
        "report", "document", "documentation", "readme", "docs/",
        "changelog", "release notes", "usage notes", "api reference",
        "algorithm_report", "summary.md", "testing.md",
    )
    chinese_words = (
        "报告", "文档", "说明文档", "使用说明", "总结", "变更记录", "接口文档",
    )
    if any(w in lower for w in prose_words) or any(w in p for w in chinese_words):
        return bool(re.search(r"\b[\w./\\-]+\.(?:md|markdown|rst|txt|html|htm)\b", lower))
    return False


def _text_report_outputs_only(expected_outputs: list[str] | None = None) -> bool:
    outputs = [str(x or "").replace("\\", "/").lower() for x in (expected_outputs or []) if str(x or "").strip()]
    if not outputs:
        return False
    prose_exts = (".md", ".markdown", ".rst", ".txt", ".html", ".htm")
    return all(o.endswith(prose_exts) for o in outputs)


def _has_non_text_implementation_output(expected_outputs: list[str] | None = None) -> bool:
    outputs = [str(x or "").replace("\\", "/").lower() for x in (expected_outputs or []) if str(x or "").strip()]
    source_or_data_exts = (
        ".py", ".pyi", ".c", ".h", ".cpp", ".hpp", ".cc", ".js", ".ts", ".tsx",
        ".go", ".rs", ".java", ".kt", ".cs", ".csv", ".json", ".jsonl",
        ".parquet", ".sqlite", ".db",
    )
    return any(o.endswith(source_or_data_exts) for o in outputs)


def _is_code_project_companion_output(prompt: str, expected_outputs: list[str] | None = None) -> bool:
    """Detect project files that should remain with code ownership.

    Code helpers may own companion files when they are part of a runnable or
    testable project: README/docs, fixtures, examples, manifests, configs, UI
    assets, and small text/data files that must stay consistent with source and
    tests. Standalone final reports or polished documents still belong to edit.

    当 README、文档、fixture、配置、示例等是代码工程的一部分时，归 code 维护；独立最终报告仍归 edit。
    """
    p = prompt or ""
    lower = p.lower()
    outputs = [str(x or "").replace("\\", "/").lower() for x in (expected_outputs or []) if str(x or "").strip()]
    if not outputs:
        return False

    final_doc_signals = (
        "final report", "polished report", "standalone report", "deliverable report",
        "document assembly", "write the final", "based on existing results",
        "turn into a docx", "office", "docx", "pptx", "xlsx", "pdf",
        "最终报告", "独立报告", "正式报告", "成文", "排版", "基于已有结果", "写入 docx",
    )
    if any(s in lower or s in p for s in final_doc_signals):
        return False

    project_signals = (
        "project", "scaffold", "repository", "repo", "codebase", "package",
        "module", "source", "implementation", "implement", "debug", "compile",
        "test", "tests", "fixture", "example", "sample", "manifest", "config",
        "build", "runnable", "run locally", "agent", "api", "ui", "cli",
        "工程", "项目", "代码", "源码", "实现", "调试", "编译", "测试",
        "脚手架", "可运行", "配置", "示例", "样例", "接口",
    )
    if not any(s in lower or s in p for s in project_signals):
        return False

    companion_names = (
        "readme", "license", "changelog", "contributing", "makefile",
        "cmakelists.txt", "package.json", "pyproject.toml", "setup.py",
        "requirements.txt", "tsconfig.json", "vite.config", "webpack.config",
        "rollup.config", "eslint.config", ".gitignore", "dockerfile",
    )
    companion_dirs = (
        "docs/", "doc/", "tests/", "test/", "fixtures/", "fixture/",
        "examples/", "example/", "samples/", "sample/", "config/", "configs/",
        "scripts/", "assets/", "public/", "static/", "templates/",
    )
    companion_exts = (
        ".md", ".markdown", ".rst", ".txt", ".html", ".htm", ".css",
        ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg",
        ".env.example", ".csv",
    )
    return any(
        any(name in o.rsplit("/", 1)[-1] for name in companion_names)
        or any(o == d.rstrip("/") or o.startswith(d) for d in companion_dirs)
        or o.endswith(companion_exts)
        for o in outputs
    )


def _is_readonly_project_analysis_task(prompt: str, expected_outputs: list[str] | None = None) -> bool:
    """Detect architecture/project-understanding tasks that fit project analysis helpers."""
    p = prompt or ""
    lower = p.lower()
    outputs = [str(x or "").strip() for x in (expected_outputs or []) if str(x or "").strip()]
    if outputs:
        return False
    read_only_signals = (
        "read-only", "read only", "do not modify", "without modifying",
        "只读", "不要修改", "不修改", "无需修改",
    )
    project_signals = (
        "architecture review", "project map", "architecture map", "structure map",
        "large files", "split candidates", "split risks", "entry points",
        "module responsibilities", "dependency overview", "project structure",
        "source evidence", "read real project file evidence",
        "架构审查", "架构评审", "工程结构", "项目结构", "拆分候选",
        "拆分风险", "模块职责", "入口点", "依赖概览", "阅读真实文件",
    )
    return (
        any(s in lower or s in p for s in read_only_signals)
        and any(s in lower or s in p for s in project_signals)
    )


def _auto_correct_obvious_helper_kind(kind: str, prompt: str, expected_outputs: list[str] | None = None) -> tuple[str, str | None]:
    """Preserve the main process helper-kind decision.

    Kind choice is model-owned: the main process proposes it and the LLM guard
    can reject with structured feedback. This function intentionally performs
    no non-LLM kind rewriting.

    helper 类型由主进程和 LLM 守卫决定；这里不做非 LLM 模式改写。
    """
    return kind, None


# 2026-05-12 P45: helper 类型配置化 (参考 Claude Code AgentTool 设计)
# 病因(参考工程师审查): kind 分支散落 12+ 处, 不同 kind 的属性(超时/工具/模型/
# stuck 检测)在调用点临时判断, 增加 kind 时容易遗漏。
# 这里集中定义每种 kind 的属性, 作为**单一信息源** (single source of truth)。
# 当前各分支保持原代码不变(避免大规模重构回归), 但新代码应优先查 HELPER_CONFIGS。
# 未来逐步把分支替换为 get_helper_config(kind).attr 访问。
HELPER_CONFIGS: dict[str, dict] = {
    "code": {
        "description":          "Code, algorithm, and mathematical implementation helper",
        "default_timeout_sec":  None,   # 无硬超时, 由 stuck detector 兜底
        "has_stuck_detector":   True,   # P14 等系统级 stuck 检测启用
        "can_write_workspace":  True,
        "can_run_bash":         True,
        "can_spawn_subhelper":  False,  # 不允许子 helper 套娃
        "default_model_tier":   "main", # main / lite
        "supports_resume":      True,
        "supports_hard_mode":    True,
        "supports_verify":      True,    # 主线程可派 verify 校验
        "_branch_locations": "delegate.py:4872, 4878, 5650, 7111, 7186, 8056, 9947",
    },
    "edit": {
        "description":          "Document and artifact editing helper",
        "default_timeout_sec":  None,
        "has_stuck_detector":   True,
        "can_write_workspace":  True,
        "can_run_bash":         False,  # edit 不应跑 shell
        "can_spawn_subhelper":  False,
        "default_model_tier":   "main",
        "supports_resume":      True,
        "supports_hard_mode":    True,
        "supports_verify":      False,
        "_branch_locations": "delegate.py:4878, 7111",
    },
    "project_map": {
        "description":          "Project structure and architecture summary helper",
        "default_timeout_sec":  None,
        "has_stuck_detector":   True,
        "can_write_workspace":  False,
        "can_run_bash":         False,
        "can_spawn_subhelper":  False,
        "default_model_tier":   "lite",
        "supports_resume":      False,
        "supports_hard_mode":    True,
        "supports_verify":      False,
        "_branch_locations": "delegate.py:_select_helper_system/_filter_tools_for_kind",
    },
    "file_summary": {
        "description":          "Focused source/config file summary helper",
        "default_timeout_sec":  None,
        "has_stuck_detector":   True,
        "can_write_workspace":  False,
        "can_run_bash":         False,
        "can_spawn_subhelper":  False,
        "default_model_tier":   "lite",
        "supports_resume":      False,
        "supports_hard_mode":    True,
        "supports_verify":      False,
        "_branch_locations": "delegate.py:_select_helper_system/_filter_tools_for_kind",
    },
    "impact_review": {
        "description":          "Read-only change impact and risk review helper",
        "default_timeout_sec":  None,
        "has_stuck_detector":   True,
        "can_write_workspace":  False,
        "can_run_bash":         False,
        "can_spawn_subhelper":  False,
        "default_model_tier":   "main",
        "supports_resume":      False,
        "supports_hard_mode":    True,
        "supports_verify":      False,
        "_branch_locations": "delegate.py:_select_helper_system/_filter_tools_for_kind",
    },
    "inventory": {
        "description":          "Environment-only project inventory helper",
        "default_timeout_sec":  None,
        "has_stuck_detector":   True,
        "can_write_workspace":  False,
        "can_run_bash":         False,
        "can_spawn_subhelper":  False,
        "default_model_tier":   "lite",
        "supports_resume":      False,
        "supports_hard_mode":    True,
        "supports_verify":      False,
        "_branch_locations": "delegate.py:_select_helper_system/_filter_tools_for_kind; registry.py:tools_for_runtime_mode",
    },
    "summarize": {
        "description":          "Legacy alias for environment-only project inventory helper",
        "default_timeout_sec":  None,
        "has_stuck_detector":   True,
        "can_write_workspace":  False,
        "can_run_bash":         False,
        "can_spawn_subhelper":  False,
        "default_model_tier":   "lite",
        "supports_resume":      False,
        "supports_hard_mode":    True,
        "supports_verify":      False,
        "_branch_locations": "delegate.py:_select_helper_system/_filter_tools_for_kind; registry.py:tools_for_runtime_mode",
    },
    "final": {
        "description":          "High-resource strict helper mode",
        "default_timeout_sec":  None,
        "has_stuck_detector":   False,  # P20 关键: final 模式不再 stuck
        "can_write_workspace":  True,
        "can_run_bash":         True,
        "can_spawn_subhelper":  False,
        "default_model_tier":   "main",  # 或 max (取决于触发原因)
        "supports_resume":      True,
        "supports_hard_mode":    False,
        "supports_verify":      True,
        "_branch_locations": "delegate.py:4506, 4872, 5650, 8056, 9947",
    },
    "verify": {
        "description":          "Read-only adversarial verification helper",
        "default_timeout_sec":  None,
        "has_stuck_detector":   True,
        "can_write_workspace":  False,  # verify 不应写产物
        "can_run_bash":         False,
        "can_spawn_subhelper":  False,
        "default_model_tier":   "main",
        "supports_resume":      False,
        "supports_hard_mode":    True,
        "supports_verify":      False,  # 不能验证 verify
        "_branch_locations": "delegate.py:4874, 4903, 5050, 5717",
    },
    "draw": {
        "description":          "Drawing and data-visualization helper",
        "default_timeout_sec":  None,
        "has_stuck_detector":   True,
        "can_write_workspace":  True,
        "can_run_bash":         False,  # draw uses python/workspace, not bash
        "can_spawn_subhelper":  False,
        "default_model_tier":   "main",
        "supports_resume":      True,
        "supports_hard_mode":    True,
        "supports_verify":      True,    # 画错图也可 verify
        "_branch_locations": "delegate.py:2286 (system prompt only)",
    },
    "tts": {
        "description":          "Speech synthesis resource helper",
        "default_timeout_sec":  None,
        "has_stuck_detector":   True,
        "can_write_workspace":  True,
        "can_run_bash":         False,
        "can_spawn_subhelper":  False,
        "default_model_tier":   "main",
        "supports_resume":      False,
        "supports_hard_mode":    True,
        "supports_verify":      False,
        "_branch_locations": "delegate.py:_select_helper_system/_filter_tools_for_kind",
    },
    "read": {
        "description":          "File-content and visual-evidence reading helper",
        "default_timeout_sec":  None,
        "has_stuck_detector":   True,
        "can_write_workspace":  True,
        "can_run_bash":         False,
        "can_spawn_subhelper":  False,
        "default_model_tier":   "main",
        "supports_resume":      True,
        "supports_hard_mode":    True,
        "supports_verify":      False,
        "_branch_locations": "delegate.py:_select_helper_system/_filter_tools_for_kind",
    },
}


def get_helper_config(kind: str) -> dict:
    """获取 helper kind 的配置 (P45)。

    向后兼容: "coding" → "code"。
    未知 kind: 返回 code 配置 (fallback)。
    """
    normalized_kind, _ = _normalize_helper_kind_mode(kind)
    return HELPER_CONFIGS.get(normalized_kind, HELPER_CONFIGS["code"])


# 按 helper kind 过滤工具集：用物理 schema 隔离兜住边界，不只靠 prompt 自律。
def _filter_tools_for_kind(kind: str, all_tools: list) -> list:
    """根据 helper kind 返回过滤后的工具列表。

    edit:   只做最终产物装配/改写；不跑命令、不做广泛材料读取或源码索引
    verify: 移除 edit_file, multi_edit, insert_in_file, office — 只读 verify
    draw:   移除 office, edit_file, multi_edit, insert_in_file, bash — 专画图
    tts/read: 只暴露对应资源工具和少量读写/心跳工具
    code:   保留完整工程工具，但不暴露 tts/ocr 资源工具
    其他:   默认按 code 兼容旧调用；已知 kind 按上方边界过滤

    向后兼容：收到 kind="final" 或 mode="final" 时映射为 code+hard；新调用应使用 kind=code, mode=hard。
    """
    k = str(kind or "").strip().lower()
    if k in ("coding", "code"):
        k = "code"
    if k == "final":
        k = "code"
    if k == "ocr":
        k = "read"
    if k == "summarize":
        k = "inventory"

    cache_key = (k, _helper_tool_filter_signature(all_tools))
    cached = _HELPER_TOOL_FILTER_CACHE.get(cache_key)
    if cached is not None:
        return cached

    _DISABLE_MAP = {
        "verify": {"bash", "edit_file", "multi_edit", "insert_in_file", "office"},
        "draw": {"office", "edit_file", "multi_edit", "insert_in_file", "bash"},
    }
    _PROJECT_ANALYSIS_TOOLS = {
        "inspect_file", "read_file", "read_function", "search_in_file",
        "search_files", "search_across_files", "code_index", "fetch_to_temp",
        "fetch_indexed_file", "fetch_group_file", "todo_read", "todo_write", "recall_thread",
        "expand_warm", "expand_cold", "expand_kb", "progress_note",
        "request_resource", "ask_user_question", "read_skill",
    }
    _INVENTORY_TOOLS = set(_PROJECT_ANALYSIS_TOOLS) | {"workspace", "python"}
    _ALLOW_MAP = {
        "edit": {
            "office", "inspect_file", "read_file", "search_in_file", "search_files",
            "fetch_indexed_file", "fetch_group_file", "fetch_to_temp", "workspace", "edit_file", "multi_edit", "insert_in_file", "todo_write",
            "todo_read", "progress_note", "request_resource", "ask_user_question",
            "read_skill",
        },
        "project_map": _PROJECT_ANALYSIS_TOOLS,
        "file_summary": _PROJECT_ANALYSIS_TOOLS,
        "impact_review": _PROJECT_ANALYSIS_TOOLS,
        "inventory": _INVENTORY_TOOLS,
        "tts": {
            "tts", "inspect_file", "read_file", "search_in_file",
            "fetch_indexed_file", "fetch_group_file", "fetch_to_temp", "workspace",
            "request_resource",
        },
        "read": {
            "inspect_file", "read_file", "search_in_file", "search_files",
            "fetch_indexed_file", "fetch_group_file", "fetch_to_temp", "office", "ocr", "workspace",
            "todo_write", "todo_read", "progress_note", "request_resource",
            "ask_user_question", "read_skill",
        },
    }
    allowed = _ALLOW_MAP.get(k)
    disabled = _DISABLE_MAP.get(k, set())

    filtered = []
    for t in all_tools:
        if not isinstance(t, dict):
            filtered.append(t)
            continue
        name = (t.get("function", {}) or {}).get("name", "")
        if allowed is not None:
            if name in allowed:
                if name == "workspace" and k in {"edit", "read", "tts", "inventory", "verify"}:
                    from app.llm.tools.tool_schemas import (
                        EDIT_WORKSPACE_TOOL_SCHEMA,
                        OCR_WORKSPACE_TOOL_SCHEMA,
                        SUMMARY_WORKSPACE_TOOL_SCHEMA,
                        TTS_WORKSPACE_TOOL_SCHEMA,
                        VERIFY_WORKSPACE_TOOL_SCHEMA,
                    )
                    if k == "read":
                        filtered.append(OCR_WORKSPACE_TOOL_SCHEMA)
                    elif k == "tts":
                        filtered.append(TTS_WORKSPACE_TOOL_SCHEMA)
                    elif k == "inventory":
                        filtered.append(SUMMARY_WORKSPACE_TOOL_SCHEMA)
                    elif k == "verify":
                        filtered.append(VERIFY_WORKSPACE_TOOL_SCHEMA)
                    else:
                        filtered.append(EDIT_WORKSPACE_TOOL_SCHEMA)
                    continue
                filtered.append(t)
            continue
        if name == "workspace" and k == "verify":
            from app.llm.tools.tool_schemas import VERIFY_WORKSPACE_TOOL_SCHEMA
            filtered.append(VERIFY_WORKSPACE_TOOL_SCHEMA)
            continue
        if name in {"processes", "mark_avoid_mention"}:
            continue
        if (name == "tts" and k != "tts") or (name == "ocr" and k != "read"):
            continue
        if name in disabled:
            continue
        filtered.append(t)
    _HELPER_TOOL_FILTER_CACHE[cache_key] = filtered
    return filtered
