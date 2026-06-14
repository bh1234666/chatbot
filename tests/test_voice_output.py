"""
voice_output 的语音意图判定回归测试。
"""
import asyncio
import time
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.llm.voice_output import (
    _parse_voice_classifier_label,
    _project_reply_shape_facts,
    _round3_parallel_decision,
    _round3_voice_route_snapshot,
    _voice_preference_hint,
    decide_voice,
    decide_voice_with_context_lite,
    decide_voice_intent_from_user,
    should_keep_round2_tts_tool,
)


async def _drain_after_first(gen):
    first = await gen.__anext__()
    rest = [tok async for tok in gen]
    return first, "".join([first, *rest])


def test_voice_reply_intent_is_demand():
    assert decide_voice_intent_from_user("用语音回复我") == "neutral"
    assert decide_voice_intent_from_user("说给我听") == "neutral"
    assert should_keep_round2_tts_tool("语音回复我，内容是:关注塔菲喵") is False


def test_voice_file_request_is_not_voice_reply():
    assert decide_voice_intent_from_user("生成一段随便内容的语音文件") == "neutral"
    assert decide_voice_intent_from_user("输出音频文件，文字回复") == "neutral"
    assert should_keep_round2_tts_tool("生成一段随便内容的语音文件") is False
    assert should_keep_round2_tts_tool("输出音频文件，文字回复") is False


def test_voice_classifier_label_parser_is_strict():
    assert _parse_voice_classifier_label("voice") == "voice"
    assert _parse_voice_classifier_label("text") == "text"
    assert _parse_voice_classifier_label("text, not voice") == "text"
    assert _parse_voice_classifier_label("not voice") == "unavailable"
    assert _parse_voice_classifier_label("choose voice") == "unavailable"


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
async def test_voice_preference_one_is_voice_boundary():
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
    explicit_decision = await decide_voice(
        reply_text=short,
        user_message="use voice reply",
        voice_preference=1.0,
    )

    assert short_decision.use_voice is True
    assert "preference is 1" in short_decision.reason
    assert explicit_decision.use_voice is True
    assert long_decision.use_voice is False
    assert long_decision.too_long is True


@pytest.mark.asyncio
async def test_round3_parallel_streams_text_before_voice_authorization(monkeypatch):
    from app.core import orchestrator
    import app.llm.voice_output as voice_output

    async def fake_round3(*args, voice_intent="neutral", delivery_candidate="text", **kwargs):
        if delivery_candidate == "text":
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
        first, final_text = await _drain_after_first(gen)
    finally:
        await gen.aclose()

    assert first == "A"
    assert final_text == "AB"
    assert _round3_parallel_decision.get() == "voice"
    assert 0.45 <= time.perf_counter() - start < 1.0


@pytest.mark.asyncio
async def test_round3_parallel_mid_preference_records_classifier_voice(monkeypatch):
    from app.core import orchestrator
    import app.llm.voice_output as voice_output

    async def fake_round3(*args, voice_intent="neutral", delivery_candidate="text", **kwargs):
        if delivery_candidate == "text":
            await asyncio.sleep(0.02)
            yield "T"
        else:
            await asyncio.sleep(0.05)
            yield "V"

    async def fake_decide(*args, **kwargs):
        await asyncio.sleep(0.1)
        return "voice"

    monkeypatch.setattr(orchestrator, "_round3", fake_round3)
    monkeypatch.setattr(voice_output, "decide_voice_with_context_lite", fake_decide)

    gen = orchestrator._round3_parallel("", None, "u", "hello", [], voice_preference=0.7)
    try:
        first, _ = await _drain_after_first(gen)
    finally:
        await gen.aclose()

    assert first == "T"
    assert _round3_parallel_decision.get() == "voice"


@pytest.mark.asyncio
async def test_round3_parallel_does_not_wait_two_seconds_before_first_token(monkeypatch):
    from app.core import orchestrator
    import app.llm.voice_output as voice_output

    async def fake_round3(*args, voice_intent="neutral", delivery_candidate="text", **kwargs):
        if delivery_candidate == "text":
            await asyncio.sleep(0.02)
            yield "T"
        else:
            await asyncio.sleep(0.05)
            yield "V"

    async def slow_decide(*args, **kwargs):
        await asyncio.sleep(2.1)
        return "voice"

    monkeypatch.setattr(orchestrator, "_round3", fake_round3)
    monkeypatch.setattr(voice_output, "decide_voice_with_context_lite", slow_decide)

    start = time.perf_counter()
    gen = orchestrator._round3_parallel("", None, "u", "hello", [], voice_preference=0.9)
    try:
        first = await gen.__anext__()
        first_elapsed = time.perf_counter() - start
        rest = [tok async for tok in gen]
    finally:
        await gen.aclose()

    elapsed = time.perf_counter() - start
    assert first == "T"
    assert "".join([first, *rest]) == "T"
    assert _round3_parallel_decision.get() == "voice"
    assert first_elapsed < 0.3
    assert 2.0 <= elapsed < 2.6


@pytest.mark.asyncio
async def test_round3_parallel_high_preference_honors_classifier_text(monkeypatch):
    from app.core import orchestrator
    from app.schemas.api import ResponsePlan
    import app.llm.voice_output as voice_output

    async def fake_round3(*args, voice_intent="neutral", delivery_candidate="text", **kwargs):
        if delivery_candidate == "text":
            await asyncio.sleep(0.02)
            yield "T"
        else:
            await asyncio.sleep(0.05)
            yield "V"

    async def slow_decide(*args, **kwargs):
        await asyncio.sleep(1.0)
        return "text"

    monkeypatch.setattr(orchestrator, "_round3", fake_round3)
    monkeypatch.setattr(voice_output, "decide_voice_with_context_lite", slow_decide)

    plan = ResponsePlan(
        intent="simple greeting",
        key_points=["short natural reply"],
        tone="natural",
        length_hint="短",
    )
    start = time.perf_counter()
    gen = orchestrator._round3_parallel("", plan, "u", "你好", [], voice_preference=0.9)
    try:
        first = await gen.__anext__()
    finally:
        await gen.aclose()

    assert first == "T"
    assert time.perf_counter() - start < 0.3


@pytest.mark.asyncio
async def test_round3_parallel_max_preference_skips_classifier_and_authorizes_voice(monkeypatch):
    from app.core import orchestrator
    import app.llm.voice_output as voice_output

    async def fake_round3(*args, voice_intent="neutral", delivery_candidate="text", **kwargs):
        if delivery_candidate == "text":
            await asyncio.sleep(0.02)
            yield "T"
        else:
            await asyncio.sleep(0.05)
            yield "V"

    async def fail_decide(*args, **kwargs):
        raise AssertionError("voice_preference=1 should not call classifier")

    monkeypatch.setattr(orchestrator, "_round3", fake_round3)
    monkeypatch.setattr(voice_output, "decide_voice_with_context_lite", fail_decide)

    gen = orchestrator._round3_parallel("", None, "u", "hello", [], voice_preference=1.0)
    try:
        first, _ = await _drain_after_first(gen)
    finally:
        await gen.aclose()

    assert first == "T"
    assert _round3_parallel_decision.get() == "voice"


@pytest.mark.asyncio
async def test_round3_parallel_explicit_voice_request_uses_classifier(monkeypatch):
    from app.core import orchestrator
    import app.llm.voice_output as voice_output
    from app.schemas.api import ResponsePlan

    async def fake_round3(*args, voice_intent="neutral", delivery_candidate="text", **kwargs):
        if delivery_candidate == "text":
            await asyncio.sleep(0.02)
            yield "T"
        else:
            await asyncio.sleep(0.05)
            yield "V"

    calls = []

    async def fake_decide(*args, **kwargs):
        calls.append(kwargs)
        await asyncio.sleep(0.1)
        return "voice"

    monkeypatch.setattr(orchestrator, "_round3", fake_round3)
    monkeypatch.setattr(voice_output, "decide_voice_with_context_lite", fake_decide)

    plan = ResponsePlan(
        intent="explicit voice reply",
        key_points=["short natural reply"],
        tone="natural",
        length_hint="短",
    )
    start = time.perf_counter()
    gen = orchestrator._round3_parallel("", plan, "u", "用语音回复我：你好", [], voice_preference=0.5)
    try:
        first, _ = await _drain_after_first(gen)
    finally:
        await gen.aclose()

    assert first == "T"
    assert _round3_parallel_decision.get() == "voice"
    assert calls
    assert calls[0]["user_message"] == "用语音回复我：你好"
    assert time.perf_counter() - start < 0.5


@pytest.mark.asyncio
async def test_round3_parallel_medium_preference_records_classifier_voice(monkeypatch):
    from app.core import orchestrator
    from app.schemas.api import ResponsePlan
    import app.llm.voice_output as voice_output

    async def fake_round3(*args, voice_intent="neutral", delivery_candidate="text", **kwargs):
        if delivery_candidate == "text":
            await asyncio.sleep(0.02)
            yield "T"
        else:
            await asyncio.sleep(0.05)
            yield "V"

    async def slow_decide(*args, **kwargs):
        await asyncio.sleep(1.0)
        return "voice"

    monkeypatch.setattr(orchestrator, "_round3", fake_round3)
    monkeypatch.setattr(voice_output, "decide_voice_with_context_lite", slow_decide)

    plan = ResponsePlan(
        intent="simple greeting",
        key_points=["short natural reply"],
        tone="natural",
        length_hint="短",
    )
    gen = orchestrator._round3_parallel("", plan, "u", "你好", [], voice_preference=0.5)
    try:
        first, _ = await _drain_after_first(gen)
    finally:
        await gen.aclose()

    assert first == "T"
    assert _round3_parallel_decision.get() == "voice"


@pytest.mark.asyncio
async def test_round3_parallel_high_preference_unknown_plan_greeting_lag_uses_voice(monkeypatch):
    from app.core import orchestrator
    import app.llm.voice_output as voice_output

    async def fake_round3(*args, voice_intent="neutral", delivery_candidate="text", **kwargs):
        if delivery_candidate == "text":
            await asyncio.sleep(0.02)
            yield "T"
        else:
            await asyncio.sleep(0.05)
            yield "V"

    async def slow_decide(*args, **kwargs):
        await asyncio.sleep(1.0)
        return "voice"

    monkeypatch.setattr(orchestrator, "_round3", fake_round3)
    monkeypatch.setattr(voice_output, "decide_voice_with_context_lite", slow_decide)

    gen = orchestrator._round3_parallel("", None, "u", "你好", [], voice_preference=0.9)
    try:
        first, _ = await _drain_after_first(gen)
    finally:
        await gen.aclose()

    assert first == "T"
    assert _round3_parallel_decision.get() == "voice"


@pytest.mark.asyncio
async def test_round3_parallel_file_delivery_records_classifier_voice(monkeypatch):
    from app.core import orchestrator
    from app.schemas.api import ResponsePlan
    import app.llm.voice_output as voice_output

    async def fake_round3(*args, voice_intent="neutral", delivery_candidate="text", **kwargs):
        if delivery_candidate == "text":
            await asyncio.sleep(0.02)
            yield "T"
        else:
            await asyncio.sleep(0.05)
            yield "V"

    async def slow_decide(*args, **kwargs):
        await asyncio.sleep(1.0)
        return "voice"

    monkeypatch.setattr(orchestrator, "_round3", fake_round3)
    monkeypatch.setattr(voice_output, "decide_voice_with_context_lite", slow_decide)

    plan = ResponsePlan(
        intent="deliver report file",
        key_points=["report.docx is ready"],
        tone="natural",
        length_hint="短",
        deliverables=["report.docx"],
    )
    gen = orchestrator._round3_parallel("", plan, "u", "发报告", [], voice_preference=0.9)
    try:
        first, _ = await _drain_after_first(gen)
    finally:
        await gen.aclose()

    assert first == "T"
    assert _round3_parallel_decision.get() == "voice"


def test_round3_neutral_voice_candidate_does_not_claim_explicit_voice_request():
    from app.core import context
    from app.schemas.api import ResponsePlan

    plan = ResponsePlan(
        intent="simple greeting",
        key_points=["short natural reply"],
        tone="natural",
        length_hint="短",
    )
    output_shape_facts = {
        "length_hint": "short",
        "key_point_count": 3,
        "deliverable_count": 1,
        "partial_delivery_count": 0,
        "content_unit_count": 4,
        "has_user_facing_files": True,
        "likely_readable": True,
        "likely_structured": True,
        "likely_multi_sentence": True,
        "predicted_output_envelope": "structured_or_revisitable_result",
        "delivery_visibility_evidence": "reply is likely to contain structured details",
        "information_boundary": "final reply should preserve the same 4 planned user-facing content unit(s)",
    }

    neutral_voice = context.round3_messages(
        "persona",
        plan,
        "u",
        "你好",
        [],
        voice_intent="neutral",
        delivery_candidate="voice",
        output_shape_facts=output_shape_facts,
    )
    neutral_text = "\n".join(m["content"] for m in neutral_voice)

    assert "Candidate delivery form: possible voice reply" in neutral_text
    assert "did not necessarily request voice" in neutral_text
    assert "voice-ready speech" in neutral_text
    assert "may be synthesized and sent as the final voice reply" in neutral_text
    assert "Do not turn a completed answer into a wait/status placeholder" in neutral_text
    assert "same user-facing information boundary" in neutral_text
    assert "must not omit required facts" in neutral_text
    assert "Shared output-shape facts" in neutral_text
    assert "predicted_output_envelope: structured_or_revisitable_result" in neutral_text
    assert "final reply should preserve the same 4 planned user-facing content unit(s)" in neutral_text
    assert "not permission to drop facts" in neutral_text
    assert "user explicitly requested voice reply" not in neutral_text

    explicit_voice = context.round3_messages(
        "persona",
        plan,
        "u",
        "用语音回复我：你好",
        [],
        voice_intent="demand",
        delivery_candidate="voice",
    )
    explicit_text = "\n".join(m["content"] for m in explicit_voice)

    assert "user explicitly requested voice reply" in explicit_text
    assert "Candidate delivery form: possible voice reply" in explicit_text

    text_candidate = context.round3_messages(
        "persona",
        plan,
        "u",
        "你好",
        [],
        voice_intent="neutral",
        delivery_candidate="text",
    )
    text_candidate_text = "\n".join(m["content"] for m in text_candidate)
    assert "same user-facing information boundary" in text_candidate_text
    assert "do not add or drop facts" in text_candidate_text
    assert "not permission for either candidate to omit facts" in text_candidate_text


@pytest.mark.asyncio
async def test_round3_parallel_keeps_voice_intent_neutral_and_separate_from_candidate(monkeypatch):
    from app.core import orchestrator
    from app.llm import voice_output

    captured = []

    async def fake_round3(*args, voice_intent="neutral", delivery_candidate="text", **kwargs):
        captured.append((voice_intent, delivery_candidate))
        if delivery_candidate == "text":
            await asyncio.sleep(0.02)
            yield "T"
        else:
            await asyncio.sleep(0.05)
            yield "V"

    async def slow_decide(*args, **kwargs):
        await asyncio.sleep(0.05)
        return "text"

    monkeypatch.setattr(orchestrator, "_round3", fake_round3)
    monkeypatch.setattr(voice_output, "decide_voice_with_context_lite", slow_decide)

    gen = orchestrator._round3_parallel("", None, "u", "你好", [], voice_preference=0.2)
    try:
        first, _ = await _drain_after_first(gen)
    finally:
        await gen.aclose()

    assert first == "T"
    assert ("neutral", "text") in captured
    assert ("neutral", "voice") in captured


@pytest.mark.asyncio
async def test_round3_parallel_does_not_locally_promote_explicit_voice_wording_into_intent(monkeypatch):
    from app.core import orchestrator
    from app.llm import voice_output

    captured = []

    async def fake_round3(*args, voice_intent="neutral", delivery_candidate="text", **kwargs):
        captured.append((voice_intent, delivery_candidate))
        yield "T" if delivery_candidate == "text" else "V"

    async def fake_decide(*args, **kwargs):
        return "text"

    monkeypatch.setattr(orchestrator, "_round3", fake_round3)
    monkeypatch.setattr(voice_output, "decide_voice_with_context_lite", fake_decide)

    gen = orchestrator._round3_parallel("", None, "u", "用语音回复我", [], voice_preference=0.4)
    try:
        await _drain_after_first(gen)
    finally:
        await gen.aclose()

    assert ("neutral", "text") in captured
    assert ("neutral", "voice") in captured


@pytest.mark.asyncio
async def test_round3_parallel_starts_classifier_without_waiting_for_candidate_preview(monkeypatch):
    from app.core import orchestrator
    from app.llm import voice_output
    from app.schemas.api import ResponsePlan

    events: list[tuple[str, str, float]] = []
    start = time.perf_counter()

    async def fake_round3(*args, voice_intent="neutral", delivery_candidate="text", **kwargs):
        await asyncio.sleep(0.2)
        events.append(("round3_first_token", delivery_candidate, time.perf_counter() - start))
        yield "V" if delivery_candidate == "voice" else "T"

    async def fake_decide(*args, **kwargs):
        events.append(("classifier_started", "", time.perf_counter() - start))
        previews = kwargs.get("candidate_previews") or {}
        assert previews["text"]["visible_chars"] == 0
        assert previews["voice"]["visible_chars"] == 0
        assert kwargs["voice_preference"] == 0.6
        return "text"

    monkeypatch.setattr(orchestrator, "_round3", fake_round3)
    monkeypatch.setattr(voice_output, "decide_voice_with_context_lite", fake_decide)

    plan = ResponsePlan(
        intent="answer with multiple facts",
        key_points=["first fact", "second fact"],
        tone="natural",
        length_hint="short",
    )
    gen = orchestrator._round3_parallel("", plan, "u", "说明一下", [], voice_preference=0.6)
    try:
        first = await gen.__anext__()
    finally:
        await gen.aclose()

    assert first == "T"
    classifier_time = next(t for name, _side, t in events if name == "classifier_started")
    first_token_time = min(t for name, _side, t in events if name == "round3_first_token")
    assert classifier_time < first_token_time
    snapshot = voice_output._round3_voice_route_snapshot.get()
    assert snapshot["preview_wait_policy"] == "none; classifier starts in parallel with round3 candidates"
    assert snapshot["preview_wait_timed_out"] is False
    assert snapshot["projected_reply_shape"]["predicted_output_envelope"] == "multi_sentence_answer"
    assert "information_boundary" in snapshot["projected_reply_shape"]


@pytest.mark.asyncio
async def test_round3_parallel_starts_candidates_and_classifier_without_order_dependency(monkeypatch):
    from app.core import orchestrator
    from app.llm import voice_output
    from app.schemas.api import ResponsePlan

    events: list[str] = []

    async def fake_round3(*args, delivery_candidate="text", **kwargs):
        events.append(f"{delivery_candidate}_entered")
        await asyncio.sleep(0.01)
        yield "V" if delivery_candidate == "voice" else "T"

    async def fake_decide(*args, **kwargs):
        events.append("classifier_entered")
        await asyncio.sleep(0.02)
        return "text"

    monkeypatch.setattr(orchestrator, "_round3", fake_round3)
    monkeypatch.setattr(voice_output, "decide_voice_with_context_lite", fake_decide)

    plan = ResponsePlan(
        intent="answer with evidence",
        key_points=["one fact", "second fact"],
        tone="natural",
        length_hint="short",
    )
    gen = orchestrator._round3_parallel("", plan, "u", "说明一下", [], voice_preference=0.6)
    try:
        first, _ = await _drain_after_first(gen)
    finally:
        await gen.aclose()

    assert first == "T"
    assert "classifier_entered" in events
    assert "text_entered" in events
    assert "voice_entered" in events
    assert events.index("text_entered") < events.index("classifier_entered") + 3
    assert events.index("voice_entered") < events.index("classifier_entered") + 3


@pytest.mark.asyncio
async def test_round3_parallel_does_not_wait_for_classifier_before_starting_candidates(monkeypatch):
    from app.core import orchestrator
    from app.llm import voice_output
    from app.schemas.api import ResponsePlan

    events: list[tuple[str, float]] = []
    captured_shape = {}
    start = time.perf_counter()

    async def fake_round3(*args, delivery_candidate="text", output_shape_facts=None, **kwargs):
        events.append((f"{delivery_candidate}_entered", time.perf_counter() - start))
        captured_shape[delivery_candidate] = output_shape_facts
        await asyncio.sleep(0.01)
        yield "V" if delivery_candidate == "voice" else "T"

    async def slow_decide(*args, **kwargs):
        events.append(("classifier_entered", time.perf_counter() - start))
        await asyncio.sleep(0.2)
        return "text"

    monkeypatch.setattr(orchestrator, "_round3", fake_round3)
    monkeypatch.setattr(voice_output, "decide_voice_with_context_lite", slow_decide)

    plan = ResponsePlan(
        intent="answer with readable evidence",
        key_points=["first fact", "second fact", "third fact"],
        tone="natural",
        length_hint="short",
        deliverables=["report.txt"],
    )
    gen = orchestrator._round3_parallel("", plan, "u", "说明一下", [], voice_preference=0.6)
    try:
        first, _ = await _drain_after_first(gen)
    finally:
        await gen.aclose()

    assert first == "T"
    times = {name: ts for name, ts in events}
    assert times["text_entered"] < 0.1
    assert times["voice_entered"] < 0.1
    assert times["classifier_entered"] < 0.1
    assert times["text_entered"] < 0.2
    assert times["voice_entered"] < 0.2
    assert captured_shape["text"]["predicted_output_envelope"] == "structured_or_revisitable_result"
    assert captured_shape["voice"]["predicted_output_envelope"] == "structured_or_revisitable_result"


@pytest.mark.asyncio
async def test_round3_parallel_classifier_uses_live_candidate_snapshot(monkeypatch):
    from app.core import orchestrator
    from app.llm import voice_output
    from app.schemas.api import ResponsePlan

    captured = {}

    async def fake_round3(*args, delivery_candidate="text", **kwargs):
        if delivery_candidate == "text":
            yield "这是需要保留阅读的状态："
        else:
            yield "口播候选也先说明状态。"

    async def fake_decide(*args, **kwargs):
        captured.update(kwargs)
        return "text"

    monkeypatch.setattr(orchestrator, "_round3", fake_round3)
    monkeypatch.setattr(voice_output, "decide_voice_with_context_lite", fake_decide)

    plan = ResponsePlan(
        intent="inspect webpage and report status",
        key_points=["page unavailable", "include blocker status"],
        tone="natural",
        length_hint="short",
    )
    gen = orchestrator._round3_parallel("", plan, "u", "查看这个网页", [], voice_preference=0.9)
    try:
        first, _ = await _drain_after_first(gen)
    finally:
        await gen.aclose()

    assert first == "这是需要保留阅读的状态："
    previews = captured["candidate_previews"]
    assert previews["text"]["visible_chars"] > 0
    assert previews["voice"]["visible_chars"] > 0
    assert "需要保留阅读" in previews["text"]["text"]
    assert "口播候选" in previews["voice"]["text"]


def test_round3_shared_output_shape_hint_includes_request_visibility_evidence():
    from app.core.round_prompts import round3_shared_output_shape_hint
    from app.llm.voice_output import _project_reply_shape_facts
    from app.schemas.api import ResponsePlan

    facts = _project_reply_shape_facts(
        ResponsePlan(
            intent="inspect webpage and report status",
            key_points=["page status"],
            tone="natural",
            length_hint="short",
        ),
        user_message="查看这个网页",
    )

    hint = round3_shared_output_shape_hint(facts, delivery_candidate="voice")

    assert "- request_visibility_evidence:" in hint
    assert "查看" in hint
    assert "网页" in hint
    assert "not a local delivery rule" in hint
    assert "do not change the information boundary" in hint


@pytest.mark.asyncio
async def test_round3_parallel_low_preference_neutral_greeting_does_not_fake_voice_intent(monkeypatch):
    from app.core import orchestrator
    from app.llm import voice_output

    captured_round3 = []
    captured_decision = {}
    log_lines = []

    async def fake_round3(*args, voice_intent="neutral", delivery_candidate="text", **kwargs):
        captured_round3.append((voice_intent, delivery_candidate))
        if delivery_candidate == "text":
            await asyncio.sleep(0.02)
            yield "T"
        else:
            await asyncio.sleep(0.05)
            yield "V"

    async def decide_text(*args, **kwargs):
        captured_decision.update(kwargs)
        await asyncio.sleep(0.05)
        return "text"

    def fake_log(tag, message=None, *args, **kwargs):
        log_lines.append((tag, str(message or "")))

    monkeypatch.setattr(orchestrator, "_round3", fake_round3)
    monkeypatch.setattr(voice_output, "decide_voice_with_context_lite", decide_text)
    monkeypatch.setattr(orchestrator.debug, "log", fake_log)

    gen = orchestrator._round3_parallel("", None, "u", "你好", [], voice_preference=0.1)
    try:
        first, _ = await _drain_after_first(gen)
    finally:
        await gen.aclose()

    assert first == "T"
    assert captured_decision["voice_preference"] == 0.1
    assert captured_decision["user_message"] == "你好"
    assert captured_decision["candidate_previews"]["text"] == {
        "text": "",
        "raw_chars": 0,
        "visible_chars": 0,
        "done": False,
        "truncated": False,
    }
    assert captured_decision["candidate_previews"]["voice"] == {
        "text": "",
        "raw_chars": 0,
        "visible_chars": 0,
        "done": False,
        "truncated": False,
    }
    assert ("neutral", "text") in captured_round3
    assert ("neutral", "voice") in captured_round3
    assert not any(intent == "demand" for intent, _ in captured_round3)
    assert any(
        tag == "round3.parallel_deciding"
        and "voice_preference=0.10" in message
        and "user_voice_intent=neutral" in message
        for tag, message in log_lines
    )
    assert any(
        tag == "round3.parallel_decided"
        and "winner=text" in message
        and "voice_preference=0.10" in message
        and "user_voice_intent=neutral" in message
        for tag, message in log_lines
    )


def test_voice_delivery_classifier_prompt_carries_preference_as_llm_fact():
    from app.llm import aux_prompts
    from app.llm.voice_output import _voice_preference_hint

    system_prompt = aux_prompts.VOICE_DELIVERY_CLASSIFIER_SYSTEM
    prompt = aux_prompts.VOICE_DELIVERY_CLASSIFIER_USER_TEMPLATE.format(
        plan_intent="chat",
        plan_length="short",
        plan_tone="warm",
        plan_key_points="- explain previous identity mismatch\n- ask user to choose one mode",
        plan_avoid="(none)",
        plan_deliverables="(none)",
        projected_reply_shape=(
            "length_hint=short; key_points=2; deliverables=0; partial_deliveries=0; "
            "user_facing_files=no; round3 is expected to cover the listed key points"
        ),
        projected_reply_shape_facts=(
            "length_hint=short; key_point_count=2; deliverable_count=0; "
            "partial_delivery_count=0; has_user_facing_files=no; likely_readable=no; "
            "likely_structured=no; likely_multi_sentence=yes; "
            "predicted_output_envelope=multi_sentence_answer; "
            "information_boundary=final reply should preserve the same 2 planned user-facing content unit(s); "
            "request_visibility_evidence=(none); "
            "delivery_visibility_evidence=reply likely has multiple user-facing facts"
        ),
        candidate_output_previews=(
            "- voice candidate preview (partial, chars=24): explain identity mismatch\n"
            "- text candidate preview (partial, chars=24): explain identity mismatch"
        ),
        delivery_state="no",
        persona_context="Taffy persona excerpt",
        recent_context="user: hello",
        user_message="hello",
        voice_preference=0.1,
        preference_hint=_voice_preference_hint(0.1),
    )

    assert "continuous preference 0.10" in prompt
    assert "plan key points likely to appear in final reply" in prompt
    assert "explain previous identity mismatch" in prompt
    assert "projected final reply shape from the same plan round3 will follow" in prompt
    assert "shared output-shape facts for this route decision" in prompt
    assert "likely_multi_sentence=yes" in prompt
    assert "key_points=2" in prompt
    assert "delivery_visibility_evidence" in prompt
    assert "request_visibility_evidence" in prompt
    assert "route-start candidate output previews, if any were already available without waiting" in prompt
    assert "voice candidate preview" in prompt
    assert "predicted_output_envelope" in prompt
    assert "information_boundary" in prompt
    assert "delivery_visibility_evidence" in prompt
    assert "current user-facing file delivery" in prompt
    assert "Taffy persona excerpt" in prompt
    assert "stable character setting" in system_prompt
    assert "continuous strength signal" in system_prompt
    assert "not a hard threshold" in system_prompt
    assert "plan key points are likely to be expressed in the final reply" in system_prompt
    assert "task/result oriented, structured, revisitable, or readability-sensitive" in system_prompt
    assert "task/result oriented, structured, revisitable, or readability-sensitive" in system_prompt
    assert "stronger evidence of the final output shape" in system_prompt
    assert "roughly predict the final delivery result" in system_prompt
    assert "marked final/done" in system_prompt
    assert "For persona voice preference below 0.20, choose text" in system_prompt
    assert "For persona voice preference 0.80 or above, strongly favor voice" in system_prompt
    assert "too long, dense, structured, copyable, or revisitable" in system_prompt
    assert "task/result request whose reply commonly carries evidence, blocker status, or status" in system_prompt
    assert "identity answers" in system_prompt
    assert "Do not turn numeric ranges into local delivery rules" in system_prompt
    assert "A low persona voice preference is real evidence against voice delivery" in system_prompt
    assert "shortness or conversational comfort alone" in system_prompt
    assert "neutral greeting, thanks, or acknowledgement" in system_prompt
    assert "recent user acceptance of voice in the same conversation" in system_prompt
    assert "do not carry old delivery mode" in system_prompt
    assert "Persona context and recent context are evidence" in system_prompt
    assert "Infer explicit voice/text/audio-artifact wording directly from the user message" in system_prompt
    assert "artifact tasks handled outside this final delivery decision" in system_prompt
    assert "delivery authorization" in aux_prompts.VOICE_DELIVERY_CLASSIFIER_SYSTEM
    assert not hasattr(aux_prompts, "VOICE_DELIVERY_FINAL_REVIEW_SYSTEM")


def test_voice_preference_hint_boundaries_match_persona_settings():
    high = _voice_preference_hint(0.9)
    strong = _voice_preference_hint(0.8)
    medium = _voice_preference_hint(0.5)
    very_low = _voice_preference_hint(0.1)

    assert "continuous preference 0.90" in high
    assert "continuous preference 0.80" in strong
    assert "continuous preference 0.50" in medium
    assert "continuous preference 0.10" in very_low
    assert "0.80+ = strong voice willingness" in high
    assert "0.80+ = strong voice willingness" in strong
    assert "strongly favor voice for short conversational replies" in strong
    assert "too long, dense, structured, copyable, or revisitable" in strong
    assert "balanced voice willingness" in medium
    assert "very low voice willingness" in very_low
    assert "not a local automatic threshold" in high
    assert "not a local automatic threshold" in strong
    assert "not a local automatic threshold" in medium
    assert "not a local automatic threshold" in very_low


@pytest.mark.asyncio
async def test_voice_preference_extremes_are_the_only_local_preference_boundaries(monkeypatch):
    from app.llm import client as llm_client
    from app.schemas.api import ResponsePlan

    def fail_json(*args, **kwargs):
        raise AssertionError("preference boundary should not call classifier")

    monkeypatch.setattr(llm_client, "chat_json", fail_json)

    plan = ResponsePlan(
        intent="回应用户问候",
        key_points=["short natural reply"],
        tone="natural",
        length_hint="短",
    )

    assert await decide_voice_with_context_lite(
        plan, "", "你好", voice_preference=0.0,
    ) == "text"
    assert await decide_voice_with_context_lite(
        plan, "", "你好", voice_preference=1.0,
    ) == "voice"


@pytest.mark.asyncio
async def test_voice_delivery_classifier_receives_file_delivery_fact(monkeypatch):
    from app.llm import voice_output

    captured = {}

    async def fake_json(messages, **kwargs):
        captured["messages"] = messages
        return {"delivery": "text"}

    monkeypatch.setattr("app.llm.client.chat_json", fake_json)

    decision = await voice_output.decide_voice_with_context_lite(
        plan=None,
        persona="",
        user_message="发给我文件",
        voice_preference=0.9,
        has_user_facing_files=True,
    )

    assert decision == "text"
    user_prompt = captured["messages"][1]["content"]
    assert "current user-facing file delivery: yes" in user_prompt


@pytest.mark.asyncio
async def test_voice_delivery_classifier_task_context_uses_raw_plan_not_local_label(monkeypatch):
    from app.llm import voice_output
    from app.schemas.api import ResponsePlan

    captured = {}

    async def fake_json(messages, **kwargs):
        captured["messages"] = messages
        return {"delivery": "text"}

    monkeypatch.setattr("app.llm.client.chat_json", fake_json)

    decision = await voice_output.decide_voice_with_context_lite(
        plan=ResponsePlan(
            intent="inspect webpage and report findings",
            key_points=["check readable page content"],
            tone="natural",
            length_hint="short",
        ),
        persona="永雏塔菲，短闲聊更常用语音回复",
        recent_messages=[{"role": "user", "content": "你好"}],
        user_message="查看这个网页",
        voice_preference=0.9,
        has_user_facing_files=False,
    )

    assert decision == "text"
    user_prompt = captured["messages"][1]["content"]
    assert "inspect webpage and report findings" in user_prompt
    assert "plan key points likely to appear in final reply" in user_prompt
    assert "check readable page content" in user_prompt
    assert "projected final reply shape from the same plan round3 will follow" in user_prompt
    assert "key_points=1" in user_prompt
    assert "request_visibility_evidence=" in user_prompt
    assert "predicted_output_envelope=readable_status_or_evidence_summary" in user_prompt
    assert "查看这个网页" in user_prompt
    assert "task_or_readable_content" not in user_prompt
    assert "plain_greeting" not in user_prompt
    assert "永雏塔菲" in user_prompt
    assert "user: 你好" in user_prompt


@pytest.mark.asyncio
async def test_voice_delivery_classifier_receives_actual_candidate_previews(monkeypatch):
    from app.llm import voice_output
    from app.schemas.api import ResponsePlan

    captured = {}

    async def fake_json(messages, **kwargs):
        captured["messages"] = messages
        return {"delivery": "text"}

    monkeypatch.setattr("app.llm.client.chat_json", fake_json)

    decision = await voice_output.decide_voice_with_context_lite(
        plan=ResponsePlan(
            intent="answer identity mismatch",
            key_points=["explain previous role mismatch", "ask which mode to keep"],
            tone="natural",
            length_hint="short",
        ),
        persona="low voice preference persona",
        user_message="你不是猫娘吗",
        recent_messages=[],
        voice_preference=0.1,
        candidate_previews={
            "voice": "喵，是这样：前面一次对话里你让我切换成猫娘，后面我又按默认助手身份回答，所以这里需要先确认你想保留哪种模式。",
            "text": "前面角色切换和默认身份回答不一致，我先确认你接下来要哪种模式。",
        },
    )

    assert decision == "text"
    user_prompt = captured["messages"][1]["content"]
    assert "route-start candidate output previews, if any were already available without waiting" in user_prompt
    assert "voice candidate preview (non-canonical style probe) (partial" in user_prompt
    assert "前面一次对话里你让我切换成猫娘" in user_prompt
    assert "canonical text candidate preview (final reply content shape) (partial" in user_prompt
    assert "predicted_output_envelope=multi_sentence_answer" in user_prompt
    assert "final reply should preserve the same 2 planned user-facing content unit" in user_prompt


@pytest.mark.asyncio
async def test_voice_delivery_classifier_receives_structured_candidate_shape(monkeypatch):
    from app.llm import voice_output
    from app.schemas.api import ResponsePlan

    captured = {}

    async def fake_json(messages, **kwargs):
        captured["messages"] = messages
        return {"delivery": "text"}

    monkeypatch.setattr("app.llm.client.chat_json", fake_json)

    decision = await voice_output.decide_voice_with_context_lite(
        plan=ResponsePlan(
            intent="inspect webpage and summarize result",
            key_points=["page unavailable", "explain blocker briefly"],
            tone="natural",
            length_hint="short",
        ),
        persona="high voice preference persona",
        user_message="查看这个网页",
        recent_messages=[],
        voice_preference=0.9,
        candidate_previews={
            "voice": {
                "text": "这个网页现在打不开，我只能说明目前无法查看到页面内容。",
                "raw_chars": 27,
                "visible_chars": 27,
                "done": True,
                "truncated": False,
            },
            "text": {
                "text": "我这边没有拿到网页内容，所以不能声称已经查看。需要可访问页面后才能继续。",
                "raw_chars": 36,
                "visible_chars": 36,
                "done": True,
                "truncated": False,
            },
        },
    )

    assert decision == "text"
    user_prompt = captured["messages"][1]["content"]
    assert "voice candidate preview (non-canonical style probe) (visible_chars=27, raw_chars=27, status=final)" in user_prompt
    assert "canonical text candidate preview (final reply content shape) (visible_chars=36, raw_chars=36, status=final)" in user_prompt
    assert "When the canonical text preview is marked final/done" in captured["messages"][0]["content"]
    assert "non-canonical voice preview only for style/listening-comfort evidence" in captured["messages"][0]["content"]
    assert "roughly predict the final delivery result" in captured["messages"][0]["content"]
    assert "predicted_output_envelope=readable_status_or_evidence_summary" in user_prompt
    assert "request_visibility_evidence=" in user_prompt
    assert "delivery_visibility_evidence=" in user_prompt


def test_round2_voice_handoff_has_no_legacy_persona_guard_path():
    import app.core.guard_prompts as guard_prompts
    import app.core.orchestrator as orchestrator

    assert not hasattr(guard_prompts, "PERSONA_VOICE_GUARD_SYSTEM")
    assert not hasattr(guard_prompts, "PERSONA_VOICE_GUARD_USER_TEMPLATE")
    assert not hasattr(orchestrator, "_persona_voice_reply_guard")
    src = Path("app/core/orchestrator.py").read_text(encoding="utf-8")
    assert "json.persona_voice_guard" not in src


@pytest.mark.asyncio
async def test_parallel_voice_route_authorizes_without_final_review(monkeypatch):
    async def forbidden_chat_json(*_args, **_kwargs):
        raise AssertionError("decide_voice must not run a post-route voice review")

    monkeypatch.setattr("app.llm.client.chat_json", forbidden_chat_json)

    token = _round3_parallel_decision.set("voice")
    try:
        decision = await decide_voice(
            reply_text="\u4f60\u597d\u5440",
            user_message="\u4f60\u597d",
            voice_preference=0.2,
        )
    finally:
        _round3_parallel_decision.reset(token)

    assert decision.use_voice is True
    assert decision.voice_text == "\u4f60\u597d\u5440"
    assert decision.reason == "parallel route decision authorized voice"


@pytest.mark.asyncio
async def test_parallel_voice_route_does_not_reclassify_structured_final_text(monkeypatch):
    async def forbidden_chat_json(*_args, **_kwargs):
        raise AssertionError("final text must not trigger a second delivery LLM review")

    monkeypatch.setattr("app.llm.client.chat_json", forbidden_chat_json)

    token = _round3_parallel_decision.set("voice")
    try:
        decision = await decide_voice(
            reply_text="结果如下：\n- https://example.com\n- `status=ok`\n- 已完成",
            user_message="check this page",
            voice_preference=0.9,
        )
    finally:
        _round3_parallel_decision.reset(token)

    assert decision.use_voice is True
    assert "https://example.com" not in decision.voice_text
    assert "status=ok" in decision.voice_text


def test_voice_output_has_no_post_route_delivery_review_prompt():
    import app.llm.aux_prompts as aux_prompts
    import app.llm.voice_output as voice_output

    src = Path("app/llm/voice_output.py").read_text(encoding="utf-8", errors="ignore")
    assert not hasattr(aux_prompts, "VOICE_DELIVERY_FINAL_REVIEW_SYSTEM")
    assert not hasattr(aux_prompts, "VOICE_DELIVERY_FINAL_REVIEW_USER_TEMPLATE")
    assert not hasattr(voice_output, "_review_voice_delivery_with_final_reply")
    assert "json.voice_delivery_final_review" not in src
    assert "final reply voice review" not in src

@pytest.mark.asyncio
async def test_parallel_text_classifier_is_authoritative_for_neutral_turns():
    token = _round3_parallel_decision.set("text")
    try:
        decision = await decide_voice(
            reply_text="\u4f60\u597d\u5440",
            user_message="\u4f60\u597d",
            voice_preference=0.9,
        )
    finally:
        _round3_parallel_decision.reset(token)

    assert decision.use_voice is False
    assert "parallel pre-decision: text" in decision.reason


@pytest.mark.asyncio
async def test_decide_voice_neutral_turn_needs_llm_decision():
    medium = await decide_voice(
        reply_text="\u4f60\u597d\u5440",
        user_message="\u4f60\u597d",
        voice_preference=0.5,
    )
    upper_medium = await decide_voice(
        reply_text="\u4f60\u597d\u5440",
        user_message="\u4f60\u597d",
        voice_preference=0.7,
    )
    high = await decide_voice(
        reply_text="\u4f60\u597d\u5440",
        user_message="\u4f60\u597d",
        voice_preference=0.9,
    )

    assert medium.use_voice is False
    assert upper_medium.use_voice is False
    assert high.use_voice is False
    assert high.llm_decision_available is False
    assert "no LLM voice decision" in high.reason


@pytest.mark.asyncio
async def test_voice_classifier_failure_returns_unavailable_without_local_decision(monkeypatch):
    from app.llm import voice_output
    from app.schemas.api import ResponsePlan

    async def failing_json(*_args, **_kwargs):
        raise RuntimeError("json classifier unavailable")

    monkeypatch.setattr("app.llm.client.chat_json", failing_json)

    plan = ResponsePlan(
        intent="simple greeting",
        key_points=["short natural reply"],
        tone="natural",
        length_hint="短",
    )

    high = await voice_output.decide_voice_with_context_lite(
        plan=plan,
        persona="",
        user_message="你好",
        voice_preference=0.9,
    )
    upper_medium = await voice_output.decide_voice_with_context_lite(
        plan=plan,
        persona="",
        user_message="你好",
        voice_preference=0.7,
    )
    file_delivery = await voice_output.decide_voice_with_context_lite(
        plan=ResponsePlan(
            intent="deliver report file",
            key_points=["report.docx is ready"],
            tone="natural",
            length_hint="短",
            deliverables=["report.docx"],
        ),
        persona="",
        user_message="发报告",
        voice_preference=0.9,
        has_user_facing_files=True,
    )
    unknown_plan_greeting = await voice_output.decide_voice_with_context_lite(
        plan=None,
        persona="",
        user_message="你好",
        voice_preference=0.9,
    )
    unknown_plan_task = await voice_output.decide_voice_with_context_lite(
        plan=None,
        persona="",
        user_message="please inspect this webpage",
        voice_preference=0.9,
    )
    non_conversational = await voice_output.decide_voice_with_context_lite(
        plan=ResponsePlan(
            intent="answer the current task",
            key_points=["plain answer"],
            tone="natural",
            length_hint="",
        ),
        persona="",
        user_message="处理一下",
        voice_preference=0.9,
    )

    assert high == "unavailable"
    assert upper_medium == "unavailable"
    assert file_delivery == "unavailable"
    assert unknown_plan_greeting == "unavailable"
    assert unknown_plan_task == "unavailable"
    assert non_conversational == "unavailable"


@pytest.mark.asyncio
async def test_voice_classifier_uses_json_llm_decision(monkeypatch):
    from app.llm import voice_output
    from app.schemas.api import ResponsePlan

    captured = {}

    async def json_decision(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return {"delivery": "voice"}

    monkeypatch.setattr("app.llm.client.chat_json", json_decision)

    plan = ResponsePlan(
        intent="simple greeting",
        key_points=["short natural reply"],
        tone="natural",
        length_hint="short",
    )

    decision = await voice_output.decide_voice_with_context_lite(
        plan=plan,
        persona="high voice persona",
        user_message="hello",
        voice_preference=0.1,
    )

    assert decision == "voice"
    assert captured["kwargs"]["metrics_tag"] == "json.voice_delivery_classifier"
    assert '{"delivery":"voice"}' in captured["messages"][0]["content"]


@pytest.mark.asyncio
async def test_voice_classifier_json_decision_text(monkeypatch):
    from app.llm import voice_output
    from app.schemas.api import ResponsePlan

    async def json_decision(*_args, **_kwargs):
        return {"delivery": "text"}

    monkeypatch.setattr("app.llm.client.chat_json", json_decision)

    decision = await voice_output.decide_voice_with_context_lite(
        plan=ResponsePlan(intent="simple greeting", key_points=[], tone="natural", length_hint="short"),
        persona="",
        user_message="hello",
        voice_preference=0.9,
    )

    assert decision == "text"


@pytest.mark.asyncio
async def test_voice_classifier_single_json_request_is_not_stream_race(monkeypatch):
    from app.llm import client as llm_client
    import app.llm.voice_output as voice_output
    from app.schemas.api import ResponsePlan

    async def quick_json(*_args, **_kwargs):
        await asyncio.sleep(0.02)
        return {"delivery": "text"}

    monkeypatch.setattr(llm_client, "chat_json", quick_json)
    monkeypatch.setattr(voice_output, "_VOICE_CLASSIFIER_TIMEOUT_SEC", 1.0)

    started = time.monotonic()
    decision = await voice_output.decide_voice_with_context_lite(
        plan=ResponsePlan(intent="simple greeting", key_points=[], tone="natural", length_hint="short"),
        persona="",
        user_message="hello",
        voice_preference=0.9,
    )
    elapsed = time.monotonic() - started

    assert decision == "text"
    assert elapsed < 0.5


def test_voice_delivery_path_has_no_local_fallback_decision_rules():
    import re

    sources = {
        "app/llm/voice_output.py": Path("app/llm/voice_output.py").read_text(encoding="utf-8", errors="ignore"),
        "app/core/orchestrator.py": Path("app/core/orchestrator.py").read_text(encoding="utf-8", errors="ignore"),
    }
    forbidden_terms = (
        "local_voice_delivery_fallback",
        "round3.parallel_decision_fallback",
        "local fallback=voice",
        "local fallback=text",
        "fallback=voice",
        "fallback=text",
        "structural element",
        "voice unsuitable",
        "URL/email",
        "bullet items",
    )

    for path, src in sources.items():
        for term in forbidden_terms:
            assert term not in src, f"{term!r} reintroduced in {path}"

        for lineno, line in enumerate(src.splitlines(), 1):
            if "voice_preference" not in line:
                continue
            if line.lstrip().startswith("#"):
                continue
            for match in re.finditer(r"voice_preference\s*(?:<=|<|>=|>)\s*([0-9]+(?:\.[0-9]+)?)", line):
                value = match.group(1)
                assert value in {"0.0", "1.0"}, (
                    f"local voice_preference threshold {value} in {path}:{lineno}: {line.strip()}"
                )


def test_tts_execution_has_no_persona_guard_llm_review():
    registry_src = Path("app/llm/tools/registry.py").read_text(encoding="utf-8", errors="ignore")
    orchestrator_src = Path("app/core/orchestrator_entry.py").read_text(encoding="utf-8", errors="ignore")
    aux_src = Path("app/llm/aux_prompts.py").read_text(encoding="utf-8", errors="ignore")

    forbidden = (
        "tts_persona_guard",
        "set_current_tts_guard_context",
        "reset_current_tts_guard_context",
        "set_current_tts_delivery_context",
        "reset_current_tts_delivery_context",
        "json.tts_persona_guard",
        "TTS_PERSONA_GUARD_SYSTEM",
        "TTS_PERSONA_GUARD_USER_TEMPLATE",
        "persona_guard_refused_tts",
    )
    for term in forbidden:
        assert term not in registry_src
        assert term not in orchestrator_src
        assert term not in aux_src

@pytest.mark.asyncio
async def test_round3_parallel_classifier_unavailable_remains_visible(monkeypatch):
    from app.core import orchestrator
    import app.llm.voice_output as voice_output

    async def fake_round3(*args, voice_intent="neutral", delivery_candidate="text", **kwargs):
        if delivery_candidate == "text":
            await asyncio.sleep(0.02)
            yield "T"
        else:
            await asyncio.sleep(0.05)
            yield "V"

    async def unavailable_decide(*args, **kwargs):
        await asyncio.sleep(0.05)
        return "unavailable"

    monkeypatch.setattr(orchestrator, "_round3", fake_round3)
    monkeypatch.setattr(voice_output, "decide_voice_with_context_lite", unavailable_decide)

    token = _round3_parallel_decision.set("")
    try:
        gen = orchestrator._round3_parallel("", None, "u", "hello", [], voice_preference=0.7)
        try:
            first = await gen.__anext__()
        finally:
            await gen.aclose()

        assert first == "T"
        assert _round3_parallel_decision.get() == "unavailable"

        decision = await decide_voice(
            reply_text="你好呀",
            user_message="hello",
            voice_preference=0.7,
        )
    finally:
        _round3_parallel_decision.reset(token)

    assert decision.use_voice is False
    assert decision.llm_decision_available is False
    assert "voice delivery not authorized" in decision.reason


@pytest.mark.asyncio
async def test_voice_route_classifier_receives_output_shape_facts(monkeypatch):
    from app.schemas.api import ResponsePlan

    captured = {}

    async def fake_json(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return {"delivery": "text"}

    monkeypatch.setattr("app.llm.client.chat_json", fake_json)

    plan = ResponsePlan(
        intent="inspect webpage and report status",
        key_points=[
            "page unavailable",
            "include blocker status",
            "preserve the relevant link/status detail",
        ],
        tone="natural",
        length_hint="short",
        deliverables=["status.txt"],
    )
    decision = await decide_voice_with_context_lite(
        plan,
        persona="",
        user_message="查看这个网页并整理结果",
        recent_messages=[],
        voice_preference=0.9,
        candidate_previews={
            "text": {"text": "结果如下：\n- 页面不可访问\n- 需要保留状态和链接", "done": True, "raw_chars": 30, "visible_chars": 30},
            "voice": {"text": "我查到几个点，稍等我整理。", "done": False, "raw_chars": 13, "visible_chars": 13},
        },
    )

    assert decision == "text"
    user_prompt = captured["messages"][1]["content"]
    assert "shared output-shape facts for this route decision" in user_prompt
    assert "key_point_count=3" in user_prompt
    assert "deliverable_count=1" in user_prompt
    assert "likely_readable=yes" in user_prompt
    assert "request_visibility_evidence=" in user_prompt
    assert "delivery_visibility_evidence=" in user_prompt
    assert "voice candidate preview" in user_prompt
    assert "canonical text candidate preview" in user_prompt
    assert captured["kwargs"]["metrics_tag"] == "json.voice_delivery_classifier"


@pytest.mark.asyncio
async def test_short_webpage_blocker_is_text_at_route_classifier(monkeypatch):
    from app.schemas.api import ResponsePlan

    captured = {}

    async def fake_json(messages, **kwargs):
        captured["messages"] = messages
        captured["kwargs"] = kwargs
        return {"delivery": "text"}

    monkeypatch.setattr("app.llm.client.chat_json", fake_json)

    plan = ResponsePlan(
        intent="inspect webpage and summarize result",
        key_points=["page unavailable", "explain blocker briefly"],
        tone="natural",
        length_hint="short",
    )
    decision = await decide_voice_with_context_lite(
        plan,
        persona="",
        user_message="查看这个网页",
        recent_messages=[],
        voice_preference=0.9,
        candidate_previews={
            "text": {
                "text": "我这边没有拿到网页内容，所以不能声称已经查看。需要可访问页面后才能继续。",
                "raw_chars": 36,
                "visible_chars": 36,
                "done": True,
                "truncated": False,
            },
            "voice": {
                "text": "网页现在打不开，我只能说明目前无法查看。",
                "raw_chars": 20,
                "visible_chars": 20,
                "done": True,
                "truncated": False,
            },
        },
    )

    assert decision == "text"
    user_prompt = captured["messages"][1]["content"]
    system_prompt = captured["messages"][0]["content"]
    assert "task outcome, blocker, inspected-material status" in system_prompt
    assert "When the active request asks to inspect, browse, open, read, check" in system_prompt
    assert "canonical text candidate preview (final reply content shape)" in user_prompt
    assert "non-canonical style probe" in user_prompt

@pytest.mark.asyncio
async def test_real_taffy_persona_high_voice_preference_uses_voice_for_short_chat(monkeypatch):
    from app.memory import persona_files

    async def fake_chat_json(*_args, **_kwargs):
        return {"delivery": "voice", "reason": "高语音倾向短回复"}

    monkeypatch.setattr("app.llm.client.chat_json", fake_chat_json)

    persona = persona_files.load_persona("永雏塔菲")
    preference = persona_files.persona_voice_preference_by_content(persona.content, -1)

    token = _round3_parallel_decision.set("voice")
    try:
        decision = await decide_voice(
            reply_text="哼哼，塔菲当然记住啦。",
            user_message="你好",
            persona=persona.content,
            voice_preference=preference,
        )
    finally:
        _round3_parallel_decision.reset(token)

    assert preference >= 0.8
    assert decision.use_voice is True


@pytest.mark.asyncio
async def test_real_catgirl_persona_medium_preference_stays_text_without_classifier_voice():
    from app.memory import persona_files

    persona = persona_files.load_persona("猫娘")
    preference = persona_files.persona_voice_preference_by_content(persona.content, -1)

    decision = await decide_voice(
        reply_text="喵，知道啦。",
        user_message="你好",
        persona=persona.content,
        voice_preference=preference,
    )

    assert 0.4 <= preference <= 0.7
    assert decision.use_voice is False


@pytest.mark.asyncio
async def test_medium_preference_short_turn_uses_classifier(monkeypatch):
    from app.llm import client as llm_client
    from app.schemas.api import ResponsePlan

    async def fake_json(*args, **kwargs):
        return {"delivery": "voice"}

    monkeypatch.setattr(llm_client, "chat_json", fake_json)
    decision = await decide_voice_with_context_lite(
        ResponsePlan(intent="short greeting", key_points=[], tone="natural", length_hint="short"),
        persona="medium voice preference persona",
        user_message="hi",
        recent_messages=[],
        voice_preference=0.5,
        has_user_facing_files=False,
    )

    assert decision == "voice"


@pytest.mark.asyncio
async def test_high_preference_short_turn_uses_classifier(monkeypatch):
    from app.llm import client as llm_client
    from app.schemas.api import ResponsePlan

    async def fake_json(*args, **kwargs):
        return {"delivery": "text"}

    monkeypatch.setattr(llm_client, "chat_json", fake_json)
    decision = await decide_voice_with_context_lite(
        ResponsePlan(intent="short greeting", key_points=[], tone="natural", length_hint="short"),
        persona="high voice preference persona",
        user_message="hi",
        recent_messages=[],
        voice_preference=0.9,
        has_user_facing_files=False,
    )

    assert decision == "text"


@pytest.mark.asyncio
async def test_task_context_uses_classifier_instead_of_local_shortcut(monkeypatch):
    from app.llm import client as llm_client
    from app.schemas.api import ResponsePlan

    captured = {}

    async def fake_json(messages, **kwargs):
        captured["messages"] = messages
        return {"delivery": "voice"}

    monkeypatch.setattr(llm_client, "chat_json", fake_json)
    decision = await decide_voice_with_context_lite(
        ResponsePlan(
            intent="inspect webpage and report findings",
            key_points=["网页内容已查看，稍后给出结果"],
            tone="natural",
            length_hint="short",
        ),
        persona="high voice preference persona",
        user_message="查看这个网页",
        recent_messages=[],
        voice_preference=0.9,
        has_user_facing_files=False,
    )

    assert decision == "voice"
    user_prompt = captured["messages"][1]["content"]
    assert "inspect webpage and report findings" in user_prompt
    assert "网页内容已查看，稍后给出结果" in user_prompt
    assert "round3 is expected to cover the listed key points" in user_prompt
    assert "查看这个网页" in user_prompt
    assert "task_or_readable_content" not in user_prompt
    assert "plain_greeting" not in user_prompt


@pytest.mark.asyncio
async def test_voice_classifier_timeout_returns_unavailable(monkeypatch):
    from app.llm import client as llm_client
    import app.llm.voice_output as voice_output
    from app.schemas.api import ResponsePlan

    async def hanging_json(*args, **kwargs):
        await asyncio.sleep(10)
        return {"delivery": "voice"}

    monkeypatch.setattr(llm_client, "chat_json", hanging_json)
    monkeypatch.setattr(voice_output, "_VOICE_CLASSIFIER_TIMEOUT_SEC", 0.01)

    decision = await decide_voice_with_context_lite(
        ResponsePlan(
            intent="inspect webpage and report findings",
            key_points=["check readable page content"],
            tone="natural",
            length_hint="medium",
        ),
        persona="high voice preference persona",
        user_message="please inspect this webpage",
        recent_messages=[],
        voice_preference=0.9,
        has_user_facing_files=False,
    )

    assert decision == "unavailable"


@pytest.mark.asyncio
async def test_voice_classifier_uses_single_json_decision(monkeypatch):
    from app.llm import client as llm_client
    import app.llm.voice_output as voice_output
    from app.schemas.api import ResponsePlan

    calls = 0

    async def fake_json(*args, **kwargs):
        nonlocal calls
        calls += 1
        return {"delivery": "voice"}

    monkeypatch.setattr(llm_client, "chat_json", fake_json)
    monkeypatch.setattr(voice_output, "_VOICE_CLASSIFIER_TIMEOUT_SEC", 0.2)

    started = time.monotonic()
    decision = await decide_voice_with_context_lite(
        ResponsePlan(
            intent="short greeting",
            key_points=["short natural reply"],
            tone="natural",
            length_hint="short",
        ),
        persona="high voice preference persona",
        user_message="hello",
        recent_messages=[],
        voice_preference=0.9,
        has_user_facing_files=False,
    )
    elapsed = time.monotonic() - started

    assert decision == "voice"
    assert calls == 1
    assert elapsed < 0.2


@pytest.mark.asyncio
async def test_real_catgirl_medium_preference_file_delivery_honors_classifier_voice(monkeypatch):
    from app.core import orchestrator
    from app.llm import voice_output
    from app.memory import persona_files
    from app.schemas.api import ResponsePlan

    persona = persona_files.load_persona("猫娘")
    preference = persona_files.persona_voice_preference_by_content(persona.content, -1)

    async def fake_round3(*args, voice_intent="neutral", delivery_candidate="text", **kwargs):
        if delivery_candidate == "text":
            await asyncio.sleep(0.02)
            yield "T"
        else:
            await asyncio.sleep(0.05)
            yield "V"

    async def slow_decide(*args, **kwargs):
        await asyncio.sleep(1.0)
        return "voice"

    monkeypatch.setattr(orchestrator, "_round3", fake_round3)
    monkeypatch.setattr(voice_output, "decide_voice_with_context_lite", slow_decide)

    plan = ResponsePlan(
        intent="deliver report file",
        key_points=["report.docx is ready"],
        tone="natural",
        length_hint="短",
        deliverables=["report.docx"],
    )
    gen = orchestrator._round3_parallel(
        persona.content,
        plan,
        "u",
        "发报告",
        [],
        voice_preference=preference,
    )
    try:
        first, _ = await _drain_after_first(gen)
    finally:
        await gen.aclose()

    assert 0.4 <= preference <= 0.7
    assert first == "T"
    assert _round3_parallel_decision.get() == "voice"


@pytest.mark.asyncio
async def test_real_taffy_high_preference_task_status_honors_classifier_text(monkeypatch):
    from app.core import orchestrator
    from app.llm import voice_output
    from app.memory import persona_files
    from app.schemas.api import ResponsePlan

    persona = persona_files.load_persona("永雏塔菲")
    preference = persona_files.persona_voice_preference_by_content(persona.content, -1)

    async def fake_round3(*args, voice_intent="neutral", delivery_candidate="text", **kwargs):
        if delivery_candidate == "text":
            await asyncio.sleep(0.02)
            yield "T"
        else:
            await asyncio.sleep(0.05)
            yield "V"

    monkeypatch.setattr(orchestrator, "_round3", fake_round3)

    async def slow_decide(*args, **kwargs):
        await asyncio.sleep(1.0)
        return "text"

    monkeypatch.setattr(voice_output, "decide_voice_with_context_lite", slow_decide)

    plan = ResponsePlan(
        intent="任务状态简述",
        key_points=["网页内容已查看，稍后给出结果"],
        tone="natural",
        length_hint="短",
    )
    gen = orchestrator._round3_parallel(
        persona.content,
        plan,
        "u",
        "查看这个网页",
        [],
        voice_preference=preference,
    )
    try:
        first = await gen.__anext__()
    finally:
        await gen.aclose()

    assert preference >= 0.8
    assert first == "T"
