from __future__ import annotations


def test_warn_and_error_accept_structured_payload(monkeypatch) -> None:
    from app.core import debug

    emitted = []
    written = []
    monkeypatch.setattr(debug.settings, "debug_mode", True)
    monkeypatch.setattr(debug, "_emit_console", lambda category, msg, payload, **kwargs: emitted.append((category, msg, payload)))
    monkeypatch.setattr(debug, "_write_file", lambda category, msg, payload=None: written.append((category, msg, payload)))

    debug.warn("round3.protocol_leak.blocked", "blocked hidden protocol", {"prefix": "tool_call"})
    debug.error("delegate.resume_preempt_blocked", "old helper still alive")

    assert emitted[0] == (
        "WARN",
        "round3.protocol_leak.blocked: blocked hidden protocol",
        {"prefix": "tool_call"},
    )
    assert written[0] == emitted[0]
    assert emitted[1] == (
        "ERROR",
        "delegate.resume_preempt_blocked: old helper still alive",
        None,
    )
    assert written[1] == emitted[1]
