import json

import pytest


@pytest.mark.asyncio
async def test_expand_warm_empty_match_is_fact_not_tool_failure(monkeypatch):
    from app.llm.tools import registry

    async def fake_expand_warm(archive_id, ids):
        assert archive_id == "arch"
        assert ids == ["w_001"]
        return []

    monkeypatch.setattr(registry.warm_mem, "expand_warm", fake_expand_warm)

    result = json.loads(await registry._handle_expand_warm("arch", {"ids": ["w_001"]}))

    assert result["ok"] is True
    assert result["items"] == []
    assert result["matched_count"] == 0
    assert "No requested warm-memory IDs matched" in result["no_match_fact"]
    assert "主题词猜测不是证据" in result["no_match_fact"]


@pytest.mark.asyncio
async def test_expand_cold_and_kb_empty_match_are_fact_not_tool_failure(monkeypatch):
    from app.llm.tools import registry

    async def fake_expand_cold(archive_id, ids, depth, viewer_user_id):
        assert (archive_id, ids, depth, viewer_user_id) == ("arch", ["c_001"], 2, "u")
        return []

    async def fake_expand_kb(archive_id, ids, depth, viewer_user_id):
        assert (archive_id, ids, depth, viewer_user_id) == ("arch", ["kb_001"], 1, "u")
        return []

    monkeypatch.setattr(registry.cold_mem, "expand_cold", fake_expand_cold)
    monkeypatch.setattr(registry.kb_mem, "expand_kb", fake_expand_kb)

    cold = json.loads(await registry._handle_expand_cold("arch", "u", {"ids": ["c_001"], "depth": 2}))
    kb = json.loads(await registry._handle_expand_kb("arch", "u", {"ids": ["kb_001"]}))

    for result in (cold, kb):
        assert result["ok"] is True
        assert result["items"] == []
        assert result["matched_count"] == 0
        assert "matched the current index" in result["no_match_fact"]
