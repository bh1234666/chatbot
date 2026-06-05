import asyncio
import logging

import pytest


@pytest.mark.asyncio
async def test_retry_local_timeout_is_not_retried(monkeypatch):
    from app.llm import client as llm_client

    calls = 0

    async def never_returns():
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()

    monkeypatch.setattr(llm_client.settings, "llm_call_timeout_sec", 0.01)

    with pytest.raises(asyncio.TimeoutError):
        await llm_client._retry(never_returns, label="unit.timeout")

    assert calls == 1


@pytest.mark.asyncio
async def test_chat_stream_first_chunk_timeout_closes_lock_path(monkeypatch):
    from app.llm import client as llm_client

    class HangingStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

        async def close(self):
            self.closed = True

    async def fake_retry(fn, label, **kwargs):
        return HangingStream()

    monkeypatch.setattr(llm_client, "_retry", fake_retry)
    monkeypatch.setattr(llm_client.settings, "llm_stream_first_chunk_timeout_sec", 0.01)

    with pytest.raises(asyncio.TimeoutError):
        async for _ in llm_client.chat_stream([{"role": "user", "content": "hi"}]):
            pass


@pytest.mark.asyncio
async def test_chat_json_with_upgrade_timeout_returns_none_without_error_log(monkeypatch, caplog):
    from app.llm import client as llm_client

    async def timeout_chat_json(*args, **kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(llm_client, "chat_json", timeout_chat_json)

    with caplog.at_level(logging.WARNING, logger="app.llm.client"):
        result = await llm_client.chat_json_with_upgrade(
            [{"role": "user", "content": "compress"}],
            validate=lambda raw: True,
            label="kb",
            lite_first=False,
        )

    assert result is None
    assert "main+thinking timed out" in caplog.text
    assert "main+thinking attempt error" not in caplog.text


@pytest.mark.asyncio
async def test_chat_json_with_upgrade_lite_failure_logs_warning_without_trace(monkeypatch, caplog):
    from app.llm import client as llm_client

    calls = 0

    async def flaky_chat_json(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("bad json")
        return {"ok": True}

    monkeypatch.setattr(llm_client, "chat_json", flaky_chat_json)

    with caplog.at_level(logging.WARNING, logger="app.llm.client"):
        result = await llm_client.chat_json_with_upgrade(
            [{"role": "user", "content": "compress"}],
            validate=lambda raw: raw.get("ok") is True,
            label="kb",
        )

    assert result == {"ok": True}
    assert "lite attempt failed" in caplog.text
    assert "lite attempt error" not in caplog.text


@pytest.mark.asyncio
async def test_chat_json_records_non_stream_usage_with_metrics_tag(monkeypatch):
    from app.core import metrics
    from app.llm import client as llm_client

    metrics.reset()
    cache_logs: list[str] = []

    class Usage:
        prompt_tokens = 100
        completion_tokens = 7
        prompt_cache_hit_tokens = 80
        prompt_cache_miss_tokens = 20

    class Message:
        content = '"ok": true}'

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]
        usage = Usage()

    class Completions:
        async def create(self, **kwargs):
            return Response()

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    async def fake_retry(call, **kwargs):
        return await call()

    def fake_debug_log(category, message, payload=None):
        if category == "llm.cache_stats":
            cache_logs.append(message)

    monkeypatch.setattr(llm_client, "_client_for_spec", lambda spec: Client())
    monkeypatch.setattr(llm_client, "_retry", fake_retry)
    monkeypatch.setattr(llm_client.debug, "log", fake_debug_log)

    result = await llm_client.chat_json(
        [{"role": "user", "content": "return json"}],
        lite=True,
        metrics_tag="json.unit",
    )

    assert result == {"ok": True}
    assert cache_logs == [
        "P49 [json.unit]: model=deepseek-v4-flash prompt=100 completion=7 "
        "cache_hit=80 cache_miss=20 hit_rate=80%"
    ]
    snapshot = metrics.snapshot()
    assert snapshot["calls"]["json.unit|deepseek-v4-flash"] == 1
    assert snapshot["cache_hit_tokens"]["json.unit|deepseek-v4-flash"] == 80


@pytest.mark.asyncio
async def test_chat_json_records_nested_cached_tokens_usage(monkeypatch):
    from app.core import metrics
    from app.llm import client as llm_client

    metrics.reset()
    cache_logs: list[str] = []

    class Usage:
        prompt_tokens = 100
        completion_tokens = 7
        prompt_tokens_details = {"cached_tokens": 70}

    class Message:
        content = '{"ok": true}'

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]
        usage = Usage()

    class Completions:
        async def create(self, **kwargs):
            return Response()

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    async def fake_retry(call, **kwargs):
        return await call()

    def fake_debug_log(category, message, payload=None):
        if category == "llm.cache_stats":
            cache_logs.append(message)

    monkeypatch.setattr(llm_client, "_client_for_spec", lambda spec: Client())
    monkeypatch.setattr(llm_client, "_retry", fake_retry)
    monkeypatch.setattr(llm_client.debug, "log", fake_debug_log)

    result = await llm_client.chat_json(
        [{"role": "user", "content": "return json"}],
        lite=True,
        metrics_tag="json.nested",
    )

    assert result == {"ok": True}
    assert cache_logs == [
        "P49 [json.nested]: model=deepseek-v4-flash prompt=100 completion=7 "
        "cache_hit=70 cache_miss=30 hit_rate=70%"
    ]
    snapshot = metrics.snapshot()
    assert snapshot["cache_hit_tokens"]["json.nested|deepseek-v4-flash"] == 70
    assert snapshot["cache_miss_tokens"]["json.nested|deepseek-v4-flash"] == 30


@pytest.mark.asyncio
async def test_chat_json_records_responses_style_input_token_usage(monkeypatch):
    from app.core import metrics
    from app.llm import client as llm_client

    metrics.reset()
    cache_logs: list[str] = []

    class Usage:
        input_tokens = 120
        output_tokens = 9
        input_tokens_details = {"cached_tokens": 96}

    class Message:
        content = '{"ok": true}'

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]
        usage = Usage()

    class Completions:
        async def create(self, **kwargs):
            return Response()

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    async def fake_retry(call, **kwargs):
        return await call()

    def fake_debug_log(category, message, payload=None):
        if category == "llm.cache_stats":
            cache_logs.append(message)

    monkeypatch.setattr(llm_client, "_client_for_spec", lambda spec: Client())
    monkeypatch.setattr(llm_client, "_retry", fake_retry)
    monkeypatch.setattr(llm_client.debug, "log", fake_debug_log)

    result = await llm_client.chat_json(
        [{"role": "user", "content": "return json"}],
        lite=True,
        metrics_tag="json.responses",
    )

    assert result == {"ok": True}
    assert cache_logs == [
        "P49 [json.responses]: model=deepseek-v4-flash prompt=120 completion=9 "
        "cache_hit=96 cache_miss=24 hit_rate=80%"
    ]
    snapshot = metrics.snapshot()
    assert snapshot["cache_hit_tokens"]["json.responses|deepseek-v4-flash"] == 96
    assert snapshot["cache_miss_tokens"]["json.responses|deepseek-v4-flash"] == 24


@pytest.mark.asyncio
async def test_chat_json_bare_retry_records_retry_usage_tag(monkeypatch):
    from app.core import metrics
    from app.llm import client as llm_client

    metrics.reset()
    cache_logs: list[str] = []
    calls = 0

    class Usage:
        prompt_tokens = 50
        completion_tokens = 4
        prompt_cache_hit_tokens = 30
        prompt_cache_miss_tokens = 20

    class Message:
        content = '{"ok": true}'

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]
        usage = Usage()

    class Completions:
        async def create(self, **kwargs):
            return Response()

    class Chat:
        completions = Completions()

    class Client:
        chat = Chat()

    async def fake_retry(call, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("bad first response")
        return await call()

    def fake_debug_log(category, message, payload=None):
        if category == "llm.cache_stats":
            cache_logs.append(message)

    monkeypatch.setattr(llm_client, "_client_for_spec", lambda spec: Client())
    monkeypatch.setattr(llm_client, "_retry", fake_retry)
    monkeypatch.setattr(llm_client.debug, "log", fake_debug_log)

    result = await llm_client.chat_json(
        [{"role": "user", "content": "return json"}],
        lite=True,
        metrics_tag="json.unit_retry",
    )

    assert result == {"ok": True}
    assert calls == 2
    assert cache_logs == [
        "P49 [json.unit_retry.bare_retry]: model=deepseek-v4-flash prompt=50 completion=4 "
        "cache_hit=30 cache_miss=20 hit_rate=60%"
    ]
    snapshot = metrics.snapshot()
    assert snapshot["calls"]["json.unit_retry.bare_retry|deepseek-v4-flash"] == 1


@pytest.mark.asyncio
async def test_chat_stream_records_stream_usage(monkeypatch):
    from app.core import metrics
    from app.llm import client as llm_client

    metrics.reset()
    cache_logs: list[str] = []

    class Usage:
        prompt_tokens = 120
        completion_tokens = 12
        prompt_cache_hit_tokens = 100
        prompt_cache_miss_tokens = 20

    class Delta:
        content = "hello"

    class Choice:
        delta = Delta()

    class ContentChunk:
        choices = [Choice()]

    class UsageChunk:
        choices = []
        usage = Usage()

    class Stream:
        def __init__(self):
            self._chunks = iter([ContentChunk(), UsageChunk()])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._chunks)
            except StopIteration:
                raise StopAsyncIteration

        async def close(self):
            pass

    async def fake_retry(call, **kwargs):
        return Stream()

    def fake_debug_log(category, message, payload=None):
        if category == "llm.cache_stats":
            cache_logs.append(message)

    monkeypatch.setattr(llm_client, "_retry", fake_retry)
    monkeypatch.setattr(llm_client.debug, "log", fake_debug_log)

    chunks = []
    async for chunk in llm_client.chat_stream([{"role": "user", "content": "hi"}]):
        chunks.append(chunk)

    assert chunks == ["hello"]
    assert cache_logs == [
        f"P49 [chat_stream]: model={llm_client._legacy_model_spec(lite=False, reasoning='disabled').model} "
        "prompt=120 completion=12 cache_hit=100 cache_miss=20 hit_rate=83%"
    ]
    snapshot = metrics.snapshot()
    assert snapshot["calls"][f"chat_stream|{llm_client._legacy_model_spec(lite=False, reasoning='disabled').model}"] == 1


@pytest.mark.asyncio
async def test_chat_stream_records_nested_cached_tokens_usage(monkeypatch):
    from app.core import metrics
    from app.llm import client as llm_client

    metrics.reset()
    cache_logs: list[str] = []

    class Usage:
        prompt_tokens = 120
        completion_tokens = 12
        prompt_tokens_details = {"cached_tokens": 90}

    class Delta:
        content = "hello"

    class Choice:
        delta = Delta()

    class ContentChunk:
        choices = [Choice()]

    class UsageChunk:
        choices = []
        usage = Usage()

    class Stream:
        def __init__(self):
            self._chunks = iter([ContentChunk(), UsageChunk()])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._chunks)
            except StopIteration:
                raise StopAsyncIteration

        async def close(self):
            pass

    async def fake_retry(call, **kwargs):
        return Stream()

    def fake_debug_log(category, message, payload=None):
        if category == "llm.cache_stats":
            cache_logs.append(message)

    monkeypatch.setattr(llm_client, "_retry", fake_retry)
    monkeypatch.setattr(llm_client.debug, "log", fake_debug_log)

    chunks = []
    async for chunk in llm_client.chat_stream([{"role": "user", "content": "hi"}]):
        chunks.append(chunk)

    assert chunks == ["hello"]
    assert cache_logs == [
        f"P49 [chat_stream]: model={llm_client._legacy_model_spec(lite=False, reasoning='disabled').model} "
        "prompt=120 completion=12 cache_hit=90 cache_miss=30 hit_rate=75%"
    ]
    snapshot = metrics.snapshot()
    key = f"chat_stream|{llm_client._legacy_model_spec(lite=False, reasoning='disabled').model}"
    assert snapshot["cache_hit_tokens"][key] == 90
    assert snapshot["cache_miss_tokens"][key] == 30


@pytest.mark.asyncio
async def test_chat_stream_records_responses_style_input_token_usage(monkeypatch):
    from app.core import metrics
    from app.llm import client as llm_client

    metrics.reset()
    cache_logs: list[str] = []

    class Usage:
        input_tokens = 140
        output_tokens = 15
        input_tokens_details = {"cached_tokens": 112}

    class Delta:
        content = "hello"

    class Choice:
        delta = Delta()

    class ContentChunk:
        choices = [Choice()]

    class UsageChunk:
        choices = []
        usage = Usage()

    class Stream:
        def __init__(self):
            self._chunks = iter([ContentChunk(), UsageChunk()])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._chunks)
            except StopIteration:
                raise StopAsyncIteration

        async def close(self):
            pass

    async def fake_retry(call, **kwargs):
        return Stream()

    def fake_debug_log(category, message, payload=None):
        if category == "llm.cache_stats":
            cache_logs.append(message)

    monkeypatch.setattr(llm_client, "_retry", fake_retry)
    monkeypatch.setattr(llm_client.debug, "log", fake_debug_log)

    chunks = []
    async for chunk in llm_client.chat_stream([{"role": "user", "content": "hi"}]):
        chunks.append(chunk)

    model = llm_client._legacy_model_spec(lite=False, reasoning="disabled").model
    assert chunks == ["hello"]
    assert cache_logs == [
        f"P49 [chat_stream]: model={model} "
        "prompt=140 completion=15 cache_hit=112 cache_miss=28 hit_rate=80%"
    ]
    snapshot = metrics.snapshot()
    key = f"chat_stream|{model}"
    assert snapshot["cache_hit_tokens"][key] == 112
    assert snapshot["cache_miss_tokens"][key] == 28
