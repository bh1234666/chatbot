from __future__ import annotations

from app.config import settings
from app.core.inline_images import scan_inline_images
from app.llm.tools.command_risk import analyze_command
from app.llm.tools.helper_kinds import _filter_tools_for_kind


def test_no_gpu_mode_removes_ocr_tts_and_blocks_gpu_commands(monkeypatch):
    monkeypatch.setattr(settings, "gpu_disabled", True)
    tools = [
        {"type": "function", "function": {"name": "ocr"}},
        {"type": "function", "function": {"name": "tts"}},
        {"type": "function", "function": {"name": "read_file"}},
    ]

    names = {tool["function"]["name"] for tool in _filter_tools_for_kind("read", tools)}
    assert names == {"read_file"}
    assert scan_inline_images("archive", "group") == []
    assert not analyze_command("python train.py --device cuda", ".", is_main_thread=False).allowed
