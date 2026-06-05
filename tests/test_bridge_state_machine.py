import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class FakeRequest:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


class FakeClient:
    def __init__(self):
        self.posts = []
        self.responses = []

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if self.responses:
            return self.responses.pop(0)
        return FakeResponse()


class FakeResponse:
    status_code = 200
    text = ""

    def __init__(self, *, status_code=200, text="", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json_data = {} if json_data is None else json_data

    def json(self):
        return self._json_data


class FakeStreamResponse:
    status_code = 200

    def __init__(self, lines):
        self.lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_lines(self):
        for line in self.lines:
            yield line


class FakeStreamingClient(FakeClient):
    def __init__(self, lines):
        super().__init__()
        self.lines = lines

    def stream(self, *_args, **_kwargs):
        return FakeStreamResponse(self.lines)


def _json_response(resp):
    return json.loads(resp.body.decode("utf-8"))


def _message(raw="hello", *, message_id="m1", user_id="u1", group_id="g1", self_id="bot"):
    return {
        "post_type": "message",
        "message_type": "group",
        "group_id": group_id,
        "user_id": user_id,
        "self_id": self_id,
        "message_id": message_id,
        "raw_message": raw,
        "message": [],
        "sender": {"nickname": "Alice"},
    }


async def _install_bridge_fakes(monkeypatch, napcat_bridge, *, at_bot=False):
    client = FakeClient()
    observed = []
    scheduled = []

    async def fake_check_participate(_client, group_id):
        return True, "archive1"

    async def fake_download(_body, raw_message, _archive_id, _group_id, _user_id="", _user_name=""):
        return raw_message

    async def fake_observe(_client, archive_id, group_id, user_id, user_name, content, addressed_bot):
        observed.append({
            "archive_id": archive_id,
            "group_id": group_id,
            "user_id": user_id,
            "user_name": user_name,
            "content": content,
            "addressed_bot": addressed_bot,
        })

    async def fake_sync(_group_id, _archive_id):
        return None

    def fake_schedule(coro, *, name=None):
        scheduled.append(name)
        coro.close()
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        fut.set_result(None)
        return fut

    monkeypatch.setattr(napcat_bridge, "get_http_client", lambda: client)
    monkeypatch.setattr(napcat_bridge, "_check_participate", fake_check_participate)
    monkeypatch.setattr(napcat_bridge, "_message_addressed_to_bot", lambda _body, _bot_qq="": at_bot)
    monkeypatch.setattr(napcat_bridge, "_download_incoming_media", fake_download)
    monkeypatch.setattr(napcat_bridge, "_observe_message", fake_observe)
    monkeypatch.setattr(napcat_bridge, "_sync_group_files_fire_and_forget", fake_sync)

    import app.core.bg_tasks as bg_tasks
    monkeypatch.setattr(bg_tasks, "schedule", fake_schedule)
    return client, observed, scheduled


def _reset_bridge_state(napcat_bridge):
    napcat_bridge._seen_msg_ids.clear()
    napcat_bridge._pending_queue.clear()
    napcat_bridge._processing_lock.clear()
    napcat_bridge._currently_processing.clear()
    napcat_bridge._abort_injected_messages.clear()
    napcat_bridge._pkey_last_seen.clear()
    napcat_bridge._participate_cache.clear()
    napcat_bridge._chat_archive_cache.clear()
    napcat_bridge._botctl_sessions.clear()


async def test_chat_and_reply_forwards_upstream_persona_progress_to_group():
    import napcat_bridge

    progress_message = "<lite-model-persona-progress>"
    client = FakeStreamingClient([
        "event: progress",
        "data: " + json.dumps({"kind": "working", "message": progress_message, "persona_safe": True}, ensure_ascii=False),
        "event: token",
        "data: " + json.dumps({"text": "人设内回复"}, ensure_ascii=False),
        "event: done",
        "data: {}",
    ])

    reply, files, voice_reply, suppress_text, voice_reply_file = await napcat_bridge._chat_and_reply(
        client,
        "archive1",
        "123",
        "u1",
        "Alice",
        "hi",
        client_msg_id="m1",
    )

    assert reply == "人设内回复"
    assert files == []
    assert voice_reply is False
    assert suppress_text is False
    assert voice_reply_file == ""
    assert any(
        url.endswith("/send_group_msg") and kwargs["json"] == {"group_id": 123, "message": progress_message}
        for url, kwargs in client.posts
    )


async def test_chat_and_reply_suppresses_static_internal_progress_to_group():
    import napcat_bridge

    client = FakeStreamingClient([
        "event: progress",
        "data: " + json.dumps({"kind": "loading_memory", "message": "正在加载记忆和人设"}, ensure_ascii=False),
        "event: progress",
        "data: " + json.dumps({"kind": "analyzing_intent", "message": "正在分析用户意图"}, ensure_ascii=False),
        "event: progress",
        "data: " + json.dumps({"kind": "planning_tools", "message": "正在规划要调用的工具"}, ensure_ascii=False),
        "event: token",
        "data: " + json.dumps({"text": "人设内回复"}, ensure_ascii=False),
        "event: done",
        "data: {}",
    ])

    reply, *_ = await napcat_bridge._chat_and_reply(
        client,
        "archive1",
        "123",
        "u1",
        "Alice",
        "hi",
        client_msg_id="m1",
    )

    assert reply == "人设内回复"
    sent_messages = [kwargs["json"]["message"] for url, kwargs in client.posts if url.endswith("/send_group_msg")]
    assert "正在加载记忆和人设" not in sent_messages
    assert "正在分析用户意图" not in sent_messages
    assert "正在规划要调用的工具" not in sent_messages


async def test_chat_and_reply_injects_message_when_abort_marker_arrives():
    import napcat_bridge

    client = FakeStreamingClient([
        "event: token",
        "data: " + json.dumps({"text": "已停"}, ensure_ascii=False),
        "event: abort_marker",
        "data: " + json.dumps({"trace_id": "t1"}, ensure_ascii=False),
        "event: done",
        "data: {}",
    ])

    reply, *_ = await napcat_bridge._chat_and_reply(
        client,
        "archive1",
        "123",
        "u1",
        "Alice",
        "hi",
        client_msg_id="m1",
    )

    assert reply == "已停"
    inject_posts = [kwargs for url, kwargs in client.posts if url.endswith("/v1/chat/interrupt_message")]
    assert inject_posts
    assert inject_posts[0]["json"]["message"] == "hi"


async def test_send_generated_files_falls_back_when_napcat_returns_failed_body():
    import napcat_bridge

    client = FakeClient()
    client.responses = [
        FakeResponse(json_data={"status": "failed", "retcode": 1200, "message": "bad path"}),
        FakeResponse(json_data={"status": "ok", "retcode": 0}),
    ]

    await napcat_bridge._send_generated_files(
        client,
        "123",
        [{
            "name": "paper.pdf",
            "url": "/v1/chat/files/a/g/paper.pdf",
            "local_path": "Z:/missing/paper.pdf",
        }],
    )

    assert client.posts[0][0].endswith("/send_group_msg")
    assert "下载：" in client.posts[0][1]["json"]["message"]


async def test_send_generated_files_uses_done_payload_file_not_reply_text(tmp_path):
    import napcat_bridge

    file_path = tmp_path / "actual.pdf"
    file_path.write_bytes(b"doc")
    client = FakeClient()

    await napcat_bridge._send_generated_files(
        client,
        "123",
        [{
            "name": "actual.pdf",
            "url": "/v1/chat/files/a/g/actual.pdf",
            "local_path": str(file_path),
        }],
    )

    assert client.posts == [(
        f"{napcat_bridge.NAPCAT_URL}/upload_group_file",
        {"json": {"group_id": 123, "file": str(file_path), "name": "actual.pdf"}, "timeout": 60.0},
    )]


async def test_chat_and_reply_captures_done_files_from_sse():
    import napcat_bridge

    files_payload = [{"name": "actual.docx", "url": "/v1/chat/files/a/g/actual.docx", "local_path": "F:/actual.docx"}]
    client = FakeStreamingClient([
        "event: token",
        "data: " + json.dumps({"text": "文件已经好了"}, ensure_ascii=False),
        "event: done",
        "data: " + json.dumps({"files": files_payload}, ensure_ascii=False),
    ])

    reply, files, *_ = await napcat_bridge._chat_and_reply(
        client,
        "archive1",
        "123",
        "u1",
        "Alice",
        "hi",
        client_msg_id="m1",
    )

    assert reply == "文件已经好了"
    assert files == files_payload


async def test_duplicate_message_returns_ok_without_side_effects(monkeypatch):
    import napcat_bridge

    _reset_bridge_state(napcat_bridge)
    napcat_bridge._is_duplicate_message("dup1")
    monkeypatch.setattr(napcat_bridge, "get_http_client", lambda: (_ for _ in ()).throw(AssertionError("client should not be used")))

    resp = await napcat_bridge.napcat_callback(FakeRequest(_message(message_id="dup1")))

    assert resp.status_code == 200
    assert _json_response(resp) == {"status": "ok"}
    assert not napcat_bridge._pending_queue


async def test_non_admin_botctl_is_ignored(monkeypatch):
    import napcat_bridge

    _reset_bridge_state(napcat_bridge)
    client = FakeClient()

    async def fake_admin_group(_client):
        return "admin-group"

    monkeypatch.setattr(napcat_bridge, "get_http_client", lambda: client)
    monkeypatch.setattr(napcat_bridge, "_get_admin_group", fake_admin_group)

    resp = await napcat_bridge.napcat_callback(
        FakeRequest(_message("botctl list", message_id="botctl1", group_id="normal-group"))
    )

    assert _json_response(resp) == {"status": "ignored"}
    assert client.posts == []


async def test_admin_botctl_runs_command_and_sends_result(monkeypatch):
    import napcat_bridge

    _reset_bridge_state(napcat_bridge)
    client = FakeClient()
    commands = []

    async def fake_admin_group(_client):
        return "123"

    def fake_run_command(cmd):
        commands.append(cmd)
        return "done"

    monkeypatch.setattr(napcat_bridge, "get_http_client", lambda: client)
    monkeypatch.setattr(napcat_bridge, "_get_admin_group", fake_admin_group)
    monkeypatch.setattr(napcat_bridge, "run_command", fake_run_command)

    resp = await napcat_bridge.napcat_callback(
        FakeRequest(_message("botctl status", message_id="botctl-admin", group_id="123"))
    )

    assert _json_response(resp) == {"status": "ok"}
    assert commands == ["status"]
    assert client.posts == [(
        f"{napcat_bridge.NAPCAT_URL}/send_group_msg",
        {"json": {"group_id": 123, "message": "done"}},
    )]


async def test_admin_botctl_blocks_admin_group_removal(monkeypatch):
    import napcat_bridge

    _reset_bridge_state(napcat_bridge)
    client = FakeClient()

    async def fake_admin_group(_client):
        return "123"

    monkeypatch.setattr(napcat_bridge, "get_http_client", lambda: client)
    monkeypatch.setattr(napcat_bridge, "_get_admin_group", fake_admin_group)
    monkeypatch.setattr(
        napcat_bridge,
        "run_command",
        lambda _cmd: (_ for _ in ()).throw(AssertionError("admin removal should be blocked")),
    )

    resp = await napcat_bridge.napcat_callback(
        FakeRequest(_message("botctl admin off", message_id="botctl-block", group_id="123"))
    )

    assert _json_response(resp) == {"status": "ok"}
    assert len(client.posts) == 1
    assert "不能从群内移除 admin 群" in client.posts[0][1]["json"]["message"]


async def test_admin_botctl_multiturn_session_continues_with_choice(monkeypatch):
    import napcat_bridge

    _reset_bridge_state(napcat_bridge)
    client = FakeClient()
    commands = []
    pending_inputs = []

    async def fake_admin_group(_client):
        return "123"

    seen = False

    def fake_run_command(cmd):
        nonlocal seen
        commands.append(cmd)
        if cmd == "danger op" and not seen:
            seen = True
            return "__BOTCTL_AWAIT__请选择"
        return "confirmed"

    monkeypatch.setattr(napcat_bridge, "get_http_client", lambda: client)
    monkeypatch.setattr(napcat_bridge, "_get_admin_group", fake_admin_group)
    monkeypatch.setattr(napcat_bridge, "run_command", fake_run_command)
    monkeypatch.setattr(napcat_bridge, "set_pending_input", lambda value: pending_inputs.append(value))

    first = await napcat_bridge.napcat_callback(
        FakeRequest(_message("botctl danger op", message_id="botctl-multi-1", group_id="123"))
    )
    second = await napcat_bridge.napcat_callback(
        FakeRequest(_message("botctl 1", message_id="botctl-multi-2", group_id="123"))
    )

    assert _json_response(first) == {"status": "ok"}
    assert _json_response(second) == {"status": "ok"}
    assert commands == ["danger op", "danger op"]
    assert pending_inputs == ["1"]
    assert "u1" not in napcat_bridge._botctl_sessions
    assert [post[1]["json"]["message"] for post in client.posts] == ["请选择", "confirmed"]


async def test_non_at_message_is_observed_and_schedules_file_sync(monkeypatch):
    import napcat_bridge

    _reset_bridge_state(napcat_bridge)
    _client, observed, scheduled = await _install_bridge_fakes(monkeypatch, napcat_bridge, at_bot=False)

    resp = await napcat_bridge.napcat_callback(FakeRequest(_message("hello", message_id="observe1")))

    assert _json_response(resp) == {"status": "observed"}
    assert observed == [{
        "archive_id": "archive1",
        "group_id": "g1",
        "user_id": "u1",
        "user_name": "Alice",
        "content": "hello",
        "addressed_bot": False,
    }]
    assert scheduled == ["bridge.sync_files:g1"]


async def test_non_at_stop_message_aborts_when_same_user_busy(monkeypatch):
    import napcat_bridge

    _reset_bridge_state(napcat_bridge)
    client, _observed, _scheduled = await _install_bridge_fakes(monkeypatch, napcat_bridge, at_bot=False)
    pkey = ("g1", "u1")
    lock = napcat_bridge._processing_lock[pkey]
    await lock.acquire()
    try:
        resp = await napcat_bridge.napcat_callback(FakeRequest(_message("停", message_id="stop1")))
    finally:
        lock.release()

    assert _json_response(resp) == {"status": "observed"}
    assert any(url.endswith("/v1/chat/abort") for url, _kwargs in client.posts)


async def test_at_message_processes_without_abort_when_different_user_busy(monkeypatch):
    import napcat_bridge

    _reset_bridge_state(napcat_bridge)
    client, _observed, _scheduled = await _install_bridge_fakes(monkeypatch, napcat_bridge, at_bot=True)
    other_lock = napcat_bridge._processing_lock[("g1", "other-user")]
    await other_lock.acquire()

    async def fake_ensure_persona(_client, _archive_id):
        return None

    async def fake_chat_and_reply(*_args, **_kwargs):
        return "ok", [], False, False, None

    monkeypatch.setattr(napcat_bridge, "_ensure_persona", fake_ensure_persona)
    monkeypatch.setattr(napcat_bridge, "_chat_and_reply", fake_chat_and_reply)

    try:
        resp = await napcat_bridge.napcat_callback(FakeRequest(_message("[CQ:at,qq=bot] hi", message_id="parallel1", group_id="123")))
    finally:
        other_lock.release()

    assert _json_response(resp) == {"status": "ok"}
    assert not any(url.endswith("/v1/chat/abort") for url, _kwargs in client.posts)
    assert any(
        url.endswith("/send_group_msg") and kwargs["json"]["message"] == "[CQ:at,qq=u1] ok"
        for url, kwargs in client.posts
    )


async def test_empty_group_message_returns_empty_without_side_effects(monkeypatch):
    import napcat_bridge

    _reset_bridge_state(napcat_bridge)
    monkeypatch.setattr(napcat_bridge, "get_http_client", lambda: (_ for _ in ()).throw(AssertionError("client should not be used")))

    resp = await napcat_bridge.napcat_callback(FakeRequest(_message("   ", message_id="empty1")))

    assert _json_response(resp) == {"status": "empty"}
    assert not napcat_bridge._seen_msg_ids


async def test_at_message_queues_and_aborts_when_same_user_busy(monkeypatch):
    import napcat_bridge

    _reset_bridge_state(napcat_bridge)
    client, _observed, _scheduled = await _install_bridge_fakes(monkeypatch, napcat_bridge, at_bot=True)
    pkey = ("g1", "u1")
    lock = napcat_bridge._processing_lock[pkey]
    await lock.acquire()
    try:
        resp = await napcat_bridge.napcat_callback(FakeRequest(_message("[CQ:at,qq=bot] hi", message_id="queue1")))
    finally:
        lock.release()

    assert _json_response(resp)["status"] == "queued"
    assert list(napcat_bridge._pending_queue[pkey]) == [("u1", "Alice", "[CQ:at,qq=bot] hi")]
    assert list(napcat_bridge._abort_injected_messages[pkey]) == ["[CQ:at,qq=bot] hi"]
    interrupt_posts = [kwargs for url, kwargs in client.posts if url.endswith("/v1/chat/interrupt_message")]
    assert interrupt_posts
    assert interrupt_posts[0]["json"]["message"] == "[CQ:at,qq=bot] hi"
    abort_posts = [kwargs for url, kwargs in client.posts if url.endswith("/v1/chat/abort")]
    assert abort_posts
    assert abort_posts[0]["json"] == {"archive_id": "archive1", "group_id": "g1", "user_id": "u1"}
