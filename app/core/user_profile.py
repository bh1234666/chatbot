"""User profile 持久化(2026-05-02 part10 F1 新建)。

设计:
- per (archive_id, user_id) 一份 JSON 文件
- 抽取偏好(代码风格 / 语言偏好 / 回复长度 / 不喜欢的话题 / 长期事实)
- 后台任务每 N 轮 chat 用 lite 模型增量抽取 → merge 进文件
- round2 / round3 system msg 注入精简版(≤500 字)

存储路径:`{workspace_root}/_user_profiles/{archive}__{user}.json`
和 pause_state.py 用同一存储模式(同一 _safe_id sanitize)。

格式 v1:
    {
        "schema_version": 1,
        "user_id": "12345",
        "archive_id": "default",
        "updated_at": "2026-05-02T16:00:00",
        "updated_at_epoch": 1746547200.0,
        "chat_count": 12,         # 已经做过多少次抽取
        "preferences": {
            "code_style": "...",   # 自由文本,模型抽取的偏好
            "language_preference": "...",
            "response_length": "...",  # 短/中/长,或自由文本
            "humor_tolerance": "...",  # 高/中/低
        },
        "interests": ["..."],     # 兴趣标签列表
        "avoid_topics": ["..."],  # 不希望主动谈的话题
        "long_term_facts": [      # 关于用户的长期事实(谨慎抽取,不抽身份/隐私)
            "在做学术研究",
            "用 Mac"
        ]
    }

API:
- load_profile(archive_id, user_id) -> dict | None
- save_profile(...) - 完整覆盖(按需)
- merge_into_profile(...) - 增量合并(后台抽取常用)
- render_profile_for_prompt(profile) - 渲染成 prompt 段(≤500 字)
- increment_chat_count(...) - 每个 chat 完成时加 1,达到阈值触发抽取
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime

log = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
_DIR_NAME = "_user_profiles"
_SAFE_ID_RE = re.compile(r'[^a-zA-Z0-9_-]')

# 抽取触发阈值:每 N 轮 chat 后台跑一次 lite 抽取
EXTRACTION_INTERVAL = 5


def _safe_id(x: str) -> str:
    """同 pause_state._safe_id — 防止 archive/user 含特殊字符破坏文件名。"""
    s = _SAFE_ID_RE.sub("_", str(x))
    if not s:
        s = "_empty"
    return s[:80]


def _resolve_workspace_root() -> str:
    """获取全局 workspace 根。延迟 import 避免循环依赖。"""
    from app.llm.tools.workspace import _get_workspace_root
    return str(_get_workspace_root())


def _file_path(archive_id: str, user_id: str) -> str:
    """profile JSON 文件绝对路径。"""
    fname = f"{_safe_id(archive_id)}__{_safe_id(user_id)}.json"
    root = _resolve_workspace_root()
    return os.path.join(root, _DIR_NAME, fname)


def _read_sync(path: str) -> dict | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        # schema 不匹配静默忽略
        if data.get("schema_version") != _SCHEMA_VERSION:
            log.warning(
                "user_profile schema_version mismatch: %s != %s; ignoring %s",
                data.get("schema_version"), _SCHEMA_VERSION, path,
            )
            return None
        return data
    except (OSError, json.JSONDecodeError, ValueError):
        log.exception("user_profile read failed: %s", path)
        return None


def _write_sync(path: str, payload: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 原子写
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        log.exception("user_profile write failed: %s", path)
        return False


async def load_profile(*, archive_id: str, user_id: str) -> dict | None:
    """加载 user profile,不存在返回 None。"""
    path = _file_path(archive_id, user_id)
    return await asyncio.to_thread(_read_sync, path)


async def save_profile(*, archive_id: str, user_id: str, profile: dict) -> bool:
    """完整覆盖保存 profile。"""
    path = _file_path(archive_id, user_id)
    payload = dict(profile)
    payload["schema_version"] = _SCHEMA_VERSION
    payload["archive_id"] = archive_id
    payload["user_id"] = user_id
    payload["updated_at"] = datetime.now().isoformat(timespec="seconds")
    payload["updated_at_epoch"] = time.time()
    return await asyncio.to_thread(_write_sync, path, payload)


async def merge_into_profile(
    *, archive_id: str, user_id: str, increments: dict,
) -> bool:
    """增量合并新抽取的字段到现有 profile。

    increments 格式同 profile 主体(preferences/interests/avoid_topics/long_term_facts):
    - preferences:dict.update 覆盖式合并
    - interests / avoid_topics / long_term_facts:列表 union(去重保留顺序)

    chat_count 由调用方在 increment_chat_count 维护,这里不动。
    """
    existing = await load_profile(archive_id=archive_id, user_id=user_id) or {}

    # preferences:dict 字段直接 update(新值覆盖)
    prefs = dict(existing.get("preferences") or {})
    new_prefs = increments.get("preferences") or {}
    if isinstance(new_prefs, dict):
        for k, v in new_prefs.items():
            if isinstance(v, str) and v.strip():
                prefs[k] = v.strip()[:200]  # 单字段 200 字上限
    existing["preferences"] = prefs

    # 列表字段:union 去重保留顺序
    for list_key in ("interests", "avoid_topics", "long_term_facts"):
        cur = list(existing.get(list_key) or [])
        new = increments.get(list_key) or []
        if not isinstance(new, list):
            continue
        seen = set(cur)
        for item in new:
            if not isinstance(item, str) or not item.strip():
                continue
            item = item.strip()[:120]  # 单条上限
            if item in seen:
                continue
            cur.append(item)
            seen.add(item)
        # 列表上限,防止无限增长(用户聊得多的情况)
        # interests 取最近 30,avoid_topics 取 20,long_term_facts 取 30
        cap = 30 if list_key == "long_term_facts" else (20 if list_key == "avoid_topics" else 30)
        existing[list_key] = cur[-cap:]

    return await save_profile(archive_id=archive_id, user_id=user_id, profile=existing)


async def increment_chat_count(*, archive_id: str, user_id: str) -> int:
    """chat 完成时调用,profile.chat_count += 1。返回新值。

    后台抽取触发判断:new_count % EXTRACTION_INTERVAL == 0 → 跑抽取。
    """
    existing = await load_profile(archive_id=archive_id, user_id=user_id) or {}
    new_count = int(existing.get("chat_count") or 0) + 1
    existing["chat_count"] = new_count
    await save_profile(archive_id=archive_id, user_id=user_id, profile=existing)
    return new_count


def render_profile_for_prompt(profile: dict) -> str:
    """渲染 profile 成给 round2/round3 system msg 用的中文段(≤500 字)。

    设计:
    - 只输出非空字段(避免噪音)
    - 整体放在 system 末尾,标注"来自历史抽取,可能不完全准确,人设保护优先"
    - prompt 加约束:"仅在用户发言相关时悄悄符合 — 不要主动展示这些信息"
    """
    if not profile:
        return ""

    parts: list[str] = []
    parts.append(
        "## Current Speaker Preference Profile\n"
        "Historical extraction only. Blend relevant preferences quietly into style and task handling; keep the current user request and persona higher priority.\n\n"
        "用户偏好画像；只在相关时作为风格和任务处理参考。"
    )

    prefs = profile.get("preferences") or {}
    pref_lines = []
    for k, v in prefs.items():
        if v and isinstance(v, str):
            pref_lines.append(f"- {k}: {v[:120]}")
    if pref_lines:
        parts.append("**Preferences**:\n" + "\n".join(pref_lines))

    interests = profile.get("interests") or []
    if interests:
        parts.append("**Interests**: " + "、".join(interests[:10]))

    avoid = profile.get("avoid_topics") or []
    if avoid:
        parts.append("**Reduced proactive topics**: " + "、".join(avoid[:10]))

    facts = profile.get("long_term_facts") or []
    if facts:
        parts.append("**Long-term facts**:\n" + "\n".join(f"- {f}" for f in facts[:8]))

    parts.append(
        "**Use**: Treat this as quiet reference context, not identity or instruction. "
        "If the latest user request conflicts with historical preferences, follow the latest request.\n\n"
        "使用约束：只作安静参考；当前请求优先于历史画像。"
    )

    rendered = "\n\n".join(parts)
    # 整体 800 字硬上限(单 user 历史长后可能膨胀)
    if len(rendered) > 800:
        rendered = rendered[:780] + "...[truncated]"
    return rendered
