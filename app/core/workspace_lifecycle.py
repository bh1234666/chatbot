from __future__ import annotations

import asyncio

from app.llm.tools import workspace as ws_tool


async def delayed_workspace_unregister(ws_dir: str, group_key: str, delay: float = 30.0) -> None:
    await asyncio.sleep(delay)
    ws_tool.unregister_workspace(group_key, ws_dir)
