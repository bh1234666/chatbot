import pytest

from app.core.orchestrator_utils import (
    _clean_deliverable_filenames,
    _is_internal_deliverable_file,
)


def test_internal_deliverable_boundary_blocks_helper_evidence():
    internal_names = [
        "_session_manifest.json",
        ".helper_arch_full_report.txt",
        "helper_file_sum_top6__py_cmd_ab12cd34.py",
        "helper_file_sum_next6_report.txt",
        "file_sum_top6__analysis_output.txt",
        "file_sum_top6_analyse2_out.txt",
        "file_sum_top6_analyse_full.txt",
        "_delegate_task/output.txt",
        "_helpers_shared/report.txt",
        "_env/app/core/orchestrator.py",
        "_py_cmd_ab12cd34.py",
        ".secret/report.txt",
        "ocr_result_page1.txt",
        "framework_contract_ch6_v2.md",
        "shared_framework_contract.json",
        "amsc_framework_contract.txt",
    ]

    assert all(_is_internal_deliverable_file(name) for name in internal_names)


def test_internal_deliverable_boundary_allows_user_artifacts():
    visible_names = [
        "analysis_report.md",
        "architecture_review.txt",
        "data_summary.csv",
        "chart.png",
        ".temp/random_test_voice_140833.wav",
        "src/algolab/graph.py",
        "reports/file_sum_top6.md",
    ]

    assert not any(_is_internal_deliverable_file(name) for name in visible_names)


def test_clean_deliverables_normalizes_without_overriding_model_choice():
    cleaned = _clean_deliverable_filenames([
        "analysis_report.md — final report",
        "_session_manifest.json",
        "helper_file_sum_top6__py_cmd_ab12cd34.py",
        "file_sum_top6__analysis_output.txt",
        ".temp/random_test_voice_140833.wav",
        "chart.png",
    ])

    assert cleaned == [
        "analysis_report.md",
        "_session_manifest.json",
        "helper_file_sum_top6__py_cmd_ab12cd34.py",
        "file_sum_top6__analysis_output.txt",
        ".temp/random_test_voice_140833.wav",
        "chart.png",
    ]


def test_deliverable_warning_review_treats_plan_selection_as_evidence():
    from pathlib import Path

    src = Path("app/core/orchestrator_entry.py").read_text(encoding="utf-8")

    assert "`current_deliverables` and `plan_*` fields are also review inputs" in src
    assert "old assistant messages or broad workspace listings" in src
    assert "already existed before this round" in src
    assert "remain selected for model review" in src


def test_deliverable_warning_facts_are_not_raw_round3_key_points():
    from pathlib import Path

    src = Path("app/core/orchestrator_entry.py").read_text(encoding="utf-8")
    block = src[
        src.find("if _deliverable_warning_facts:"):
        src.find("want = set(plan.deliverables) if plan.deliverables else set()", src.find("if _deliverable_warning_facts:"))
    ]

    assert "warning_facts=_deliverable_warning_facts" in block
    assert "plan.key_points" not in block
    assert "plan.internal_note" in block
    assert "deliverable boundary facts recorded for final response" in block
        

def test_prefix_resolution_does_not_pick_latest_candidate_symbolically():
    from pathlib import Path

    src = Path("app/core/orchestrator_entry.py").read_text(encoding="utf-8")

    assert "workspace.prefix_resolve.ambiguous" in src
    assert "no automatic choice was made" in src
    assert "getmtime" not in src[src.find("if missing:"):src.find("if _resolved:")]


def test_round3_prompt_mentions_user_visible_boundary():
    from app.core import context as ctx_build
    from app.schemas.api import ResponsePlan

    plan = ResponsePlan(
        intent="reply",
        key_points=["answer directly"],
        tone="plain",
        length_hint="short",
    )
    messages = ctx_build.round3_messages(
        "You are bot. Answer directly.",
        plan,
        "A",
        "hi",
        [],
        light=True,
    )
    joined = "\n".join(str(m.get("content") or "") for m in messages)

    assert "User-visible wording boundary" in joined
    assert "helper/delegate/Round*" in joined
    assert "_helpers_shared" in joined
    assert "_shared" in joined
    assert ".temp" in joined
    assert "toolchain/helper wording" in joined
    assert "_voice_*.wav" in joined


def test_round3_prompt_mentions_file_delivery_only():
    from app.core import context as ctx_build
    from app.schemas.api import ResponsePlan

    plan = ResponsePlan(
        intent="reply",
        key_points=["answer directly"],
        tone="plain",
        length_hint="short",
        deliverables=["analysis_report.md", "_voice_e1cbf550_1781276502334.wav"],
    )
    messages = ctx_build.round3_messages(
        "You are bot. Answer directly.",
        plan,
        "A",
        "hi",
        [],
        light=True,
        files=[
            ("analysis_report.md", "/v1/chat/files/archive/group/analysis_report.md"),
            ("_voice_e1cbf550_1781276502334.wav", "/v1/chat/files/archive/group/_voice_e1cbf550_1781276502334.wav"),
        ],
    )
    joined = "\n".join(str(m.get("content") or "") for m in messages)

    assert "Generated files" in joined
    assert "analysis_report.md" in joined
    assert "_voice_e1cbf550_1781276502334.wav" in joined


def test_round3_sanitizer_rewrites_internal_workflow_terms():
    from app.core.orchestrator_entry import _looks_like_user_visible_protocol_text

    assert _looks_like_user_visible_protocol_text("<｜tool_calls｜>")
    assert _looks_like_user_visible_protocol_text("tool_calls name=read_file")
    assert not _looks_like_user_visible_protocol_text("helper report says the page was fetched")


def test_round3_prompt_hides_stale_voice_artifact_by_instruction():
    from app.core import context as ctx_build
    from app.schemas.api import ResponsePlan

    stale_voice = "_voice_e1cbf550_1781276502334.wav"
    plan = ResponsePlan(
        intent=f"Inspect the requested web page; do not send {stale_voice}.",
        key_points=[f"The current task is page inspection; {stale_voice} is old context."],
        tone="plain",
        length_hint="short",
        callbacks=[f"Do not expose helper state or {stale_voice}."],
        avoid=[f"Do not present {stale_voice} as the current artifact."],
        deliverables=[stale_voice],
        delivery_partial=[stale_voice],
    )
    messages = ctx_build.round3_messages(
        "You are bot. Answer directly.",
        plan,
        "A",
        "hi",
        [],
        light=True,
        files=[(stale_voice, "/v1/chat/files/archive/group/_voice_e1cbf550_1781276502334.wav")],
    )
    joined = "\n".join(str(m.get("content") or "") for m in messages)

    assert "User-visible wording boundary" in joined
    assert "_voice_*.wav" in joined
    assert stale_voice in joined


def test_round2_voice_handoff_uses_prompt_boundary_not_safe_label_helper():
    from pathlib import Path

    src = Path("app/core/orchestrator_entry.py").read_text(encoding="utf-8")
    prompt_src = Path("app/core/context.py").read_text(encoding="utf-8")

    assert "_round3_visible_file_label" not in src
    assert "_prepare_round2_voice_handoff_before_delivery" not in src
    assert "User-visible wording boundary" in prompt_src
    assert "_voice_*.wav" in prompt_src
    assert "已生成语音回复已通过本轮复核" not in src


@pytest.mark.asyncio
async def test_delivery_review_can_drop_preexisting_audio_for_unrelated_request(monkeypatch, tmp_path):
    import app.llm.model_pool as model_pool
    from app.core.orchestrator_entry import _review_explicit_deliverables_with_warnings
    from app.schemas.api import ResponsePlan

    fname = "_voice_e1cbf550_1781276502334.wav"
    (tmp_path / fname).write_bytes(b"RIFF" + b"\0" * 64)
    plan = ResponsePlan(
        intent="Inspect the official web page.",
        key_points=[],
        tone="plain",
        length_hint="short",
        deliverables=[fname],
    )
    captured = {}

    async def fake_chat_json(messages, **kwargs):
        captured["messages"] = messages
        return {
            "deliverables": [],
            "reason": f"The current request asks to inspect a page; {fname} is a pre-existing historical file.",
        }

    monkeypatch.setattr(model_pool, "chat_json", fake_chat_json)
    monkeypatch.setattr(model_pool, "resolve_task", lambda _task: object())

    await _review_explicit_deliverables_with_warnings(
        plan,
        user_message="Please inspect this official web page.",
        workspace_dir=str(tmp_path),
        files_before={fname},
        warning_facts=[
            "Delivery fact: all selected file(s) already existed before this round: "
            f"{fname}. This can be a valid re-delivery only when the current user request explicitly asks."
        ],
    )

    assert plan.deliverables == []
    assert any(
        "inspect a page" in str(item).lower() or "current request" in str(item).lower()
        for item in plan.key_points
    ) or not plan.key_points
    payload = str(captured["messages"])
    assert "current_deliverables" in payload
    assert fname in payload
    assert "already existed before this round" in payload


@pytest.mark.asyncio
async def test_delivery_review_can_keep_preexisting_audio_when_current_request_asks(monkeypatch, tmp_path):
    import app.llm.model_pool as model_pool
    from app.core.orchestrator_entry import _review_explicit_deliverables_with_warnings
    from app.schemas.api import ResponsePlan

    fname = "random_test_voice_final.wav"
    (tmp_path / fname).write_bytes(b"RIFF" + b"\0" * 64)
    plan = ResponsePlan(
        intent="Resend the requested audio file.",
        key_points=[],
        tone="plain",
        length_hint="short",
        deliverables=[fname],
    )
    captured = {}

    async def fake_chat_json(messages, **kwargs):
        captured["messages"] = messages
        return {
            "deliverables": [fname],
            "reason": "The current request explicitly asks to resend the existing audio file.",
        }

    monkeypatch.setattr(model_pool, "chat_json", fake_chat_json)
    monkeypatch.setattr(model_pool, "resolve_task", lambda _task: object())

    await _review_explicit_deliverables_with_warnings(
        plan,
        user_message="请把刚才那个测试语音文件重新发我",
        workspace_dir=str(tmp_path),
        files_before={fname},
        warning_facts=[
            "Delivery fact: all selected audio file(s) already existed before this round: "
            f"{fname}. This can be a valid re-delivery only when the current user request explicitly asks."
        ],
    )

    assert plan.deliverables == [fname]
    payload = str(captured["messages"])
    assert '"extension": ".wav"' in payload
    assert '"preexisting_at_round_start": true' in payload
    assert "重新发" in payload


@pytest.mark.asyncio
async def test_delivery_review_sanitizes_kept_system_voice_filename_in_round3_facts(monkeypatch, tmp_path):
    import app.llm.model_pool as model_pool
    from app.core.orchestrator_entry import _review_explicit_deliverables_with_warnings
    from app.schemas.api import ResponsePlan

    fname = "_voice_e1cbf550_1781276502334.wav"
    (tmp_path / fname).write_bytes(b"RIFF" + b"\0" * 64)
    plan = ResponsePlan(
        intent="Resend the requested audio file.",
        key_points=[],
        tone="plain",
        length_hint="short",
        deliverables=[fname],
    )

    async def fake_chat_json(messages, **kwargs):
        return {
            "deliverables": [fname],
            "reason": f"The current request explicitly asks to resend {fname}.",
        }

    monkeypatch.setattr(model_pool, "chat_json", fake_chat_json)
    monkeypatch.setattr(model_pool, "resolve_task", lambda _task: object())

    await _review_explicit_deliverables_with_warnings(
        plan,
        user_message="请重发刚才那条语音",
        workspace_dir=str(tmp_path),
        files_before={fname},
        warning_facts=[
            "Delivery fact: all selected audio file(s) already existed before this round: "
            f"{fname}. This can be a valid re-delivery only when the current user request explicitly asks."
        ],
    )

    assert plan.deliverables == [fname]
    assert fname not in "\n".join(str(item) for item in plan.key_points)


def test_round2_voice_handoff_has_no_post_helper_llm_review():
    from pathlib import Path

    src = Path("app/core/orchestrator_entry.py").read_text(encoding="utf-8")

    assert "_prepare_round2_voice_handoff_before_delivery" not in src
    assert "_review_round2_voice_reply_file_candidate" not in src
    assert "json.voice_reply_file_review" not in src
    assert "voice.round2_handoff.review" not in src
    assert "_persona_voice_reply_guard" not in Path("app/core/orchestrator.py").read_text(encoding="utf-8")
    assert "json.persona_voice_guard" not in Path("app/core/orchestrator.py").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_delivery_review_can_keep_preexisting_file_when_current_request_reuses_it(monkeypatch, tmp_path):
    import app.llm.model_pool as model_pool
    from app.core.orchestrator_entry import _review_explicit_deliverables_with_warnings
    from app.schemas.api import ResponsePlan

    fname = "compression_report.docx"
    (tmp_path / fname).write_bytes(b"PK\x03\x04report")
    plan = ResponsePlan(
        intent="Resend the existing report.",
        key_points=[],
        tone="plain",
        length_hint="short",
        deliverables=[fname],
    )

    async def fake_chat_json(messages, **kwargs):
        return {
            "deliverables": [fname],
            "reason": "The current request explicitly asks to resend the existing report.",
        }

    monkeypatch.setattr(model_pool, "chat_json", fake_chat_json)
    monkeypatch.setattr(model_pool, "resolve_task", lambda _task: object())

    await _review_explicit_deliverables_with_warnings(
        plan,
        user_message="Resend the report from last time.",
        workspace_dir=str(tmp_path),
        files_before={fname},
        warning_facts=[
            "Delivery fact: all selected file(s) already existed before this round: "
            f"{fname}. This can be a valid re-delivery only when the current user request explicitly asks."
        ],
    )

    assert plan.deliverables == [fname]


@pytest.mark.asyncio
async def test_delivery_review_failure_keeps_explicit_deliverables(monkeypatch, tmp_path):
    import app.llm.model_pool as model_pool
    from app.core.orchestrator_entry import _review_explicit_deliverables_with_warnings
    from app.schemas.api import ResponsePlan

    fname = "_voice_e1cbf550_1781276502334.wav"
    (tmp_path / fname).write_bytes(b"RIFF" + b"\0" * 64)
    plan = ResponsePlan(
        intent="Inspect the official web page.",
        key_points=[],
        tone="plain",
        length_hint="short",
        deliverables=[fname],
    )

    async def failing_chat_json(messages, **kwargs):
        raise TimeoutError("review timeout")

    monkeypatch.setattr(model_pool, "chat_json", failing_chat_json)
    monkeypatch.setattr(model_pool, "resolve_task", lambda _task: object())

    await _review_explicit_deliverables_with_warnings(
        plan,
        user_message="Please inspect this official web page.",
        workspace_dir=str(tmp_path),
        files_before={fname},
        warning_facts=[
            "Delivery fact: all selected file(s) already existed before this round: "
            f"{fname}. This can be valid only when the current request asks for it."
        ],
    )

    assert plan.deliverables == [fname]
