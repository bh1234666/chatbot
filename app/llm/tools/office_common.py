"""office 工具通用 helper:错误响应构造(_err)、JSON 可序列化化(_jsonable)、
依赖库检测(_check_lib)、输出目录解析(_resolve_out_dir)。供 office 各子模块共用。

2026-05-20 重构: 从 llm/tools/office.py 原样抽出。closure 自包含(4 函数, 0 unsafe)。
"""
import json
import os
from typing import Any


def _err(msg: str, *, action: str = "", hint: str = "", extra: dict | None = None) -> str:
    """Return a standardized error response.

    The 'error' field tells the model what went wrong.
    The 'hint' field (when present) tells the model how to fix it and retry.
    The 'action' field (when present) identifies which action was being attempted.
    """
    out: dict = {"ok": False, "error": msg}
    if action:
        out["action"] = action
    if hint:
        out["hint"] = hint
    if extra:
        out.update(extra)
    return json.dumps(out, ensure_ascii=False)


def _check_lib(fmt: str) -> str | None:
    if fmt == "docx":
        try:
            import docx  # noqa: F401
        except ImportError:
            return ("python-docx not installed. Install with: "
                    "`workspace run \"pip install python-docx\"`")
    elif fmt == "pptx":
        try:
            import pptx  # noqa: F401
        except ImportError:
            return ("python-pptx not installed. Install with: "
                    "`workspace run \"pip install python-pptx\"`")
    elif fmt == "xlsx":
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            return ("openpyxl not installed. Install with: "
                    "`workspace run \"pip install openpyxl\"`")
    return None


def _jsonable(val: Any) -> Any:
    """openpyxl 单元格值可能是 datetime / Decimal,序列化 fallback。"""
    if val is None or isinstance(val, (str, int, float, bool)):
        return val
    return str(val)


def _resolve_out_dir(args: dict, rel_path: str) -> str:
    out_dir = str(args.get("out_dir", "")).strip()
    if not out_dir:
        stem = os.path.splitext(os.path.basename(rel_path))[0] or "extracted"
        out_dir = f"{stem}_images"
    return out_dir
