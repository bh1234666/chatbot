"""D24: lightweight KB node refinement.

The task improves old KB node headlines/content for searchability. It should
not make a foreground user wait on a large background LLM batch, so each cycle
uses a small candidate batch and short source snippets. The LLM still decides
whether a node needs rewriting: it may return {"keep_original": true}.
"""
from __future__ import annotations

from app.core.dream.prompt_catalog import (
    D24_REFINE_SYSTEM,
)
_D24_REFINE_SYSTEM = D24_REFINE_SYSTEM


import asyncio
import json
import time
from typing import Any

from app.core.dream.dream_log import dream_log
from app.core.dream.event_bus import event_bus
from app.core.dream.registry import register_dream_task
from app.core.dream.task_base import InfoDrivenTask


D24_THRESHOLD = 20
D24_MAX_PER_RUN = 4
D24_SOURCE_MSG_LIMIT = 3
D24_SOURCE_MSG_CHARS = 120
D24_NODE_BACKOFF_BASE_SEC = 3600.0
D24_NODE_BACKOFF_MAX_SEC = 24 * 3600.0




def _validate_d24_output(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    if raw.get("keep_original"):
        return True
    headline = raw.get("headline", "")
    content = raw.get("content", "")
    return 5 <= len(headline) <= 60 and 30 <= len(content) <= 800


def _load_node_metadata(raw: Any) -> dict:
    if raw:
        try:
            meta = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            meta = {}
    else:
        meta = {}
    return dict(meta) if isinstance(meta, dict) else {}


def _d24_node_backoff_seconds(failures: int) -> float:
    if failures <= 1:
        return D24_NODE_BACKOFF_BASE_SEC
    if failures == 2:
        return min(D24_NODE_BACKOFF_MAX_SEC, D24_NODE_BACKOFF_BASE_SEC * 6)
    return D24_NODE_BACKOFF_MAX_SEC


def _d24_node_backoff_until(meta: dict) -> float:
    try:
        return float(meta.get("d24_refine_backoff_until") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _d24_node_failure_count(meta: dict) -> int:
    try:
        return max(0, int(meta.get("d24_refine_failures") or 0))
    except (TypeError, ValueError):
        return 0


def _d24_node_is_backed_off(meta: dict, now: float) -> bool:
    return _d24_node_backoff_until(meta) > now


async def _mark_refine_failed(node: dict, reason: str) -> None:
    from app.db.pool import pool

    meta = _load_node_metadata(node.get("file_metadata"))
    failures = _d24_node_failure_count(meta) + 1
    now = time.time()
    meta.update({
        "d24_refine_failures": failures,
        "d24_refine_last_failed_at": now,
        "d24_refine_backoff_until": now + _d24_node_backoff_seconds(failures),
        "d24_refine_last_error": str(reason)[:160],
    })
    node["file_metadata"] = meta

    try:
        async with pool().acquire() as conn:
            await conn.execute(
                "UPDATE cold_nodes SET file_metadata = $1, updated_at = NOW() WHERE id = $2",
                json.dumps(meta, ensure_ascii=False),
                node["id"],
            )
    except Exception as e:
        dream_log.warn(
            "dream.task.d24_refine.failure_mark_failed",
            f"id={node.get('id')} err={e!r}"[:200],
        )


def _candidate_score(node: dict) -> int:
    import re

    headline = node.get("headline") or ""
    content = node.get("content") or ""
    text = f"{headline}\n{content}"
    abstract_re = re.compile(r"(偏好|喜欢|倾向|习惯|用户问|用户需要|用户希望|用户要求)")
    if (node.get("node_type") or "") == "preference":
        return 100
    if abstract_re.search(headline):
        return 90
    if abstract_re.search(content):
        return 60
    return 10


async def _find_unrefined_nodes(limit: int) -> list[dict]:
    from app.db.pool import pool

    async with pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, archive_id, group_id, headline, content, source_refs,
                   file_metadata, node_type, salience, created_at
            FROM cold_nodes
            WHERE scope = 'kb'
              AND node_type IN ('fact', 'preference', 'event')
              AND created_at < NOW() - INTERVAL '1 hour'
            LIMIT 500
        """)

    if not rows:
        return []

    eligible = []
    skipped_backoff = 0
    now = time.time()
    for row in rows:
        node = dict(row)
        meta = _load_node_metadata(node.get("file_metadata"))
        if str(meta.get("refined", "")).lower() == "true":
            continue
        if meta.get("merged_to"):
            continue
        if _d24_node_is_backed_off(meta, now):
            skipped_backoff += 1
            continue
        eligible.append((_candidate_score(node), _d24_node_failure_count(meta), node))

    if skipped_backoff:
        dream_log.log(
            "dream.task.d24_refine.skip_backoff",
            f"skipped {skipped_backoff} nodes with active node backoff",
        )

    eligible.sort(key=lambda x: (
        -x[0],
        x[1],
        -(x[2].get("salience") or 0),
        x[2].get("created_at") or "",
    ))
    return [node for _, _, node in eligible[:limit]]


async def _get_source_messages(
    archive_id: str,
    group_id: str,
    source_refs: list,
    limit: int = D24_SOURCE_MSG_LIMIT,
) -> list[str]:
    from app.db.pool import pool

    if not source_refs:
        return []

    try:
        msg_ids = [int(r) for r in source_refs[:limit]]
    except Exception:
        return []

    async with pool().acquire() as conn:
        rows = await conn.fetch("""
            SELECT user_name, content FROM group_messages
            WHERE archive_id = $1 AND group_id = $2 AND id = ANY($3)
            LIMIT $4
        """, archive_id, group_id, msg_ids, limit)

    return [
        f"{row['user_name']}: {(row['content'] or '')[:D24_SOURCE_MSG_CHARS]}"
        for row in rows
    ]


async def _llm_refine(node: dict, source_msgs: list[str]) -> dict | None:
    from app.llm import client as llm

    src_text = "\n".join(source_msgs) if source_msgs else "(no source snippets)"
    user_text = (
        f"## old headline\n{node['headline']}\n\n"
        f"## old content\n{(node['content'] or '')[:360]}\n\n"
        f"## source snippets\n{src_text}\n\n"
        "## task\n"
        "Decide whether this KB node is already concrete enough. "
        "If it is concrete enough, return keep_original. "
        "Only rewrite when the node is vague or hard to search."
    )
    messages = [
        {"role": "system", "content": _D24_REFINE_SYSTEM},
        {"role": "user", "content": user_text},
    ]
    return await llm.chat_json_with_upgrade(
        messages,
        validate=_validate_d24_output,
        label="dream_d24_refine",
        lite_first=True,
    )


async def _apply_refinement(node: dict, refinement: dict) -> bool:
    from app.db.pool import pool
    from app.memory.kb import sanitize_headline, sanitize_summary

    meta = _load_node_metadata(node.get("file_metadata"))
    meta.update({
        "refined": True,
        "refined_at": time.time(),
        "refined_by": "d24_refine",
    })

    if refinement.get("keep_original"):
        try:
            async with pool().acquire() as conn:
                await conn.execute("""
                    UPDATE cold_nodes
                    SET file_metadata = $1, updated_at = NOW()
                    WHERE id = $2
                """, json.dumps(meta, ensure_ascii=False), node["id"])
            return True
        except Exception as e:
            dream_log.error("dream.task.d24_refine.update_failed", f"err={e!r}"[:200])
            return False

    try:
        async with pool().acquire() as conn:
            await conn.execute("""
                UPDATE cold_nodes
                SET headline = $1, content = $2,
                    file_metadata = $3, updated_at = NOW()
                WHERE id = $4
            """,
                sanitize_headline(refinement["headline"]),
                sanitize_summary(refinement["content"]),
                json.dumps(meta, ensure_ascii=False),
                node["id"],
            )
        return True
    except Exception as e:
        dream_log.error("dream.task.d24_refine.update_failed", f"err={e!r}"[:200])
        return False


@register_dream_task
class D24Refine(InfoDrivenTask):
    """D24: refine KB node headline/content."""

    name = "d24_refine"
    threshold = D24_THRESHOLD
    uses_llm = True

    async def info_fn(self) -> float:
        return float(event_bus.total_count("kb_nodes_added"))

    async def _do_work(self) -> None:
        candidates = await _find_unrefined_nodes(D24_MAX_PER_RUN)
        if not candidates:
            return

        try:
            from app.config import settings as _s
            global_budget = float(getattr(_s, "dream_task_timeout_sec", 120.0))
        except Exception:
            global_budget = 120.0

        per_node_timeout = max(8.0, min(30.0, global_budget / max(len(candidates), 1) * 0.8))
        consecutive_failures = 0
        max_consecutive_failures = 2
        success = 0
        early_aborted = False

        for node in candidates:
            source_refs = node.get("source_refs") or []
            if isinstance(source_refs, str):
                try:
                    source_refs = json.loads(source_refs)
                except Exception:
                    source_refs = []

            source_msgs = await _get_source_messages(
                node["archive_id"], node["group_id"], source_refs
            )

            try:
                refinement = await asyncio.wait_for(
                    _llm_refine(node, source_msgs),
                    timeout=per_node_timeout,
                )
                consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                consecutive_failures += 1
                await _mark_refine_failed(node, f"timeout after {per_node_timeout:.0f}s")
                dream_log.warn(
                    "dream.task.d24_refine.node_timeout",
                    f"id={node['id']} timeout after {per_node_timeout:.0f}s "
                    f"(consecutive_failures={consecutive_failures})",
                )
                if consecutive_failures >= max_consecutive_failures:
                    dream_log.warn(
                        "dream.task.d24_refine.early_abort",
                        f"aborting cycle after {consecutive_failures} consecutive failures",
                    )
                    early_aborted = True
                    break
                continue
            except Exception as e:
                consecutive_failures += 1
                await _mark_refine_failed(node, repr(e))
                dream_log.warn(
                    "dream.task.d24_refine.llm_failed",
                    f"id={node['id']} err={e!r}"[:200],
                )
                if consecutive_failures >= max_consecutive_failures:
                    dream_log.warn(
                        "dream.task.d24_refine.early_abort",
                        f"aborting cycle after {consecutive_failures} consecutive failures",
                    )
                    early_aborted = True
                    break
                continue

            if refinement is None:
                continue
            if await _apply_refinement(node, refinement):
                success += 1

        if early_aborted and success == 0:
            self.suspended_until = time.time() + 900.0
            dream_log.warn(
                "dream.task.d24_refine.backoff",
                "consecutive failures with zero success; backing off 900s",
            )

        if success:
            dream_log.log(
                "dream.task.d24_refine.cycle_done",
                f"refined {success} nodes",
            )
