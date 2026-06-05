import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))


class DebugRecorder:
    def __init__(self):
        self.events = []

    def log(self, category, message, payload=None):
        self.events.append((category, message, payload))


async def test_bg_user_profile_update_skips_until_interval(monkeypatch):
    from app.core import user_profile_maintenance as upm

    calls = []

    async def fake_increment_chat_count(**kwargs):
        return upm.user_profile.EXTRACTION_INTERVAL - 1

    async def fake_merge_into_profile(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(upm.user_profile, "increment_chat_count", fake_increment_chat_count)
    monkeypatch.setattr(upm.user_profile, "merge_into_profile", fake_merge_into_profile)

    await upm.bg_user_profile_update(
        archive_id="a",
        user_id="u",
        user_message="m",
        assistant_message="r",
        trace_id="t",
        debug=DebugRecorder(),
    )

    assert calls == []


async def test_bg_user_profile_update_extracts_and_merges(monkeypatch):
    from app.core import user_profile_maintenance as upm
    import app.llm.model_pool as model_pool
    import app.llm.client as llm_client

    merged = []

    async def fake_increment_chat_count(**kwargs):
        return upm.user_profile.EXTRACTION_INTERVAL

    async def fake_merge_into_profile(**kwargs):
        merged.append(kwargs)
        return True

    class FakeCompletions:
        async def create(self, **kwargs):
            assert kwargs["messages"][0]["role"] == "system"
            assert kwargs["messages"][1]["role"] == "user"
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"preferences":{"response_length":"短"},"interests":["Python"],"avoid_topics":[],"long_term_facts":[]}'))]
            )

    class FakeClient:
        chat = SimpleNamespace(completions=FakeCompletions())

    async def no_wait_for(awaitable, timeout):
        return await awaitable

    monkeypatch.setattr(upm.user_profile, "increment_chat_count", fake_increment_chat_count)
    monkeypatch.setattr(upm.user_profile, "merge_into_profile", fake_merge_into_profile)
    monkeypatch.setattr(model_pool, "resolve_task", lambda name: SimpleNamespace(model="m"))
    monkeypatch.setattr(llm_client, "_client_for_spec", lambda spec: FakeClient())
    monkeypatch.setattr(asyncio, "wait_for", no_wait_for)
    debug = DebugRecorder()

    await upm.bg_user_profile_update(
        archive_id="a",
        user_id="u",
        user_message="用户说以后短一点",
        assistant_message="好的",
        trace_id="t",
        debug=debug,
    )

    assert merged[0]["increments"]["preferences"]["response_length"] == "短"
    assert [event[0] for event in debug.events] == ["user_profile.extract_trigger", "user_profile.extract.done"]


def test_profile_extraction_dynamic_facts_do_not_change_system_prefix():
    from app.core import user_profile_maintenance as upm
    from app.core.prompt_cache_observer import describe_prompt_cache_input

    messages_a = [
        {"role": "system", "content": upm.PROFILE_EXTRACTION_SYSTEM},
        {"role": "user", "content": upm._build_profile_extraction_user_payload("短一点", "好的")},
    ]
    messages_b = [
        {"role": "system", "content": upm.PROFILE_EXTRACTION_SYSTEM},
        {"role": "user", "content": upm._build_profile_extraction_user_payload("以后用英文", "明白")},
    ]

    shape_a = describe_prompt_cache_input(messages=messages_a)
    shape_b = describe_prompt_cache_input(messages=messages_b)

    assert shape_a["system_static_hash"] == shape_b["system_static_hash"]
    assert shape_a["messages_hash"] != shape_b["messages_hash"]
