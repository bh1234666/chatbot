"""
voice_output 的语音意图判定回归测试。
"""
import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.llm.voice_output import (
    _round3_parallel_decision,
    decide_voice,
    decide_voice_intent_from_user,
    should_keep_round2_tts_tool,
)


def test_voice_reply_intent_is_demand():
    assert decide_voice_intent_from_user("用语音回复我") == "demand"
    assert decide_voice_intent_from_user("说给我听") == "demand"
    assert should_keep_round2_tts_tool("语音回复我，内容是:关注塔菲喵") is False


def test_voice_file_request_is_not_voice_reply():
    assert decide_voice_intent_from_user("生成一段随便内容的语音文件") == "refuse"
    assert decide_voice_intent_from_user("输出音频文件，文字回复") == "refuse"
    assert should_keep_round2_tts_tool("生成一段随便内容的语音文件") is True
    assert should_keep_round2_tts_tool("输出音频文件，文字回复") is True


def test_orchestrator_marks_audio_generation_as_new_unless_user_reuses():
    from app.core.orchestrator_entry import (
        _is_audio_file_artifact_request,
        _is_new_audio_file_request,
    )

    assert _is_new_audio_file_request("测试，生成语音文件，内容为你好") is True
    assert _is_new_audio_file_request("合成一个 wav 文件") is True
    assert _is_new_audio_file_request("测试，输出一段随机测试语音") is True
    assert _is_new_audio_file_request("重发刚才那个语音文件") is False
    assert _is_new_audio_file_request("复用现有 hello.wav，不用重新生成") is False
    assert _is_audio_file_artifact_request("重发刚才那个语音文件") is True
    assert _is_audio_file_artifact_request("用语音回复我") is False


@pytest.mark.asyncio
async def test_voice_preference_zero_forces_text_even_with_parallel_voice():
    token = _round3_parallel_decision.set("voice")
    try:
        decision = await decide_voice(
            reply_text="记住了，测试暗号：蓝色钟表。",
            user_message="记住这个测试暗号：蓝色钟表。然后回复你记住了什么。",
            voice_preference=0.0,
        )
    finally:
        _round3_parallel_decision.reset(token)

    assert decision.use_voice is False
    assert "preference is 0" in decision.reason


@pytest.mark.asyncio
async def test_voice_preference_zero_blocks_explicit_voice_reply():
    token = _round3_parallel_decision.set("voice")
    try:
        decision = await decide_voice(
            reply_text="记住了，测试暗号：蓝色钟表。",
            user_message="用语音回复我：记住这个测试暗号。",
            voice_preference=0.0,
        )
    finally:
        _round3_parallel_decision.reset(token)

    assert decision.use_voice is False
    assert "preference is 0" in decision.reason


@pytest.mark.asyncio
async def test_voice_preference_one_uses_voice_unless_too_long():
    short = "\u4f60\u597d\u5440"
    long = "\u8fd9\u662f\u4e00\u4e2a\u5f88\u957f\u7684\u56de\u590d\u3002" * 260

    short_decision = await decide_voice(
        reply_text=short,
        user_message="hello",
        voice_preference=1.0,
    )
    long_decision = await decide_voice(
        reply_text=long,
        user_message="hello",
        voice_preference=1.0,
    )

    assert short_decision.use_voice is True
    assert long_decision.use_voice is False
    assert long_decision.too_long is True


@pytest.mark.asyncio
async def test_round3_parallel_streams_text_before_slow_voice_decision(monkeypatch):
    from app.core import orchestrator
    import app.llm.voice_output as voice_output

    async def fake_round3(*args, voice_intent="refuse", **kwargs):
        if voice_intent == "refuse":
            await asyncio.sleep(0.05)
            yield "A"
            await asyncio.sleep(0.05)
            yield "B"
        else:
            await asyncio.sleep(0.2)
            yield "V"

    async def fake_decide(*args, **kwargs):
        await asyncio.sleep(0.5)
        return "voice"

    monkeypatch.setattr(orchestrator, "_round3", fake_round3)
    monkeypatch.setattr(voice_output, "decide_voice_with_context_lite", fake_decide)

    start = time.perf_counter()
    gen = orchestrator._round3_parallel("", None, "u", "m", [], voice_preference=0.5)
    try:
        first = await gen.__anext__()
    finally:
        await gen.aclose()

    assert first == "A"
    assert time.perf_counter() - start < 0.25
