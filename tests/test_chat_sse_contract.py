import json
import sys
import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.schemas.api import ChatRequest


async def _collect_sse(resp):
    events = []
    async for item in resp.body_iterator:
        if not isinstance(item, dict):
            continue
        events.append((item.get("event"), json.loads(item.get("data", "{}"))))
    return events


def test_chat_and_environment_stream_routes_are_registered():
    from app.main import app

    routes = {
        (getattr(route, "path", ""), tuple(sorted(getattr(route, "methods", []) or [])))
        for route in app.routes
    }

    assert ("/v1/chat/stream", ("POST",)) in routes
    assert ("/v1/environment/stream", ("POST",)) in routes


async def test_chat_stream_events_keep_done_before_complete(monkeypatch):
    from app.api import chat

    async def fake_get_archive(archive_id):
        return {"archive_id": archive_id}

    async def fake_active(group_id):
        return "archive"

    async def fake_orchestrate(req, trace_id, **kwargs):
        yield "meta", {"trace_id": trace_id}
        yield "progress", {"round": "loading"}
        yield "token", {"text": "hi"}
        yield "done", {"trace_id": trace_id, "text": "hi"}
        yield "complete", {"trace_id": trace_id}

    class Guard:
        async def acquire(self, *args, **kwargs):
            return None

        async def release(self, *args, **kwargs):
            return True

    monkeypatch.setattr(chat.archive_dao, "get_archive", fake_get_archive)
    monkeypatch.setattr(chat.bot_config, "get_active_archive", fake_active)
    monkeypatch.setattr(chat, "orchestrate", fake_orchestrate)
    monkeypatch.setattr(chat, "get_group_guard", lambda: Guard())

    req = ChatRequest(archive_id="archive", group_id="group", user_id="user", message="hello")
    resp = await chat.chat_stream(req)
    events = await _collect_sse(resp)
    names = [name for name, _ in events]

    assert names == ["meta", "progress", "token", "done", "complete"]
    for name, payload in events:
        if name == "progress":
            assert payload["kind"]
            assert payload["message"]


async def test_chat_stream_declares_utf8_sse(monkeypatch):
    from app.api import chat

    async def fake_get_archive(archive_id):
        return {"archive_id": archive_id}

    async def fake_active(group_id):
        return "archive"

    async def fake_orchestrate(req, trace_id, **kwargs):
        yield "complete", {"trace_id": trace_id}

    class Guard:
        async def acquire(self, *args, **kwargs):
            return None

        async def release(self, *args, **kwargs):
            return True

    monkeypatch.setattr(chat.archive_dao, "get_archive", fake_get_archive)
    monkeypatch.setattr(chat.bot_config, "get_active_archive", fake_active)
    monkeypatch.setattr(chat, "orchestrate", fake_orchestrate)
    monkeypatch.setattr(chat, "get_group_guard", lambda: Guard())

    req = ChatRequest(archive_id="archive", group_id="group", user_id="user", message="中文流")
    resp = await chat.chat_stream(req)

    assert "text/event-stream" in resp.headers["content-type"]
    assert "charset=utf-8" in resp.headers["content-type"].lower()


@pytest.mark.asyncio
async def test_auto_continue_check_calls_model_and_normalizes(monkeypatch):
    from app.api import chat
    from app.llm import model_pool
    from app.schemas.api import AutoContinueCheckRequest

    seen = {}

    async def fake_chat_json(messages, *, model_spec=None, **kwargs):
        seen["messages"] = messages
        seen["model_spec"] = model_spec
        return {
            "should_continue": True,
            "confidence": 0.91,
            "reason": "reply says the next section will continue",
            "continue_message": "继续",
        }

    monkeypatch.setattr(model_pool, "chat_json", fake_chat_json)

    req = AutoContinueCheckRequest(
        user_message="写一个较长的实现计划并继续推进",
        assistant_reply="第一部分完成。下一步我会继续补实现细节。",
        recent_context="用户要求完整计划。",
        auto_continue_elapsed_sec=12,
        max_auto_continue_sec=600,
    )
    result = await chat.auto_continue_check(req)

    assert result.should_continue is True
    assert result.confidence == pytest.approx(0.91)
    assert result.continue_message.startswith("继续完成同一任务：")
    assert seen["model_spec"].model
    assert "assistant_reply" in seen["messages"][1]["content"]
    assert seen["messages"][1]["content"].startswith('{"assistant_reply":')
    assert "\n" not in seen["messages"][1]["content"]
    assert ',"user_message":' in seen["messages"][1]["content"]


def test_auto_continue_user_payload_is_stable_compact_json():
    from app.api.chat import _auto_continue_user_payload
    from app.schemas.api import AutoContinueCheckRequest

    req = AutoContinueCheckRequest(
        user_message="用户需求",
        assistant_reply="阶段一完成，下一步继续。",
        recent_context="上下文",
        auto_continue_elapsed_sec=12.34567,
        max_auto_continue_sec=600,
    )

    payload = _auto_continue_user_payload(req)

    assert payload == (
        '{"assistant_reply":"阶段一完成，下一步继续。",'
        '"auto_continue_elapsed_sec":12.346,'
        '"max_auto_continue_sec":600.0,'
        '"recent_context":"上下文",'
        '"user_message":"用户需求"}'
    )


def test_auto_continue_prompt_requests_self_contained_continue_message():
    from app.core.guard_prompts import AUTO_CONTINUE_JUDGE_SYSTEM

    assert "self-contained" in AUTO_CONTINUE_JUDGE_SYSTEM
    assert "same task anchored" in AUTO_CONTINUE_JUDGE_SYSTEM
    assert "Use plain" in AUTO_CONTINUE_JUDGE_SYSTEM
    assert "Do not add new requirements" in AUTO_CONTINUE_JUDGE_SYSTEM


@pytest.mark.asyncio
async def test_auto_continue_check_time_limit_skips_model(monkeypatch):
    from app.api import chat
    from app.llm import model_pool
    from app.schemas.api import AutoContinueCheckRequest

    async def fail_chat_json(*args, **kwargs):
        raise AssertionError("model should not be called when time limit is reached")

    monkeypatch.setattr(model_pool, "chat_json", fail_chat_json)

    req = AutoContinueCheckRequest(
        user_message="继续写",
        assistant_reply="还没写完。",
        auto_continue_elapsed_sec=600,
        max_auto_continue_sec=600,
    )
    result = await chat.auto_continue_check(req)

    assert result.should_continue is False
    assert result.reason == "auto_continue_time_limit_reached"


@pytest.mark.asyncio
async def test_auto_continue_plain_continue_is_anchored(monkeypatch):
    from app.api import chat
    from app.llm import model_pool
    from app.schemas.api import AutoContinueCheckRequest

    async def fake_chat_json(messages, *, model_spec=None, **kwargs):
        return {
            "should_continue": True,
            "confidence": 0.9,
            "reason": "same task remains incomplete",
            "continue_message": "继续",
        }

    monkeypatch.setattr(model_pool, "chat_json", fake_chat_json)

    result = await chat.auto_continue_check(AutoContinueCheckRequest(
        user_message="修复 app/core/orchestrator.py 中的中断问题并运行测试",
        assistant_reply="已定位问题，下一步继续修改。",
        auto_continue_elapsed_sec=5,
        max_auto_continue_sec=600,
    ))

    assert result.should_continue is True
    assert result.continue_message == "继续完成同一任务：修复 app/core/orchestrator.py 中的中断问题并运行测试"


@pytest.mark.asyncio
async def test_auto_continue_check_respects_uncertain_model_despite_unfinished_words(monkeypatch):
    from app.api import chat
    from app.llm import model_pool
    from app.schemas.api import AutoContinueCheckRequest

    async def fake_chat_json(messages, *, model_spec=None, **kwargs):
        return {
            "should_continue": False,
            "confidence": 0.1,
            "reason": "",
            "continue_message": "继续",
        }

    monkeypatch.setattr(model_pool, "chat_json", fake_chat_json)

    req = AutoContinueCheckRequest(
        user_message="统计 app 里最大的 12 个 Python 文件，必须给出真实排行。",
        assistant_reply="目前这一步还没实际执行。如果你要我继续，下一步就是真正跑一次全量遍历。",
        auto_continue_elapsed_sec=20,
        max_auto_continue_sec=600,
    )
    result = await chat.auto_continue_check(req)

    assert result.should_continue is False
    assert result.confidence == pytest.approx(0.1)
    assert result.reason == ""


@pytest.mark.asyncio
async def test_auto_continue_check_respects_model_for_preparation_reply(monkeypatch):
    from app.api import chat
    from app.llm import model_pool
    from app.schemas.api import AutoContinueCheckRequest

    async def fake_chat_json(messages, *, model_spec=None, **kwargs):
        return {
            "should_continue": False,
            "confidence": 0.6,
            "reason": "not an actual implementation",
            "continue_message": "继续",
        }

    monkeypatch.setattr(model_pool, "chat_json", fake_chat_json)

    req = AutoContinueCheckRequest(
        user_message="实现一个低风险改动，最后运行相关 pytest。",
        assistant_reply="好的，我先确认当前项目结构和现有测试情况，再动手。跑一下目录结构和找到相关文件。",
        auto_continue_elapsed_sec=20,
        max_auto_continue_sec=600,
    )
    result = await chat.auto_continue_check(req)

    assert result.should_continue is False
    assert result.confidence == pytest.approx(0.6)
    assert result.reason == "not an actual implementation"


@pytest.mark.asyncio
async def test_auto_continue_check_respects_model_for_staged_work_reply(monkeypatch):
    from app.api import chat
    from app.llm import model_pool
    from app.schemas.api import AutoContinueCheckRequest

    async def fake_chat_json(messages, *, model_spec=None, **kwargs):
        return {
            "should_continue": False,
            "confidence": 0.1,
            "reason": "Both user message and assistant reply are question marks",
            "continue_message": "继续",
        }

    monkeypatch.setattr(model_pool, "chat_json", fake_chat_json)

    req = AutoContinueCheckRequest(
        user_message="请完成实现并测试",
        assistant_reply="我完成了第一阶段，下一步可以继续写测试。",
        auto_continue_elapsed_sec=20,
        max_auto_continue_sec=600,
    )
    result = await chat.auto_continue_check(req)

    assert result.should_continue is False
    assert result.confidence == pytest.approx(0.1)
    assert result.reason == "Both user message and assistant reply are question marks"


@pytest.mark.asyncio
async def test_auto_continue_check_http_contract(monkeypatch):
    import httpx
    from fastapi import FastAPI

    from app.api import chat
    from app.llm import model_pool

    async def fake_chat_json(messages, *, model_spec=None, **kwargs):
        return {
            "should_continue": True,
            "confidence": 0.8,
            "reason": "partial reply",
            "continue_message": "继续",
        }

    monkeypatch.setattr(model_pool, "chat_json", fake_chat_json)

    app = FastAPI()
    app.include_router(chat.router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/v1/chat/auto-continue/check",
            json={
                "user_message": "完成一个长报告",
                "assistant_reply": "我先完成了第一部分，下一步继续写第二部分。",
                "recent_context": "用户要求完整报告。",
                "auto_continue_elapsed_sec": 10,
                "max_auto_continue_sec": 300,
            },
        )

    assert resp.status_code == 200
    assert resp.json() == {
        "should_continue": True,
        "confidence": 0.8,
        "reason": "partial reply",
        "continue_message": "继续完成同一任务：完成一个长报告",
    }


async def test_chat_stream_duplicate_replays_cached_text_token(monkeypatch):
    from app.api import chat

    async def fake_get_archive(archive_id):
        return {"archive_id": archive_id}

    async def fake_active(group_id):
        return "archive"

    async def fake_orchestrate(req, trace_id, **kwargs):
        yield "meta", {"trace_id": trace_id}
        yield "token", {"text": "hi"}
        yield "done", {"trace_id": trace_id}
        yield "complete", {"trace_id": trace_id}

    class Guard:
        async def acquire(self, *args, **kwargs):
            return None

        async def release(self, *args, **kwargs):
            return True

    monkeypatch.setattr(chat.archive_dao, "get_archive", fake_get_archive)
    monkeypatch.setattr(chat.bot_config, "get_active_archive", fake_active)
    monkeypatch.setattr(chat, "orchestrate", fake_orchestrate)
    monkeypatch.setattr(chat, "get_group_guard", lambda: Guard())
    chat._idempotency_cache.clear()

    req = ChatRequest(
        archive_id="archive",
        group_id="group",
        user_id="user",
        message="hello",
        client_msg_id="same",
    )
    first = await chat.chat_stream(req)
    await _collect_sse(first)

    second = await chat.chat_stream(req)
    events = await _collect_sse(second)

    assert [name for name, _ in events] == ["meta", "token", "done", "complete"]
    assert events[1][1]["text"] == "hi"
    assert events[1][1]["duplicate"] is True
    assert events[2][1]["duplicate"] is True


async def test_chat_stream_duplicate_suppresses_cached_text_for_voice_reply(monkeypatch):
    from app.api import chat

    async def fake_get_archive(archive_id):
        return {"archive_id": archive_id}

    async def fake_active(group_id):
        return "archive"

    async def fake_orchestrate(req, trace_id, **kwargs):
        yield "meta", {"trace_id": trace_id}
        yield "token", {"text": "这句会被语音替代"}
        yield "done", {
            "trace_id": trace_id,
            "voice_reply": True,
            "_suppress_text": True,
            "voice_reply_file": "reply.wav",
        }
        yield "complete", {"trace_id": trace_id}

    class Guard:
        async def acquire(self, *args, **kwargs):
            return None

        async def release(self, *args, **kwargs):
            return True

    monkeypatch.setattr(chat.archive_dao, "get_archive", fake_get_archive)
    monkeypatch.setattr(chat.bot_config, "get_active_archive", fake_active)
    monkeypatch.setattr(chat, "orchestrate", fake_orchestrate)
    monkeypatch.setattr(chat, "get_group_guard", lambda: Guard())
    chat._idempotency_cache.clear()

    req = ChatRequest(
        archive_id="archive",
        group_id="group",
        user_id="user",
        message="voice",
        client_msg_id="same-voice",
    )
    first = await chat.chat_stream(req)
    await _collect_sse(first)

    second = await chat.chat_stream(req)
    events = await _collect_sse(second)

    assert [name for name, _ in events] == ["meta", "done", "complete"]
    assert events[1][1]["voice_reply"] is True
    assert events[1][1]["_suppress_text"] is True
    assert all(payload.get("text") != "这句会被语音替代" for _, payload in events)


async def test_chat_stream_releases_guard_after_error_event(monkeypatch):
    from app.api import chat

    released = False

    async def fake_get_archive(archive_id):
        return {"archive_id": archive_id}

    async def fake_active(group_id):
        return "archive"

    async def fake_orchestrate(req, trace_id, **kwargs):
        raise RuntimeError("boom")
        yield

    class Guard:
        async def acquire(self, *args, **kwargs):
            return None

        async def release(self, *args, **kwargs):
            nonlocal released
            released = True
            return True

    monkeypatch.setattr(chat.archive_dao, "get_archive", fake_get_archive)
    monkeypatch.setattr(chat.bot_config, "get_active_archive", fake_active)
    monkeypatch.setattr(chat, "orchestrate", fake_orchestrate)
    monkeypatch.setattr(chat, "get_group_guard", lambda: Guard())

    req = ChatRequest(archive_id="archive", group_id="group", user_id="user", message="hello")
    resp = await chat.chat_stream(req)
    events = await _collect_sse(resp)

    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "internal_error"
    assert released

async def test_chat_stream_shields_guard_release_when_stream_is_cancelled(monkeypatch):
    from app.api import chat

    released = False
    abort_signalled = False

    async def fake_get_archive(archive_id):
        return {"archive_id": archive_id}

    async def fake_active(group_id):
        return "archive"

    async def fake_orchestrate(req, trace_id, **kwargs):
        yield "meta", {"trace_id": trace_id}
        raise asyncio.CancelledError()

    class Guard:
        async def acquire(self, *args, **kwargs):
            return None

        async def release(self, *args, **kwargs):
            nonlocal released
            released = True
            return True

        async def signal_abort(self, **kwargs):
            nonlocal abort_signalled
            abort_signalled = True
            return True

    monkeypatch.setattr(chat.archive_dao, "get_archive", fake_get_archive)
    monkeypatch.setattr(chat.bot_config, "get_active_archive", fake_active)
    monkeypatch.setattr(chat, "orchestrate", fake_orchestrate)
    monkeypatch.setattr(chat, "get_group_guard", lambda: Guard())

    req = ChatRequest(archive_id="archive", group_id="group", user_id="user", message="hello")
    resp = await chat.chat_stream(req)
    iterator = resp.body_iterator.__aiter__()

    first = await iterator.__anext__()
    assert first["event"] == "meta"
    with pytest.raises(asyncio.CancelledError):
        await iterator.__anext__()
    assert abort_signalled
    assert released



    from app.api import chat

    async def fake_get_archive(archive_id):
        return {"archive_id": archive_id}

    async def fake_active(group_id):
        return "active"

    monkeypatch.setattr(chat.archive_dao, "get_archive", fake_get_archive)
    monkeypatch.setattr(chat.bot_config, "get_active_archive", fake_active)
    monkeypatch.setattr(chat.settings, "strict_active_archive", True)

    req = ChatRequest(archive_id="stale", group_id="group", user_id="user", message="hello")
    with pytest.raises(HTTPException) as exc:
        await chat.chat_stream(req)

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "archive_mismatch"
    assert exc.value.detail["active_archive_id"] == "active"


async def test_chat_stream_allows_active_archive_mismatch_when_not_strict(monkeypatch):
    from app.api import chat

    async def fake_get_archive(archive_id):
        return {"archive_id": archive_id}

    async def fake_active(group_id):
        return "active"

    async def fake_orchestrate(req, trace_id, **kwargs):
        yield "complete", {"trace_id": trace_id}

    class Guard:
        async def acquire(self, *args, **kwargs):
            return None

        async def release(self, *args, **kwargs):
            return True

    monkeypatch.setattr(chat.archive_dao, "get_archive", fake_get_archive)
    monkeypatch.setattr(chat.bot_config, "get_active_archive", fake_active)
    monkeypatch.setattr(chat.settings, "strict_active_archive", False)
    monkeypatch.setattr(chat, "orchestrate", fake_orchestrate)
    monkeypatch.setattr(chat, "get_group_guard", lambda: Guard())

    req = ChatRequest(archive_id="stale", group_id="group", user_id="user", message="hello")
    resp = await chat.chat_stream(req)
    events = await _collect_sse(resp)

    assert events[-1][0] == "complete"


async def test_chat_stream_ignores_missing_active_archive(monkeypatch):
    from app.api import chat

    async def fake_get_archive(archive_id):
        return {"archive_id": archive_id}

    async def fake_active(group_id):
        return None

    async def fake_orchestrate(req, trace_id, **kwargs):
        yield "complete", {"trace_id": trace_id}

    class Guard:
        async def acquire(self, *args, **kwargs):
            return None

        async def release(self, *args, **kwargs):
            return True

    monkeypatch.setattr(chat.archive_dao, "get_archive", fake_get_archive)
    monkeypatch.setattr(chat.bot_config, "get_active_archive", fake_active)
    monkeypatch.setattr(chat.settings, "strict_active_archive", True)
    monkeypatch.setattr(chat, "orchestrate", fake_orchestrate)
    monkeypatch.setattr(chat, "get_group_guard", lambda: Guard())

    req = ChatRequest(archive_id="archive", group_id="group", user_id="user", message="hello")
    resp = await chat.chat_stream(req)
    events = await _collect_sse(resp)

    assert events[-1][0] == "complete"


async def test_chat_stream_passes_interrupt_messages_getter(monkeypatch):
    from app.api import chat
    from app.schemas.api import InterruptMessageRequest

    captured = {}

    async def fake_get_archive(archive_id):
        return {"archive_id": archive_id}

    async def fake_active(group_id):
        return "archive"

    async def fake_orchestrate(req, trace_id, **kwargs):
        getter = kwargs.get("interrupt_messages_getter")
        captured["messages"] = getter() if getter else []
        yield "done", {"trace_id": trace_id}
        yield "complete", {"trace_id": trace_id}

    class Guard:
        async def acquire(self, *args, **kwargs):
            return None

        async def release(self, *args, **kwargs):
            return True

        async def is_busy(self, *args, **kwargs):
            return True

        async def signal_abort(self, *args, **kwargs):
            return True

    monkeypatch.setattr(chat.archive_dao, "get_archive", fake_get_archive)
    monkeypatch.setattr(chat.bot_config, "get_active_archive", fake_active)
    monkeypatch.setattr(chat, "orchestrate", fake_orchestrate)
    monkeypatch.setattr(chat, "get_group_guard", lambda: Guard())
    chat._interrupt_messages.clear()

    await chat.interrupt_message(InterruptMessageRequest(
        archive_id="archive",
        group_id="group",
        user_id="user",
        message="停，顺便告诉我进度",
        client_msg_id="m2",
    ))
    req = ChatRequest(archive_id="archive", group_id="group", user_id="user", message="start")
    resp = await chat.chat_stream(req)
    await _collect_sse(resp)

    assert captured["messages"] == ["停，顺便告诉我进度"]
    assert chat._interrupt_messages == {}


async def test_chat_stream_interrupt_payload_cache_shares_queue(monkeypatch):
    from app.api import chat
    from collections import deque
    import time

    seen = {}

    async def fake_get_archive(archive_id):
        return {"archive_id": archive_id}

    async def fake_active(group_id):
        return "archive"

    async def fake_orchestrate(req, trace_id, **kwargs):
        seen["messages"] = kwargs["interrupt_messages_getter"]()
        seen["payloads"] = kwargs["interrupt_payloads_getter"]()
        seen["payloads_again"] = kwargs["interrupt_payloads_getter"]()
        yield "done", {"trace_id": trace_id}
        yield "complete", {"trace_id": trace_id}

    class Guard:
        async def acquire(self, *args, **kwargs):
            return None

        async def release(self, *args, **kwargs):
            return True

        async def is_busy(self, *args, **kwargs):
            return True

    monkeypatch.setattr(chat.archive_dao, "get_archive", fake_get_archive)
    monkeypatch.setattr(chat.bot_config, "get_active_archive", fake_active)
    monkeypatch.setattr(chat, "orchestrate", fake_orchestrate)
    monkeypatch.setattr(chat, "get_group_guard", lambda: Guard())
    chat._interrupt_messages.clear()
    chat._interrupt_messages[("archive", "group", "user")] = deque([
        {
            "message": "????",
            "client_msg_id": "bg1",
            "kind": "background",
            "source": "env_background_finished",
            "meta": {"task_id": "bg-task"},
            "ts": time.monotonic(),
        }
    ])

    req = ChatRequest(archive_id="archive", group_id="group", user_id="user", message="start")
    resp = await chat.chat_stream(req)
    await _collect_sse(resp)

    assert seen["messages"] == []
    assert len(seen["payloads"]) == 1
    assert seen["payloads"][0]["kind"] == "background"
    assert seen["payloads"][0]["source"] == "env_background_finished"
    assert seen["payloads_again"] == seen["payloads"]
    assert chat._interrupt_messages == {}


def test_mid_turn_control_interrupt_keeps_original_task():
    from app.core.orchestrator_entry import _message_with_user_interrupts

    base = "请检查当前工程里和中断、自动继续、文件上传、记忆回忆有关的几个接口。"
    payloads = [
        {
            "message": "请中断当前回答并重新整理。",
            "kind": "user",
            "source": "chat_interrupt",
        }
    ]

    merged = _message_with_user_interrupts(base, payloads)

    assert base in merged
    assert "Mid-turn control interruption" in merged
    assert "重新整理" in merged
    assert "Treat it as the latest instruction" not in merged


def test_mid_turn_non_control_interrupt_overrides_original_task():
    from app.core.orchestrator_entry import _message_with_user_interrupts

    base = "请检查当前工程里和中断、自动继续、文件上传、记忆回忆有关的几个接口。"
    payloads = [
        {
            "message": "改为总结 test_deliverable_boundaries 的真实测试结果。",
            "kind": "user",
            "source": "chat_interrupt",
        }
    ]

    merged = _message_with_user_interrupts(base, payloads)

    assert base in merged
    assert "Mid-turn user interruption" in merged
    assert "Treat it as the latest instruction" in merged
    assert "Mid-turn control interruption" not in merged


async def test_chat_interrupt_message_resolves_environment_project(monkeypatch, tmp_path):
    from app.api import chat
    from app.schemas.api import InterruptMessageRequest

    seen = {}

    async def fake_resolve_environment_project(**kwargs):
        seen["resolve"] = kwargs
        return {
            "archive_id": "arch_env",
            "group_id": "env_user_user",
            "project_key": kwargs.get("project_id") or "p",
            "root_dir": str(tmp_path),
        }

    class Guard:
        async def is_busy(self, archive_id, group_id, user_id):
            seen["busy"] = (archive_id, group_id, user_id)
            return archive_id == "arch_env" and group_id == "env_user_user"

        def get_stage(self, archive_id, group_id, user_id):
            return "round2"

        async def signal_abort(self, **kwargs):
            seen["abort"] = kwargs
            return True

    monkeypatch.setattr(chat, "resolve_environment_project", fake_resolve_environment_project)
    monkeypatch.setattr(chat, "get_group_guard", lambda: Guard())
    chat._interrupt_messages.clear()

    ok = await chat.interrupt_message(InterruptMessageRequest(
        archive_id="local_archive",
        group_id="local_group",
        user_id="user",
        message="插入当前任务",
        client_msg_id="env-insert",
        current_dir=str(tmp_path),
        project_id="project-a",
    ))

    assert ok == {"ok": True, "queued": True, "aborted": True, "stage": "round2", "reason": ""}
    assert seen["resolve"] == {
        "user_id": "user",
        "current_dir": str(tmp_path),
        "project_id": "project-a",
    }
    assert seen["busy"] == ("arch_env", "env_user_user", "user")
    assert seen["abort"] == {
        "archive_id": "arch_env",
        "group_id": "env_user_user",
        "user_id": "user",
    }
    assert chat._pop_interrupt_messages("arch_env", "env_user_user", "user") == ["插入当前任务"]


async def test_chat_and_environment_interrupt_queues_are_shared(monkeypatch):
    from app.api import chat
    from app.api import environment
    from app.schemas.api import InterruptMessageRequest

    class Guard:
        async def is_busy(self, *args, **kwargs):
            return True

        def get_stage(self, *args, **kwargs):
            return "round2"

        async def signal_abort(self, **kwargs):
            return True

    monkeypatch.setattr(environment, "get_group_guard", lambda: Guard())
    chat._interrupt_messages.clear()

    ok = await environment.interrupt_message(InterruptMessageRequest(
        archive_id="archive",
        group_id="group",
        user_id="user",
        message="stop",
        client_msg_id="shared",
    ))

    assert ok == {"ok": True, "queued": True, "aborted": True, "stage": "round2", "reason": ""}
    assert chat._pop_interrupt_messages("archive", "group", "user") == ["stop"]


async def test_interrupt_message_round3_queues_without_preempting(monkeypatch):
    from app.api import chat
    from app.schemas.api import InterruptMessageRequest

    class Guard:
        async def is_busy(self, *args, **kwargs):
            return True

        def get_stage(self, *args, **kwargs):
            return "round3"

        async def signal_abort(self, **kwargs):
            return False

    monkeypatch.setattr(chat, "get_group_guard", lambda: Guard())
    chat._interrupt_messages.clear()

    ok = await chat.interrupt_message(InterruptMessageRequest(
        archive_id="archive",
        group_id="group",
        user_id="user",
        message="下一轮再处理",
        client_msg_id="r3",
    ))

    assert ok == {
        "ok": True,
        "queued": True,
        "aborted": False,
        "stage": "round3",
        "reason": "queued_no_preempt",
    }
    assert chat._pop_interrupt_messages("archive", "group", "user") == ["下一轮再处理"]


async def test_chat_command_monitor_aliases(monkeypatch):
    from app.api import chat

    async def fake_snapshot(**kwargs):
        return {"active_commands": [{"archive_id": kwargs["archive_id"]}], "active_command_count": 1}

    async def fake_abort(command_id):
        return {"ok": True, "command_id": command_id}

    async def fake_history(**kwargs):
        return [{"event": "workflow", "payload": {"archive_id": kwargs["archive_id"]}}]

    monkeypatch.setattr(chat.env_monitor, "snapshot", fake_snapshot)
    monkeypatch.setattr(chat.env_monitor, "abort_command", fake_abort)
    monkeypatch.setattr(chat.env_monitor, "history", fake_history)

    assert await chat.active_chat_commands(archive_id="a") == {
        "active_commands": [{"archive_id": "a"}],
        "active_command_count": 1,
    }
    assert await chat.abort_chat_command("cmd1") == {"ok": True, "command_id": "cmd1"}
    assert await chat.monitor_history(archive_id="a") == {
        "items": [{"event": "workflow", "payload": {"archive_id": "a"}}]
    }


async def test_chat_abort_signals_guard_and_enters_stop_mode(monkeypatch):
    from app.api import chat

    calls = {}

    class Guard:
        async def signal_abort(self, **kwargs):
            calls["abort"] = kwargs
            return True

        def enter_stop_mode(self, **kwargs):
            calls["stop"] = kwargs

    monkeypatch.setattr(chat, "get_group_guard", lambda: Guard())

    result = await chat.abort_chat({
        "archive_id": "archive",
        "group_id": "group",
        "user_id": "user",
    })

    assert result == {"ok": True}
    assert calls["abort"] == {
        "archive_id": "archive",
        "group_id": "group",
        "user_id": "user",
    }
    assert calls["stop"]["duration_sec"] == 20.0


async def test_environment_abort_signals_guard(monkeypatch):
    from app.api import environment

    calls = {}

    class Guard:
        async def signal_abort(self, **kwargs):
            calls["abort"] = kwargs
            return True

    monkeypatch.setattr(environment, "get_group_guard", lambda: Guard())

    result = await environment.abort_environment({
        "archive_id": "env_archive",
        "group_id": "env_user_project",
        "user_id": "user",
    })

    assert result == {"ok": True}
    assert calls["abort"] == {
        "archive_id": "env_archive",
        "group_id": "env_user_project",
        "user_id": "user",
    }


async def test_environment_abort_can_resolve_current_dir(monkeypatch, tmp_path):
    from app.api import environment

    calls = {}

    async def fake_resolve_environment_project(**kwargs):
        calls["resolve"] = kwargs
        return {"archive_id": "resolved_archive", "group_id": "env_user_user"}

    class Guard:
        async def signal_abort(self, **kwargs):
            calls["abort"] = kwargs
            return True

    monkeypatch.setattr(environment, "resolve_environment_project", fake_resolve_environment_project)
    monkeypatch.setattr(environment, "get_group_guard", lambda: Guard())

    result = await environment.abort_environment({
        "user_id": "user",
        "current_dir": str(tmp_path),
        "project_id": "p1",
    })

    assert result == {"ok": True}
    assert calls["resolve"] == {
        "user_id": "user",
        "current_dir": str(tmp_path),
        "project_id": "p1",
    }
    assert calls["abort"] == {
        "archive_id": "resolved_archive",
        "group_id": "env_user_user",
        "user_id": "user",
    }


async def test_chat_abort_can_resolve_environment_current_dir(monkeypatch, tmp_path):
    from app.api import chat

    calls = {}

    async def fake_resolve_environment_project(**kwargs):
        calls["resolve"] = kwargs
        return {"archive_id": "resolved_archive", "group_id": "env_user_user"}

    class Guard:
        async def signal_abort(self, **kwargs):
            calls["abort"] = kwargs
            return True

        def enter_stop_mode(self, **kwargs):
            calls["stop"] = kwargs

    monkeypatch.setattr(chat, "resolve_environment_project", fake_resolve_environment_project)
    monkeypatch.setattr(chat, "get_group_guard", lambda: Guard())

    result = await chat.abort_chat({
        "user_id": "user",
        "current_dir": str(tmp_path),
        "project_id": "p1",
    })

    assert result == {"ok": True}
    assert calls["resolve"] == {
        "user_id": "user",
        "current_dir": str(tmp_path),
        "project_id": "p1",
    }
    assert calls["abort"] == {
        "archive_id": "resolved_archive",
        "group_id": "env_user_user",
        "user_id": "user",
    }


async def test_chat_stream_rejects_unknown_archive_before_active_archive_check(monkeypatch):
    from app.api import chat

    active_called = False

    async def fake_get_archive(archive_id):
        return None

    async def fake_active(group_id):
        nonlocal active_called
        active_called = True
        return "archive"

    monkeypatch.setattr(chat.archive_dao, "get_archive", fake_get_archive)
    monkeypatch.setattr(chat.bot_config, "get_active_archive", fake_active)

    req = ChatRequest(archive_id="missing", group_id="group", user_id="user", message="hello")
    with pytest.raises(HTTPException) as exc:
        await chat.chat_stream(req)

    assert exc.value.status_code == 404
    assert active_called is False


def test_all_orchestrator_progress_events_are_structured():
    from app.core import orchestrator
    from app.core.round2_stage import R2_STAGE_TABLE

    static_progress = [
        orchestrator._progress_payload("loading_memory", "loading", "正在加载记忆和人设"),
        orchestrator._progress_payload("analyzing_intent", "analyzing", "正在分析用户意图"),
        orchestrator._progress_payload("composing_reply", "responding", "正在组织回复"),
        orchestrator._progress_payload("updating_memory", "maintaining", "正在更新记忆"),
    ]
    stage_progress = [cfg["progress_event"] for cfg in R2_STAGE_TABLE.values()]

    for payload in static_progress + stage_progress:
        assert set(["kind", "round", "message"]).issubset(payload)
        assert payload["kind"]
        assert payload["round"]
        assert payload["message"]


async def test_dynamic_round2_progress_events_are_structured(monkeypatch):
    from app.core import orchestrator
    from app.schemas.api import ResponsePlan, TendencyAnalysis

    async def fake_round2(*args, progress_queue=None, progress_log=None, **kwargs):
        if progress_queue is not None:
            await progress_queue.put(("progress", {
                "kind": "planning_tools",
                "round": "planning",
                "tool_iter": 3,
                "event": "scheduled",
                "message": "正在确认工具结果",
            }))
        if progress_log is not None:
            progress_log.append("正在确认工具结果")
        return ResponsePlan(
            intent="answer",
            key_points=["ok"],
            tone="casual",
            length_hint="short",
            avoid=[],
        )

    monkeypatch.setattr(orchestrator, "_round2", fake_round2)

    events = []
    async for ev_type, ev_data in orchestrator._drive_round2(
        [],
        TendencyAnalysis(tendencies={}, rationale="test"),
        persona="p",
        workspace_dir="",
        archive_id="a",
        group_id="g",
        user_id="u",
        abort_event=asyncio.Event(),
        progress_log=[],
        think=False,
        tier="low",
        helper_lite=True,
        needs_tools=False,
        needs_recall=False,
        prior_plan=None,
        max_iter=1,
        user_message_text="hello",
    ):
        events.append((ev_type, ev_data))

    progress = [data for ev_type, data in events if ev_type == "progress"]
    assert progress
    for payload in progress:
        assert set(["kind", "round", "message"]).issubset(payload)
    assert events[-1][0] == "_plan"


async def test_drive_round2_returns_fallback_plan_when_round2_task_raises(monkeypatch):
    from app.core import orchestrator
    from app.schemas.api import TendencyAnalysis

    async def failing_round2(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(orchestrator, "_round2", failing_round2)

    events = []
    async for ev_type, ev_data in orchestrator._drive_round2(
        [],
        TendencyAnalysis(tendencies={}, rationale="test"),
        persona="p",
        workspace_dir="",
        archive_id="a",
        group_id="g",
        user_id="u",
        abort_event=asyncio.Event(),
        progress_log=[],
        think=False,
        tier="low",
        helper_lite=True,
        needs_tools=False,
        needs_recall=False,
        prior_plan=None,
        max_iter=1,
        user_message_text="hello",
    ):
        events.append((ev_type, ev_data))

    assert events[-1][0] == "_plan"
    assert events[-1][1]["plan"].intent


async def test_drive_round2_cancels_round2_task_when_generator_is_cancelled(monkeypatch):
    from app.core import orchestrator
    from app.schemas.api import TendencyAnalysis

    round2_cancelled = asyncio.Event()

    async def hanging_round2(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            round2_cancelled.set()
            raise

    monkeypatch.setattr(orchestrator, "_round2", hanging_round2)

    async def consume():
        async for _ev_type, _ev_data in orchestrator._drive_round2(
            [],
            TendencyAnalysis(tendencies={}, rationale="test"),
            persona="p",
            workspace_dir="",
            archive_id="a",
            group_id="g",
            user_id="u",
            abort_event=asyncio.Event(),
            progress_log=[],
            think=False,
            tier="low",
            helper_lite=True,
            needs_tools=False,
            needs_recall=False,
            prior_plan=None,
            max_iter=1,
            user_message_text="hello",
        ):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(round2_cancelled.wait(), 1)


def test_explicit_fresh_ocr_request_requires_image_signal():
    from app.core import orchestrator

    assert orchestrator._is_explicit_fresh_ocr_request(
        "重新识别 _downloaded_media/img_abc.jpg，不要引用之前的识别结论"
    )
    assert orchestrator._is_explicit_fresh_ocr_request(
        "please rerun OCR on screenshot.png and do not use previous result"
    )
    assert not orchestrator._is_explicit_fresh_ocr_request("不要引用之前的回答，重新解释一下")


def test_ocr_concept_questions_do_not_trigger_visual_intent():
    from app.core.message_routing import has_image_intent_in_msg

    assert not has_image_intent_in_msg("OCR 是什么？用一句话解释。")
    assert not has_image_intent_in_msg("检查日志时 OCR 的 accurate 档位是什么意思？")
    assert has_image_intent_in_msg("请 OCR 一下这张截图")
    assert has_image_intent_in_msg("重新 OCR _downloaded_media/img_abc.jpg")


async def test_round2_preflights_fresh_ocr_helper_and_injects_hint(monkeypatch, tmp_path):
    from app.core import orchestrator
    from app.schemas.api import TendencyAnalysis
    from app.llm.tools import registry

    dispatched = []
    captured_messages = {}

    async def fake_dispatch(name, args, **kwargs):
        dispatched.append((name, args, kwargs))
        return json.dumps({
            "ok": True,
            "results": [{
                "task_id": "fresh_ocr",
                "ok": True,
                "kind": "read",
                "report": "识别结果摘要: 7 8 9 10 题可见。",
                "files": [],
                "deliverables": [],
                "file_map": [{
                    "helper_name": "ocr_report.txt",
                    "main_name": "fresh_ocr_ocr_report.txt",
                    "shared_name": "_helpers_shared/fresh_ocr/ocr_report.txt",
                }],
            }],
        }, ensure_ascii=False)

    async def fake_chat_with_tools_loop(messages, tools, **kwargs):
        captured_messages["messages"] = messages
        captured_messages["tools"] = tools
        return (
            json.dumps({
                "intent": "基于重新识别结果回答",
                "key_points": ["7 8 9 10 题可见"],
                "tone": "自然",
                "length_hint": "短",
                "avoid": [],
                "callbacks": [],
                "deliverables": [],
            }, ensure_ascii=False),
            messages,
        )

    monkeypatch.setattr(registry, "dispatch", fake_dispatch)
    monkeypatch.setattr(orchestrator.llm, "chat_with_tools_loop", fake_chat_with_tools_loop)

    progress_queue = asyncio.Queue()
    plan = await orchestrator._round2(
        [{"role": "user", "content": "重新识别 _downloaded_media/img_abc.jpg，不要引用之前的识别结论"}],
        TendencyAnalysis(tendencies={}, rationale="test"),
        archive_id="archive",
        group_id="group",
        user_id="user",
        workspace_dir=str(tmp_path),
        progress_queue=progress_queue,
        abort_event=asyncio.Event(),
        progress_log=[],
        think=False,
        tier="low",
        helper_lite=True,
        needs_tools=True,
        needs_recall=True,
        inline_images=[],
        prior_plan=None,
        max_iter=1,
        user_message_text="重新识别 _downloaded_media/img_abc.jpg，不要引用之前的识别结论",
    )

    assert plan.intent == "基于重新识别结果回答"
    assert dispatched
    name, args, kwargs = dispatched[0]
    assert name == "delegate"
    assert args["tasks"][0]["kind"] == "read"
    assert args["tasks"][0]["task_id"] == "fresh_ocr"
    assert args["wait_window_sec"] == 180
    assert kwargs["workspace_dir"] == str(tmp_path)

    joined_system = "\n".join(
        m.get("content", "") for m in captured_messages["messages"] if m.get("role") == "system"
    )
    joined_user = "\n".join(
        m.get("content", "") for m in captured_messages["messages"] if m.get("role") == "user"
    )
    assert "Mandatory Fresh OCR" not in joined_system
    assert "Fresh OCR Preflight Result" not in joined_system
    assert "Mandatory Fresh OCR" in joined_user
    assert "kind='read'" in joined_user
    assert "Fresh OCR Preflight Result" in joined_user
    assert "识别结果摘要: 7 8 9 10 题可见。" in joined_user
    assert "_helpers_shared/fresh_ocr/ocr_report.txt" in joined_user

    tool_names = {t["function"]["name"] for t in captured_messages["tools"]}
    assert "delegate" in tool_names


async def test_round2_sends_static_system_prefix_before_dynamic_user_tail(monkeypatch, tmp_path):
    from app.core import orchestrator
    from app.core.prompt_cache_observer import describe_prompt_cache_input
    from app.schemas.api import TendencyAnalysis
    from app.llm.tools import registry

    captured_messages = {}

    async def fake_dispatch(name, args, **kwargs):
        return json.dumps({"ok": True}, ensure_ascii=False)

    async def fake_chat_with_tools_loop(messages, tools, **kwargs):
        captured_messages["messages"] = messages
        return (
            json.dumps({
                "intent": "produce report",
                "key_points": ["done"],
                "tone": "concise",
                "length_hint": "short",
                "avoid": [],
                "callbacks": [],
                "deliverables": [],
            }, ensure_ascii=False),
            messages,
        )

    monkeypatch.setattr(registry, "dispatch", fake_dispatch)
    monkeypatch.setattr(orchestrator.llm, "chat_with_tools_loop", fake_chat_with_tools_loop)

    plan = await orchestrator._round2(
        [{"role": "system", "content": "## Context And Safety Contract\nstable"}, {"role": "user", "content": "base"}],
        TendencyAnalysis(
            tendencies={},
            rationale="test",
            is_coding_task=True,
            is_document_task=False,
        ),
        archive_id="archive",
        group_id="group",
        user_id="user",
        workspace_dir=str(tmp_path),
        progress_queue=asyncio.Queue(),
        abort_event=asyncio.Event(),
        progress_log=[],
        think=False,
        tier="low",
        helper_lite=True,
        parallelizable=True,
        needs_tools=True,
        needs_recall=False,
        inline_images=[],
        prior_plan=None,
        max_iter=1,
        user_message_text="为 A.csv 写一个 Python 分析脚本并输出报告",
    )

    assert plan.intent == "produce report"
    messages = captured_messages["messages"]
    shape = describe_prompt_cache_input(messages=messages)
    roles = [message["role"] for message in messages]

    assert roles[:shape["leading_system_count"]] == ["system"] * shape["leading_system_count"]
    assert shape["leading_system_count"] > 1
    assert "user" not in roles[:shape["leading_system_count"]]
    assert roles[shape["leading_system_count"]] == "user"

    stable_prefix = "\n".join(
        message["content"] for message in messages[:shape["leading_system_count"]]
    )
    dynamic_tail = "\n".join(
        message["content"] for message in messages[shape["leading_system_count"]:]
    )
    assert "## Toolchain Continuation" in stable_prefix
    assert "## You are the orchestrator, not the worker" in stable_prefix
    assert "Active Task Contract Anchor" not in stable_prefix
    assert "A.csv" not in stable_prefix
    assert "Active Task Contract Anchor" in dynamic_tail
    assert "A.csv" in dynamic_tail
