"""
D4: Per-Agent DB-driven 工作区清理

设计原则 (2026-05-16 用户明确):
1. **大小限制是针对单个智能体的** — 每个 (archive_id, group_id) 是独立的智能体工作区,
   各自有 200/500/1000/.../4000 MB 跨档触发. 不是整个 ws_root 总和.
2. **每个智能体的工作区完全属于该智能体** — D4 可以删除该工作区内任何
   "无 DB 引用 + 老于阈值" 的文件 (这些大概率是版本更替/意外终止的孤儿).
3. **D4 绝对不离开当前智能体的工作区操作** — 严格路径边界.
4. **DB-driven**: 用 synced_files + cold_nodes 精确判定哪些不能动.

工作流:
1. 列所有 (archive_id, group_id) 智能体工作区
2. 对每个智能体独立:
   a. du 该智能体目录, 决定是否需要清理 (>200MB 才考虑)
   b. 查 DB (限定该 archive+group) 拉受保护路径
   c. 扫该智能体工作区, 减去保护集合 = 候选
   d. LLM 决策 / 规则 fallback
   e. 应用决策 (路径必须在该智能体目录内)

紧急判定: max(per-agent size) > emergency_mb → 触发 cancel 其他任务给 D4 让路.
"""
from __future__ import annotations

from app.core.dream.prompt_catalog import (
    D4_WORKSPACE_CLEANUP_PROMPT,
)
_LLM_PROMPT = D4_WORKSPACE_CLEANUP_PROMPT


import asyncio
import json
import os
import time
from typing import Any

from app.core.dream.dream_log import dream_log
from app.core.dream.registry import register_dream_task
from app.core.dream.task_base import InfoDrivenTask


# 跨档阈值 (MB) - 每跨过一档就触发 (per agent)
_THRESHOLDS_MB = [200, 500, 1000, 1500, 2000, 3000, 4000]

# 永远不动的目录名 (智能体工作区内)
# 2026-05-17 Round 14j: D4 目录策略分两类
# 完全跳过 (top-level skip): 内部全是用户产物, 不递归进去
# 递归但 per-file 检查: 内部 mixed (user + bot 生成), 用 DB 决定每个文件命运
_FULLY_PROTECTED_DIR_NAMES = {
    "_downloaded_media",  # 用户聊天里发的图片 (不在 synced_files, 没法 per-file 判定)
    "commit_to_main",     # commit 锁定的成品
    "archive",            # 已归档老制品
}
# 递归扫进去, 但 per-file 用 DB protected_paths 判断
_RECURSIVE_BUT_FILTER_DIR_NAMES = {
    "group_files",        # 大头! user 文件 (DB synced_files protected) + bot 生成的 tts wav 等
    "_helpers_shared",    # helper 产物 (commit_to_main 之后) — 旧的可以清
    "_shared",            # 旧版共享 — 同上
}
# 兼容旧用法 (仍在某些地方引用)
_PROTECTED_DIR_NAMES = _FULLY_PROTECTED_DIR_NAMES | _RECURSIVE_BUT_FILTER_DIR_NAMES

# .temp / .prev 内部不应该被 D4 直接动 (.temp 还可能在用)
_SKIP_TEMP_DIRS = {".temp", ".prev"}


def _get_aggressiveness(agent_mb: float) -> str:
    """基于单智能体工作区大小."""
    if agent_mb < 200: return "none"
    if agent_mb < 500: return "low"
    if agent_mb < 1000: return "medium"
    if agent_mb < 1500: return "high"
    if agent_mb < 2000: return "very_high"
    if agent_mb < 3000: return "alert"
    return "critical"


def _get_age_threshold_hours(agent_mb: float) -> float:
    """根据单智能体大小决定 age 阈值. 越大越激进."""
    if agent_mb < 500: return 24
    if agent_mb < 1000: return 12
    if agent_mb < 1500: return 6
    if agent_mb < 2000: return 3
    if agent_mb < 3000: return 1
    return 0.5


def _du_mb(path: str) -> float:
    """同步: 递归算目录大小 (MB)."""
    total = 0
    try:
        for r, _, fs in os.walk(path):
            for f in fs:
                fp = os.path.join(r, f)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    continue
    except OSError:
        return 0.0
    return total / (1024 * 1024)


async def _list_agents(ws_root: str) -> list[tuple[str, str, str]]:
    """列所有智能体. 返回 [(archive_id, group_id, group_path), ...]"""
    agents = []
    try:
        archives = await asyncio.to_thread(os.listdir, ws_root)
    except OSError:
        return agents
    
    for archive_id in archives:
        archive_path = os.path.join(ws_root, archive_id)
        if not await asyncio.to_thread(os.path.isdir, archive_path):
            continue
        try:
            groups = await asyncio.to_thread(os.listdir, archive_path)
        except OSError:
            continue
        for group_id in groups:
            group_path = os.path.join(archive_path, group_id)
            if await asyncio.to_thread(os.path.isdir, group_path):
                agents.append((archive_id, group_id, group_path))
    return agents


async def _max_agent_size_mb(ws_root: str) -> tuple[float, str, str]:
    """找最大的单智能体工作区. 返回 (size_mb, archive_id, group_id) 或 (0, '', '')."""
    if not ws_root or not os.path.isdir(ws_root):
        return (0.0, "", "")
    
    agents = await _list_agents(ws_root)
    if not agents:
        return (0.0, "", "")
    
    max_size = 0.0
    max_archive = ""
    max_group = ""
    for archive_id, group_id, group_path in agents:
        size = await asyncio.to_thread(_du_mb, group_path)
        if size > max_size:
            max_size = size
            max_archive = archive_id
            max_group = group_id
    return (max_size, max_archive, max_group)


async def _get_max_agent_size_mb() -> float:
    """获取最大单智能体工作区大小 (用于触发判定)."""
    try:
        from app.core.dream.cache import _get_workspace_root
        ws_root = _get_workspace_root()
        if not ws_root:
            return 0.0
        size, _, _ = await _max_agent_size_mb(ws_root)
        return size
    except Exception:
        return 0.0


# ──────────────────────────────────────────────────────────────────
# DB 查询: 单智能体的受保护路径
# ──────────────────────────────────────────────────────────────────

async def _query_protected_paths(archive_id: str, group_id: str) -> dict[str, dict]:
    """
    查 DB 拉该智能体的"绝对不能删"路径.
    返回 dict: {relative_path: {"reason": ..., "node_id": ...}}
    """
    from app.db.pool import pool
    
    protected: dict[str, dict] = {}
    
    try:
        async with pool().acquire() as conn:
            # 1. synced_files: 用户上传的群文件
            rows = await conn.fetch("""
                SELECT workspace_path, file_name, kb_node_id
                FROM synced_files
                WHERE archive_id = $1 AND group_id = $2
                  AND workspace_path != ''
            """, archive_id, group_id)
            
            for r in rows:
                wp = r["workspace_path"]
                if wp:
                    protected[wp] = {
                        "reason": "user_uploaded",
                        "file_name": r["file_name"],
                        "node_id": r["kb_node_id"],
                    }
            
            # 2. cold_nodes file 节点 (KB 索引的, NOT deleted, NOT placeholder)
            # 2026-05-17 Round 14h: 不用 ->>, Python 层 filter
            # 2026-05-17 Round 14j: 也排除 placeholder content (D3 应该清掉但 emergency
            #   阻塞 D3 跑不了 → placeholder 一直在 protected → D4 永远清不了它指的工作区
            #   文件. 让 D4 自己识别 placeholder 不当 protected).
            from app.memory.kb import _is_placeholder_content
            rows = await conn.fetch("""
                SELECT id, headline, content, file_metadata
                FROM cold_nodes
                WHERE archive_id = $1 AND group_id = $2
                  AND scope = 'kb' AND node_type = 'file'
            """, archive_id, group_id)
            
            for r in rows:
                meta = r["file_metadata"]
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        continue
                if not isinstance(meta, dict):
                    continue
                # Python 层 filter deleted
                if str(meta.get("deleted", "")).lower() == "true":
                    continue
                # 2026-05-17 Round 14j: 排除 placeholder (D3 该清但被 emergency 阻塞).
                # 不排除会形成死锁: D4 把 placeholder 当 protected, D3 跑不到 → 永远清不了.
                try:
                    if _is_placeholder_content(r["headline"], r["content"] or ""):
                        continue  # placeholder 不算 protected, D4 可清
                except Exception:
                    pass
                
                wp = meta.get("workspace_path", "")
                if wp and wp not in protected:
                    protected[wp] = {
                        "reason": "kb_indexed",
                        "headline": r["headline"],
                        "node_id": r["id"],
                    }
    except Exception as e:
        dream_log.warn(
            "dream.task.d4_workspace_cleanup.db_query_failed",
            f"archive={archive_id} group={group_id} err={e!r}"[:200],
        )
    
    return protected


# ──────────────────────────────────────────────────────────────────
# 单智能体: 候选收集
# ──────────────────────────────────────────────────────────────────

def _scan_agent_workspace(
    group_path: str, protected: dict, age_threshold_h: float,
    archive_id: str, group_id: str,
) -> list[dict]:
    """同步扫单智能体工作区, 找候选清理项.
    
    类型 A: _delegate_* 沙箱 (helper 工作区)
    类型 B: 主区文件 (DB 中无引用) - 可能是版本更替/意外终止的孤儿
    
    2026-05-17 Round 14h 重写: 递归扫子目录.
    旧版只 os.listdir(group_path) 顶层 — group_files/ 子目录里的垃圾完全看不到.
    实测用户说"工作区几百兆垃圾", 但 D4 报 0 candidates → 必然是子目录里的.
    新版 os.walk 递归, **per-file** 检查 protected (用 workspace 相对路径).
    """
    candidates: list[dict] = []
    now = time.time()
    threshold_sec = age_threshold_h * 3600
    
    # 诊断计数
    _seen = 0
    _skipped_too_new = 0
    _skipped_too_small = 0
    _skipped_protected = 0
    _skipped_special_dir = 0
    
    # ── 类型 A: _delegate_* 沙箱 (顶层 _delegate_ 目录) ──
    try:
        top_entries = os.listdir(group_path)
    except OSError:
        return candidates
    
    for entry in top_entries:
        if not entry.startswith("_delegate_"):
            continue
        entry_path = os.path.join(group_path, entry)
        if not os.path.isdir(entry_path):
            continue
        try:
            mtime = os.path.getmtime(entry_path)
            age_sec = now - mtime
            if age_sec < threshold_sec:
                _skipped_too_new += 1
                continue
            size_mb = _du_mb(entry_path)
            if size_mb < 0.1:
                _skipped_too_small += 1
                continue
            
            summary_path = os.path.join(entry_path, ".helper_summary.txt")
            summary = ""
            if os.path.isfile(summary_path):
                try:
                    with open(summary_path, "r", encoding="utf-8", errors="replace") as f:
                        summary = f.read()[:300]
                except OSError:
                    pass
            
            candidates.append({
                "task_id": entry,
                "path": entry_path,
                "kind": "sandbox",
                "age_hours": round(age_sec / 3600, 1),
                "size_mb": round(size_mb, 1),
                "summary": summary,
                "archive_id": archive_id,
                "group_id": group_id,
            })
        except OSError:
            continue
    
    # ── 类型 B: 递归扫所有文件 (含 group_files/ 子目录) ──
    # 把 protected 路径正规化 (统一用正斜杠)
    protected_norm = set()
    for p in protected.keys():
        norm = p.replace(os.sep, "/").strip("/")
        if norm:
            protected_norm.add(norm)
    
    # 2026-05-17 Round 14j 修订: 只跳 _FULLY_PROTECTED (commit_to_main / archive /
    # _downloaded_media). _RECURSIVE_BUT_FILTER (group_files / _helpers_shared / _shared)
    # 递归进去, per-file 用 DB protected_paths 严格检查.
    # 不这样改, 大 agent 全在 group_files/ 里, D4 永远清不到子目录里的 bot wav 等.
    for root_dir, sub_dirs, files in os.walk(group_path, topdown=True):
        # in-place 过滤掉不扫的目录
        sub_dirs[:] = [
            d for d in sub_dirs
            if d not in _FULLY_PROTECTED_DIR_NAMES
               and d not in _SKIP_TEMP_DIRS
               and not d.startswith("_delegate_")
        ]
        
        for fname in files:
            _seen += 1
            fpath = os.path.join(root_dir, fname)
            # workspace 相对路径 (统一正斜杠)
            try:
                rel = os.path.relpath(fpath, group_path).replace(os.sep, "/")
            except ValueError:
                continue
            
            # 跳特殊文件 (manifest, helper_summary)
            if fname in {"_session_manifest.json", ".helper_summary.txt"}:
                _skipped_special_dir += 1
                continue
            
            # 跳保护 — per-file 检查
            if rel in protected_norm:
                _skipped_protected += 1
                continue
            
            try:
                mtime = os.path.getmtime(fpath)
                age_sec = now - mtime
                if age_sec < threshold_sec:
                    _skipped_too_new += 1
                    continue
                size_mb = os.path.getsize(fpath) / (1024 * 1024)
                if size_mb < 0.1:
                    _skipped_too_small += 1
                    continue
            except OSError:
                continue
            
            # 候选: 孤儿文件
            candidates.append({
                "task_id": f"orphan:{rel}",
                "path": fpath,
                "kind": "orphan_file",
                "age_hours": round(age_sec / 3600, 1),
                "size_mb": round(size_mb, 1),
                "summary": f"主区文件 ({rel[:80]}) - DB 中无引用 (孤儿/残留/bot 生成)",
                "archive_id": archive_id,
                "group_id": group_id,
                "rel_path": rel,
            })
    
    # 按 size 降序
    candidates.sort(key=lambda c: -c["size_mb"])
    
    # 诊断 log: 让用户看到为什么 0 candidates
    if not candidates:
        dream_log.log(
            "dream.task.d4_workspace_cleanup.scan_diagnostic",
            f"agent={archive_id[:12]}/{group_id[:12]} group_path={group_path[:80]}: "
            f"seen={_seen} protected={_skipped_protected} "
            f"too_new(<{age_threshold_h}h)={_skipped_too_new} "
            f"too_small(<0.1MB)={_skipped_too_small} "
            f"special={_skipped_special_dir} "
            f"protected_paths_count={len(protected_norm)}",
        )
    
    return candidates


# ──────────────────────────────────────────────────────────────────
# LLM 决策 + 规则 fallback (单智能体)
# ──────────────────────────────────────────────────────────────────



def _validate_d4_output(raw: Any, input_task_ids: set[str]) -> bool:
    if not isinstance(raw, dict):
        return False
    decisions = raw.get("decisions", [])
    if not isinstance(decisions, list):
        return False
    for d in decisions:
        if not isinstance(d, dict):
            return False
        tid = d.get("task_id", "")
        if tid not in input_task_ids:
            return False
        if d.get("action") not in ("keep", "partial_delete", "delete"):
            return False
    return True


async def _llm_decide(
    candidates: list[dict], agent_mb: float, aggressiveness: str,
    archive_id: str, group_id: str, demoted: bool,
) -> dict | None:
    from app.llm import client as llm
    
    candidates_text = "\n".join(
        f"- task_id={c['task_id']}, kind={c['kind']}, age={c['age_hours']}h, size={c['size_mb']}MB\n"
        f"  summary={c['summary'][:120]!r}"
        for c in candidates[:20]
    )
    
    prompt = _LLM_PROMPT.format(
        archive=archive_id[:30], group=group_id[:30],
        agent_mb=int(agent_mb),
        aggressiveness=aggressiveness,
        n=len(candidates),
        candidates=candidates_text,
    )
    
    input_ids = {c["task_id"] for c in candidates}
    
    def _validate(raw):
        return _validate_d4_output(raw, input_ids)
    
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "Decide the cleanup action for each candidate.\n\n逐项选择清理动作。"},
    ]
    
    # 2026-05-16 Round 14: D4 默认用 lite 模型.
    # 实测 (trace 23:38): D4 跑了 184s, 其中 183s 是 main+max thinking 一次 LLM 调用
    # 输出 1983 字 JSON 决策. 期间 emergency mode 阻塞所有其他 dream 任务.
    # 但 D4 的决策很简单 ("孤儿文件 → 删"), lite 完全够用. main+max 是巨大浪费.
    # 旧逻辑只在 demoted 或 ws > 3000 时用 lite, 反直觉 — 普通场景反而慢.
    # 改成: 默认 lite_first=True (chat_json_with_upgrade 默认行为).
    # lite 失败/validate 失败时自动升级 main+max — 保留质量兜底.
    raw = await llm.chat_json_with_upgrade(
        messages,
        validate=_validate,
        label="dream_d4_cleanup",
        lite_first=True,
    )
    return raw


def _rule_based_decisions(candidates: list[dict], agent_mb: float) -> dict:
    """LLM 失败时的规则兜底.
    DB-driven 后候选都已"安全可清", 规则可以稍激进.
    """
    decisions = []
    age_thresh = _get_age_threshold_hours(agent_mb)
    aggressive = agent_mb > 2000
    
    for c in candidates:
        kind = c["kind"]
        if kind == "sandbox":
            if c["age_hours"] > age_thresh and c["size_mb"] > 20:
                decisions.append({
                    "task_id": c["task_id"],
                    "action": "delete" if aggressive else "partial_delete",
                    "reason": f"rule_fallback: sandbox age>{age_thresh}h",
                })
            else:
                decisions.append({
                    "task_id": c["task_id"], "action": "keep",
                    "reason": "rule_fallback: keep",
                })
        else:
            # 孤儿: DB 无引用, 直接 delete
            if c["age_hours"] > age_thresh:
                decisions.append({
                    "task_id": c["task_id"], "action": "delete",
                    "reason": f"rule_fallback: orphan age>{age_thresh}h",
                })
            else:
                decisions.append({
                    "task_id": c["task_id"], "action": "keep",
                    "reason": "rule_fallback: still recent",
                })
    
    return {"decisions": decisions}


async def _apply_decision(candidate: dict, action: str) -> int:
    """执行决策. 返回释放的 MB.
    
    硬安全: 路径必须在该智能体目录内, 路径白名单 + basename 检查双重保险.
    """
    path = candidate["path"]
    kind = candidate["kind"]
    archive_id = candidate.get("archive_id", "")
    group_id = candidate.get("group_id", "")
    
    # ── 硬安全 1: 路径必须包含正确的 (archive_id, group_id), 不能越界 ──
    if archive_id and group_id:
        expected_prefix_part = f"{archive_id}{os.sep}{group_id}"
        if expected_prefix_part not in path:
            dream_log.error(
                "dream.task.d4_workspace_cleanup.path_escape",
                f"path={path} doesn't contain archive/group prefix",
            )
            return 0
    
    # ── 硬安全 2: basename 合法 (sandbox / orphan) ──
    basename = os.path.basename(path)
    if kind == "sandbox":
        if not basename.startswith("_delegate_"):
            dream_log.warn(
                "dream.task.d4_workspace_cleanup.basename_escape",
                f"path={path} kind=sandbox 但 basename 不以 _delegate_ 开头",
            )
            return 0
    elif kind in ("orphan_file", "orphan_dir"):
        if basename in _PROTECTED_DIR_NAMES or basename in _SKIP_TEMP_DIRS:
            dream_log.warn(
                "dream.task.d4_workspace_cleanup.protected_basename",
                f"path={path} basename={basename} 在保护列表",
            )
            return 0
    else:
        return 0
    
    freed = 0.0
    
    if action == "delete":
        try:
            import shutil
            freed = candidate["size_mb"]
            if kind == "orphan_file":
                if not os.path.isfile(path):
                    return 0
                os.unlink(path)
            else:
                if not os.path.isdir(path):
                    return 0
                shutil.rmtree(path, ignore_errors=True)
            dream_log.log(
                "dream.task.d4_workspace_cleanup.deleted",
                f"path={basename} freed={freed:.1f}MB kind={kind} "
                f"agent={archive_id[:12]}/{group_id[:12]}",
            )
        except Exception as e:
            dream_log.error("dream.task.d4_workspace_cleanup.delete_failed", f"err={e!r}"[:200])
            return 0
    
    elif action == "partial_delete":
        # 孤儿文件 partial → 退化为 delete
        if kind == "orphan_file":
            return await _apply_decision(candidate, "delete")
        
        if not os.path.isdir(path):
            return 0
        
        artifact_exts = {".o", ".obj", ".exe", ".dll", ".so", ".pyc"}
        artifact_dirs = {"__pycache__"}
        
        try:
            for r, dirs, fs in os.walk(path, topdown=True):
                pyc_dirs = [d for d in dirs if d in artifact_dirs]
                for pd in pyc_dirs:
                    pd_path = os.path.join(r, pd)
                    try:
                        size = _du_mb(pd_path)
                        import shutil
                        shutil.rmtree(pd_path, ignore_errors=True)
                        freed += size
                        dirs.remove(pd)
                    except OSError:
                        pass
                
                for f in fs:
                    ext = os.path.splitext(f)[1].lower()
                    if ext in artifact_exts:
                        fp = os.path.join(r, f)
                        try:
                            sz = os.path.getsize(fp) / (1024 * 1024)
                            os.unlink(fp)
                            freed += sz
                        except OSError:
                            pass
            
            if freed > 0:
                dream_log.log(
                    "dream.task.d4_workspace_cleanup.partial_deleted",
                    f"path={basename} freed={freed:.1f}MB "
                    f"agent={archive_id[:12]}/{group_id[:12]}",
                )
        except Exception as e:
            dream_log.error("dream.task.d4_workspace_cleanup.partial_failed", f"err={e!r}"[:200])
    
    return int(freed)


# ──────────────────────────────────────────────────────────────────
# Task 主类
# ──────────────────────────────────────────────────────────────────

@register_dream_task
class D4WorkspaceCleanup(InfoDrivenTask):
    """D4: Per-Agent DB-driven 工作区清理.
    
    每个 (archive_id, group_id) 是独立智能体, 各自有 200-4000 MB 阈值.
    跨档触发: 当**最大智能体**跨过新档时全局触发, 各自独立清理.
    """
    
    name = "d4_workspace_cleanup"
    threshold = 100
    uses_llm = True
    
    async def info_fn(self) -> float:
        """信息量 = 最大单智能体工作区大小 MB."""
        return await _get_max_agent_size_mb()
    
    async def should_run(self) -> bool:
        """跨档触发 + 超紧急阈值兜底."""
        import time as _t
        if self.suspended_until > _t.time():
            return False
        try:
            current = await self.info_fn()
        except Exception:
            return False
        
        last = self.last_run_info
        
        # 跨档判定
        for threshold in _THRESHOLDS_MB:
            if last < threshold <= current:
                return True
        
        # 涨了 ≥ threshold MB
        if (current - last) >= self.threshold and current > 200:
            return True
        
        # 任意智能体超紧急阈值 + 距上次跑 ≥ 60s → 强制重试
        try:
            from app.config import settings as _s
            emergency_mb = getattr(_s, "dream_emergency_workspace_mb", 3500)
        except Exception:
            emergency_mb = 3500
        
        if current > emergency_mb and (_t.time() - self.last_success_at) >= 60:
            return True
        
        return False
    
    async def _do_work(self) -> None:
        from app.core.dream.cache import _get_workspace_root
        ws_root = _get_workspace_root()
        if not ws_root or not os.path.isdir(ws_root):
            return
        
        agents = await _list_agents(ws_root)
        if not agents:
            return
        
        # 2026-05-16: 按 size 降序遍历, 先处理最大的 agent
        # 原因 (实测 trace 22:59): D4 顺序遍历漏掉真正大的 (max_agent=3670MB),
        # 卡在 351MB 的小 agent 上 LLM 决策. emergency 一直触发不解除.
        agents_with_size = []
        for archive_id, group_id, group_path in agents:
            size = await asyncio.to_thread(_du_mb, group_path)
            agents_with_size.append((size, archive_id, group_id, group_path))
        agents_with_size.sort(key=lambda x: -x[0])  # 大→小
        
        # 对每个智能体独立处理
        total_freed_all = 0
        agents_cleaned = 0
        
        for agent_size, archive_id, group_id, group_path in agents_with_size:
            try:
                freed = await self._cleanup_one_agent(
                    archive_id, group_id, group_path,
                    precomputed_size=agent_size,
                )
                if freed > 0:
                    total_freed_all += freed
                    agents_cleaned += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:
                dream_log.error(
                    "dream.task.d4_workspace_cleanup.agent_failed",
                    f"archive={archive_id[:12]} group={group_id[:12]} err={e!r}"[:200],
                )
        
        if agents_cleaned > 0:
            dream_log.log(
                "dream.task.d4_workspace_cleanup.cycle_done",
                f"freed_total={total_freed_all}MB across {agents_cleaned} agents "
                f"(of {len(agents)} total)",
            )
    
    async def _cleanup_one_agent(
        self, archive_id: str, group_id: str, group_path: str,
        precomputed_size: float | None = None,
    ) -> int:
        """清理单个智能体工作区. 返回释放的 MB."""
        # 1. du 该智能体 (优先用预计算的, 否则重新算)
        if precomputed_size is not None:
            agent_mb = precomputed_size
        else:
            agent_mb = await asyncio.to_thread(_du_mb, group_path)
        if agent_mb < 200:
            return 0  # 太小, 不用清
        
        aggressiveness = _get_aggressiveness(agent_mb)
        age_threshold_h = _get_age_threshold_hours(agent_mb)
        
        # 2. 查 DB 拉受保护路径
        protected = await _query_protected_paths(archive_id, group_id)
        
        # 3. 扫该智能体工作区
        candidates = await asyncio.to_thread(
            _scan_agent_workspace, group_path, protected,
            age_threshold_h, archive_id, group_id,
        )
        candidates = candidates[:20]  # 单 agent 最多 20 个候选
        
        if not candidates:
            # 2026-05-17 Round 14f: 即使无候选也 log
            # 实测大 agent (3670MB) 全 protected, 没 candidate, 静默返 0 → 
            # supervisor 看不出"D4 没真清". 加 log 让前台可见.
            dream_log.log(
                "dream.task.d4_workspace_cleanup.no_candidates",
                f"agent={archive_id[:12]}/{group_id[:12]} size={agent_mb:.0f}MB: "
                f"no orphan/sandbox files ≥{age_threshold_h}h old "
                f"(all files probably user-uploaded & protected)",
            )
            return 0
        
        dream_log.log(
            "dream.task.d4_workspace_cleanup.agent_scanned",
            f"agent={archive_id[:12]}/{group_id[:12]} size={agent_mb:.0f}MB "
            f"aggr={aggressiveness} candidates={len(candidates)} "
            f"(orphans={sum(1 for c in candidates if c['kind'].startswith('orphan'))}, "
            f"sandboxes={sum(1 for c in candidates if c['kind']=='sandbox')})",
        )
        
        # 4. LLM 决策
        decisions = None
        try:
            decisions = await _llm_decide(
                candidates, agent_mb, aggressiveness,
                archive_id, group_id, demoted=self.demoted,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            dream_log.warn(
                "dream.task.d4_workspace_cleanup.llm_failed",
                f"err={e!r}; falling back to rules"[:200],
            )
        
        if decisions is None:
            decisions = _rule_based_decisions(candidates, agent_mb)
        
        # 5. 执行
        cand_map = {c["task_id"]: c for c in candidates}
        total_freed = 0
        kept = 0
        deleted = 0
        partial = 0
        
        # Size cap: 单 agent 单次最多删 30%
        max_delete_mb = agent_mb * 0.3
        deleted_mb = 0
        
        for d in decisions.get("decisions", []):
            cand = cand_map.get(d["task_id"])
            if not cand:
                continue
            action = d["action"]
            if action == "keep":
                kept += 1
                continue
            
            if deleted_mb + cand["size_mb"] > max_delete_mb:
                dream_log.warn(
                    "dream.task.d4_workspace_cleanup.size_cap_reached",
                    f"agent={archive_id[:12]}/{group_id[:12]} "
                    f"would exceed 30% ({max_delete_mb:.0f}MB), stopping",
                )
                break
            
            freed = await _apply_decision(cand, action)
            total_freed += freed
            deleted_mb += freed
            
            if action == "delete":
                deleted += 1
            elif action == "partial_delete":
                partial += 1
        
        if total_freed > 0:
            dream_log.log(
                "dream.task.d4_workspace_cleanup.agent_done",
                f"agent={archive_id[:12]}/{group_id[:12]} "
                f"size_before={agent_mb:.0f}MB freed={total_freed}MB "
                f"deleted={deleted} partial={partial} kept={kept}",
            )
        
        return total_freed
