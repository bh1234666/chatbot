"""工作区会话持久化:原子写 JSON、写会话 manifest、读取编辑计数。

2026-05-20 重构: 从 llm/tools/workspace.py 原样抽出。closure 自包含(3 函数, 0 unsafe),
仅依赖 stdlib(json/os/time)。workspace.py re-export 兼容(含被本地测试 import 的
_write_session_manifest;其 inspect.getsource 读取的函数体逐字未变)。
"""
import json
import os
import time


def _write_session_manifest(temp_ws: str, main_ws: str | None = None) -> None:
    """记录 session 开始时已存在的文件(用于后续检测新产物)。

    扫描 .temp/ 和主工作区根目录全部文件,统一汇入 files_before
    供 helper 产物过滤跨会话污染和 _clean_main_workspace_before_spawn 区分新旧。
    2026-05-08 Fix 5: 扫描 main_ws 全部根目录文件(不仅 .helper_*),
    使旧 task 残留能被识别并清理。
    """
    manifest_path = os.path.join(temp_ws, "_session_manifest.json")
    files_before: list[str] = []
    try:
        if os.path.isdir(temp_ws):
            for entry in os.listdir(temp_ws):
                if entry == "_session_manifest.json":
                    continue
                files_before.append(entry)
        # 扫描主工作区根目录全部文件
        if main_ws and os.path.isdir(main_ws):
            for entry in os.listdir(main_ws):
                # 仅记录根目录文件和顶层目录名(子目录内容不递归)
                files_before.append(entry)
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump({"files_before": sorted(files_before), "created_at": time.time()}, f)
    except OSError:
        pass


def _atomic_write_json(path: str, data) -> bool:
    """原子写 JSON 到 path。tmp + os.replace,失败回滚 tmp。"""
    tmp = f"{path}.tmp.{os.getpid()}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)  # POSIX & Windows 都原子
        return True
    except (OSError, TypeError):
        try:
            if os.path.isfile(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        return False


def _peek_edit_count(ws_dir: str, path: str) -> int:
    """偷看当前 path 的 edit 次数, 不递增。供 edit_file/multi_edit/insert_in_file
    入口的 P69 硬阻断检查使用。"""
    history_path = os.path.join(ws_dir, ".edit_history.json")
    try:
        if os.path.isfile(history_path):
            with open(history_path, "r", encoding="utf-8") as f:
                history = json.load(f) or {}
            entry = history.get(path)
            if isinstance(entry, dict):
                return int(entry.get("count", 0))
    except (OSError, json.JSONDecodeError):
        pass
    return 0
