"""todo 工具处理器:handle_todo_write / handle_todo_read。

2026-05-20 重构: 从 llm/tools/workspace.py 原样抽出。closure 自包含(2 函数, 0 unsafe),
仅依赖 stdlib(json/os/time)。workspace.py re-export 兼容。
"""
import json
import os
import time


_TODO_CONTENT_SOFT_LIMIT = 240
_TODO_CONTENT_HARD_HINT_LIMIT = 800


async def handle_todo_write(ws_dir: str, todos: list) -> dict:
    """管理任务 todo list。

    todos: [{"id": str, "content": str, "status": "pending"|"in_progress"|"completed"}, ...]

    2026-05-03 增强:对比上次持久化的 todos,**检测新完成项**,在响应里
    回传 `_completed_todos` 字段供 client.py 触发任务边界折叠
    (一个 todo 完成 = 一个语义子任务边界 = 之前的 tool call 可压缩成
    "完成了 X" 一条总结)。
    """
    if not isinstance(todos, list):
        return {"ok": False, "error": "todos must be an array"}

    # ── 读上次的 todos 作为对比基线(检测新完成项)──
    todos_path = os.path.join(ws_dir, ".todos.json")
    prev_status: dict[str, str] = {}
    prev_content: dict[str, str] = {}
    if os.path.isfile(todos_path):
        try:
            with open(todos_path, "r", encoding="utf-8") as f:
                _prev = json.load(f)
            if isinstance(_prev, list):
                for pt in _prev:
                    if isinstance(pt, dict):
                        pid = str(pt.get("id", "")).strip()
                        if pid:
                            prev_status[pid] = pt.get("status", "")
                            prev_content[pid] = pt.get("content", "")
        except (OSError, json.JSONDecodeError):
            pass

    # 验证 + 规范化
    normalized = []
    seen_ids = set()
    in_progress_count = 0
    demoted_in_progress: list[dict] = []
    long_content_items: list[dict] = []
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return {"ok": False, "error": f"todos[{i}] must be an object"}
        tid = str(t.get("id", "")).strip()
        content = str(t.get("content", "")).strip()
        status = str(t.get("status", "pending")).strip()
        if not tid:
            tid = str(i + 1)
        if tid in seen_ids:
            return {"ok": False, "error": f"duplicate todo id: {tid!r}"}
        seen_ids.add(tid)
        if not content:
            return {"ok": False, "error": f"todos[{i}] (id={tid}): content is required"}
        if len(content) > _TODO_CONTENT_SOFT_LIMIT:
            long_content_items.append({
                "id": tid,
                "chars": len(content),
                "preview": content[:120],
            })
        if status not in ("pending", "in_progress", "completed"):
            return {
                "ok": False,
                "error": f"todos[{i}] (id={tid}): status must be pending/in_progress/completed, got {status!r}",
            }
        if status == "in_progress":
            in_progress_count += 1
            if in_progress_count > 1:
                demoted_in_progress.append({"id": tid, "content": content[:120]})
                status = "pending"
        normalized.append({"id": tid, "content": content, "status": status})

    in_progress_count = min(in_progress_count, 1)

    # ── 检测**本次新完成的** todo(上次不是 completed,这次是)──
    # 这些就是任务边界,告诉 client.py 可以折叠该任务区间内的 tool call
    newly_completed: list[dict] = []
    for t in normalized:
        tid = t["id"]
        if t["status"] == "completed" and prev_status.get(tid) != "completed":
            newly_completed.append({
                "id": tid,
                "content": t["content"][:120],  # 短描述,作为折叠 summary
            })

    # 持久化
    try:
        with open(todos_path, "w", encoding="utf-8") as f:
            json.dump(normalized, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return {"ok": False, "error": f"failed to persist todos: {e}"}

    # 格式化展示
    icon_map = {"pending": "☐", "in_progress": "▶", "completed": "✓"}
    display_lines = []
    n_pending = n_in_progress = n_done = 0
    for t in normalized:
        icon = icon_map.get(t["status"], "?")
        display_lines.append(f"  {icon} [{t['id']}] {t['content']}")
        if t["status"] == "pending":
            n_pending += 1
        elif t["status"] == "in_progress":
            n_in_progress += 1
        elif t["status"] == "completed":
            n_done += 1

    summary = f"todos: {n_done} done, {n_in_progress} in progress, {n_pending} pending"
    result: dict = {
        "ok": True,
        "action": "todo_write",
        "summary": summary,
        "display": "\n".join(display_lines) if display_lines else "(empty)",
        "counts": {
            "total": len(normalized),
            "pending": n_pending,
            "in_progress": n_in_progress,
            "completed": n_done,
        },
    }
    if demoted_in_progress:
        result["normalization_warning"] = (
            f"Multiple in_progress todos were submitted ({1 + len(demoted_in_progress)}). "
            f"The first one was kept and {len(demoted_in_progress)} extra item(s) were demoted to pending.\n\n"
            "todo 状态已规范化；只保留一个 in_progress。"
        )
        result["demoted_in_progress"] = demoted_in_progress
    if long_content_items:
        max_chars = max(int(item["chars"]) for item in long_content_items)
        result["content_length_warning"] = {
            "issue": "todo_content_long_for_planning_state",
            "long_items": long_content_items[:5],
            "max_content_chars": max_chars,
            "fact": (
                "todo_write stores checklist state. Long prose, scripts, document bodies, tables, patches, "
                "or final answers are artifact content and are usually better carried by workspace, office, "
                "or edit tools. The submitted todos were still recorded."
            ),
            "中文概要": "todo_write 已记录本次清单；长正文、脚本或文档内容通常应放入 workspace/office/edit 等产物工具。",
        }
        if max_chars >= _TODO_CONTENT_HARD_HINT_LIMIT:
            result["next_action_facts"] = [
                "A todo item exceeded 800 characters.",
                "The task can continue from the recorded checklist.",
                "For a DOCX body, office(action='write'/'append', ...) can create document blocks directly.",
                "For scripts or long text artifacts, workspace/edit tools can create files.",
            ]

    # 2026-05-10 Patch 78: 过度 todo_write 检测
    # 病因(trace bb69a01654554ad0):mergesort helper 单任务跑了 5 min,177 次 todo_write,
    # 比 bash + workspace 加起来还多。LLM 把 todo_write 当"思考工具",每个步骤前都
    # 先更新 todos 再做事 → 浪费 LLM round trip(每次 6-10s)。同任务理论 1-2 min 完成,
    # 实际 5 min 主要消耗在过度 todo 维护。
    # 修法:用 .todos.json 同目录 .todos_call_count.json 计数 todo_write 调用,
    # 短时间内 ≥10 次时返回 throttle_warning,引导 LLM 减少 todo 频次。
    try:
        _count_path = os.path.join(ws_dir, ".todos_call_count.json")
        _cnt_data: dict = {}
        if os.path.isfile(_count_path):
            try:
                with open(_count_path, "r", encoding="utf-8") as f:
                    _cnt_data = json.load(f) or {}
                if not isinstance(_cnt_data, dict):
                    _cnt_data = {}
            except (OSError, json.JSONDecodeError):
                _cnt_data = {}
        _cnt_data["count"] = int(_cnt_data.get("count", 0)) + 1
        _cnt_data["last_at"] = time.time()
        try:
            with open(_count_path, "w", encoding="utf-8") as f:
                json.dump(_cnt_data, f, ensure_ascii=False)
        except OSError:
            pass
        _cnt = _cnt_data["count"]
        # 阈值:≥10 次提示;≥30 次强警告(明显过度)
        result["todo_write_count"] = _cnt
        if _cnt >= 30:
            result["throttle_warning"] = {
                "issue": "frequent_todo_write_calls",
                "count": _cnt,
                "fact": (
                    f"todo_write has been called {_cnt} times in this task. It records planning state; "
                    "it does not execute work, verify artifacts, or deliver files. Frequent updates can consume "
                    "LLM/tool iterations without changing workspace artifacts."
                ),
                "state_change_guidance": (
                    "A todo update is most useful when a task becomes complete, blocked, or enters a new stage."
                ),
                "中文概要": "todo_write 只是计划状态记录；频繁更新会消耗轮次，但不会执行、验证或交付产物。",
            }
            result["next_action_facts"] = [
                "The current todo list has been recorded.",
                "No workspace artifact is created by todo_write itself.",
                "Execution, verification, delivery, or resource requests require the relevant tool calls.",
            ]
            result["legacy_throttle_warning"] = (
                f"todo_write has been called {_cnt} times in this task. "
                "Treat it as state tracking, not a thinking tool. "
                "Update todos again only when a task becomes complete, blocked, or enters a new stage.\n\n"
                "todo_write 调用过多；只在状态变化时更新。"
            )
        elif _cnt >= 10:
            result["throttle_hint"] = {
                "issue": "repeated_todo_write_calls",
                "count": _cnt,
                "fact": (
                    f"todo_write has been called {_cnt} times in this task. It stores planning state; "
                    "workspace artifacts change only through execution/editing/delivery tools."
                ),
                "中文概要": "todo_write 是状态记录；工作区产物只会通过执行、编辑或交付类工具改变。",
            }
    except Exception:
        pass  # throttle 检测失败不阻塞 todo_write
    if newly_completed:
        # client.py 扫这个字段触发任务边界折叠
        result["_completed_todos"] = newly_completed
        result["_fold_hint"] = (
            f"detected {len(newly_completed)} newly-completed todo(s); "
            "client may compact tool calls between previous task boundary and now"
        )
    return result


async def handle_todo_read(ws_dir: str) -> dict:
    """读取当前 todo list。"""
    todos_path = os.path.join(ws_dir, ".todos.json")
    if not os.path.exists(todos_path):
        return {
            "ok": True,
            "action": "todo_read",
            "todos": [],
            "summary": "(no todos yet — use todo_write to create)",
            "display": "(empty)",
        }
    try:
        with open(todos_path, "r", encoding="utf-8") as f:
            todos = json.load(f)
    except Exception as e:
        return {"ok": False, "error": f"failed to read todos: {e}"}

    icon_map = {"pending": "☐", "in_progress": "▶", "completed": "✓"}
    lines = [f"  {icon_map.get(t.get('status'), '?')} [{t.get('id')}] {t.get('content')}" for t in todos]
    n_done = sum(1 for t in todos if t.get("status") == "completed")
    return {
        "ok": True,
        "action": "todo_read",
        "todos": todos,
        "summary": f"{n_done}/{len(todos)} done",
        "display": "\n".join(lines) if lines else "(empty)",
    }
