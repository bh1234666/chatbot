import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class DebugRecorder:
    def __init__(self):
        self.events = []

    def log(self, category, message, payload=None):
        self.events.append((category, message, payload))


async def test_write_recovery_jsonl_uses_safe_filename(monkeypatch, tmp_path):
    from app.core import recovery_log

    monkeypatch.setattr(recovery_log.settings, "workspace_root", str(tmp_path / "workspaces"))
    debug = DebugRecorder()

    await recovery_log.write_recovery_jsonl(
        archive_id="archive/../x",
        group_id="group:1",
        user_id="user*2",
        speaker="Alice",
        user_message="hello",
        assistant_message="hi",
        trace_id="t1",
        hot_write_ok=False,
        gm_write_ok=True,
        debug=debug,
    )

    files = list((tmp_path / "recovery").glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].name == "archive____x_group_1_user_2.jsonl"
    record = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert record["trace_id"] == "t1"
    assert record["user_message"] == "hello"
    assert record["hot_write_ok"] is False
    assert record["gm_write_ok"] is True
    assert debug.events[-1][0] == "memory.recovery.jsonl"
