"""特征测试:orchestrator_checks.py 抽取(2026-05-20)。

orchestrator.py 的 macro 升级信号 / autofix deliverables / sibling 文件检查 helpers
抽到 orchestrator_checks.py。本测试用纯 AST 校验(离线无依赖):
  1. orchestrator_checks 定义了预期的 13 个符号;
  2. orchestrator.py 通过 re-export 仍暴露这 13 个符号(import-before-use)。
不依赖第三方库,可离线运行。
"""
import ast
import os

ROOT = os.path.join(os.path.dirname(__file__), "..", "app", "core")

EXPECTED = {
    "_MACRO_YELLOW_ITER_THRESHOLD", "_MACRO_HARD_ITER_THRESHOLD",
    "_MACRO_BATCH_TIMEOUT_KEYWORDS", "_check_macro_escalation_signals",
    "_has_workspace_files_produced", "_AUTOFIX_DELIVERY_EXTS",
    "_AUTOFIX_SKIP_PATTERNS", "_AUTOFIX_INTERMEDIATE_SCRIPT_PREFIXES",
    "_AUTOFIX_PRODUCTION_HINTS", "_AUTOFIX_FILE_INTENT_KEYWORDS",
    "_AUTOFIX_FINAL_DELIVERABLE_EXTS", "_check_sibling_files",
    "_autofix_deliverables",
}


def _top_level_names(path):
    t = ast.parse(open(path, encoding="utf-8").read())
    names = set()
    for n in t.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Assign):
            for tg in n.targets:
                if isinstance(tg, ast.Name):
                    names.add(tg.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            names.add(n.target.id)
    return names, t


def test_checks_module_defines_expected_symbols():
    names, _ = _top_level_names(os.path.join(ROOT, "orchestrator_checks.py"))
    missing = EXPECTED - names
    assert not missing, f"orchestrator_checks 缺少: {missing}"


def test_orchestrator_reexports_all_symbols():
    path = os.path.join(ROOT, "orchestrator.py")
    t = ast.parse(open(path, encoding="utf-8").read())
    reexported = set()
    for n in ast.walk(t):
        if isinstance(n, ast.ImportFrom) and n.module == "app.core.orchestrator_checks":
            reexported |= {a.name for a in n.names}
    missing = EXPECTED - reexported
    assert not missing, f"orchestrator.py 未 re-export: {missing}"


def test_reexport_before_first_use():
    """re-export import 必须出现在任何符号被引用之前(否则运行时 NameError)。"""
    path = os.path.join(ROOT, "orchestrator.py")
    lines = open(path, encoding="utf-8").read().split("\n")
    rex_line = next(
        (i for i, l in enumerate(lines, 1)
         if "from app.core.orchestrator_checks import" in l),
        None,
    )
    assert rex_line, "未找到 orchestrator_checks 的 re-export import"
    import re
    for i, l in enumerate(lines, 1):
        if i >= rex_line:
            break
        for s in EXPECTED:
            assert not re.search(r"\b" + re.escape(s) + r"\b", l), \
                f"符号 {s} 在 re-export(L{rex_line})之前被引用(L{i})"


if __name__ == "__main__":
    test_checks_module_defines_expected_symbols()
    test_orchestrator_reexports_all_symbols()
    test_reexport_before_first_use()
    print("test_orchestrator_checks: 3 passed")
