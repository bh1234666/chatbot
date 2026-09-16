from __future__ import annotations

import base64
import json
import struct
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.llm.tools import image_generation, model_vision
from app.llm.tools.helper_kinds import _filter_tools_for_kind
from app.llm.tools.registry import _handle_ocr


def _png(width: int = 512, height: int = 512) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + b"\x80\x40\x20" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


@pytest.mark.asyncio
async def test_model_vision_uses_gpt56_sol(monkeypatch, tmp_path: Path):
    image = tmp_path / "input.png"
    image.write_bytes(_png())
    captured = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="detailed description"))])

    monkeypatch.setattr(settings, "model_vision_enabled", True)
    monkeypatch.setattr(settings, "gpt55_api_key", "test-key")
    monkeypatch.setattr(settings, "gpt55_base_url", "https://example.test/v1")
    monkeypatch.setattr(model_vision, "AsyncOpenAI", lambda **_: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions())))

    result = await model_vision.describe_image(image_path=image, purpose="read it")
    assert result["ok"] is True
    assert captured["model"] == "gpt-5.6-sol"
    assert captured["messages"][0]["content"][1]["type"] == "image_url"


@pytest.mark.asyncio
async def test_image2_text_to_image_saves_and_describes(monkeypatch, tmp_path: Path):
    payload = base64.b64encode(_png()).decode("ascii")

    class FakeImages:
        async def generate(self, **kwargs):
            assert kwargs["model"] == "gpt-image-2"
            return SimpleNamespace(data=[SimpleNamespace(b64_json=payload, url=None, revised_prompt="revised")])

    async def fake_describe_image(**kwargs):
        return {"ok": True, "description": "generated scene description"}

    monkeypatch.setattr(settings, "image_generation_enabled", True)
    monkeypatch.setattr(settings, "gpt55_api_key", "test-key")
    monkeypatch.setattr(settings, "gpt55_base_url", "https://example.test/v1")
    monkeypatch.setattr(image_generation, "AsyncOpenAI", lambda **_: SimpleNamespace(images=FakeImages()))
    monkeypatch.setattr(image_generation, "describe_image", fake_describe_image)

    output = tmp_path / "generated.png"
    result = await image_generation.generate_image(prompt="a test scene", output_path=output)
    assert result.ok is True
    assert result.mode == "text-to-image"
    assert result.description == "generated scene description"
    assert output.is_file()


def test_image_generation_tool_is_exclusive_to_image_gen_helper(monkeypatch):
    monkeypatch.setattr(settings, "image_generation_enabled", True)
    tools = [
        {"type": "function", "function": {"name": "image_generate"}},
        {"type": "function", "function": {"name": "read_file"}},
    ]
    code_names = {tool["function"]["name"] for tool in _filter_tools_for_kind("code", tools)}
    image_names = {tool["function"]["name"] for tool in _filter_tools_for_kind("image_gen", tools)}
    assert "image_generate" not in code_names
    assert image_names == {"image_generate", "read_file"}


def test_model_vision_repairs_reversible_gateway_mojibake_line_by_line():
    original = "Title: OCR\u6d4b\u8bd5\u56fe\u50cf"
    damaged = original.encode("utf-8").decode("gbk")
    repaired, count = model_vision._repair_response_mojibake(damaged + "\nplain English")
    assert repaired == original + "\nplain English"
    assert count == 1


@pytest.mark.asyncio
async def test_full_nogpu_ocr_uses_remote_model_vision(monkeypatch, tmp_path: Path):
    image = tmp_path / "source.png"
    image.write_bytes(_png())

    async def fake_describe_image(**kwargs):
        assert kwargs["image_path"] == str(image)
        return {"ok": True, "text": "remote visual description", "engine": "model_vision"}

    monkeypatch.setattr(settings, "gpu_disabled", True)
    monkeypatch.setattr(settings, "vision_enabled", False)
    monkeypatch.setattr(settings, "model_vision_enabled", True)
    monkeypatch.setattr(model_vision, "describe_image", fake_describe_image)

    result = json.loads(await _handle_ocr(str(tmp_path), {"image_path": "source.png"}))
    assert result["ok"] is True
    assert result["engine"] == "model_vision"
