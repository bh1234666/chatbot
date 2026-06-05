"""L4-1: workspace.locate 单元测试."""
import pytest
import os
from app.llm.tools.workspace import handle_locate


@pytest.mark.asyncio
async def test_locate_glob_finds_main_layer(fake_workspace):
    r = await handle_locate(str(fake_workspace), "chart*.png")
    assert r["ok"]
    paths = [m["path"] for m in r["matches"]]
    assert len(paths) == 5
    assert "_downloaded_media/chart1.png" in paths
    assert "_helpers_shared/merge_charts/chart1.png" in paths
    assert r["matches"][0]["layer"] == "main"


@pytest.mark.asyncio
async def test_locate_substring_auto_wraps_stars(fake_workspace):
    r = await handle_locate(str(fake_workspace), "merged")
    assert r["ok"]
    assert r["total"] == 0
    assert "hint" in r


@pytest.mark.asyncio
async def test_locate_includes_downloaded_media_for_media_patterns(fake_workspace):
    r = await handle_locate(str(fake_workspace), "chart1.png")
    paths = [m["path"] for m in r["matches"]]
    assert "_downloaded_media/chart1.png" in paths


@pytest.mark.asyncio
async def test_locate_caps_at_max_matches(tmp_path):
    for i in range(100):
        (tmp_path / f"file_{i:03d}.png").write_bytes(b"x")
    r = await handle_locate(str(tmp_path), "*.png")
    assert r["ok"]
    assert r.get("truncated") is True
    assert r["total_matches"] == 100


@pytest.mark.asyncio
async def test_locate_empty_pattern_rejected(fake_workspace):
    r = await handle_locate(str(fake_workspace), "")
    assert not r["ok"]
    assert "pattern" in r["error"].lower()
