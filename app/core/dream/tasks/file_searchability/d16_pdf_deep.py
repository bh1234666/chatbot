"""
D16: PDF 深度提取 + LLM 摘要

问题: 现有 extract_pdf_text 只读前 10 页 + max_chars 限制, 后面找不到
目标: dream 后台跑完整 PDF + LLM 总结 + 关键词 + 章节大纲

工作流 (D 类长任务, 按页 checkpoint):
1. 找未 dream_processed 的 PDF 节点
2. 对每个 PDF:
   Step 1: 读 metadata → page_count
   Step 2..N+1: 每页 extract_text → checkpoint save (服务重启可继续)
   Step N+2: LLM (main+max) 总结 + 关键词 + 大纲
   Step N+3: UPDATE cold_nodes
3. 大 PDF (>30 页): 优先头 10 + 尾 5 + 抽样, 中间页留下次

阈值: 1 (event-driven, 每 PDF 都做)
"""
from __future__ import annotations

from app.core.dream.prompt_catalog import (
    D16_PDF_DEEP_SYSTEM,
)
_LLM_PROMPT_SYSTEM = D16_PDF_DEEP_SYSTEM


import asyncio
import json
import os
import time
from typing import Any

from app.core.dream.cache import dream_cache
from app.core.dream.dream_log import dream_log
from app.core.dream.event_bus import event_bus
from app.core.dream.registry import register_dream_task
from app.core.dream.task_base import LongRunningDreamTask
from app.core.dream.tasks.file_searchability.file_meta import (
    emit_file_indexed,
    is_active_file_metadata,
)


D16_THRESHOLD = 1
D16_MAX_PDFS_PER_RUN = 3
_D16_RAW_SCAN_LIMIT = 5000
_D16_MAX_CANDIDATES_PER_RUN = 200
D16_LARGE_PDF_PAGES = 30  # 超此页数视为大 PDF
D16_PRIORITY_PAGES_PER_END = 10  # 大 PDF 头尾各保留页数




def _validate_d16_output(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    summary = raw.get("summary", "")
    if not (50 <= len(summary) <= 800):
        return False
    if not isinstance(raw.get("outline", []), list):
        return False
    if not isinstance(raw.get("keywords", []), list):
        return False
    if not isinstance(raw.get("topics", []), list):
        return False
    return True


async def _get_unprocessed_pdfs(limit: int) -> list[dict]:
    from app.db.pool import pool
    
    async with pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, archive_id, group_id, headline, content, file_metadata
            FROM cold_nodes
            WHERE scope = 'kb' AND node_type = 'file'
              AND (file_metadata->>'dream_processed' IS NULL
                   OR file_metadata->>'dream_processed' = 'false')
              AND file_metadata->>'workspace_path' IS NOT NULL
              AND file_metadata->>'workspace_path' != ''
              AND (file_metadata->>'deleted' IS NULL
                   OR file_metadata->>'deleted' != 'true')
              AND (file_metadata->>'download_status' IS NULL
                   OR file_metadata->>'download_status' = ''
                   OR file_metadata->>'download_status' = 'done')
            ORDER BY created_at DESC
            LIMIT $1
        """, _D16_RAW_SCAN_LIMIT)
    
    pdfs = []
    for r in rows:
        meta = r["file_metadata"]
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                continue
        if not isinstance(meta, dict):
            continue
        if not is_active_file_metadata(meta):
            continue
        
        filename = meta.get("filename", "") or meta.get("file_name", "")
        if not filename.lower().endswith(".pdf"):
            continue
        
        pdfs.append({
            "id": r["id"],
            "archive_id": r["archive_id"],
            "group_id": r["group_id"],
            "headline": r["headline"],
            "content": r["content"] or "",
            "file_metadata": meta,
            "filename": filename,
        })
        
        if len(pdfs) >= limit:
            break
    
    return pdfs


def _get_pdf_path(file_meta: dict) -> str | None:
    ws_path = file_meta.get("workspace_path") or ""
    if not ws_path:
        return None
    from app.core.dream.cache import _get_workspace_root
    ws_root = _get_workspace_root()
    archive_id = file_meta.get("archive_id", "")
    group_id = file_meta.get("group_id", "")
    if not ws_root:
        return None
    full = os.path.join(ws_root, archive_id, group_id, ws_path)
    return full if os.path.isfile(full) else None


def _read_pdf_metadata(path: str) -> dict:
    """同步 - 读 PDF 元数据."""
    try:
        import pypdf
        reader = pypdf.PdfReader(path)
        return {"page_count": len(reader.pages), "ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _extract_pdf_page(path: str, page_num: int) -> str:
    """同步 - 提取单页文字."""
    try:
        import pypdf
        reader = pypdf.PdfReader(path)
        if page_num < 1 or page_num > len(reader.pages):
            return ""
        page = reader.pages[page_num - 1]
        try:
            text = page.extract_text() or ""
        except Exception:
            return ""
        return text.strip()[:4000]
    except Exception:
        return ""


@register_dream_task
class D16PdfDeepExtract(LongRunningDreamTask):
    """D16: PDF 深度提取 (按页 checkpoint)."""
    
    name = "d16_pdf_deep"
    threshold = D16_THRESHOLD
    uses_llm = True
    startup_sweep = True
    
    async def info_fn(self) -> float:
        try:
            return float(len(await _get_unprocessed_pdfs(limit=1000)))
        except Exception:
            return float(event_bus.total_count("file_uploaded"))

    async def should_run(self) -> bool:
        import time as _t
        if self.suspended_until > _t.time():
            return False
        try:
            return await self.info_fn() >= self.threshold
        except Exception:
            return False
    
    async def steps_fn(self, context: dict) -> list[dict]:
        """单 PDF 的 steps (调用前 context_id 已设置)"""
        page_count = context.get("page_count", 0)
        if not page_count:
            return [{"name": "metadata"}]
        
        steps = [{"name": "metadata"}]
        
        # 大 PDF: 优先头 10 + 尾 5 (这次跑完后, 中间页留下次)
        if page_count > D16_LARGE_PDF_PAGES:
            priority = list(range(1, D16_PRIORITY_PAGES_PER_END + 1))
            priority += list(range(
                max(D16_PRIORITY_PAGES_PER_END + 1, page_count - 4),
                page_count + 1
            ))
            pages = sorted(set(priority))
        else:
            pages = list(range(1, page_count + 1))
        
        for p in pages:
            steps.append({"name": f"page_{p:03d}", "page_num": p})
        
        steps.append({"name": "summarize"})
        steps.append({"name": "update_node"})
        return steps
    
    async def execute_step(self, step: dict, context: dict) -> Any:
        step_name = step["name"]
        
        if step_name == "metadata":
            path = context["pdf_path"]
            return await asyncio.to_thread(_read_pdf_metadata, path)
        
        if step_name.startswith("page_"):
            path = context["pdf_path"]
            page_num = step["page_num"]
            return await asyncio.to_thread(_extract_pdf_page, path, page_num)
        
        if step_name == "summarize":
            # 拼接所有页面 text → LLM 摘要
            pages_text = []
            manifest = await dream_cache.load_manifest(self.context_id, self.name)
            for k, v in manifest.get("step_results", {}).items():
                if k.startswith("page_") and isinstance(v, str) and v:
                    pages_text.append(f"[{k}]\n{v}")
            
            if not pages_text:
                return {"skip": True, "reason": "no_page_text"}
            
            full_text = "\n\n".join(pages_text)[:30000]  # LLM context cap
            
            from app.llm import client as llm
            messages = [
                {"role": "system", "content": _LLM_PROMPT_SYSTEM},
                {"role": "user", "content": f"## Extracted PDF Text\n\n{full_text}\n\nPDF 提取文本。"},
            ]
            raw = await llm.chat_json_with_upgrade(
                messages,
                validate=_validate_d16_output,
                label="dream_d16_pdf",
                lite_first=False,
            )
            return raw or {}
        
        if step_name == "update_node":
            # 把 summarize 结果写入 cold_nodes
            manifest = await dream_cache.load_manifest(self.context_id, self.name)
            results = manifest.get("step_results", {})
            summary_result = results.get("summarize") or {}
            
            await self._update_node(context, results, summary_result)
            return {"updated": True}
        
        return {}
    
    async def _update_node(self, context: dict, results: dict, summary: dict) -> None:
        from app.db.pool import pool
        from app.memory.kb import sanitize_summary
        
        node = context["node"]
        
        # 拼装新 content
        parts = []
        if summary and not summary.get("skip"):
            if summary.get("summary"):
                parts.append(f"[摘要]\n{summary['summary']}")
            if summary.get("outline"):
                parts.append("[大纲]\n" + "\n".join(summary["outline"][:20]))
            if summary.get("keywords"):
                parts.append("[关键词] " + ", ".join(summary["keywords"]))
        
        # 加入页面摘要 (前若干页关键内容)
        page_excerpts = []
        for k, v in sorted(results.items()):
            if k.startswith("page_") and isinstance(v, str) and v:
                page_excerpts.append(v[:500])
                if len(page_excerpts) >= 5:
                    break
        if page_excerpts:
            parts.append("[内容片段]\n" + "\n---\n".join(page_excerpts))
        
        new_content = sanitize_summary("\n\n".join(parts)) if parts else node["content"]
        
        # 更新 metadata
        meta = dict(node["file_metadata"])
        meta.update({
            "dream_processed": True,
            "dream_processed_at": time.time(),
            "dream_task": "d16_pdf_deep",
            "page_count": context.get("page_count", 0),
            "pages_extracted": sum(
                1 for k, v in results.items() if k.startswith("page_") and v
            ),
            "topics": summary.get("topics", []) if isinstance(summary, dict) else [],
            "keywords": summary.get("keywords", []) if isinstance(summary, dict) else [],
            "doc_type": summary.get("doc_type", "") if isinstance(summary, dict) else "",
        })
        
        try:
            async with pool().acquire() as conn:
                await conn.execute("""
                    UPDATE cold_nodes
                    SET content = $1, file_metadata = $2, updated_at = NOW()
                    WHERE id = $3
                """, new_content, json.dumps(meta, ensure_ascii=False), node["id"])
            await emit_file_indexed(node, self.name)
        except Exception as e:
            dream_log.error("dream.task.d16_pdf_deep.update_failed", repr(e)[:200])
    
    async def _do_work(self) -> None:
        candidates = await _get_unprocessed_pdfs(_D16_MAX_CANDIDATES_PER_RUN)
        if not candidates:
            return
        
        dream_log.log("dream.task.d16_pdf_deep.found", f"pdfs={len(candidates)}")
        
        attempted = 0
        for node in candidates:
            pdf_path = _get_pdf_path(node["file_metadata"])
            if not pdf_path:
                await self._mark_skipped(node, "path_invalid")
                continue
            if attempted >= D16_MAX_PDFS_PER_RUN:
                break
            attempted += 1
            
            # 读 metadata 决定 step
            meta = await asyncio.to_thread(_read_pdf_metadata, pdf_path)
            if not meta.get("ok"):
                dream_log.warn(
                    "dream.task.d16_pdf_deep.read_failed",
                    f"id={node['id']} err={meta.get('error', '?')}",
                )
                await self._mark_skipped(node, "read_failed")
                continue
            
            page_count = meta["page_count"]
            
            # 设置 context_id, 这是 LongRunningDreamTask 用的 cache key
            self.context_id = node["id"]
            
            # 准备 context
            context = {
                "task_id": node["id"],
                "pdf_path": pdf_path,
                "page_count": page_count,
                "node": node,
            }
            
            try:
                # 调用基类逻辑 (含 checkpoint)
                # 但 base 的 _do_work 用法是: 子类负责调度. 这里手动跑
                steps = await self.steps_fn(context)
                
                manifest = await dream_cache.load_manifest(self.context_id, self.name)
                completed = set(manifest.get("completed_steps", []))
                results = manifest.get("step_results", {})
                
                for step in steps:
                    step_name = step["name"]
                    if step_name in completed:
                        continue
                    try:
                        result = await asyncio.wait_for(
                            self.execute_step(step, context),
                            timeout=60,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        dream_log.warn(
                            "dream.task.d16_pdf_deep.step_failed",
                            f"id={node['id']} step={step_name} err={e!r}"[:200],
                        )
                        break  # 跳到下一个 PDF
                    
                    results[step_name] = result
                    completed.add(step_name)
                    await dream_cache.save_step(self.context_id, self.name, step_name, result)
                
                # 完成标记
                await dream_cache.mark_complete(self.context_id, self.name)
                dream_log.log(
                    "dream.task.d16_pdf_deep.indexed",
                    f"id={node['id']} pages={page_count}",
                )
            except asyncio.CancelledError:
                raise  # checkpoint 已保存, 下次接着
    
    async def _mark_skipped(self, node, reason):
        from app.db.pool import pool
        meta = dict(node["file_metadata"])
        meta.update({
            "dream_processed": True,
            "dream_processed_at": time.time(),
            "dream_skipped": True,
            "dream_skip_reason": reason,
        })
        try:
            async with pool().acquire() as conn:
                await conn.execute(
                    "UPDATE cold_nodes SET file_metadata = $1 WHERE id = $2",
                    json.dumps(meta, ensure_ascii=False), node["id"],
                )
        except Exception:
            pass
