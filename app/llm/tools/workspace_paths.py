"""工作区路径安全工具:安全解析、是否可安全清空、已知可回收的目录/文件判定。

2026-05-20 重构: 从 llm/tools/workspace.py 原样抽出。经 extract_analysis --closure
验证自包含(4 函数, 0 unsafe),仅依赖 stdlib(os/pathlib/tempfile)。workspace.py re-export 兼容。
"""
import os
import tempfile
from pathlib import Path


def _is_safe_to_wipe(path: str) -> bool:
    """判断路径是否安全可清(防误删永久主工作区)。

    安全条件 (任一):
      1. 路径含 '_delegate_' (helper 临时区典型标记)
      2. 路径在系统 tempdir 下 (Path(tempfile.gettempdir())/...)
      3. 路径含 'chatbot_workspaces' (临时 ws 目录前缀)
    """
    norm = os.path.normpath(os.path.abspath(path))
    # _delegate_ helper 临时区
    if "_delegate_" in os.path.basename(norm.rstrip(os.sep)) or \
       (os.sep + "_delegate_") in norm:
        return True
    # 系统 tempdir 下
    try:
        tempdir = os.path.normpath(os.path.abspath(tempfile.gettempdir()))
        if norm.startswith(tempdir + os.sep) or norm == tempdir:
            return True
    except Exception:
        pass
    # 临时 ws 目录前缀
    if "chatbot_workspaces" in norm:
        return True
    return False


def _is_known_reclaimable_workspace_dir(name: str) -> bool:
    return name in {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".cache",
        "node_modules",
        "dist",
        "build",
    }


def _is_known_reclaimable_workspace_file(name: str) -> bool:
    lower = name.lower()
    if lower.startswith(".helper_") and lower.endswith("_full_report.txt"):
        return True
    return lower.endswith((
        ".pyc", ".pyo", ".tmp", ".temp", ".log", ".bak", ".o", ".obj",
    ))


def _safe_resolve(ws_dir: str, rel_path: str) -> str:
    """解析 path 到工作区内的绝对路径,拒绝越界。

    ── 2026-05-04 Bug #17B 修复 ──
    旧版直接拒绝绝对路径(`absolute paths are not allowed`)。但 helper 的
    bash 工具用 `dir /b` / `ls -la` 时输出**绝对路径**,helper 看到后用
    read_file(absolute) → 直接拒。helper 在沙箱里又找不到正确的相对路径写法,
    在 phase 1 就被 stuck detector 判死(40 秒就废)。

    新逻辑:绝对路径**只要解析后落在 ws_dir 内就放行**,与相对路径等价处理。
    路径遍历检查仍然走 resolve().relative_to(ws_dir),越界直接拒,所以安全等同。
    """
    ws = Path(ws_dir).resolve()
    p_str = rel_path.lstrip("/").lstrip("\\") if not Path(rel_path).is_absolute() else rel_path
    p = Path(p_str)
    if p.is_absolute():
        # 绝对路径:直接 resolve(不拼 ws),然后看是否落在 ws 内
        resolved = p.resolve()
    else:
        # 相对路径:拼到 ws 下再 resolve
        resolved = (ws / p).resolve()
    try:
        resolved.relative_to(ws)
    except ValueError:
        # 越界 — 拒(对绝对路径,这里区分提示)
        if p.is_absolute():
            raise ValueError(
                f"absolute path must be inside this sandbox ({ws}): {rel_path!r}"
            )
        raise ValueError(f"path traversal detected: {rel_path!r}")
    return str(resolved)
