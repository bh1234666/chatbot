# -*- coding: utf-8 -*-
"""
Single source of truth for cross-cutting policies.

2026-05-15 Item 11 落地最小版:
  - "主线程禁写代码" 的判定数据(扩展名集合 + .py 字符上限)在这里统一定义。
  - app/llm/tools/workspace.py 的拒写检查从这里读 → 改一处自动两处生效。
  - app/core/context.py 的 ROUND2 prompt 描述这两个常量的文本里**人工保持同步**
    (动态拼到 prompt 里会破坏 prefix cache;等所有 policies 都稳定再考虑切动态)。

下次有人改扩展列表 / 字符上限,**只改 _MAIN_BAN_COMPILED_EXTS 或 _PY_VERIFY_MAX 即可**,
context.py 的 prompt 文字需要相应手改 — 文件顶部留一条注释提醒。
"""
from __future__ import annotations
from typing import Optional


# 主线程禁写的编译/工程语言扩展(实测 trace 教训:LLM 自己写这种文件
# 一次能浪费几分钟 streaming + 100% 被服务端拒,且 token 不可挽回)。
# 与 workspace.py 旧版硬编码列表完全一致(防回归)。
_MAIN_BAN_COMPILED_EXTS: frozenset[str] = frozenset({
    ".c", ".cpp", ".cc", ".cxx",
    ".h", ".hpp", ".hxx",
    ".js", ".ts", ".jsx", ".tsx",
    ".go", ".rs", ".java",
    ".cs", ".kt", ".kts",
    ".swift", ".m", ".mm",
})

# 主线程 .py 字符上限。允许中型验证/转换脚本直接落地，避免 chat 与
# environment 在普通 Python 检查脚本上分叉过大；实质工程源码仍应走 helper。
_PY_VERIFY_MAX: int = 8000


# ── 对外可读访问器(也用于 prompt 描述)──

def main_thread_banned_extensions() -> frozenset[str]:
    """返回主线程禁写的扩展名集合(frozenset, 调用方不要修改)。"""
    return _MAIN_BAN_COMPILED_EXTS


def main_thread_py_max_chars() -> int:
    """返回主线程 .py 字符上限。"""
    return _PY_VERIFY_MAX


def is_main_banned_extension(path: str) -> bool:
    """判定路径扩展名是否在主线程禁写列表中(大小写不敏感)。"""
    p = path.lower()
    for ext in _MAIN_BAN_COMPILED_EXTS:
        if p.endswith(ext):
            return True
    return False


def check_main_thread_write(path: str, content: str) -> Optional[str]:
    """主线程写文件检查 — 仅做"该不该拒"的判定,不构造 LLM 友好的长 error。
    返回 None 表示通过;返回字符串表示拒绝的原因(简短)。

    富文本错误(包含 delegate 调用示例、kind 选指南等)由调用方
    (workspace.py _handle_workspace_write) 自己拼,这里只做规则核心。
    """
    p = path.lower()
    if is_main_banned_extension(p):
        return f"banned_compiled_lang_ext:{p[p.rfind('.'):]}"
    if p.endswith(".py") and len(content) > _PY_VERIFY_MAX:
        return f"py_too_large:{len(content)}>{_PY_VERIFY_MAX}"
    return None
