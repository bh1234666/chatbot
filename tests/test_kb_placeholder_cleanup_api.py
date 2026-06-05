import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def test_botctl_exposes_kb_placeholder_cleanup():
    import botctl_helper
    import napcat_bridge

    assert "cleanup-kb-placeholders" in botctl_helper.COMMANDS
    assert "cleanup-kb-placeholders" in napcat_bridge._KNOWN_BOTCTL_CMDS


async def test_archive_api_cleans_kb_placeholders(monkeypatch):
    from app.api import archives

    async def fake_get_archive(archive_id):
        return {"archive_id": archive_id, "name": "demo"}

    async def fake_list_groups():
        return [
            {"group_id": "g1", "personas": [{"archive_id": "archive"}]},
            {"group_id": "g2", "personas": [{"archive_id": "other"}]},
        ]

    calls = []

    async def fake_cleanup(archive_id, group_id):
        calls.append((archive_id, group_id))
        return 2

    monkeypatch.setattr(archives.dao, "get_archive", fake_get_archive)
    monkeypatch.setattr(archives.bot_config, "list_groups", fake_list_groups)
    monkeypatch.setattr(archives.kb, "cleanup_stale_file_placeholders", fake_cleanup)

    result = await archives.cleanup_archive_kb_placeholders("archive")

    assert result["groups_scanned"] == 1
    assert result["nodes_removed"] == 2
    assert calls == [("archive", "g1")]
