import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


class DebugRecorder:
    def __init__(self):
        self.events = []

    def log(self, category, message, payload=None):
        self.events.append((category, message, payload))


def test_repair_tool_call_pairing_injects_delegate_synthetic_result():
    from app.llm.tool_pairing import repair_tool_call_pairing

    messages = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "content": "delegating",
            "tool_calls": [
                {"id": "call_delegate", "type": "function", "function": {"name": "delegate", "arguments": "{}"}},
            ],
        },
    ]
    debug = DebugRecorder()

    fixed = repair_tool_call_pairing(messages, debug=debug)

    assert fixed == 1
    assert messages[1]["tool_calls"][0]["id"] == "call_delegate"
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "call_delegate"
    assert messages[2]["_synthetic_repair"] is True
    assert any(event[0] == "llm.tools.repair_pairing.synthetic" for event in debug.events)


def test_repair_tool_call_pairing_removes_regular_orphan():
    from app.llm.tool_pairing import repair_tool_call_pairing

    messages = [
        {"role": "user", "content": "read"},
        {
            "role": "assistant",
            "content": "reading",
            "tool_calls": [
                {"id": "call_read", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            ],
        },
    ]

    fixed = repair_tool_call_pairing(messages)

    assert fixed == 1
    assert "tool_calls" not in messages[1]


def test_repair_tool_call_pairing_moves_late_tool_result_next_to_call():
    from app.llm.tool_pairing import repair_tool_call_pairing

    messages = [
        {"role": "user", "content": "read"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_read", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
            ],
        },
        {"role": "system", "content": "dynamic hint inserted too early"},
        {"role": "tool", "tool_call_id": "call_read", "content": "{\"ok\": true}"},
    ]
    debug = DebugRecorder()

    fixed = repair_tool_call_pairing(messages, debug=debug)

    assert fixed == 1
    assert messages[2]["role"] == "tool"
    assert messages[2]["tool_call_id"] == "call_read"
    assert messages[3]["role"] == "system"
    assert any(event[0] == "llm.tools.repair_pairing.moved" for event in debug.events)


def test_client_compat_repair_wrapper_uses_extracted_module():
    from app.llm.client import _repair_tool_call_pairing

    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_read", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
        ]},
    ]

    fixed = _repair_tool_call_pairing(messages)

    assert fixed == 1
    assert messages[0]["role"] == "system"
