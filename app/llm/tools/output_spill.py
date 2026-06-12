"""Bound oversized real tool output before it enters model context."""
from __future__ import annotations

import hashlib
import re
import tempfile
import time
from pathlib import Path


def _safe_name(value: str, fallback: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or ""))[:80]
    return safe or fallback


def write_tool_output_spill(
    *,
    root_dir: str,
    tool_name: str,
    label: str,
    text: str,
    extension: str = ".txt",
) -> str:
    """Save a full tool output payload and return a root-relative path when possible."""
    root = Path(root_dir or tempfile.gettempdir()).resolve()
    folder = root / "_tool_results"
    folder.mkdir(parents=True, exist_ok=True)
    suffix = extension if extension.startswith(".") else f".{extension}"
    digest = hashlib.sha256(str(text or "").encode("utf-8", errors="replace")).hexdigest()[:12]
    filename = (
        f"{int(time.time())}_{_safe_name(tool_name, 'tool')}_"
        f"{_safe_name(label, 'output')}_{digest}{suffix}"
    )
    path = folder / filename
    path.write_text(str(text or ""), encoding="utf-8", errors="replace")
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def spill_text_field(
    result: dict,
    *,
    root_dir: str,
    tool_name: str,
    field: str,
    text: str,
    visible_chars: int,
    force: bool = False,
    saved_label: str | None = None,
) -> None:
    """Replace a long text field with a head excerpt and attach recovery facts."""
    if not isinstance(text, str):
        text = str(text or "")
    if not force and len(text) <= visible_chars:
        result[field] = text
        return
    path = write_tool_output_spill(
        root_dir=root_dir,
        tool_name=tool_name,
        label=saved_label or field,
        text=text,
    )
    result[field] = text[: max(0, int(visible_chars))]
    result[f"{field}_truncated"] = True
    result[f"{field}_original_chars"] = len(text)
    result[f"{field}_full_saved_path"] = path
    result["output_truncated"] = True
    result["tool_result_truncated"] = True
    result["visible_excerpt_policy"] = (
        "A real tool text field exceeded the model-visible budget and was truncated before entering "
        f"model context. The full text was saved at `{path}` (`{field}_full_saved_path`); only the "
        "head excerpt is shown here.\n"
        "真实工具文本过长，完整内容已保存；当前上下文只显示头部摘录。"
    )
